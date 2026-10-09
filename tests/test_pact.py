import asyncio
import time

import pytest

from backend import airline
from backend.pact import AuthRequired, InvalidToken, Provider, Signer, mint_agent_token, verify_receipt

AUD = "https://northwind.example/a2a"
NEW_FLIGHT = "NW232-20261117"


@pytest.fixture
def world():
    airline.reset()
    provider = Provider("https://auth.northwind.example", AUD, approval_timeout=0.2)
    platform = Signer("https://pocket.example")
    provider.trust(platform)
    delegation = provider.grant(airline.OWNER_ID, platform.issuer, {"bookings:read"})
    session = provider.register(mint_agent_token(platform, "pkt-user-77", AUD), delegation)
    return provider, platform, session


def test_read_scope_allows_lookup(world):
    _, _, session = world
    booking = airline.get_booking(session, airline.PNR)
    assert booking["segments"][0]["departs"] == "Tue Nov 17, 4:05 PM"
    assert airline.quote_change(session, NEW_FLIGHT)["fare_difference_usd"] == 84


def test_untrusted_platform_rejected(world):
    provider, _, _ = world
    rogue = Signer("https://pocket.example")  # same name, different key
    with pytest.raises(InvalidToken):
        provider.verify_agent(mint_agent_token(rogue, "x", AUD))


def test_long_lived_agent_token_rejected(world):
    provider, platform, _ = world
    now = int(time.time())
    token = platform.sign({"iss": platform.issuer, "sub": "x", "aud": AUD, "iat": now, "exp": now + 3600})
    with pytest.raises(InvalidToken):
        provider.verify_agent(token)


def test_delegation_bound_to_its_agent_platform(world):
    provider, platform, _ = world
    other = Signer("https://other-agent.example")
    provider.trust(other)
    delegation = provider.grant(airline.OWNER_ID, platform.issuer, {"bookings:read"})
    with pytest.raises(InvalidToken):
        provider.register(mint_agent_token(other, "x", AUD), delegation)


def test_cannot_see_someone_elses_booking(world):
    provider, platform, _ = world
    stranger = provider.register(
        mint_agent_token(platform, "y", AUD), provider.grant("nw-user-9999", platform.issuer, {"bookings:read"})
    )
    with pytest.raises(airline.NotFound):
        airline.get_booking(stranger, airline.PNR)


def test_change_needs_step_up_then_succeeds(world):
    provider, _, session = world
    with pytest.raises(AuthRequired) as e:
        airline.change_flight(session, NEW_FLIGHT)
    assert e.value.missing_scopes == {"bookings:change", "payments:charge"}

    approval = provider.request_approval(session, e.value, "Move NW230 to NW232, $84")

    async def owner_approves():
        waiter = asyncio.create_task(provider.wait(approval.id))
        await asyncio.sleep(0)
        provider.resolve(approval.id, approved=True)
        return await waiter

    provider.upgrade(session, asyncio.run(owner_approves()))
    result = airline.change_flight(session, NEW_FLIGHT)
    assert result["charged_usd"] == 84

    receipt = verify_receipt(provider.receipt(session), provider.signer.public_key)
    assert receipt["actions"][0]["to"] == NEW_FLIGHT
    assert receipt["scopesUsed"] == ["bookings:change", "bookings:read", "payments:charge"]


def test_approval_is_bound_to_the_exact_change(world):
    provider, _, session = world
    with pytest.raises(AuthRequired) as e:
        airline.change_flight(session, NEW_FLIGHT)
    approval = provider.request_approval(session, e.value, "")

    async def run():
        waiter = asyncio.create_task(provider.wait(approval.id))
        await asyncio.sleep(0)
        provider.resolve(approval.id, approved=True)
        return await waiter

    provider.upgrade(session, asyncio.run(run()))
    # Scopes are granted now, but only for NW232. A pricier flight still needs the owner.
    with pytest.raises(AuthRequired):
        airline.change_flight(session, "NW234-20261117")


def test_owner_unreachable_changes_nothing(world):
    provider, _, session = world
    with pytest.raises(AuthRequired) as e:
        airline.change_flight(session, NEW_FLIGHT)
    approval = provider.request_approval(session, e.value, "")

    assert asyncio.run(provider.wait(approval.id)) is None
    assert approval.status == "expired"
    assert provider.resolve(approval.id, approved=True).status == "expired"  # too late
    with pytest.raises(AuthRequired):
        airline.change_flight(session, NEW_FLIGHT)
    assert airline.BOOKINGS[airline.PNR].charges == []

    receipt = verify_receipt(provider.receipt(session), provider.signer.public_key)
    assert receipt["actions"] == []


def test_bad_connection_rejected_before_asking_owner(world):
    _, _, session = world
    with pytest.raises(airline.InvalidChange):
        airline.quote_change(session, "NW230-20261118")


def test_booking_found_from_flight_not_from_model(world):
    _, _, session = world
    assert airline.booking_for_flight(session, NEW_FLIGHT) == airline.PNR


def test_call_code_is_single_use(world):
    provider, _, session = world
    code = provider.issue_call_code(session)
    assert provider.redeem_call_code(code) is session
    with pytest.raises(InvalidToken):
        provider.redeem_call_code(code)

