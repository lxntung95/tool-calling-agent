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

# Emerging threats: CDC sample-level datasets that record whether each sample detected the virus
EMERGING_THREATS = {
    "Measles": {
        "url": "https://data.cdc.gov/resource/akvg-8vrb.json",
        "caveat": "Measures wild-type measles virus. A detection means at least one sample was positive; "
                  "no detection does not rule out infections in the community.",
    },
    "H5 bird flu": {
        "url": "https://data.cdc.gov/resource/mtpu-urpp.json",
        "caveat": "H5 can enter wastewater from animal sources such as birds or milk, so a detection "
                  "does not confirm that any person is infected.",
    },
}

# US states, DC, and Puerto Rico -> postal code; lets tools accept a state on its own ("California")
US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Puerto Rico": "PR", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT", "Virginia": "VA",
    "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}
STATE_BY_LOWER = {name.lower(): name for name in US_STATES}

# The three viruses in the dataset, matched by how their pathogen_target text starts
VIRUSES = {"COVID-19": "sars", "Influenza A": "influenza a", "RSV": "rsv"}

TREND_WEEKS = 3        # trend compares a week with this many weeks earlier
MAX_LOCAL_LAG_WEEKS = 3  # county data may be up to this many weeks older than the state's newest week
WEEKS_OF_HISTORY = TREND_WEEKS + MAX_LOCAL_LAG_WEEKS + 1  # how far back tool 1 fetches
MIN_SITES_TO_RANK = 5  # states with fewer reporting sites are left out of national rankings (too noisy)
HIGH_LEVELS = {"High", "Very High"}  # CDC categories counted as elevated in rankings
EMERGING_WINDOW_WEEKS = 6  # look-back for detections; CDC's own display uses the past six weeks


# --- HELPERS (shared by tools; the model never sees these) ---

def geocode(location: str) -> dict:
    """Place name -> {name, state, state_code, lat, lon}. Accepts 'City, State', 'City', or a state alone
    ('California', which returns no coordinates). Raises ValueError with advice."""
    name, _, state_hint = location.partition(",")  # "Austin, Texas" -> "Austin", "Texas"
    name, hint = name.strip(), state_hint.strip().lower()

    # A bare state name means the whole state; otherwise 'California' would match a town called California
    if not hint and name.lower() in STATE_BY_LOWER:
        state = STATE_BY_LOWER[name.lower()]
        return {"name": state, "state": state, "state_code": US_STATES[state].lower(), "lat": None, "lon": None}

    resp = requests.get(GEOCODE_URL, params={"name": name, "count": 10}, timeout=10)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    us = [r for r in results if r.get("country_code") == "US"]
    if not us:
        if results:
            raise ValueError(f"'{location}' is outside the US. Wastewater data only covers US locations.")
        raise ValueError(f"Could not find '{location}'. Try a city or county name with its full state, e.g. 'Austin, Texas'.")
    match = next((r for r in us if hint and r.get("admin1", "").lower() == hint), us[0])  # prefer the named state
    state = match.get("admin1", "")
    return {
        "name": match["name"], "state": state, "state_code": US_STATES.get(state, "").lower(),
        "lat": match["latitude"], "lon": match["longitude"],
    }


def county_info(lat: float | None, lon: float | None) -> dict | None:
    """Coordinates -> {'name': 'Kings', 'fips': '36047', 'state_code': 'ny'}, or None if unavailable."""
    if lat is None or lon is None:
        return None  # a whole-state request has no single county
    try:
        resp = requests.get(COUNTY_URL, params={"lat": lat, "lon": lon, "format": "json"}, timeout=10)
        resp.raise_for_status()
        result = resp.json()["results"][0]
        name = result["county_name"]
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return None  # not fatal: tools fall back to state-level data
    for suffix in (" County", " Parish", " Borough"):
        name = name.removesuffix(suffix)
    return {"name": name, "fips": result.get("county_fips"), "state_code": (result.get("state_code") or "").lower()}


def county_at(lat: float | None, lon: float | None) -> str | None:
    """Coordinates -> county name without its suffix ('Kings'), or None if the lookup fails."""
    info = county_info(lat, lon)
    return info["name"] if info else None


def soql_text(value: str) -> str:
    """Quote a value for a Socrata query; a single quote inside text is written as two."""
    return "'" + value.replace("'", "''") + "'"


def query_socrata(url: str, params: dict) -> list[dict]:
    """Run one filtered query against a data.cdc.gov dataset and return its rows."""
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()  # turn HTTP errors (400, 500) into exceptions the tools can report
    return resp.json()


def query_cdc(params: dict) -> list[dict]:
    """Run one filtered query against the CDC wastewater activity-level dataset."""
    return query_socrata(CDC_WVAL_URL, params)


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


def summarize_detections(rows: list[dict]) -> dict:
    """Sites and samples tested, and which of them detected the virus (pcr_target_detect == 'yes')."""
    detected = [r for r in rows if (r.get("pcr_target_detect") or "").lower() == "yes"]
    return {
        "sites_tested": len({r.get("site") for r in rows}),
        "samples_tested": len(rows),
        "sites_with_detection": len({r.get("site") for r in detected}),
        "samples_with_detection": len(detected),
        "last_detection": max((r["sample_collect_date"][:10] for r in detected), default=None),
    }


