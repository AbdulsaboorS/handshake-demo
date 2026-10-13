"""Workers AI client. The only place that talks to a model."""

import asyncio
import json
import os
import re
import time
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
    body = {"model": os.environ["LLM_MODEL"], "messages": messages, "temperature": 0.1}
    if tools:
        body["tools"] = tools
    r = await post_with_retry(_client, "/chat/completions", json=body)
    return r.json()["choices"][0]["message"]


async def chat_stream(messages: list[dict], tools: list[dict], on_say: Callable[[str], None]) -> dict:
    """chat() over a stream. While the model writes a say call, its text goes to on_say as it arrives, so
    speech can start before the model finishes the turn. Returns the same message chat() would."""
    body = {"model": os.environ["LLM_MODEL"], "messages": messages, "temperature": 0.1, "tools": tools, "stream": True}
    for attempt in (1, 2):
        spoken: dict[int, str] = {}
        try:
            async with _client.stream("POST", "/chat/completions", json=body) as r:
                r.raise_for_status()
                return await _read_stream(r, spoken, on_say)
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            # Retry a blip only if nothing was said yet; half a line can't be taken back.
            server_side = not isinstance(e, httpx.HTTPStatusError) or e.response.status_code >= 500
            if attempt == 2 or spoken or not server_side:
                raise
            await asyncio.sleep(0.5)
    raise AssertionError("unreachable")


async def _read_stream(r: httpx.Response, spoken: dict[int, str], on_say: Callable[[str], None]) -> dict:
    content, calls = "", {}
    async for line in r.aiter_lines():
        if not line.startswith("data:") or line.endswith("[DONE]"):
            continue
        choices = json.loads(line[5:]).get("choices")
        delta = choices[0]["delta"] if choices else {}
        content += delta.get("content") or ""
        for d in delta.get("tool_calls") or []:
            call = calls.setdefault(d["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            call["id"] = d.get("id") or call["id"]
            call["function"]["name"] += d.get("function", {}).get("name") or ""
            call["function"]["arguments"] += d.get("function", {}).get("arguments") or ""
            if call["function"]["name"] == "say":
                text, before = _partial_text(call["function"]["arguments"]), spoken.get(d["index"], "")
                if len(text) > len(before):
                    on_say(text[len(before):])
                    spoken[d["index"]] = text
    return {"content": content, "tool_calls": [calls[i] for i in sorted(calls)]}


def _partial_text(arguments: str) -> str:
    """The "text" value of a say call whose JSON is still arriving: '{"text": "Hi, I\\'d' -> "Hi, I'd"."""
    start = arguments.find('"text"')
    start = arguments.find('"', arguments.find(":", start) + 1) if start >= 0 else -1
    if start < 0:
        return ""
    out, i = [], start + 1
    while i < len(arguments):
        c = arguments[i]
        if c == '"':
            break
        if c == "\\":
            esc = arguments[i:i + 6] if arguments[i + 1:i + 2] == "u" else arguments[i:i + 2]
            if len(esc) < (6 if esc[1:2] == "u" else 2):
                break  # escape not complete yet
            out.append(json.loads(f'"{esc}"'))
            i += len(esc)
            continue
        out.append(c)
        i += 1
    return "".join(out)


class Sentences:
    """Buffers streamed text and hands out whole sentences, so each one can be synthesized on its own."""

    def __init__(self, emit: Callable[[str], None]):
        self.emit = emit
        self.buf = ""

    def feed(self, text: str) -> None:
        self.buf += text
        # A sentence ends at . ! or ? followed by a space. "9:40 PM." waits for the space; "$84.00" never splits.
        while m := re.search(r"[.!?]\s", self.buf[20:]):
            end = 20 + m.end()
            self.emit(self.buf[:end].strip())
            self.buf = self.buf[end:]

    def flush(self) -> None:
        if self.buf.strip():
            self.emit(self.buf.strip())
        self.buf = ""


async def post_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """One retry on a server error or timeout. A blip shouldn't end a live call."""
    for attempt in (1, 2):
        try:
            r = await client.post(url, **kwargs)
            if r.status_code < 500 or attempt == 2:
                r.raise_for_status()
                return r
        except httpx.TransportError:
            if attempt == 2:
                raise
        await asyncio.sleep(0.5)
    raise AssertionError("unreachable")


SAY = tool("say", "Say this out loud to the other party. Every turn ends with exactly one say.", text="What to say")


async def run_turn(
    messages: list[dict],
    tools: list[dict],
    handlers: dict[str, Handler],
    on_tool: Callable[[str, dict, object], None],
    on_round: Callable[[float], None],
    on_sentence: Callable[[str], None],
) -> str:
    """Let the model call tools until it says something, append everything to messages, return the line.

    Speech goes through a `say` tool because Llama on Workers AI reaches for a tool whenever any are
    offered. Making speech a tool turns that habit into a reliable end-of-turn signal."""
    for _ in range(MAX_TOOL_ROUNDS):
        t = time.time()
        sentences = Sentences(on_sentence)
        msg = await chat_stream(messages, [*tools, SAY], sentences.feed)
        sentences.flush()
        on_round(time.time() - t)
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": calls} if calls else {})})
        if not calls:
            line = (msg.get("content") or "").strip()
            if line:  # the model spoke without the tool; it still gets said
                on_sentence(line)
            return line
        said = None
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
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
