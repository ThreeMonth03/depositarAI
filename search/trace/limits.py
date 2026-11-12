"""Finite budgets shared by runtime and packaging validation."""

from dataclasses import asdict, dataclass
import hashlib
import math

from search.operations.shared.private_io import canonical


@dataclass(frozen=True)
class TraceLimits:
    ttl_seconds: int = 604800
    max_turns: int = 20
    max_event_bytes: int = 1048576
    max_session_bytes: int = 8388608
    max_total_bytes: int = 104857600
    queue_items: int = 16
    queue_bytes: int = 16777216
    max_sessions: int = 100
    shutdown_seconds: float = 1.0

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == "shutdown_seconds":
                if (
                    type(value) not in {float, int}
                    or not math.isfinite(value)
                    or not 0 < value <= 10
                ):
                    raise ValueError("trace_shutdown_limit_invalid")
            elif type(value) is not int or not 1 <= value <= 1073741824:
                raise ValueError("trace_limit_invalid")
        if self.max_turns > 100 or self.max_sessions > 10000:
            raise ValueError("trace_count_limit_invalid")

    def digest(self):
        return hashlib.sha256(canonical(asdict(self))).hexdigest()
