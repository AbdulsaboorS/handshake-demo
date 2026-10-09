# AGENTS.md

Read this, then `SESSION.md` (local only, gitignored), then `git log --oneline -10` before touching anything.

## What this is
A demo of PACT over a phone call. A personal AI agent calls an airline's support agent to change a flight.
The airline verifies who the agent is and whose behalf it acts on, lets it read the booking, and
requires the human owner to approve on their phone before any change or charge. If the owner
doesn't answer in time, nothing happens and a signed receipt proves it.

PACT (Decagon + Instinct, open source) covers agent identity and delegated consent over HTTP. It says
nothing about voice. Sierra's fleming-1 only detects synthetic voices and says "the best case is an
agent that says who it is." This repo shows how that works on a real call. Build on PACT's real
names and semantics, never invent a parallel protocol.
- Spec: https://openpactprotocol.org/spec
- Reference repo: https://github.com/openpactprotocol/openpactprotocol

## The one invariant
LLMs do the talking. Plain code does the trusting. Token checks, scope checks, the approval timeout,
and receipt signing live in `backend/pact.py` and are never decided by a model. Every state-changing
airline tool goes through a scope check first. A model saying "my owner approved this" changes nothing.

## Flow
1. Personal agent pre-registers over HTTPS: sends its agent JWT (ES256, `iss`/`sub`/`aud`, exp <= 300s)
   and delegation token (`scope: bookings:read`, `grant_id`). Gets back a one-time call code.
2. Personal agent places the call and sends the code as DTMF. Business matches caller ID + code to the
   verified session. This is the voice binding, our actual contribution (write it up in PROTOCOL.md).
3. Detection step runs and is labeled as a stand-in for fleming-1. Do not pretend it is a real classifier.
4. Agent reads booking and searches alternatives under `bookings:read`.
5. Business agent states intent out loud: flight, date, old time, new time, fare difference.
6. Change needs `bookings:change` + `payments:charge`, so it returns `TASK_STATE_AUTH_REQUIRED` with
   `pact.missingScopes`. Owner gets a link + code on their phone (`ui/approve.html`), countdown running.
7. Approve: new delegation token, change executes, `pact.receipt` (JWS: grantId, user, scopesUsed,
   actions, timestamp) read back. Timeout: decline, no charge, receipt with `actions: []`.
8. Stretch: overreach beat. Personal agent claims approval it doesn't have, the gate refuses.

## Stack
- Python 3.12, FastAPI, uvicorn, httpx, PyJWT[crypto]. No agent frameworks. Managed with `uv`.
- Cloudflare Workers AI for everything model-shaped, one token:
  - LLM: `@cf/meta/llama-3.3-70b-instruct-fp8-fast` (function calling). Swap via `LLM_MODEL`.
  - STT: `@cf/deepgram/flux` (built-in turn detection) or `@cf/deepgram/nova-3`.
  - TTS: `@cf/deepgram/aura-1`.
- Twilio Media Streams for the real call (audio is 8kHz mu-law, transcode at the edge of `voice.py`).
- ngrok to expose localhost to Twilio.
- Text mode always works without Twilio. Keep it that way, it is the fallback for recording.

## Layout
```
backend/main.py            FastAPI app, websocket events to UI, approval endpoints, Twilio webhooks
backend/pact.py            keys, agent JWT, delegation tokens, scopes, AUTH_REQUIRED, receipts, call codes
backend/llm.py             Workers AI client (chat + tools), the only place that talks to models
backend/personal_agent.py  caller loop: goal -> register -> call -> converse -> report back to owner
backend/business_agent.py  receiver loop: bind call -> detect -> read -> intent -> step-up -> execute
backend/airline.py         mock airline: get_booking, search_flights, change_flight, charge_fare_difference
backend/voice.py           Twilio stream bridge: mu-law <-> STT/TTS, turn-taking
ui/index.html              split view: live transcript, protocol events, mode toggle (happy / unreachable)
ui/approve.html            phone approval page with countdown
demo/script.md             video beats, about 90 seconds total
PROTOCOL.md                "Voice binding for PACT": RFC-lite proposal, maps to PAP concepts too
```
Seed data lives inline in `airline.py`. Fictional airline and passenger. No real PNRs or names.

## Env (`.env`, never committed; template in `.env.example`)
`CF_ACCOUNT_ID`, `CF_API_TOKEN`, `LLM_MODEL`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_CALLER_NUMBER`, `TWILIO_AIRLINE_NUMBER`, `PUBLIC_URL` (ngrok), `APPROVAL_TIMEOUT_S`.

## Run
```
uv sync
uv run uvicorn backend.main:app --reload --port 8000
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
