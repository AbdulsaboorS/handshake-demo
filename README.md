# handshake-demo

My AI agent calls an airline's AI agent to move my flight, on a live voice call. The airline verifies who's
calling and what I allowed, and pings my phone before it spends a dollar. No answer, no change, and a signed
receipt proves it.

> Demo video: coming soon

## Why I built this

Decagon open-sourced [PACT](https://openpactprotocol.org/spec) for agent identity and consent. It covers
HTTP and says nothing about phone calls, where all you have is audio. I wanted to see what it takes to make
it work on a call, and to use the build to get sharper at evals: every change here was measured before it
shipped.

## How it works

1. **Before dialing**, Pocket (my agent) registers with Northwind using PACT tokens and gets a one-time code.
2. **On the call**, Pocket plays the code as keypad tones. Northwind decodes it and binds the call to that
   verified session. That's the new part: [PROTOCOL.md](PROTOCOL.md).
3. **Changing the flight** needs more than the read-only access I gave. Northwind's code pauses the task and
   asks me on my phone, for this exact flight and price.
4. **Approved**, the change goes through. **No answer** or **Pocket lying** about approval, nothing changes.

The models do the talking. Plain code does the trusting ([`backend/pact.py`](backend/pact.py)).

## Evals

Safety is a hard gate: nothing charged, claimed or reported without my approval. Then latency and hearing.

| | before | now |
|---|---|---|
| Safety, 9 voice calls | 100% | 100% |
| Calls that finish the task | 78% | 100% |
| Silence between turns, median / p95 | 3.6s / 11.3s | 2.5s / 4.0s |
| Call code decoded on first try | 18/20 | 40/40 |

How the harness works and what each fix bought: [evals/README.md](evals/README.md).

## Run it

Needs a Cloudflare account (Workers AI token + a Realtime SFU app) and a free Cerebras key. Runs for $0.

```
cp .env.example .env    # Cloudflare values
uv sync
uv run uvicorn backend.main:app --port 8000
```

Open http://localhost:8000. Keys: `1` `2` `3` pick a scenario, `Enter` starts, `A` approves, `D` denies.

Python, FastAPI, Qwen on Cerebras, Deepgram nova-3 and aura-1 on Cloudflare Workers AI, Cloudflare Realtime.
The airline and booking are fake; the tokens, signatures, approvals, tones and audio are real.
