"""The tools the harness can run, and the JSON that describes them to the model."""

import json
from collections import Counter
from datetime import date, timedelta
from statistics import median

import requests

# --- DATA SOURCES ---

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"  # place name -> coordinates, state
COUNTY_URL = "https://geo.fcc.gov/api/census/area"              # coordinates -> county (FCC Area API)
CDC_WVAL_URL = "https://data.cdc.gov/resource/atcp-73re.json"   # CDC NWSS site-level wastewater levels

# The three viruses in the dataset, matched by how their pathogen_target text starts
VIRUSES = {"COVID-19": "sars", "Influenza A": "influenza a", "RSV": "rsv"}

TREND_WEEKS = 3        # trend compares a week with this many weeks earlier
MAX_LOCAL_LAG_WEEKS = 3  # county data may be up to this many weeks older than the state's newest week
WEEKS_OF_HISTORY = TREND_WEEKS + MAX_LOCAL_LAG_WEEKS + 1  # how far back tool 1 fetches
MIN_SITES_TO_RANK = 5  # states with fewer reporting sites are left out of national rankings (too noisy)
HIGH_LEVELS = {"High", "Very High"}  # CDC categories counted as elevated in rankings


# --- HELPERS (shared by tools; the model never sees these) ---

def geocode(location: str) -> dict:
    """Place name -> {name, state, lat, lon}. Accepts 'City' or 'City, State'. Raises ValueError with advice."""
    name, _, state_hint = location.partition(",")  # "Austin, Texas" -> "Austin", "Texas"
    resp = requests.get(GEOCODE_URL, params={"name": name.strip(), "count": 10}, timeout=10)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    us = [r for r in results if r.get("country_code") == "US"]
    if not us:
        if results:
            raise ValueError(f"'{location}' is outside the US. Wastewater data only covers US locations.")
        raise ValueError(f"Could not find '{location}'. Try a city or county name with its full state, e.g. 'Austin, Texas'.")
    hint = state_hint.strip().lower()
    match = next((r for r in us if hint and r.get("admin1", "").lower() == hint), us[0])  # prefer the named state
    return {"name": match["name"], "state": match.get("admin1", ""), "lat": match["latitude"], "lon": match["longitude"]}


def county_at(lat: float, lon: float) -> str | None:
    """Coordinates -> county name without its suffix ('Kings'), or None if the lookup fails."""
    try:
        resp = requests.get(COUNTY_URL, params={"lat": lat, "lon": lon, "format": "json"}, timeout=10)
        resp.raise_for_status()
        county = resp.json()["results"][0]["county_name"]
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return None  # not fatal: the tool falls back to state-level data
    for suffix in (" County", " Parish", " Borough"):
        county = county.removesuffix(suffix)
    return county


def soql_text(value: str) -> str:
    """Quote a value for a Socrata query; a single quote inside text is written as two."""
    return "'" + value.replace("'", "''") + "'"


def query_cdc(params: dict) -> list[dict]:
    """Run one filtered query against the CDC wastewater dataset and return its rows."""
    resp = requests.get(CDC_WVAL_URL, params=params, timeout=30)
    resp.raise_for_status()  # turn HTTP errors (400, 500) into exceptions the tools can report
    return resp.json()


def serves_county(row: dict, county: str) -> bool:
    """True if this site's counties_served list (comma-separated) includes the county."""
    served = row.get("counties_served") or ""
    return county.lower() in (c.strip().lower() for c in served.split(","))


def fetch_state_rows(state: str, start: str, end: str | None = None) -> list[dict]:
    """All rows for one state with week_end in [start, end]; adds a normalized 'week' (YYYY-MM-DD) to each row."""
    where = f"state_territory = {soql_text(state)} AND week_end >= '{start}'"
    if end:
        where += f" AND week_end <= '{end}'"
    rows = query_cdc({
        "$select": "site, counties_served, pathogen_target, site_wval, site_wval_category, week_end",
        "$where": where,
        "$limit": 50000,
    })
    for r in rows:
        r["week"] = (r.get("week_end") or "")[:10]  # "2026-09-19T00:00:00.000" -> "2026-09-19"
    return rows


