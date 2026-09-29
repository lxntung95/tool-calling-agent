NAME

    Viral Pulse

---

PROJECT OBJECTIVE

    Viral Pulse is a chat agent that uses CDC wastewater data to tell you how much COVID-19,
    influenza A, and RSV is going around in a given place, and whether measles or H5 bird flu has
    shown up in wastewater recently.

    It is meant for anyone weighing a trip, a gathering, or a visit to a vulnerable relative. You
    can ask about one place, compare places or time periods, and find nearby pharmacies or urgent
    care when activity is high. The data is public and updates weekly.

    Live app (requires Columbia sign-in): https://viral-pulse-622869374309.us-east4.run.app

---

LANGUAGE / STACK

    Python | FastAPI + Uvicorn | LiteLLM | Gemini (Google Cloud Agent Platform, formerly Vertex AI)
    | uv | Google Cloud Run (continuous deployment from GitHub, Identity-Aware Proxy)

---

SAMPLE QUERIES

    1. What is the respiratory virus situation in Brooklyn this week?
    2. Has measles or bird flu been detected in California recently?
    3. Where is RSV highest in the country right now?
       Then, in the same chat: Where can I get a flu shot near Astoria, Queens?

    A few more to try:
    - Is COVID in Brooklyn higher than it was this time last year?
    - I am flying to Las Vegas next week. Is COVID higher there than in New York City right now?
      What about RSV?

---

TOOLS

    - get_current_activity(location): Gets the latest weekly wastewater levels for COVID-19,
      influenza A, and RSV, and whether each is rising, falling, or steady. It uses sites in the
      county when they have reported recently, otherwise the whole state, and says which one it
      used.
    - get_historical_comparison(location, virus, weeks_ago): Compares one virus today with a past
      week (e.g., 52 weeks ago for "this time last year"), sticking to the same sampling sites
      where it can.
    - get_national_rankings(virus, top_n, order): Ranks states by the share of their sites at the
      CDC's High or Very High level. It also gives national context, so a state at the top of the
      list does not look like a hotspot when activity is low everywhere.
    - get_emerging_threats(location): Checks for measles and H5 bird flu detections in the county
      and state over the past six weeks, and notes when an untested county was last tested.
    - find_nearby_care(location, care_type): Looks up pharmacies or urgent care centers in a city
      or neighborhood through the federal NPI Registry, with results in the requested area ranked
      first.

---

TECHNICAL METHODOLOGY

    - Harness loop: run_agent() sends the conversation to the model, runs whatever tools it calls,
      passes the results back, and keeps going until the model answers, for up to 5 rounds.
    - Session memory: Each conversation's history lives in an in-memory store keyed by session_id,
      so a follow-up like "where can I get tested nearby?" picks up the location from earlier.
    - Tool-call visibility: /chat returns the response, the session_id, and a tool_calls list with
      the name, arguments, and result of every call. The frontend shows these above each answer.
    - Server-side filtering: The CDC datasets are too big to download whole, so each tool queries
      just the rows it needs (one state, a few weeks) through the Socrata API.
    - CDC levels reported directly: Instead of calculating new averages, answers report how many
      sites sit at each CDC activity level, so every number can be traced back to the source data.
    - Trend calculation: The median site level in the latest week is compared with 3 weeks
      earlier. If it is more than 1.2 times the earlier level, it counts as "rising"; if the
      earlier level is more than 1.2 times the latest, it counts as "falling"; anything in
      between is "steady". The 1.2 cutoff is my own, not a CDC standard.
    - Location handling: Place names are geocoded to coordinates and then matched to a county with
      the FCC Area API. A state name on its own (e.g., "California") is treated as a statewide
      question.
    - Error handling: When something goes wrong (unknown place, no recent data, a service down),
      tools send the model a useful error message instead of crashing. Malformed tool arguments go
      back to the model to fix, and a failed turn is rolled back so it does not break the session.
    - Safety: The system prompt bars case-count estimates and personal medical advice, requires
      each answer to name the data week and area, and tells the model to pass along each tool's
      caveats.

---

PROJECT STRUCTURE

    - app.py: The FastAPI server, system prompt, harness loop, session store, and /chat and /clear
      routes.
    - tools.py: The five tools, their JSON schemas, shared data helpers, and run_tool().
    - index.html: The chat frontend, with light and dark mode, Markdown replies, and tool-call
      display.
    - pyproject.toml: Project metadata and dependencies.
    - uv.lock: Pinned dependency versions for reproducible installs.
    - submission.json: The deployment URL and author.

---

DATA SOURCES

    - CDC NWSS, Wastewater Viral Activity Level for SARS-CoV-2, Influenza A, and RSV
      (data.cdc.gov: atcp-73re). Some sites are sampled by WastewaterSCAN, whose data is licensed
      CC BY-NC 4.0 (non-commercial use only).
    - CDC NWSS, Wastewater Data for Measles (data.cdc.gov: akvg-8vrb) and Avian Influenza A (H5)
      (data.cdc.gov: mtpu-urpp), both licensed under ODbL.
    - CMS NPI Registry (npiregistry.cms.hhs.gov): Directory of licensed healthcare providers.
    - Open-Meteo Geocoding API: Turns place names into coordinates.
    - FCC Area API: Matches coordinates to counties.
    - None of these sources need an API key.

---

LIMITATIONS

    - Data is weekly and usually about a week behind. Some counties trail further; New York City's
      counties report 1 to 4 weeks behind upstate New York. Every answer says which week it
      covers.
    - Wastewater levels show how much virus is circulating in a community. They do not measure
      individual risk or case counts.
    - Coverage is uneven. Some counties have no sites, flu and RSV are tracked at fewer sites than
      COVID-19, and New York City stopped reporting measles and H5 testing after November 2025.
    - States with fewer than 5 reporting sites are left out of the rankings.
    - A city that spans several counties maps to the county at its center, so "New York City"
      returns Manhattan (New York County) data. "New York" or "Washington" on their own are
      treated as states.
    - The NPI Registry has no hours, walk-in availability, or vaccine stock, some listings are
      years out of date, and large cities hit its 200-result search cap, so results are a sample
      rather than a full list.
    - Sessions live in server memory and are cleared whenever Cloud Run restarts or redeploys.

---

SETUP

    1. Create a GCP project with billing and the Agent Platform API turned on.
    2. Authenticate locally: gcloud auth application-default login
    3. Install dependencies: uv sync
    4. Start the server: uv run app.py
    5. Go to http://127.0.0.1:8000 in your browser.

    Deployment: The app deploys automatically from GitHub to Cloud Run with a buildpack, using the
    entrypoint uvicorn app:app --host 0.0.0.0 --port $PORT. It sits behind Identity-Aware Proxy, so
    only columbia.edu accounts can sign in.

---

SUPPORT

    The latest code is on my GitHub: https://github.com/lxntung95
