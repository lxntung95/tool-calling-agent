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


def rows_for_virus(rows: list[dict], virus_prefix: str) -> list[dict]:
    """Keep only rows whose pathogen_target starts with the virus prefix (e.g. 'sars')."""
    return [r for r in rows if (r.get("pathogen_target") or "").lower().startswith(virus_prefix)]


def summarize_week(rows: list[dict], week: str) -> dict:
    """Site-level summary for one virus in one week: CDC category counts plus a median-based trend."""
    prior_week = (date.fromisoformat(week) - timedelta(weeks=TREND_WEEKS)).isoformat()

    def median_level(w: str) -> float | None:
        values = []
        for r in rows:
            if r["week"] != w:
                continue
            try:
                values.append(float(r["site_wval"]))
            except (KeyError, TypeError, ValueError):
                pass  # a site without a numeric level that week is skipped
        return median(values) if values else None

    now, before = median_level(week), median_level(prior_week)
    if now is None or not before:
        trend = "unknown (not enough earlier data)"
    elif now > before * 1.2:   # more than 20% higher than 3 weeks earlier
        trend = "rising"
    elif now < before / 1.2:   # more than 20% lower
        trend = "falling"
    else:
        trend = "steady"

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
        state = soql_text(place["state"])
        # 1. Newest week this state has data for
        latest = query_cdc({"$select": "max(week_end) AS latest", "$where": f"state_territory = {state}"})
        latest_week = (latest[0].get("latest") or "")[:10] if latest else ""
        if not latest_week:
            return json.dumps({"error": f"No wastewater data is reported for {place['state']}. Try a nearby state."})

        # 2. Only this state's rows for the last few weeks (the full dataset is too large to download)
        cutoff = (date.fromisoformat(latest_week) - timedelta(weeks=WEEKS_OF_HISTORY)).isoformat()
        rows = query_cdc({
            "$select": "site, counties_served, pathogen_target, site_wval, site_wval_category, week_end",
            "$where": f"state_territory = {state} AND week_end >= '{cutoff}'",
            "$limit": 50000,
        })
    except requests.RequestException as e:
        return json.dumps({"error": f"The CDC data service did not respond ({type(e).__name__}). Tell the user it is temporarily unavailable; do not retry more than once."})

    for r in rows:
        r["week"] = (r.get("week_end") or "")[:10]  # normalize "2026-09-19T00:00:00.000" -> "2026-09-19"

    # 3. Per virus: use the county's own newest week if it is recent enough, else fall back to the whole state
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
]

# What the harness runs: tool name -> Python function
TOOL_MAP = {"get_current_activity": get_current_activity}

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