def newest_week(state: str) -> str:
    """Newest week_end with any data for the state, as YYYY-MM-DD ('' if none)."""
    latest = query_cdc({"$select": "max(week_end) AS latest", "$where": f"state_territory = {soql_text(state)}"})
    return (latest[0].get("latest") or "")[:10] if latest else ""


def closest_week(rows: list[dict], target: str) -> str:
    """The week in rows nearest to the target date ('' if rows is empty)."""
    weeks = {r["week"] for r in rows}
    t = date.fromisoformat(target)
    return min(weeks, key=lambda w: abs((date.fromisoformat(w) - t).days), default="")


def median_level(rows: list[dict], week: str, sites: set[str] | None = None) -> float | None:
    """Median site_wval for one week, optionally only for the given sites."""
    values = []
    for r in rows:
        if r["week"] != week or (sites is not None and r.get("site") not in sites):
            continue
        try:
            values.append(float(r["site_wval"]))
        except (KeyError, TypeError, ValueError):
            pass  # a site without a numeric level that week is skipped
    return median(values) if values else None


def direction(now: float | None, before: float | None, up: str, down: str, same: str) -> str:
    """Label a change using a 20% band: above -> up, below -> down, within -> same."""
    if now is None or not before:
        return "unknown (not enough data)"
    if now > before * 1.2:
        return up
    if now < before / 1.2:
        return down
    return same


def rows_for_virus(rows: list[dict], virus_prefix: str) -> list[dict]:
    """Keep only rows whose pathogen_target starts with the virus prefix (e.g. 'sars')."""
    return [r for r in rows if (r.get("pathogen_target") or "").lower().startswith(virus_prefix)]


def summarize_week(rows: list[dict], week: str) -> dict:
    """Site-level summary for one virus in one week: CDC category counts plus a median-based trend."""
    prior_week = (date.fromisoformat(week) - timedelta(weeks=TREND_WEEKS)).isoformat()

    now, before = median_level(rows, week), median_level(rows, prior_week)
    trend = direction(now, before, "rising", "falling", "steady")

    current = [r for r in rows if r["week"] == week]
    categories = Counter(r.get("site_wval_category") or "Unknown" for r in current)
    return {
        "sites_reporting": len(current),
        "sites_by_level": dict(categories.most_common()),  # CDC's own categories, e.g. {"Moderate": 2, "High": 1}
        "trend": trend,
        "trend_basis": f"median site level {now:.1f} vs {before:.1f} on {prior_week}" if now is not None and before else None,
    }


# --- TOOLS ---

def get_current_activity(location: str) -> str:
    """Latest weekly wastewater levels for COVID-19, flu A, and RSV at a US place (county, else state)."""
    try:
        place = geocode(location)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    try:
        latest_week = newest_week(place["state"])
        if not latest_week:
            return json.dumps({"error": f"No wastewater data is reported for {place['state']}. Try a nearby state."})
        # Only this state's recent rows (the full dataset is too large to download)
        start = (date.fromisoformat(latest_week) - timedelta(weeks=WEEKS_OF_HISTORY)).isoformat()
        rows = fetch_state_rows(place["state"], start)
    except requests.RequestException as e:
        return json.dumps({"error": f"The CDC data service did not respond ({type(e).__name__}). Tell the user it is temporarily unavailable; do not retry more than once."})

    # Per virus: use the county's own newest week if it is recent enough, else fall back to the whole state
    county = county_at(place["lat"], place["lon"])
    freshness_cutoff = (date.fromisoformat(latest_week) - timedelta(weeks=MAX_LOCAL_LAG_WEEKS)).isoformat()
    viruses = {}
    for virus, prefix in VIRUSES.items():
        state_rows = rows_for_virus(rows, prefix)
        if not state_rows:
            viruses[virus] = {"status": f"no sites in {place['state']} reported this virus recently"}
            continue
        county_rows = [r for r in state_rows if county and serves_county(r, county)]
        county_week = max((r["week"] for r in county_rows), default="")

        if county_week and county_week >= freshness_cutoff:
            summary = summarize_week(county_rows, county_week)
            summary["area_used"] = f"{county} County"
            summary["data_week_ending"] = county_week
            if county_week < latest_week:
                summary["note"] = f"Newest local data is for the week ending {county_week}; statewide data is newer ({latest_week})."
        else:
            state_week = max(r["week"] for r in state_rows)
            summary = summarize_week(state_rows, state_week)
            summary["area_used"] = f"all of {place['state']}"
            summary["data_week_ending"] = state_week
            summary["note"] = f"No recent sites in {county or 'this'} County reported this virus, so this is statewide."
        viruses[virus] = summary

    return json.dumps({"place": f"{place['name']}, {place['state']}", "county": county, "viruses": viruses})


