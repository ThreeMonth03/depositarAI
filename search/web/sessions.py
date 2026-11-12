"""Bounded RAM-only state, with monotonic
absolute expiry and per-turn fencing.
"""

from dataclasses import dataclass, field
import secrets
import time

from search.api.conversation.contract import SearchState


class SessionError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


@dataclass
class Session:
    expires: float
    # Only the private operator mode uses None. Public owners include kind.
    owner: str | None = None
    state: SearchState = field(default_factory=SearchState)
    pending: str | None = None
    version: int = 0
    busy: bool = False
    capture_enabled: bool = False
    consented_at: str | None = None
    test_session_id: str | None = None
    trace_epoch: int = 0
    trace_attempt: int = 0
    capture_status: str = "disabled"
    deletion_status: str = "none"


class Sessions:
    def __init__(
        self,
        *,
        capacity=100,
        owner_capacity=2,
        ttl=1800,
        max_turns=20,
        clock=time.monotonic,
    ):
        self.capacity, self.ttl, self.max_turns, self.clock = (
            capacity,
            ttl,
            max_turns,
            clock,
        )
        self.owner_capacity = owner_capacity
        self.items = {}

    def reap(self):
        now = self.clock()
        for token in list(self.items):
            session = self.items[token]
            if session.expires <= now and not (
                session.owner is not None and session.busy
            ):
                del self.items[token]

    def create(self, owner=None, *, capture=False, consented_at=None):
        self.reap()
        if len(self.items) >= self.capacity:
            raise SessionError(429, "session_capacity")
        if (
            owner is not None
            and sum(session.owner == owner for session in self.items.values())
            >= self.owner_capacity
        ):
            raise SessionError(429, "session_capacity")
        token = secrets.token_urlsafe(32)
        self.items[token] = Session(
            self.clock() + self.ttl,
            owner=owner,
            capture_enabled=capture,
            consented_at=consented_at if capture else None,
            test_session_id=secrets.token_hex(16) if capture else None,
            capture_status="ready" if capture else "disabled",
        )
        return token

    def get(self, token, owner=None):
        self.reap()
        if (
            token not in self.items
            or self.items[token].owner != owner
            or self.items[token].expires <= self.clock()
        ):
            raise SessionError(410, "session_expired")
        return self.items[token]

    def begin(self, token, version, owner=None):
        session = self.get(token, owner)
        if session.busy:
            raise SessionError(429, "session_busy")
        if version != session.version:
            raise SessionError(409, "session_changed")
        if session.version >= self.max_turns:
            raise SessionError(410, "turn_limit")
        session.busy = True
        return session

    def delete(self, token, owner=None):
        session = self.items.get(token)
        if session is not None and session.owner == owner:
            if owner is not None and session.busy:
                raise SessionError(429, "session_busy")
            self.items.pop(token, None)
