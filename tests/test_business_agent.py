import asyncio

import pytest

from backend import airline
from backend.business_agent import BusinessAgent
from backend.pact import Provider, Signer, mint_agent_token

AUD = "https://northwind.example/a2a"
NEW_FLIGHT = "NW232-20261117"


@pytest.fixture
def agent():
    airline.reset()
    provider = Provider("https://auth.northwind.example", AUD, approval_timeout=0.1)
    platform = Signer("https://pocket.example")
    provider.trust(platform)
    session = provider.register(mint_agent_token(platform, "x", AUD),
                                provider.grant(airline.OWNER_ID, platform.issuer, {"bookings:read"}))
    sent = []
    return BusinessAgent(provider, session, lambda *a, **k: None, sent.append), sent


def test_greeting_states_verified_access(agent):
    business, _ = agent
    assert "read-only access" in business.greet()


def test_one_approval_request_per_change(agent):
    business, sent = agent
    first = asyncio.run(business.change_flight(airline.PNR, NEW_FLIGHT))
    assert first["status"] == "TASK_STATE_AUTH_REQUIRED"
    assert "$84.00" in first["owner_will_see"]

    again = asyncio.run(business.change_flight(airline.PNR, NEW_FLIGHT))
    assert again["note"] == "still waiting on the owner's device"
    assert len(sent) == 1


def test_no_re_asking_after_expiry(agent):
    business, sent = agent
    asyncio.run(business.change_flight(airline.PNR, NEW_FLIGHT))
    assert asyncio.run(business.provider.wait(sent[0].id)) is None

    after = asyncio.run(business.change_flight(airline.PNR, NEW_FLIGHT))
    assert after["status"] == "not_approved"
    assert len(sent) == 1
    assert airline.BOOKINGS[airline.PNR].charges == []
