import json
import uuid
from pathlib import Path

import litellm
from litellm import ModelResponse
from litellm.types.utils import ChatCompletionMessageToolCall

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tools import TOOLS, run_tool

# --- CONFIGURATION ---

# Instructions sent as the first message of every session; add one routing line per tool as each tool is built
SYSTEM_PROMPT = """You are Viral Pulse, an assistant that reports how much COVID-19, influenza A, and RSV \
is circulating in US communities, based on CDC wastewater surveillance data.

How to answer:
- For the current situation in any place, call get_current_activity. For comparisons, call it once per place.
- For comparisons with the past, such as "this time last year", call get_historical_comparison.
- Always state which week the data covers.
- Describe activity levels (very low, low, moderate, high, very high) and trends in plain language. \
Wastewater levels reflect how much virus is circulating in a community, not any individual's risk.
- Never estimate case counts or the number of people infected; wastewater data cannot support that.
- If a tool reports no recent data for a place, say so and offer the nearest place that has data.
- Keep answers concise: a few sentences, or a short list when comparing places.
- Name the area each reading comes from (the tool's area_used). A county is not a whole city; for example, \
New York County is Manhattan only, so never present one county's data as all of New York City.
- If a reading is based on only one or two sites, say so, since it may not represent the wider area.

Safety:
- Do not give personal medical advice or diagnoses. When levels are high, you may mention general \
precautions such as vaccination, handwashing, and staying home when sick, and point the user to cdc.gov \
or their healthcare provider.
- If someone describes severe symptoms or an emergency, tell them to contact a healthcare provider or call 911.

Scope:
- If asked about something unrelated to respiratory virus activity, briefly explain what you can help with.
"""
MAX_TOOL_ROUNDS = 5  # Maximum number of tool call rounds the agent can make before giving up


# --- THE HARNESS ---

def run_agent(messages: list[dict]) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    Returns the final text and a record of every tool call made along the way.
    """
    tool_calls = []

    for _ in range(MAX_TOOL_ROUNDS):
        response = litellm.completion(
            model="vertex_ai/gemini-3.5-flash-lite",  # Model to use
            vertex_location="global",                 # Google routes each call to any region with capacity available
            messages=messages,                        # Conversation history passed to the model
            tools=TOOLS,                              # List of available tools for the model to use
        )

        # Confirm a normal (non-streaming) response, then grab the model's one reply
        assert isinstance(response, ModelResponse)
        reply = response.choices[0].message

        # Append assistant's reply (text, tool calls, or both) to the context
        # model_dump() keeps it a plain dict: the raw object carries provider-specific fields that trip Pydantic when LiteLLM re-serializes it next round
        messages += [reply.model_dump()]

        # If the model did not request any tool calls, return the final response
        if not reply.tool_calls:
            return reply.content or "", tool_calls

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            assert isinstance(call, ChatCompletionMessageToolCall)  # All TOOLS are "function" type, so every call is a function call
            name = call.function.name or ""                         # Which tool the model asked for; "" falls through to unknown-tool error
            
            # Model's arguments: JSON string -> Python dict; broken JSON goes back to the model as an error
            try:
                args = json.loads(call.function.arguments)
            except json.JSONDecodeError as e:
                args = {}
                result = json.dumps({"error": f"Arguments for {name} were not valid JSON ({e}). Resend the call with a JSON object."})
            else:
                result = run_tool(name, args)  # Run the matching Python function, get a JSON string back
            
            tool_calls += [{"name": name, "args": args, "result": result}]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing.", tool_calls


# --- SESSION STORE ---

# session_id -> list of messages. In-memory, single process.
sessions: dict[str, list] = {}


# --- FastAPI APP ---

# Initialize the FastAPI application
app = FastAPI()


# Messages from the client (incoming chat requests)
class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


# Chat response model that represents the outgoing chat messages
class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


# Handle GET requests to the root URL
@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")  # Send index.html back as the page


# Handle POST requests to the /chat endpoint
@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Use the client's ID or make a new one if it sent NULL
    session_id = request.session_id or str(uuid.uuid4())

    # Initialize the session with the system prompt if it does not exist
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]  # Start history with the SYSTEM_PROMPT

    # Remember where this turn starts, so a failed turn can be undone
    checkpoint = len(sessions[session_id])

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message}]

    # Run the agent harness with the current session context and handle any exceptions
    try:
        response, tool_calls = run_agent(sessions[session_id])
    except Exception as e:
        # Undo the failed turn so a half-finished tool exchange can't break the next message
        del sessions[session_id][checkpoint:]
        # Auth, billing, a model that is not running: show it in the chat, not as a 500
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


# Clear session route that removes a session from the store
@app.post("/clear")
def clear(session_id: str | None = None):
    if session_id: sessions.pop(session_id, None)
    return {"status": "ok"}


# Run the server locally when this file is executed directly (uv run app.py); Cloud Run skips this
if __name__ == "__main__":
    # 127.0.0.1 = this machine only; 8000 = standard Python dev port
    uvicorn.run(app, host="127.0.0.1", port=8000)
