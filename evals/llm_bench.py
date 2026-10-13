"""Which LLM host gets the airline to its first spoken sentence fastest, and does it use the say tool right?

Replays a real mid-call airline turn (prompt, tools, conversation so far) a few times per model and measures
time to first sentence, total time, and whether the line came out through say. Stays well inside free limits.

  uv run python -m evals.llm_bench                     # Cloudflare (LLM_MODEL) + every Cerebras model listed
  uv run python -m evals.llm_bench --runs 3 --models llama-3.3-70b
"""

import argparse
import asyncio
import json
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv(".env")

from backend import airline, llm  # noqa: E402
from backend.business_agent import TOOLS, BusinessAgent  # noqa: E402
from backend.pact import Provider, Signer, mint_agent_token  # noqa: E402

AUD = "https://northwind.example/a2a"
CEREBRAS = "https://api.cerebras.ai/v1"


def airline_turn() -> list[dict]:
    """The airline's messages right before it offers the new flight: the slowest turn in a call."""
    airline.reset()
    provider = Provider("https://auth.northwind.example", AUD, 30)
    platform = Signer("https://pocket.example")
    provider.trust(platform)
    session = provider.register(mint_agent_token(platform, "pkt", AUD),
                                provider.grant(airline.OWNER_ID, platform.issuer, {"bookings:read"}))
    business = BusinessAgent(provider, session, lambda *a, **k: None, lambda a: None)
    business.greet()
    business.messages.append({"role": "user", "content": "I'm Abdul's AI agent, and I want to move the Seattle to Dubai "
                              "flight on November 17 to a later departure the same day."})
    return business.messages


async def one(client: httpx.AsyncClient, model: str, messages: list[dict]) -> dict:
    body = {"model": model, "messages": messages, "temperature": 0.1, "tools": [*TOOLS, llm.SAY], "stream": True}
    t0, first, said = time.time(), None, ""

    def on_sentence(_: str) -> None:
        nonlocal first
        if first is None:
            first = time.time() - t0  # the moment speech could start

    sentences = llm.Sentences(on_sentence)

    def on_say(text: str) -> None:
        nonlocal said
        said += text
        sentences.feed(text)

    spoken: dict[int, str] = {}
    async with client.stream("POST", "/chat/completions", json=body) as r:
        headers = {k: v for k, v in r.headers.items() if "ratelimit" in k}
        if r.status_code != 200:
            return {"error": f"{r.status_code} {(await r.aread()).decode()[:200]}", "headers": headers}
        msg = await llm._read_stream(r, spoken, on_say)
    sentences.flush()
    calls = [c["function"]["name"] for c in msg["tool_calls"]]
    return {"first_sentence_s": first, "total_s": time.time() - t0, "tools": calls, "said": said[:90],
            "content": msg["content"][:60], "headers": headers}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--models", help="comma separated Cerebras model ids (default: all listed)")
    args = parser.parse_args()
    messages = airline_turn()

    targets = [("cloudflare", llm._client, os.environ["LLM_MODEL"])]
    if key := os.environ.get("CEREBRAS_API_KEY"):
        cb = httpx.AsyncClient(base_url=CEREBRAS, headers={"Authorization": f"Bearer {key}"}, timeout=60)
        listed = [m["id"] for m in (await cb.get("/models")).json()["data"]]
        print("Cerebras models on this key:", ", ".join(listed))
        for m in (args.models.split(",") if args.models else listed):
            targets.append(("cerebras", cb, m))

    for host, client, model in targets:
        results = []
        for _ in range(args.runs):
            try:
                results.append(await one(client, model, messages))
            except Exception as e:  # a model that can't do tools or streaming is a result, not a crash
                results.append({"error": repr(e)[:200]})
            await asyncio.sleep(2)  # stay far under per-minute limits
        ok = [r for r in results if "error" not in r]
        firsts = sorted(r["first_sentence_s"] for r in ok if r["first_sentence_s"])
        print(f"\n{host}/{model}: {len(ok)}/{len(results)} ok")
        if firsts:
            print(f"  first sentence: median {firsts[len(firsts) // 2]:.2f}s  best {firsts[0]:.2f}s  worst {firsts[-1]:.2f}s")
        for r in results:
            print("  ", json.dumps({k: v for k, v in r.items() if k != "headers"})[:220])
        if results and results[-1].get("headers"):
            print("   limits:", results[-1]["headers"])


if __name__ == "__main__":
    asyncio.run(main())
