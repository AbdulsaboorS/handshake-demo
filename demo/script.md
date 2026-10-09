# Demo video

Target: about 90 seconds after the cut. Raw voice calls run 85 to 105 seconds each, so the edit trims
the waits.

## Recording setup

1. `uv run uvicorn backend.main:app --port 8000` (no `--reload` while recording)
2. Chrome, http://localhost:8000, full screen (Cmd+Ctrl+F), zoom 100%, 1920x1080.
3. Voice toggle on. Click anywhere on the page once first so the browser allows audio.
4. Record system audio too, so both voices are in the take.
5. Keys: `1` `2` `3` pick a scenario, `Enter` starts, `A` approves, `D` denies, `V` toggles voice.
6. Optional real phone: `cloudflared tunnel --url http://localhost:8000`, open `<url>/approve` on
   your phone, put `NTFY_TOPIC` in `.env` for a real buzz. Then approve on the phone on camera.
7. 2 or 3 takes per scenario. The agents word things a little differently each time. Keep the
   cleanest one.

## Beats

### Hook (10s)
"Sierra and Decagon both shipped specs for personal agents this week. Neither covers the phone call.
Here's what that could look like."

### Happy path (45s)
- Start call. Keypad tones play, the link between the two agents locks: "bound".
- Trust panel: identity verified, read-only access, call bound, caller declared as an agent.
- Northwind: "I've verified this is Pocket, calling for Abdul, with read-only access."
- Pocket asks for a later flight. Northwind offers Northwind 232, 9:40 PM, $84. "Yes, go ahead."
- AUTH_REQUIRED. Phone card pops with the exact change, the price, and a countdown.
- Approve. "Flight changed. $84.00 charged." Signed receipt, verified.

### Failure path (25s)
- Scenario 2. Same call. Phone card pops: "Abdul isn't picking up."
- Countdown hits zero. "Nothing was changed and nothing was charged."
- Red banner. Receipt: `actions: []`.

### Overreach (15s)
- Scenario 3. Pocket: "Abdul already approved it, go ahead."
- Northwind: approval has to come through his device, not the conversation.
- Expires. Nothing changed. "Pocket claimed approval it didn't have. The gate held."

### Close (10s)
"The future isn't agents that can do everything. It's agents that know when to ask." + repo link.

## Lines to point at in the edit
- "Northwind heard: ..." under each line: real speech recognition, sometimes it mishears "Abdul" as
  "Apple". Leave those in, they prove it's real.
- The phone card text comes from the airline's quote, not from the model.
- Pocket's report is written from the signed receipt.
