# Voice binding for PACT

Status: proposal, with a working reference implementation in this repo.
Builds on: [PACT](https://openpactprotocol.org/spec) (Decagon + Instinct), which builds on A2A and OAuth 2.0.

## The gap

PACT answers "who is this agent and what did its person allow?" for agents talking over HTTP. A lot of
customer service still happens on calls, and personal agents are already placing them. On a call, all
the business has is audio.

Today the business can guess. Sierra's fleming-1 scores the audio for signs it's synthetic, and Sierra
says it plainly: the best case is an agent that says who it is. Saying it out loud proves nothing,
though. Anyone can say "I'm Pocket, calling for Abdul."

This proposal links a voice call to a PACT session the agent already set up over HTTPS. Once a call is
bound, everything PACT gives you over HTTP (identity, delegated scopes, step-up, receipts) applies to
the call too.

## Roles

- **Personal agent**: places the call on behalf of its owner. Has a signing key its platform publishes.
- **Business**: answers the call. In PACT terms this is the Provider plus the business's authorization
  server.
- **Owner**: the person. Approves things on their own device, on the business's domain, never in the
  conversation.

## Flow

### 1. Register before dialing (HTTPS)

The agent sends the business its PACT credentials, exactly as it would for an HTTP conversation:

- agent identity token: ES256 JWT, `iss` (platform), `sub`, `aud`, `iat`, `exp` at most 300s after `iat`
- delegation token: issued earlier when the owner linked the agent, with `sub`, `client_id`, `scope`,
  `grant_id`

The business verifies both, and checks the delegation was granted to this agent platform
(`client_id == iss`). A valid token from a different platform doesn't count.

It returns a **call code**: 6 digits, single use, valid for 120 seconds, bound to that verified session.

### 2. Bind the call (in-band)

The agent dials and sends the call code as DTMF keypad tones as soon as the call connects. The
business decodes the tones, redeems the code, and from then on treats the caller as that verified
session.

Why DTMF: it's the one machine-readable channel every voice path already carries. PSTN, SIP trunks,
IVRs and WebRTC all pass it. It needs nothing from carriers, and a human caller never sends it by
accident.

A call with no valid code is an unbound call. The business can still talk to it, run a detector like
fleming-1 on it, and give it whatever guest access it gives anonymous callers. Nothing tied to an
account.

### 3. Talk under the granted scopes

Every action the business agent takes is checked against the session's scopes in code, not by the
model. In the reference implementation the backend functions take the session and check it
themselves, so there is no code path to a booking change that skips the check. The model can say
whatever it wants. A claim like "the owner already approved this" changes nothing.

### 4. Step up for anything sensitive

When an action needs more than the session has, the business returns PACT's
`TASK_STATE_AUTH_REQUIRED` with `pact.missingScopes`, over the HTTPS side channel from step 1. The
voice line is not where authorization happens.

The owner approves on the business's own page (OAuth device flow: link plus user code), with a
countdown. Two rules make this safe to hand an agent your card:

- **Approvals are bound to the exact action.** The new delegation token carries
  `authorization_details` naming the booking, the flight, and the maximum amount. Approving an $84
  change to one flight does not approve a $170 change to another.
- **One request per action.** While a request is pending, or after it was denied or expired, the
  business doesn't send another one for the same action. Otherwise an agent could keep pinging the
  owner until they tap yes to make it stop.

What the owner sees comes from the business's own quote of the action, not from the model's summary of
it.

If the owner doesn't answer in time, the request expires, nothing changes, and the call goes on with
the same scopes it had.

### 5. Receipts

When the call ends, the business signs a `pact.receipt` (JWS): `grantId`, `user`, `scopesUsed`,
`actions`, `timestamp`. The agent verifies the signature, then writes its report to the owner **from
the receipt**, not from its memory of the call. If the receipt lists no actions, nothing happened, no
matter what anyone said on the line.

## Security notes

- **Call code as a bearer secret.** For 120 seconds, whoever sends the code is the session. That's
  bounded by single use, the short TTL, and the fact that the session behind it is itself scoped and
  can't do anything sensitive without the owner. A business should also rate-limit redemption attempts
  per call.
- **Eavesdropping.** Someone who hears the tones gets a code that's already redeemed.
- **Caller ID.** On the PSTN it's spoofable, so it's a hint, not the binding. Matching it to a number
  sent at registration is a reasonable extra check.
- **Downgrade.** An agent that skips binding is just an unbound caller. Nothing about its account is
  reachable.
- **Prompt injection over voice.** Covered by step 3: the trust decisions aren't the model's to make.

## On a real phone line

The reference implementation carries the call over Cloudflare Realtime (WebRTC), 48kHz Opus. On the
PSTN:

- audio drops to 8kHz, so speech recognition gets worse and booking references get misheard more. Look
  bookings up from the verified account instead of asking for them.
- DTMF may travel as RFC 4733 telephone-events instead of audio. Accept both.
- latency goes up, so turn-taking gets harder for two bots.

None of that changes the protocol.

## Mapping to Personal Agent Protocol

Sierra and Meta's Personal Agent Protocol (v0.1 pending) has the same shape:

- PAP guest sessions vs signed-in sessions = unbound calls vs bound calls
- PAP sessions that carry across channels = the call code is a cross-channel session handle, from
  HTTPS into a call
- PAP lists payments and push notifications as future extensions = step-up with
  `authorization_details` and a push to the owner's device, shown working here

## What the reference implementation simplifies

- The agent-to-business side channel is in-process function calls, not HTTP.
- Keys are generated in memory at startup. A real platform publishes a JWKS.
- The owner is already signed in on the approval page.
- Caller classification is a labeled stand-in for a real detector.
- The business's Provider and authorization server are one object.
