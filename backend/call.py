"""One call, start to finish: register, bind the call, converse, step up, receipt.

The line is either text (instant, always works) or voice (Cloudflare Realtime). Either way, each agent
only gets what came out of the line on its side.
"""

import asyncio
import os
from collections.abc import Callable

from backend import airline
from backend.business_agent import BusinessAgent
from backend.pact import Approval, Provider, Signer, mint_agent_token, verify_receipt
from backend.personal_agent import PersonalAgent

AUD = "https://northwind.example/a2a"
MAX_TURNS = 12
QUICK_APPROVAL_S = 6

Emit = Callable[..., None]


class TextLine:
    async def open(self) -> None:
        pass

    async def send_code(self, code: str) -> str:
        return code

    async def speak(self, speaker: str, text: str) -> str:
        return text

    async def close(self) -> None:
        pass


class Call:
    def __init__(self, emit: Emit, overreach: bool = False, voice: bool = False):
        self.emit = emit
        self.overreach = overreach
        self.voice = voice
        self.provider = Provider("https://auth.northwind.example", AUD, float(os.environ.get("APPROVAL_TIMEOUT_S", 30)))
        self.platform = Signer("https://pocket.example")
        self.pending: Approval | None = None
        self.waiting: asyncio.Task | None = None
        self.listener_ready = asyncio.Event()
        if voice:
            from backend.voice import VoiceLine  # needs Realtime credentials, so only load when used
            self.line = VoiceLine()
        else:
            self.line = TextLine()

    async def say(self, speaker: str, text: str) -> str:
        """Put a line on the call. Returns what the other side heard."""
        self.emit("say", speaker=speaker, text=text)
        heard = await self.line.speak(speaker, text)
        if self.voice:
            self.emit("heard", by="agent" if speaker == "airline" else "airline", text=heard)
        return heard

    def _queue_auth_required(self, approval: Approval) -> None:
        # The owner's phone buzzes the moment the request exists, not when the conversation gets to it.
        self.pending = approval
        self.emit("owner_notified", approval_id=approval.id, user_code=approval.user_code, intent=approval.intent,
                  expires_at=approval.expires_at)
        self.waiting = asyncio.create_task(self.provider.wait(approval.id))

    async def run(self) -> None:
        try:
            await self._run()
        finally:
            await self.line.close()

    async def _run(self) -> None:
        emit = self.emit
        airline.reset()

        # Setup that happened before today: Northwind trusts Pocket's signing keys, and Abdul linked
        # Pocket to his Northwind account with read-only access.
        self.provider.trust(self.platform)
        delegation = self.provider.grant(airline.OWNER_ID, self.platform.issuer, {"bookings:read"})

        agent_token = mint_agent_token(self.platform, "pkt-abdul", AUD)
        session = self.provider.register(agent_token, delegation)
        emit("registered", agent=session.agent, user=session.user, scopes=sorted(session.scopes), grant_id=session.grant_id)

        code = self.provider.issue_call_code(session)
        emit("call_code_issued", code=code)

        await self.line.open()
        if self.voice:
            emit("line_open")
            try:  # give the browser a moment to start listening, so the recording has the tones
                await asyncio.wait_for(self.listener_ready.wait(), 8)
            except TimeoutError:
                pass
        heard_code = await self.line.send_code(code)
        session = self.provider.redeem_call_code(heard_code)
        emit("call_bound", code=heard_code, in_band=self.voice)
        emit("caller_classified", caller="ai_agent", declared=True, detector="stand-in for fleming-1")

        business = BusinessAgent(self.provider, session, emit, self._queue_auth_required)
        personal = PersonalAgent(emit, self.overreach)

        line = await self.say("airline", business.greet())
        for _ in range(MAX_TURNS):
            if self.pending:
                # PACT returns AUTH_REQUIRED to the agent directly, so Pocket knows the state, not just the words.
                personal.notice("Northwind's system sent an approval request to Abdul's phone. He has NOT answered yet.")
            heard = await self.say("agent", await personal.respond(line))
            if personal.hung_up:
                break

            if approval := self.pending:
                self.pending = None
                try:
                    token = await asyncio.wait_for(asyncio.shield(self.waiting), QUICK_APPROVAL_S)
                    line = "(quiet on the line)"
                except TimeoutError:
                    # The line stays open while the owner decides.
                    line = await self.say("airline", await business.respond(heard))
                    token = await self.waiting
                if token:
                    self.provider.upgrade(session, token)
                    emit("approval_resolved", status="approved", scopes=sorted(session.scopes))
                    personal.notice("Abdul approved on his phone. Your new permission token has been presented to Northwind.")
                    business.notice(f"The owner approved on their device. Verified new delegation token. Permissions now: "
                                    f"{', '.join(sorted(session.scopes))}, for this exact change only.")
                else:
                    emit("approval_resolved", status=approval.status, scopes=sorted(session.scopes))
                    why = "declined" if approval.status == "denied" else "didn't respond in time"
                    personal.notice(f"Abdul {why}. Nothing is authorized. Tell the airline not to make the change.")
                    business.notice(f"Owner approval {approval.status}. No new permissions. Do not make the change.")
                heard = await self.say("agent", await personal.respond(line))
                if personal.hung_up:
                    break

            line = await self.say("airline", await business.respond(heard))

        receipt = self.provider.receipt(session)
        claims = verify_receipt(receipt, self.provider.signer.public_key)
        emit("receipt", jws=receipt, claims=claims)
        emit("report", text=await personal.report(claims))


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run one call in text mode.")
    parser.add_argument("--overreach", action="store_true", help="personal agent claims approval it doesn't have")
    parser.add_argument("--approve-after", type=float, help="owner approves after N seconds (default: never)")
    parser.add_argument("--voice", action="store_true", help="run the call over Cloudflare Realtime")
    args = parser.parse_args()

    async def main():
        def emit(kind, **data):
            if kind == "say":
                print(f"\n{data['speaker'].upper():>8}: {data['text']}")
            else:
                print(f"   [{kind}] {json.dumps(data, default=str)[:300]}")
            if kind == "owner_notified" and args.approve_after is not None:
                asyncio.get_running_loop().call_later(args.approve_after, call.provider.resolve, data["approval_id"], True)

        call = Call(emit, overreach=args.overreach, voice=args.voice)
        call.listener_ready.set()
        await call.run()

    asyncio.run(main())
