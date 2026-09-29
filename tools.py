"""The tools the harness can run, and the JSON that describes them to the model."""

import json
import requests

# These URLs need to be updated based on what I decide to do with this agent
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


# Tool that will need to be overhauled once the agent's capabilities are expanded
def get_weather(location: str) -> str:
    """Get the current weather for a location."""
    try:
        places = requests.get(GEOCODE_URL, params={"name": location, "count": 1}, timeout=10).json()
        if not places.get("results"):
            return json.dumps({"error": f"City '{location}' was not found."})
        place = places["results"][0]

        current = requests.get(
            FORECAST_URL,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
            },
            timeout=10,
        ).json()["current"]
    except requests.RequestException as e:
        # The model cannot see an exception. Return something it can reason about.
        return json.dumps({"error": f"Weather service failed: {e}"})

    return json.dumps({
        "location": place["name"],
        "temp_f": current["temperature_2m"],
        "humidity": current["relative_humidity_2m"],
        "wind_mph": current["wind_speed_10m"],
    })


# What the model sees: the "set notes" in the screenplay.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather (temperature, humidity, wind) for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name, e.g. 'New York'"},
                },
                "required": ["location"],
            },
        },
    },
]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {"get_weather": get_weather}


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
