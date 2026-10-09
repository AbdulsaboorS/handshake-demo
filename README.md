# handshake-demo

My AI agent calls an airline's AI agent to move my flight. The airline checks who's calling and what
I let it do, lets it look but not touch, and pings my phone before it spends a dollar. If I don't
answer, nothing happens, and there's a signed receipt that proves it.

It's a real voice call. Two agents talking over Cloudflare Realtime, each one only knowing what it
actually heard.

> Demo video: coming soon

## Why

This month Sierra and Decagon both shipped specs for personal agents dealing with businesses. Sierra
and Meta announced the Personal Agent Protocol. Decagon and Instinct open-sourced
[PACT](https://openpactprotocol.org/spec). Sierra also shipped fleming-1, a model that guesses whether
the caller on a phone line is an AI.

PACT handles agent identity and consent over HTTP. Nobody has said how any of it works on a call,
where all you have is audio. So I built that part on top of PACT. The write-up is in
[PROTOCOL.md](PROTOCOL.md).

## What happens on the call

1. Before dialing, my agent (Pocket) sends Northwind its signed identity token and the read-only
   permission I gave it earlier. Northwind hands back a one-time 6-digit code.
2. Pocket dials and plays the code as keypad tones. Northwind decodes them and now knows exactly who's
   on the line.
3. Pocket asks to move my flight. Northwind finds a later one, says the price out loud, Pocket says go.
4. Changing a flight and charging my card needs more than read-only. Northwind's system asks me, on my
   phone, for this exact change at this exact price, with a countdown.
5. Three ways it ends:
   - **I approve.** Flight changed, $84 charged, signed receipt.
   - **I don't answer.** Request expires. Nothing changed, nothing charged, and the receipt says so.
   - **Pocket lies.** It tells Northwind "he already approved it." Doesn't matter. Permission only
     comes from my phone.
6. Pocket writes its report to me from the signed receipt, not from its memory of the call.

## The one rule

The models do the talking. Plain code does the trusting.

Token checks, scope checks, the approval timeout, and receipt signing are all regular code
([`backend/pact.py`](backend/pact.py)). The airline's backend checks the caller's permissions itself
([`backend/airline.py`](backend/airline.py)), so there's no way for a model to talk its way into a
change. The tests in [`tests/`](tests/) are mostly about that.

## Stack

- Python, FastAPI, no agent framework
- Cloudflare Workers AI for everything model-shaped: Llama 3.3 70B for both agents, Deepgram nova-3
  for speech-to-text, Deepgram aura-1 for text-to-speech
- Cloudflare Realtime (WebRTC SFU) for the call, with `aiortc` on the Python side
- ES256 JWTs via PyJWT
- One HTML file for the UI, one for the phone

The whole thing runs for about $0.

## Run it

You need a Cloudflare account with:
- a Workers AI API token (dashboard: My Profile, API Tokens, Create Token, "Workers AI" template)
- a Realtime SFU app (dashboard: Realtime, SFU, Create application)

```
cp .env.example .env    # fill in the Cloudflare values
uv sync
uv run uvicorn backend.main:app --port 8000
```

Open http://localhost:8000, pick a scenario, hit Start call. Keyboard: `1` `2` `3` pick a scenario,
`Enter` starts, `A` approves, `D` denies.

To approve from your actual phone, run `cloudflared tunnel --url http://localhost:8000` and open the
printed URL with `/approve` on the end. For a real push notification, install the free ntfy app,
subscribe to a topic, and put it in `NTFY_TOPIC`.

Without the browser:

```
uv run python -m backend.call --approve-after 3          # text mode, I approve after 3s
uv run python -m backend.call                            # I never answer
uv run python -m backend.call --overreach                # Pocket lies about approval
uv run python -m backend.call --voice --approve-after 3  # over Cloudflare Realtime
uv run pytest -q
```

## What's real and what's not

Real: the tokens and signatures, the scope checks, the approval flow and timeout, the receipts, the
keypad tones, the audio over Cloudflare, the speech recognition on both sides.

Simplified: the airline and its data are fake. The agent-to-airline side channel is a function call,
not HTTP. Keys live in memory. The "is this caller an AI" detector is a labeled stand-in for something
like fleming-1. More in [PROTOCOL.md](PROTOCOL.md#what-the-reference-implementation-simplifies).

---

The future isn't agents that can do everything. It's agents that know when to ask.
