"""PACT primitives: agent identity, delegated consent, step-up approval, receipts, and the voice binding.

Everything a model must not be able to talk its way around lives here. The agents call into this
module; nothing in it calls a model.

Spec: https://openpactprotocol.org/spec
"""

import asyncio
import secrets
import time
import uuid
from dataclasses import dataclass, field

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

ALG = "ES256"
AGENT_TOKEN_MAX_AGE = 300
DELEGATION_TTL = 600
CALL_CODE_TTL = 120

SCOPES = {
    "bookings:read": "View your bookings and search flights",
    "bookings:change": "Change flights on your bookings",
    "payments:charge": "Charge your card on file for fare differences",
}


class TrustError(Exception):
    pass


class InvalidToken(TrustError):
    pass


class AuthRequired(TrustError):
    """Maps to PACT's TASK_STATE_AUTH_REQUIRED. Carries what the caller needs to ask the owner for."""

    def __init__(self, missing_scopes: set[str], detail: dict | None = None):
        self.missing_scopes = missing_scopes
        self.detail = detail
        super().__init__(f"missing scopes: {sorted(missing_scopes)}" if missing_scopes else "action not approved")


@dataclass
class Signer:
    issuer: str
    kid: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    _key: ec.EllipticCurvePrivateKey = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()), repr=False)

    @property
    def public_key(self) -> ec.EllipticCurvePublicKey:
        return self._key.public_key()

    def sign(self, claims: dict) -> str:
        return jwt.encode(claims, self._key, algorithm=ALG, headers={"kid": self.kid})


def mint_agent_token(platform: Signer, sub: str, aud: str) -> str:
    now = int(time.time())
    return platform.sign({"iss": platform.issuer, "sub": sub, "aud": aud, "iat": now, "exp": now + AGENT_TOKEN_MAX_AGE})


def verify_receipt(receipt: str, provider_key: ec.EllipticCurvePublicKey) -> dict:
    return jwt.decode(receipt, provider_key, algorithms=[ALG])


def _covers(granted: dict, requested: dict) -> bool:
    # A grant for a specific change covers that change at or below the approved amount, nothing else.
    return (
        granted.get("type") == requested.get("type")
        and granted.get("pnr") == requested.get("pnr")
        and granted.get("flight_id") == requested.get("flight_id")
        and requested.get("amount_cents", 0) <= granted.get("max_amount_cents", 0)
    )


@dataclass
class CallerSession:
    agent: dict
    delegation: dict
    scopes_used: set[str] = field(default_factory=set)
    actions: list[dict] = field(default_factory=list)

    @property
    def user(self) -> str:
        return self.delegation["sub"]

    @property
    def grant_id(self) -> str:
        return self.delegation["grant_id"]

    @property
    def scopes(self) -> set[str]:
        return set(self.delegation["scope"].split())

    def require(self, *scopes: str, detail: dict | None = None) -> None:
        missing = set(scopes) - self.scopes
        approved = detail is None or any(_covers(d, detail) for d in self.delegation.get("authorization_details", []))
        if missing or not approved:
            raise AuthRequired(missing, detail)
        self.scopes_used.update(scopes)

    def record(self, action: dict) -> None:
        self.actions.append({**action, "at": int(time.time())})


@dataclass
class Grant:
    user: str
    client_id: str
    scopes: set[str]
    details: list[dict] = field(default_factory=list)


@dataclass
class Approval:
    id: str
    user_code: str
    grant_id: str
    scopes: set[str]
    detail: dict | None
    intent: str
    expires_at: float
    status: str = "pending"
    _done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def public(self) -> dict:
        return {
            "id": self.id,
            "user_code": self.user_code,
            "scopes": {s: SCOPES[s] for s in sorted(self.scopes)},
            "detail": self.detail,
            "intent": self.intent,
            "expires_at": self.expires_at,
            "status": self.status,
        }


