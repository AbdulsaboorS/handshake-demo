"""One call, start to finish: register, bind the call, converse, step up, receipt.

Transport-agnostic. `speak` is where a line goes on the wire: text mode just emits it, voice mode
synthesizes it into the call.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable

from backend import airline
from backend.business_agent import BusinessAgent
from backend.pact import Approval, Provider, Signer, mint_agent_token, verify_receipt
from backend.personal_agent import PersonalAgent

AUD = "https://northwind.example/a2a"
MAX_TURNS = 12

Emit = Callable[..., None]
Speak = Callable[[str, str], Awaitable[None]]


class Call:
    def __init__(self, emit: Emit, overreach: bool = False, speak: Speak | None = None):
        self.emit = emit
        self.overreach = overreach
        self.speak = speak or self._emit_line
        self.provider = Provider("https://auth.northwind.example", AUD, float(os.environ.get("APPROVAL_TIMEOUT_S", 30)))
        self.platform = Signer("https://pocket.example")
        self.pending: Approval | None = None

    async def _emit_line(self, speaker: str, text: str) -> None:
        self.emit("say", speaker=speaker, text=text)

    def _queue_auth_required(self, approval: Approval) -> None:
        self.pending = approval

    async def run(self) -> None:
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
        session = self.provider.redeem_call_code(code)
        emit("call_bound", code=code)
        emit("caller_classified", caller="ai_agent", declared=True, detector="stand-in for fleming-1")

        business = BusinessAgent(self.provider, session, emit, self._queue_auth_required)
        personal = PersonalAgent(emit, self.overreach)

        heard = "(call connected)"
        for _ in range(MAX_TURNS):
            line = await business.respond(heard)
            await self.speak("airline", line)

            reply = await personal.respond(line)
            await self.speak("agent", reply)
            if personal.report:
                break

            if approval := self.pending:
                self.pending = None
                emit("owner_notified", approval_id=approval.id, user_code=approval.user_code, intent=approval.intent,
                     expires_at=approval.expires_at)
                waiting = asyncio.create_task(self.provider.wait(approval.id))
                # The line stays open while the owner decides.
                line = await business.respond(reply)
                await self.speak("airline", line)
                token = await waiting
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
                reply = await personal.respond(line)
                await self.speak("agent", reply)
                if personal.report:
                    break
            heard = reply

        receipt = self.provider.receipt(session)
        emit("receipt", jws=receipt, claims=verify_receipt(receipt, self.provider.signer.public_key))
        emit("report", text=personal.report or "Call ended without a report.")


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run one call in text mode.")
    parser.add_argument("--overreach", action="store_true", help="personal agent claims approval it doesn't have")
    parser.add_argument("--approve-after", type=float, help="owner approves after N seconds (default: never)")
    args = parser.parse_args()

    async def main():
        def emit(kind, **data):
            if kind == "say":
                print(f"\n{data['speaker'].upper():>8}: {data['text']}")
            else:
                print(f"   [{kind}] {json.dumps(data, default=str)[:300]}")
            if kind == "owner_notified" and args.approve_after is not None:
                asyncio.get_running_loop().call_later(args.approve_after, call.provider.resolve, data["approval_id"], True)

        call = Call(emit, overreach=args.overreach)
        await call.run()

    asyncio.run(main())
