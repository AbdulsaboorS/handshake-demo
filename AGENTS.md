# AGENTS.md

Read this, then `SESSION.md` (local only, gitignored), then `git log --oneline -10` before touching anything.

## What this is
A demo of PACT over a live voice call. A personal AI agent calls an airline's support agent to change a flight.
The airline verifies who the agent is and whose behalf it acts on, lets it read the booking, and
requires the human owner to approve on their phone before any change or charge. If the owner
doesn't answer in time, nothing happens and a signed receipt proves it.

PACT (Decagon + Instinct, open source) covers agent identity and delegated consent over HTTP. It says
nothing about voice. Sierra's fleming-1 only detects synthetic voices and says "the best case is an
agent that says who it is." This repo shows how that works on a live voice call. Build on PACT's real
names and semantics, never invent a parallel protocol.
- Spec: https://openpactprotocol.org/spec
- Reference repo: https://github.com/openpactprotocol/openpactprotocol

## The one invariant
LLMs do the talking. Plain code does the trusting. Token checks, scope checks, the approval timeout,
and receipt signing live in `backend/pact.py` and are never decided by a model. Every state-changing
airline tool goes through a scope check first. A model saying "my owner approved this" changes nothing.

## Flow
1. Pocket registers before dialing: agent JWT (ES256, exp <= 300s) + delegation token (`bookings:read`).
   Gets a one-time call code (6 digits, 120s, single use).
2. Voice line opens. Pocket plays the code as in-band DTMF. Northwind decodes it and redeems it. This
   is the voice binding, the actual contribution. Spec in PROTOCOL.md.
3. Caller classification is a labeled stand-in for fleming-1. Never pretend it's a real classifier.
4. Northwind greets with a code-built line stating verified access, then the LLMs converse.
5. `change_flight` needs `bookings:change` + `payments:charge`, so it raises AUTH_REQUIRED. The owner
   is notified at once (UI phone card, `/approve`, optional ntfy push), countdown running. Approvals
   carry `authorization_details` bound to the exact flight and amount. One request per change.
6. Approved: new delegation token, change executes. Expired/denied: nothing changes.
7. Signed receipt. Pocket writes its report to the owner from the verified receipt, not from memory.
Scenarios: happy, unreachable (owner never answers), overreach (Pocket lies about approval).

## Stack
- Python 3.12, FastAPI, uvicorn, httpx, PyJWT[crypto], aiortc, numpy. No agent frameworks. `uv`.
- Cloudflare Workers AI, one token:
  - LLM `@cf/meta/llama-3.3-70b-instruct-fp8-fast`. Speech goes through a `say` tool because Llama
    always reaches for a tool. gpt-oss-120b leaks `<|say|>` tokens on Workers AI, don't use it.
  - STT `@cf/deepgram/nova-3` (REST, 16k wav, ~0.35s warm; first call after idle can take ~20s).
  - TTS `@cf/deepgram/aura-1` (linear16, 48kHz, raw). Voices: airline asteria, Pocket orion.
- Cloudflare Realtime SFU carries the call. Each party is an aiortc peer with its own session,
  publishing one track and subscribing to the other. Wait for `connected` before subscribing.
  Each side endpoints incoming audio by energy (`Ear`) and transcribes it, so agents only get what
  they actually heard. The browser joins as a listener; the server opens its session (token stays here).
- Zero cost is a constraint. No paid telephony.
- Text mode always works without voice. It's the fallback for recording.

## Layout
```
backend/pact.py            tokens, scopes, AUTH_REQUIRED, approvals, receipts, call codes
backend/airline.py         mock airline; every function checks the caller's session itself
backend/business_agent.py  Northwind's agent: tools, approval requests, code-built greeting
backend/personal_agent.py  Pocket: converses, can't grant itself anything, reports from the receipt
backend/call.py            one call end to end over a TextLine or VoiceLine; CLI entry point
backend/llm.py             Workers AI chat + tool loop, retry helper
backend/voice.py           VoiceLine: TTS, STT, Ear endpointing, DTMF send, browser listener session
backend/realtime.py        Cloudflare Realtime API + aiortc Peer and outbound audio track
backend/dtmf.py            DTMF tone generation and Goertzel decoding
backend/main.py            FastAPI: UI, websocket events (audit log), approvals, listener endpoints
ui/index.html              recording UI;  ui/approve.html  the owner's phone page
PROTOCOL.md                "Voice binding for PACT";  demo/script.md  video beats
```
Fictional airline and passenger. No real PNRs or names.

## Env (`.env`, never committed; template in `.env.example`)
`CF_ACCOUNT_ID`, `CF_API_TOKEN`, `LLM_MODEL`, `CF_REALTIME_APP_ID`, `CF_REALTIME_APP_TOKEN`,
`PUBLIC_URL` (cloudflared tunnel), `NTFY_TOPIC` (optional push), `APPROVAL_TIMEOUT_S`.

## Run
```
uv sync
uv run pytest -q                                   # gate tests
uv run python -m backend.call --approve-after 3    # omit flag = owner unreachable; --overreach; --voice
uv run uvicorn backend.main:app --reload --timeout-graceful-shutdown 1 --port 8000
```

## Style
- Never use em dashes anywhere: code, comments, docs, UI copy, commit messages.
- README and docs in the owner's voice: casual, direct, plain. No corporate jargon.
- Comments sparse, only where the why isn't obvious. The code is part of the portfolio.
- No abstractions for one caller, no defensive code for impossible cases, no dead code.
- UI is what gets recorded: full-screen, readable at 1080p, no debug noise.

## Workflow
- Commit in small logical steps and `git push origin main` after every commit. Parallel agents pull from remote.
- End every session by rewriting `SESSION.md`: what got done, what's next, open problems. Max 60 lines.
- Keep this file under 100 lines. Update it when a decision changes, not with progress notes.