def get_historical_comparison(location: str, virus: str, weeks_ago: int = 52) -> str:
    """Compare one virus's wastewater level at a US place now versus weeks_ago weeks earlier."""
    if virus not in VIRUSES:
        return json.dumps({"error": f"Unknown virus '{virus}'. Use one of: {list(VIRUSES)}."})
    if not 1 <= weeks_ago <= 156:
        return json.dumps({"error": "weeks_ago must be between 1 and 156 (3 years). Use 52 for 'this time last year'."})
    try:
        place = geocode(location)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    try:
        latest_week = newest_week(place["state"])
        if not latest_week:
            return json.dumps({"error": f"No wastewater data is reported for {place['state']}. Try a nearby state."})
        latest = date.fromisoformat(latest_week)
        target = latest - timedelta(weeks=weeks_ago)
        # Two small windows instead of everything in between: recent weeks, and a few weeks around the target date
        recent = fetch_state_rows(place["state"], (latest - timedelta(weeks=MAX_LOCAL_LAG_WEEKS)).isoformat())
        # Past window is wide enough to cover a county that lags the state by up to MAX_LOCAL_LAG_WEEKS
        past_start = target - timedelta(weeks=2 + MAX_LOCAL_LAG_WEEKS)
        past = fetch_state_rows(place["state"], past_start.isoformat(), (target + timedelta(weeks=2)).isoformat())
    except requests.RequestException as e:
        return json.dumps({"error": f"The CDC data service did not respond ({type(e).__name__}). Tell the user it is temporarily unavailable; do not retry more than once."})

    prefix = VIRUSES[virus]
    recent, past = rows_for_virus(recent, prefix), rows_for_virus(past, prefix)
    if not recent:
        return json.dumps({"error": f"No recent {virus} data for {place['state']}; its sites may not test for {virus}. Try another virus or a nearby state."})
    if not past:
        return json.dumps({"error": f"No {virus} data for {place['state']} around {target.isoformat()}. Try a smaller weeks_ago."})

    # Use the county only if it has data in BOTH periods, so the two readings cover the same area
    county = county_at(place["lat"], place["lon"])
    county_recent = [r for r in recent if county and serves_county(r, county)]
    county_past = [r for r in past if county and serves_county(r, county)]
    if county_recent and county_past:
        area, recent, past = f"{county} County", county_recent, county_past
    else:
        area = f"all of {place['state']}"

    now_week = max(r["week"] for r in recent)
    # Measure weeks_ago from the week actually used, which can lag the state's newest week
    then_week = closest_week(past, (date.fromisoformat(now_week) - timedelta(weeks=weeks_ago)).isoformat())

    # Like-for-like: prefer sites that reported in both weeks, since the set of sites changes over time
    sites_now = {r.get("site") for r in recent if r["week"] == now_week}
    sites_then = {r.get("site") for r in past if r["week"] == then_week}
    shared = {s for s in sites_now & sites_then if s}
    basis = f"the same {len(shared)} site(s) in both weeks" if shared else "different sets of sites in each week"
    compare_sites = shared or None

    level_now = median_level(recent, now_week, compare_sites)
    level_then = median_level(past, then_week, compare_sites)

    def snapshot(rows: list[dict], week: str) -> dict:
        current = [r for r in rows if r["week"] == week]
        return {
            "week_ending": week,
            "sites_reporting": len(current),
            "sites_by_level": dict(Counter(r.get("site_wval_category") or "Unknown" for r in current).most_common()),
        }

    return json.dumps({
        "place": f"{place['name']}, {place['state']}",
        "virus": virus,
        "area_used": area,
        "note": None if area.endswith("County") else f"{county or 'This'} County lacked data in one of the periods, so both readings are statewide.",
        "now": snapshot(recent, now_week),
        "then": snapshot(past, then_week),
        "change": direction(level_now, level_then, "higher now", "lower now", "about the same"),
        "change_basis": (
            f"median site level {level_now:.1f} now vs {level_then:.1f} then, using {basis}"
            if level_now is not None and level_then is not None else None
        ),
    })