def county_not_tested(url: str, state_code: str, county: dict) -> str:
    """Explain that a county had no testing in the window, including when it was last tested, if ever."""
    message = f"no sites in {county['name']} County tested for this in the window"
    try:
        last = query_socrata(url, {
            "$select": "max(sample_collect_date) AS last",
            "$where": f"state_territory = {soql_text(state_code)} AND county_fips like {soql_text('%' + county['fips'] + '%')}",
        })
    except requests.RequestException:
        return message  # the extra context is optional; keep the main answer
    last_date = (last[0].get("last") or "")[:10] if last else ""
    return f"{message} (last tested {last_date})" if last_date else f"{message} (no testing on record)"


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
            summary["note"] = (
                f"No recent sites in {county} County reported this virus, so this is statewide." if county
                else "Statewide reading."
            )
        viruses[virus] = summary

    place_label = place["state"] if place["lat"] is None else f"{place['name']}, {place['state']}"
    return json.dumps({"place": place_label, "county": county, "viruses": viruses})


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
        "place": place["state"] if place["lat"] is None else f"{place['name']}, {place['state']}",
        "virus": virus,
        "area_used": area,
        "note": None if area.endswith("County") else (
            f"{county} County lacked data in one of the periods, so both readings are statewide." if county
            else "Statewide comparison."
        ),
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


def get_emerging_threats(location: str) -> str:
    """Recent wastewater detections of measles and H5 bird flu near a US place (county and state)."""
    try:
        place = geocode(location)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    county = county_info(place["lat"], place["lon"])  # None for a whole-state request
    state_code = (county or {}).get("state_code") or place["state_code"]
    if not state_code:
        return json.dumps({"error": f"Could not identify the state for '{location}'. Try a city with its full state name."})

    threats = {}
    for threat, source in EMERGING_THREATS.items():
        try:
            # Anchor the window on the newest sample nationally, since uploads lag collection
            newest = query_socrata(source["url"], {"$select": "max(sample_collect_date) AS latest"})
            newest_date = (newest[0].get("latest") or "")[:10] if newest else ""
            if not newest_date:
                threats[threat] = {"status": "no recent national data available"}
                continue
            start = (date.fromisoformat(newest_date) - timedelta(weeks=EMERGING_WINDOW_WEEKS)).isoformat()
            rows = query_socrata(source["url"], {
                "$select": "site, county_fips, counties_served, sample_collect_date, pcr_target_detect",
                "$where": f"state_territory = {soql_text(state_code)} AND sample_collect_date >= '{start}'",
                "$limit": 50000,
            })
        except requests.RequestException as e:
            threats[threat] = {"error": f"The CDC data service did not respond ({type(e).__name__}); report this threat as unavailable."}
            continue

        window = f"{start} to {newest_date}"
        if not rows:
            threats[threat] = {"window": window, "status": f"no sites in {place['state']} tested for this in the window", "caveat": source["caveat"]}
            continue

        # Match the county by FIPS code; a site can serve several counties, listed like "36081, 36061, 36047"
        county_rows = [
            r for r in rows
            if county and county["fips"] in (f.strip() for f in (r.get("county_fips") or "").split(","))
        ]
        state_summary = summarize_detections(rows)
        state_summary["counties_with_detection"] = sorted({
            r.get("counties_served") or "unknown" for r in rows if (r.get("pcr_target_detect") or "").lower() == "yes"
        })
        threats[threat] = {
            "window": window,
            "county": (
                summarize_detections(county_rows) if county_rows
                else county_not_tested(source["url"], state_code, county) if county
                else "not applicable (whole-state request)"
            ),
            "state": state_summary,
            "caveat": source["caveat"],
        }

    return json.dumps({
        "place": place["name"] if not county else f"{place['name']}, {place['state']}",
        "county": f"{county['name']} County" if county else None,
        "threats": threats,
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
                        "description": "US city or county with its full state name, e.g. 'Brooklyn, New York' or 'Las Vegas, Nevada', or a state alone for statewide data, e.g. 'California'",
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
                        "description": "US city or county with its full state name, e.g. 'Brooklyn, New York', or a state alone for statewide data, e.g. 'California'",
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
    {
        "type": "function",
        "function": {
            "name": "get_emerging_threats",
            "description": (
                "Check CDC wastewater testing for recent detections of measles and H5 bird flu near a US place, "
                "over roughly the past six weeks. Returns, for the place's county and its whole state, how many sites "
                "and samples were tested and how many detected each virus, with the date of the latest detection and "
                "a caveat on interpreting it. Use this when asked about measles, bird flu, or other emerging threats; "
                "it does not cover COVID-19, flu, or RSV."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "US city or county with its full state name, e.g. 'Brooklyn, New York', or a state alone for statewide data, e.g. 'California'",
                    },
                },
                "required": ["location"],
            },
        },
    },
]

# What the harness runs: tool name -> Python function
TOOL_MAP = {
    "get_current_activity": get_current_activity,
    "get_historical_comparison": get_historical_comparison,
    "get_national_rankings": get_national_rankings,
    "get_emerging_threats": get_emerging_threats,
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
