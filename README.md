NAME

    Viral Pulse

---

PROJECT OBJECTIVE

    A web-based chat agent that tells people how much COVID-19, influenza A, and RSV is circulating
    where they live or are traveling, plus recent wastewater detections of measles and H5 bird flu,
    using CDC wastewater surveillance data.

    Target user: members of the general public deciding whether to travel, attend a gathering, or
    visit a vulnerable relative. Wastewater data is public and updated weekly, but the CDC's
    dashboards are built for epidemiologists. Viral Pulse answers plain-language questions about a
    specific place, compares places and time periods, and points to nearby pharmacies or urgent
    care when activity is high.

    Live app (Columbia sign-in required): https://viral-pulse-622869374309.us-east4.run.app

---

LANGUAGE / STACK

    Python 3.12+ | FastAPI + Uvicorn | LiteLLM | Gemini (Google Cloud Agent Platform, formerly
    Vertex AI) | uv | Google Cloud Run (continuous deploy from GitHub, Identity-Aware Proxy)

---

SAMPLE QUERIES

    1. What's the respiratory virus situation in Brooklyn this week?
    2. Has measles or bird flu been detected in wastewater in California recently?
    3. Where is RSV highest in the country right now?
       Then, in the same chat: Where can I get a flu shot near Astoria, Queens?

    More to try:
    - Is COVID in Brooklyn higher than it was this time last year?
    - I'm flying to Las Vegas next week. Is COVID higher there than in New York City right now?
      What about RSV?

---

TOOLS

    - get_current_activity(location): latest weekly wastewater levels for COVID-19, influenza A,
      and RSV, with a rising, falling, or steady trend. Uses sites serving the place's county when
      they reported recently, otherwise the whole state, and says which it used.
    - get_historical_comparison(location, virus, weeks_ago): one virus now versus a past week, for
      example 52 weeks ago for "this time last year", comparing the same sampling sites where
      possible.
    - get_national_rankings(virus, top_n, order): states ranked by the share of their sites at the
      CDC's High or Very High level, with national context so a "top" state is not mistaken for a
      hotspot when activity is low everywhere.
    - get_emerging_threats(location): measles and H5 bird flu detections over the past six weeks
      for the place's county and state, including when an untested county was last tested.
    - find_nearby_care(location, care_type): pharmacies or urgent care centers in a city or
      neighborhood from the federal NPI Registry, ranked to favor the requested area.

---

TECHNICAL METHODOLOGY

    - Harness loop: run_agent() sends the conversation to the model, runs any tools it requests,
      feeds the results back, and repeats until the model answers (at most 5 rounds).
    - Session memory: an in-memory store keeps each conversation's history, keyed by session_id,
      so follow-up questions such as "where can I get tested nearby?" reuse the earlier place.
    - Transparent tool use: /chat returns response, session_id, and tool_calls (name, args, and
      result of every call), and the page shows each tool call above the answer.
    - Server-side filtering: the CDC datasets are too large to download, so every tool queries
      only the rows it needs (one state, a few weeks) through the Socrata query API.
    - Levels, not invented averages: answers report how many sites sit at each CDC category, so
      every number can be checked against the source data.
    - Trends: the median site level in the newest week versus 3 weeks earlier. More than 20%
      higher is "rising", more than 20% lower is "falling", otherwise "steady" (our own threshold).
    - Location handling: place names are geocoded, then mapped to a county through the FCC Area
      API. A bare state name ("California") is treated as a whole-state request.
    - Error handling: tools return actionable errors to the model instead of crashing (unknown
      place, no recent data, service unavailable), malformed tool arguments are sent back to the
      model, and a failed turn is rolled back so it cannot break the session.
    - Safety: the system prompt forbids case-count estimates and personal medical advice, requires
      stating the data week and area, and passes on each tool's caveats.

---

PROJECT STRUCTURE

    - app.py: FastAPI server, system prompt, harness loop, session store, /chat and /clear routes.
    - tools.py: the five tools, their JSON schemas, shared data helpers, and run_tool().
    - index.html: chat frontend (light and dark mode, Markdown replies, tool-call display).
    - pyproject.toml: project metadata and dependencies.
    - uv.lock: locked dependency versions for reproducible installs.
    - submission.json: deployment URL and author.

---

DATA & SOURCE

    - CDC NWSS, Wastewater Viral Activity Level for SARS-CoV-2, Influenza A and RSV
      (data.cdc.gov dataset atcp-73re). Some sites are sampled by WastewaterSCAN, whose data is
      licensed CC BY-NC 4.0 for non-commercial use.
    - CDC NWSS, Wastewater Data for Measles (akvg-8vrb) and Avian Influenza A (H5) (mtpu-urpp),
      licensed ODbL.
    - CMS NPI Registry (npiregistry.cms.hhs.gov): licensed healthcare providers.
    - Open-Meteo Geocoding API: place names to coordinates.
    - FCC Area API: coordinates to county.
    - None of the sources require an API key.

---

LIMITATIONS

    - Data is weekly and lags by about a week; some counties lag further (New York City's counties
      report 1 to 4 weeks behind upstate New York). Every answer states the week it covers.
    - Wastewater levels show how much virus circulates in a community, not individual risk or
      case counts.
    - Coverage is uneven: some counties have no sites, flu and RSV have fewer sites than COVID,
      and New York City stopped reporting measles and H5 testing after November 2025.
    - Rankings exclude states with fewer than 5 reporting sites.
    - A city spanning several counties resolves to the county at its center point, so "New York
      City" returns Manhattan (New York County) data. "New York" or "Washington" on their own are
      treated as the state.
    - The NPI Registry has no hours, walk-in availability, or vaccine stock, some registrations
      are years old, and large cities exceed its 200-result search cap, so results are a sample.
    - Sessions live in server memory and reset when Cloud Run restarts or redeploys.

---

SETUP

    1. Create a GCP project with billing and the Agent Platform API enabled.
    2. Authenticate locally: gcloud auth application-default login
    3. Install dependencies: uv sync
    4. Run the server: uv run app.py
    5. Open http://127.0.0.1:8000

    Deployment: continuous deploy from GitHub to Cloud Run with a buildpack and the entrypoint
    uvicorn app:app --host 0.0.0.0 --port $PORT, behind Identity-Aware Proxy for columbia.edu.

---

SUPPORT

    Visit my GitHub repository for the latest scripts and downloads:
    https://github.com/lxntung95
