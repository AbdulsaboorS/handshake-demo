import asyncio

import pytest

from backend.call import Call, TextLine
from backend.pact import InvalidToken


class Connected(Exception):
    pass


class LossyLine(TextLine):
    """Garbles the call code a set number of times, then stops the call at the first spoken line."""

    def __init__(self, garbled: int):
        self.garbled = garbled

    async def send_code(self, code: str) -> str:
        if self.garbled:
            self.garbled -= 1
            return code[1:]
        return code

    async def speak(self, speaker: str, text: str) -> tuple[str, dict]:
        raise Connected


def run(garbled: int) -> list[str]:
    events = []
    call = Call(lambda kind, **data: events.append(kind))
    call.line = LossyLine(garbled)
    asyncio.run(call.run())
    return events


def test_one_garbled_code_is_resent():
    with pytest.raises(Connected):
        run(garbled=1)


def test_two_garbled_codes_leave_the_call_unbound():
    with pytest.raises(InvalidToken):
        run(garbled=2)