def get_national_rankings(virus: str, top_n: int = 5, order: str = "highest") -> str:
    """Rank US states by the share of their wastewater sites at High or Very High for one virus."""
    if virus not in VIRUSES:
        return json.dumps({"error": f"Unknown virus '{virus}'. Use one of: {list(VIRUSES)}."})
    if order not in ("highest", "lowest"):
        return json.dumps({"error": "order must be 'highest' or 'lowest'."})
    top_n = max(1, min(int(top_n), 20))  # keep the answer readable

    prefix_filter = f"lower(pathogen_target) like '{VIRUSES[virus]}%'"
    try:
        latest = query_cdc({"$select": "max(week_end) AS latest", "$where": prefix_filter})
        latest_week = (latest[0].get("latest") or "")[:10] if latest else ""
        if not latest_week:
            return json.dumps({"error": f"No {virus} data is available nationally right now."})
        # A few recent weeks, since states report on different schedules
        start = (date.fromisoformat(latest_week) - timedelta(weeks=MAX_LOCAL_LAG_WEEKS)).isoformat()
        rows = query_cdc({
            "$select": "state_territory, site, site_wval, site_wval_category, week_end",
            "$where": f"{prefix_filter} AND week_end >= '{start}'",
            "$limit": 50000,
        })
    except requests.RequestException as e:
        return json.dumps({"error": f"The CDC data service did not respond ({type(e).__name__}). Tell the user it is temporarily unavailable; do not retry more than once."})

    # Group rows by state, keeping only each state's own newest week
    by_state: dict[str, list[dict]] = {}
    for r in rows:
        r["week"] = (r.get("week_end") or "")[:10]
        by_state.setdefault(r.get("state_territory") or "Unknown", []).append(r)

    ranked, too_few, all_current = [], [], []
    for state, state_rows in by_state.items():
        week = max(r["week"] for r in state_rows)
        current = [r for r in state_rows if r["week"] == week]
        all_current += current  # every state counts toward national context, even ones too small to rank
        if len(current) < MIN_SITES_TO_RANK:
            too_few.append(state)
            continue
        high = sum(1 for r in current if r.get("site_wval_category") in HIGH_LEVELS)
        ranked.append({
            "state": state,
            "week_ending": week,
            "sites_reporting": len(current),
            "percent_sites_high_or_very_high": round(100 * high / len(current)),
            "median_site_level": median_level(current, week),
        })

    # Primary sort: share of elevated sites; tiebreaker: median site level
    ranked.sort(
        key=lambda s: (s["percent_sites_high_or_very_high"], s["median_site_level"] or 0),
        reverse=(order == "highest"),
    )
    for i, s in enumerate(ranked, start=1):
        s["rank"] = i
        if s["median_site_level"] is not None:
            s["median_site_level"] = round(s["median_site_level"], 1)

    national_high = sum(1 for r in all_current if r.get("site_wval_category") in HIGH_LEVELS)
    return json.dumps({
        "virus": virus,
        "order": order,
        "newest_national_week": latest_week,
        # National context, so a "top" state is not mistaken for a hotspot when activity is low everywhere
        "national_sites_reporting": len(all_current),
        "national_percent_sites_high_or_very_high": round(100 * national_high / len(all_current)) if all_current else None,
        "states": ranked[:top_n],
        "states_ranked": len(ranked),
        "method": (
            f"States ranked by the percent of their reporting sites at High or Very High in their newest week, "
            f"with median site level as a tiebreaker. States with fewer than {MIN_SITES_TO_RANK} reporting sites "
            f"are not ranked ({len(too_few)} left out)."
        ),
    })


