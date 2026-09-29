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

# Needs to be updated based on what I decide the agent should do and how it should behave
SYSTEM_PROMPT = (
    "You are a helpful assistant. When a question depends on the weather or "
    "outdoor conditions, call get_weather first, then answer in a sentence."
)
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
            args = json.loads(call.function.arguments)              # Model's arguments: JSON string -> Python dict
            result = run_tool(name, args)                           # Run the matching Python function, get a JSON string back
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

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message}]

    # Run the agent harness with the current session context and handle any exceptions
    try:
        response, tool_calls = run_agent(sessions[session_id])
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
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