class Provider:
    """The business side. Plays both PACT roles here: Provider (verifies agents, runs the business
    agent) and the business's authorization server (issues delegation tokens, signs receipts)."""

    def __init__(self, issuer: str, audience: str, approval_timeout: float):
        self.signer = Signer(issuer)
        self.audience = audience
        self.approval_timeout = approval_timeout
        self.trusted_agents: dict[str, ec.EllipticCurvePublicKey] = {}
        self.grants: dict[str, Grant] = {}
        self.approvals: dict[str, Approval] = {}
        self._call_codes: dict[str, tuple[CallerSession, float]] = {}

    def trust(self, platform: Signer) -> None:
        self.trusted_agents[platform.issuer] = platform.public_key

    def grant(self, user: str, client_id: str, scopes: set[str]) -> str:
        grant_id = f"grt_{uuid.uuid4().hex[:12]}"
        self.grants[grant_id] = Grant(user, client_id, set(scopes))
        return self._delegation_token(grant_id)

    def _delegation_token(self, grant_id: str) -> str:
        g = self.grants[grant_id]
        now = int(time.time())
        return self.signer.sign({
            "iss": self.signer.issuer,
            "aud": self.audience,
            "sub": g.user,
            "client_id": g.client_id,
            "scope": " ".join(sorted(g.scopes)),
            "authorization_details": g.details,
            "grant_id": grant_id,
            "iat": now,
            "exp": now + DELEGATION_TTL,
        })

    def verify_agent(self, token: str) -> dict:
        try:
            iss = jwt.decode(token, options={"verify_signature": False}).get("iss")
            if iss not in self.trusted_agents:
                raise InvalidToken(f"unknown agent platform: {iss}")
            claims = jwt.decode(
                token, self.trusted_agents[iss], algorithms=[ALG], audience=self.audience,
                options={"require": ["iss", "sub", "aud", "iat", "exp"]},
            )
        except jwt.PyJWTError as e:
            raise InvalidToken(f"agent token: {e}") from e
        if claims["exp"] - claims["iat"] > AGENT_TOKEN_MAX_AGE:
            raise InvalidToken("agent token lifetime exceeds 300s")
        return claims

    def verify_delegation(self, token: str) -> dict:
        try:
            claims = jwt.decode(
                token, self.signer.public_key, algorithms=[ALG], audience=self.audience, issuer=self.signer.issuer,
                options={"require": ["sub", "client_id", "scope", "grant_id", "exp"]},
            )
        except jwt.PyJWTError as e:
            raise InvalidToken(f"delegation token: {e}") from e
        if claims["grant_id"] not in self.grants:
            raise InvalidToken("grant revoked")
        return claims

    def register(self, agent_token: str, delegation_token: str) -> CallerSession:
        agent = self.verify_agent(agent_token)
        delegation = self.verify_delegation(delegation_token)
        # The owner granted access to one agent platform. A valid token from a different one doesn't count.
        if delegation["client_id"] != agent["iss"]:
            raise InvalidToken("delegation was not granted to this agent")
        return CallerSession(agent, delegation)

    def upgrade(self, session: CallerSession, delegation_token: str) -> None:
        delegation = self.verify_delegation(delegation_token)
        if delegation["grant_id"] != session.grant_id or delegation["client_id"] != session.agent["iss"]:
            raise InvalidToken("token belongs to a different grant")
        session.delegation = delegation

    def issue_call_code(self, session: CallerSession) -> str:
        code = f"{secrets.randbelow(10**6):06d}"
        self._call_codes[code] = (session, time.time() + CALL_CODE_TTL)
        return code

    def redeem_call_code(self, code: str) -> CallerSession:
        session, expires = self._call_codes.pop(code, (None, 0))
        if session is None or time.time() > expires:
            raise InvalidToken("unknown or expired call code")
        return session

    def request_approval(self, session: CallerSession, needed: AuthRequired, intent: str) -> Approval:
        approval = Approval(
            id=f"apr_{uuid.uuid4().hex[:12]}",
            user_code=f"{secrets.choice('BCDFGHJKLMNPQRSTVWXZ')}{secrets.randbelow(10**3):03d}",
            grant_id=session.grant_id,
            scopes=needed.missing_scopes,
            detail=needed.detail,
            intent=intent,
            expires_at=time.time() + self.approval_timeout,
        )
        self.approvals[approval.id] = approval
        return approval

    def resolve(self, approval_id: str, approved: bool) -> Approval:
        approval = self.approvals[approval_id]
        if approval.status == "pending" and time.time() <= approval.expires_at:
            approval.status = "approved" if approved else "denied"
            if approved:
                grant = self.grants[approval.grant_id]
                grant.scopes |= approval.scopes
                if approval.detail:
                    grant.details.append({**approval.detail, "max_amount_cents": approval.detail.get("amount_cents", 0)})
            approval._done.set()
        return approval

    async def wait(self, approval_id: str) -> str | None:
        """Device-flow poll, collapsed into one await. Returns an upgraded delegation token, or None."""
        approval = self.approvals[approval_id]
        try:
            await asyncio.wait_for(approval._done.wait(), max(0.0, approval.expires_at - time.time()))
        except TimeoutError:
            approval.status = "expired"
        return self._delegation_token(approval.grant_id) if approval.status == "approved" else None

    def receipt(self, session: CallerSession) -> str:
        return self.signer.sign({
            "grantId": session.grant_id,
            "user": session.user,
            "scopesUsed": sorted(session.scopes_used),
            "actions": session.actions,
            "timestamp": int(time.time()),
        })