# --- TOOL DEFINITIONS: what the model sees ("set notes" in the screenplay) ---

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_activity",
            "description": (
                "Get the latest weekly wastewater activity levels for COVID-19, influenza A, and RSV at a US place, "
                "from CDC wastewater surveillance. Returns, per virus, how many sampling sites are at each CDC level "
                "(Very Low to Very High) and whether levels are rising, falling, or steady versus 3 weeks earlier. "
                "Uses sites serving the place's county when they reported recently, otherwise the whole state; "
                "each virus says which area and data week it used. "
                "Call once per place; for comparisons, call it for each place."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "US city or county with its full state name, e.g. 'Brooklyn, New York' or 'Las Vegas, Nevada'",
                    },
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_historical_comparison",
            "description": (
                "Compare one virus's wastewater activity at a US place now versus a past week, e.g. 'this time last year'. "
                "Returns the CDC level counts for both weeks and whether activity is higher, lower, or about the same. "
                "Compares the same sampling sites across both weeks when possible. Use this for any question about "
                "how current levels compare with the past; use get_current_activity for the current situation alone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "US city or county with its full state name, e.g. 'Brooklyn, New York'",
                    },
                    "virus": {
                        "type": "string",
                        "enum": list(VIRUSES),
                        "description": "Which virus to compare",
                    },
                    "weeks_ago": {
                        "type": "integer",
                        "description": "How many weeks back to compare with: 52 for this time last year, 4 for about a month ago. Between 1 and 156.",
                    },
                },
                "required": ["location", "virus"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_national_rankings",
            "description": (
                "Rank US states by current wastewater activity for one virus: the percent of each state's sampling sites "
                "at the CDC's High or Very High level, with median site level as a tiebreaker. Use this for questions like "
                "'where is RSV highest right now?' or 'which states have the least COVID?'. Not for a single place; "
                "use get_current_activity for that."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "virus": {
                        "type": "string",
                        "enum": list(VIRUSES),
                        "description": "Which virus to rank states by",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "How many states to return, 1 to 20. Default 5.",
                    },
                    "order": {
                        "type": "string",
                        "enum": ["highest", "lowest"],
                        "description": "'highest' for where activity is worst, 'lowest' for where it is least. Default 'highest'.",
                    },
                },
                "required": ["virus"],
            },
        },
    },
]

# What the harness runs: tool name -> Python function
TOOL_MAP = {
    "get_current_activity": get_current_activity,
    "get_historical_comparison": get_historical_comparison,
    "get_national_rankings": get_national_rankings,
}

# Fail at startup, not mid-conversation, if a schema and the map disagree
assert {t["function"]["name"] for t in TOOLS} == set(TOOL_MAP), "TOOLS and TOOL_MAP names don't match"


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    # Model asked for a tool that does not exist: tell it which ones do
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})

    # Look up the function and call it with the model's arguments
    try:
        return TOOL_MAP[name](**args)
    # Wrong or missing argument names (or a TypeError bug inside the tool)
    except TypeError as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}"})
    # Safety net: any other failure is reported to the model instead of crashing the turn
    except Exception as e:
        return json.dumps({"error": f"{name} failed: {type(e).__name__}: {e}"})
