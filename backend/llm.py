"""Workers AI client. The only place that talks to a model."""

import json
import os
from collections.abc import Awaitable, Callable

import httpx
from dotenv import load_dotenv

load_dotenv()

MAX_TOOL_ROUNDS = 6

_client = httpx.AsyncClient(
    base_url=f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CF_ACCOUNT_ID']}/ai/v1",
    headers={"Authorization": f"Bearer {os.environ['CF_API_TOKEN']}"},
    timeout=60,
)

Handler = Callable[..., Awaitable[object]]


def tool(name: str, description: str, **params: str) -> dict:
    """Tool schema shorthand. Every param is a required string, which is all these agents need."""
    return {"type": "function", "function": {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {p: {"type": "string", "description": d} for p, d in params.items()},
            "required": list(params),
        },
    }}


async def chat(messages: list[dict], tools: list[dict] | None = None) -> dict:
    body = {"model": os.environ["LLM_MODEL"], "messages": messages, "temperature": 0.3}
    if tools:
        body["tools"] = tools
    r = await _client.post("/chat/completions", json=body)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


SAY = tool("say", "Say this out loud to the other party. Every turn ends with exactly one say.", text="What to say")


async def run_turn(
    messages: list[dict],
    tools: list[dict],
    handlers: dict[str, Handler],
    on_tool: Callable[[str, dict, object], None],
) -> str:
    """Let the model call tools until it says something, append everything to messages, return the line.

    Speech goes through a `say` tool because Llama on Workers AI reaches for a tool whenever any are
    offered. Making speech a tool turns that habit into a reliable end-of-turn signal."""
    for _ in range(MAX_TOOL_ROUNDS):
        msg = await chat(messages, [*tools, SAY])
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": calls} if calls else {})})
        if not calls:
            return (msg.get("content") or "").strip()
        said = None
        for call in calls:
            name, args = call["function"]["name"], json.loads(call["function"]["arguments"] or "{}")
            if name == "say":
                said, result = args.get("text", ""), {"status": "said"}
            else:
                handler = handlers.get(name)
                try:
                    result = await handler(**args) if handler else {"error": f"unknown tool {name}"}
                except TypeError as e:  # model sent the wrong arguments
                    result = {"error": str(e)}
                on_tool(name, args, result)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})
        if said is not None:
            return said.strip()
    raise RuntimeError("model kept calling tools without saying anything")
