NAME

    Tool-Calling Agent

---

PROJECT OBJECTIVE

    A web-based chat agent that uses tool calling to answer user questions with live external data. The agent runs a harness loop around a Gemini model, executes the tools the model requests, and returns both the final answer and a record of every tool call made along the way.

---

LANGUAGE / STACK

    Python | Gemini (Google Cloud Agent Platform, formerly Vertex AI), uv, Google Cloud Run

---

TECHNICAL METHODOLOGY

    - Harness Loop: run_agent() sends the conversation to the model, executes any tool calls it requests, feeds the results back, and repeats until the model returns a final answer.
    - Session Memory: a session store keeps each user's conversation history across turns, keyed by session_id.
    - Transparent Tool Use: the /chat endpoint returns the response, session_id, and tool_calls (name, args, and result of every call), and the page displays the tool calls above the assistant's answer.
    - Model Portability: the model is set by a single string (vertex_ai/gemini-3.5-flash-lite), so the same server can run against a local model instead.

---

PROJECT STRUCTURE

    - app.py: web server entry point, harness loop, session store, and /chat endpoint.
    - tools.py: tool definitions available to the agent.
    - index.html: chat frontend.
    - pyproject.toml: project metadata and dependencies.
    - uv.lock: locked dependency versions for reproducible installs.

---

DATA & SOURCE

    - Weather: Open-Meteo API (no API key required).

---

SETUP

    1. Create a GCP project with billing and the Agent Platform API enabled.
    2. Authenticate locally: gcloud auth application-default login
    3. Run the server: uv run app.py
    4. Open http://localhost:8000

    To run against a local model instead (no Google account needed):
        MODEL=ollama_chat/qwen2.5:1.5b uv run app.py

---

SAMPLE QUERIES

    1. Is it nice enough to go for a walk in New York?

---

SUPPORT

    Visit my GitHub repository for the latest scripts and downloads:
    https://github.com/lxntung95