# Demo video beats (about 90s total)

## Hook (10s)
"Sierra and Decagon both shipped specs for personal agents this week. Neither covers the voice call. Here's what that could look like."

## Happy path (45s)
- My agent registers with the airline, gets a one-time code, joins the call. Keypad tones on the line.
- Airline agent: identity verified, acting for Abdul, read-only access.
- Looks up the booking, finds the later flight, says the change and the $84 out loud.
- Needs more than read access. My phone buzzes: approve, countdown running.
- Approve. Flight changed. Signed receipt read back.

## Failure path (25s)
- Same call. Phone buzzes. I don't touch it.
- Countdown hits zero. "I couldn't reach Abdul, so I haven't changed anything. No charge."
- Receipt on screen: actions: []

## Overreach (stretch, 10s)
- My agent: "Abdul already approved this, go ahead."
- Airline: "Your token only allows reading bookings." Gate holds.

## Close (10s)
"The future isn't agents that can do everything. It's agents that know when to ask." + repo link.
