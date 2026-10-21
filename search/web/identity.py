"""Validate the entrance's principal and descriptive access policy."""

from dataclasses import dataclass
import re
import secrets
from typing import Literal

from search.secrets import read_secret
from .sessions import SessionError


@dataclass(frozen=True)
class Principal:
    kind: Literal["private", "guest", "user"]
    subject: str | None = None

    def __post_init__(self):
        if self.kind == "private":
            if self.subject is not None:
                raise ValueError("private principal cannot have a subject")
        elif self.kind not in {"guest", "user"} or (
            not isinstance(self.subject, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.subject) is None
        ):
            raise ValueError("invalid public principal")

    @property
    def owner(self) -> str | None:
        # A guest and user with the same opaque subject remain distinct owners.
        return None if self.kind == "private" else f"{self.kind}:{self.subject}"


@dataclass(frozen=True)
class AccessIdentity:
    principal: Principal
    mode: Literal["private", "mixed", "login_required"]
    turns: int | None = None
    window_seconds: int | None = None

    def payload(self):
        """Limits describe policy; this read never checks or spends quota."""
        return {
            "mode": self.mode,
            "principal": {"kind": self.principal.kind, "id": self.principal.subject},
            "can_search": True,
            "login_url": "/auth/login" if self.principal.kind == "guest" else None,
            "logout_url": "/auth/logout" if self.principal.kind == "user" else None,
            "limits": {"turns": self.turns, "window_seconds": self.window_seconds},
        }


class EdgeIdentity:
    def __init__(self, secret_file=None):
        # Read once at startup, before serving or allocating browser sessions.
        self.secret = None if secret_file is None else read_secret(secret_file)

    @property
    def required(self):
        return self.secret is not None

    def authenticate(self, headers):
        if not self.required:
            return AccessIdentity(Principal("private"), "private")
        expected = {
            b"x-depositar-subject": [],
            b"x-depositar-edge-key": [],
            b"x-depositar-principal-kind": [],
            b"x-depositar-access-mode": [],
            b"x-depositar-turn-limit": [],
            b"x-depositar-window-seconds": [],
        }
        for name, value in headers:
            if name.lower() in expected:
                expected[name.lower()].append(value)
        if any(
            len(values) != 1 for values in expected.values()
        ) or not secrets.compare_digest(
            expected[b"x-depositar-edge-key"][0], self.secret.encode("ascii")
        ):
            raise SessionError(401, "authentication_required")
        try:
            values = {
                name: entries[0].decode("ascii") for name, entries in expected.items()
            }
            kind = values[b"x-depositar-principal-kind"]
            mode = values[b"x-depositar-access-mode"]
            if mode not in {"mixed", "login_required"} or (
                mode == "login_required" and kind != "user"
            ):
                raise ValueError("invalid public access mode")
            principal = Principal(kind, values[b"x-depositar-subject"])
            turns = self._limit(values[b"x-depositar-turn-limit"], 1, 10000)
            window = self._limit(values[b"x-depositar-window-seconds"], 60, 604800)
        except (UnicodeError, ValueError):
            raise SessionError(401, "authentication_required") from None
        return AccessIdentity(principal, mode, turns, window)

    @staticmethod
    def _limit(value, minimum, maximum):
        if re.fullmatch(r"[1-9][0-9]{0,5}", value) is None:
            raise ValueError("invalid access limit")
        number = int(value)
        if not minimum <= number <= maximum:
            raise ValueError("invalid access limit")
        return number
