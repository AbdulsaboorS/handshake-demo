"""Mock Northwind Airways backend. Fictional airline, fictional booking.

Every function takes the caller's verified PACT session and checks scopes itself, so there is no path
to a booking change that skips the gate.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.pact import CallerSession

OWNER_ID = "nw-user-1042"
PNR = "K7XQ2M"
MIN_CONNECTION = timedelta(minutes=90)

TZ = {"SEA": ZoneInfo("America/Los_Angeles"), "DXB": ZoneInfo("Asia/Dubai"), "MED": ZoneInfo("Asia/Riyadh")}


class NotFound(Exception):
    pass


class InvalidChange(Exception):
    pass


@dataclass(frozen=True)
class Flight:
    id: str
    number: str
    origin: str
    destination: str
    departs: datetime
    arrives: datetime
    fare_cents: int


@dataclass
class Booking:
    pnr: str
    owner: str
    passenger: str
    segments: list[Flight]
    charges: list[dict]


def _flight(number: str, origin: str, destination: str, departs: str, arrives: str, fare: int) -> Flight:
    dep = datetime.fromisoformat(departs).replace(tzinfo=TZ[origin])
    arr = datetime.fromisoformat(arrives).replace(tzinfo=TZ[destination])
    return Flight(f"{number}-{dep:%Y%m%d}", number, origin, destination, dep, arr, fare * 100)


SCHEDULE: list[Flight] = []
BOOKINGS: dict[str, Booking] = {}


def reset() -> None:
    SCHEDULE[:] = [
        _flight("NW230", "SEA", "DXB", "2026-11-17 16:05", "2026-11-18 18:55", 1240),
        _flight("NW232", "SEA", "DXB", "2026-11-17 21:40", "2026-11-19 00:30", 1324),
        _flight("NW234", "SEA", "DXB", "2026-11-17 23:55", "2026-11-19 02:45", 1410),
        _flight("NW230", "SEA", "DXB", "2026-11-18 16:05", "2026-11-19 18:55", 1290),
        _flight("NW604", "DXB", "MED", "2026-11-19 08:50", "2026-11-19 09:50", 310),
    ]
    BOOKINGS.clear()
    BOOKINGS[PNR] = Booking(PNR, OWNER_ID, "Abdul S.", [SCHEDULE[0], SCHEDULE[4]], [])


reset()


def _fmt(dt: datetime) -> str:
    return f"{dt:%a %b} {dt.day}, {dt:%I:%M %p}".replace(" 0", " ")


def _flight_view(f: Flight) -> dict:
    return {
        "flight_id": f.id,
        "flight": f.number,
        "route": f"{f.origin} -> {f.destination}",
        "departs": _fmt(f.departs),
        "arrives": _fmt(f.arrives),
        "fare_usd": f.fare_cents / 100,
    }


def _owned_booking(session: CallerSession, pnr: str) -> Booking:
    booking = BOOKINGS.get(pnr.upper())
    # Same error whether it doesn't exist or isn't theirs, so a caller can't probe for PNRs.
    if booking is None or booking.owner != session.user:
        raise NotFound(f"no booking {pnr} for this customer")
    return booking


def _plan_change(booking: Booking, flight_id: str) -> tuple[int, Flight, int]:
    new = next((f for f in SCHEDULE if f.id == flight_id), None)
    if new is None:
        raise NotFound(f"no flight {flight_id}")
    i = next((i for i, s in enumerate(booking.segments) if (s.origin, s.destination) == (new.origin, new.destination)), None)
    if i is None:
        raise InvalidChange(f"{new.number} doesn't replace any segment on {booking.pnr}")
    old = booking.segments[i]
    if new.id == old.id:
        raise InvalidChange(f"already booked on {new.number}")
    if i + 1 < len(booking.segments) and booking.segments[i + 1].departs - new.arrives < MIN_CONNECTION:
        raise InvalidChange(f"{new.number} arrives too late to connect to {booking.segments[i + 1].number}")
    return i, new, max(0, new.fare_cents - old.fare_cents)


def get_booking(session: CallerSession, pnr: str) -> dict:
    session.require("bookings:read")
    b = _owned_booking(session, pnr)
    return {"pnr": b.pnr, "passenger": b.passenger, "segments": [_flight_view(s) for s in b.segments]}


def search_flights(session: CallerSession, origin: str, destination: str, date: str) -> list[dict]:
    session.require("bookings:read")
    day = datetime.fromisoformat(date).date()
    return [_flight_view(f) for f in SCHEDULE
            if (f.origin, f.destination) == (origin.upper(), destination.upper()) and f.departs.date() == day]


def quote_change(session: CallerSession, pnr: str, flight_id: str) -> dict:
    session.require("bookings:read")
    b = _owned_booking(session, pnr)
    i, new, diff = _plan_change(b, flight_id)
    return {"pnr": b.pnr, "from": _flight_view(b.segments[i]), "to": _flight_view(new), "fare_difference_usd": diff / 100}


def change_detail(pnr: str, flight_id: str, amount_cents: int) -> dict:
    return {"type": "flight_change", "pnr": pnr.upper(), "flight_id": flight_id, "amount_cents": amount_cents}


def change_flight(session: CallerSession, pnr: str, flight_id: str) -> dict:
    """Charge and rebook together, so there is never a charge without a change or the reverse."""
    b = _owned_booking(session, pnr)
    i, new, diff = _plan_change(b, flight_id)
    scopes = ("bookings:change", "payments:charge") if diff else ("bookings:change",)
    session.require(*scopes, detail=change_detail(b.pnr, new.id, diff))

    old = b.segments[i]
    b.segments[i] = new
    if diff:
        b.charges.append({"amount_cents": diff, "reason": f"fare difference {old.number} -> {new.number}"})
    session.record({"type": "flight_change", "pnr": b.pnr, "from": old.id, "to": new.id, "charged_cents": diff})
    return {"status": "changed", "pnr": b.pnr, "new_flight": _flight_view(new), "charged_usd": diff / 100}
