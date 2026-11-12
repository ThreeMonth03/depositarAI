"""Bound private trace IO behind finite, nonblocking in-memory admission."""

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import threading
import time

from search.operations.shared.private_io import canonical
from .contract import validate_event
from .limits import TraceLimits
from .store import TraceStore


@dataclass
class _Session:
    epoch: int = 0
    capture_status: str = "queued"
    deletion_status: str = "none"
    accepted: int = 0
    gaps: dict[int, str] = field(default_factory=dict)
    has_gaps: bool = False
    retry_at: float = 0.0
    touched: float = field(default_factory=time.monotonic)


class TraceSink:
    """Keep request-thread work bounded and never wait on filesystem locks."""

    def __init__(self, root: Path | None, limits=None):
        self.root = root
        self.limits = limits or TraceLimits()
        self.store = (
            TraceStore(root, self.limits) if root is not None else None
        )
        self._condition = threading.Condition()
        self._queue = deque()
        self._bytes = 0
        self._sessions = {}
        self._deletes = set()
        self._inflight = None
        self._thread = None
        self._closed = False
        self._deadline = None
        self._inflight_size = 0
        self._healthy = root is not None
        self._next_prune = 0.0

    def start(self):
        if self.root is None or self._thread is not None or self._closed:
            return
        self._thread = threading.Thread(
            target=self._run, name="private-search-trace", daemon=True
        )
        self._thread.start()

    def _session(self, identifier):
        session = self._sessions.get(identifier)
        if session is None:
            if len(self._sessions) >= self.limits.max_sessions:
                return None
            session = _Session()
            self._sessions[identifier] = session
        session.touched = time.monotonic()
        return session

    def enqueue(self, value):
        """Freeze an allowlisted event and admit it without touching disk."""
        if self.root is None:
            return "disabled"
        try:
            event = validate_event(value)
            size = len(canonical(event))
        except (ValueError, TypeError, RecursionError):
            return "unavailable"
        with self._condition:
            session = self._session(event["test_session_id"])
            if session is None:
                return "session_limit"
            status = self._admit(event, size, session)
            session.capture_status = status
            if status != "queued":
                self._gap(session, event["turn"], status)
            self._condition.notify()
            return status

    def _admit(self, event, size, session):
        if (
            session.deletion_status != "none"
            or event["epoch"] != session.epoch
        ):
            return "revoked"
        if self._closed or not self._healthy:
            return "unavailable"
        if size > self.limits.max_event_bytes:
            return "oversize"
        if (
            session.accepted >= self.limits.max_turns
            or event["turn"] > self.limits.max_turns
        ):
            return "session_limit"
        if (
            len(self._queue) + bool(self._inflight) >= self.limits.queue_items
            or self._bytes + size > self.limits.queue_bytes
        ):
            return "queue_full"
        self._queue.append((event, size))
        self._bytes += size
        session.accepted += 1
        return "queued"

    def _gap(self, session, turn, status):
        if status == "revoked" or session.deletion_status != "none":
            return
        session.gaps[min(turn, 100)] = status
        session.has_gaps = True

    def note_gap(self, identifier, epoch, turn, code):
        """Retain finite payload-free gaps independently of the data queue."""
        from .files import session_id

        session_id(identifier)
        if (
            type(epoch) is not int
            or epoch < 0
            or type(turn) is not int
            or not 1 <= turn <= 100
            or code
            not in {
                "oversize",
                "queue_full",
                "session_limit",
                "total_limit",
                "unavailable",
                "incomplete",
                "shutdown",
                "projection_invalid",
            }
        ):
            return "unavailable"
        if self.root is None:
            return "disabled"
        with self._condition:
            session = self._session(identifier)
            if session is None:
                return "session_limit"
            if session.epoch != epoch or session.deletion_status != "none":
                return "revoked"
            session.capture_status = code
            if not self._closed:
                self._gap(session, turn, code)
                self._condition.notify()
            return code

    def revoke(self, identifier, epoch):
        """Fence publication immediately and schedule asynchronous cleanup."""
        from .files import session_id

        session_id(identifier)
        if type(epoch) is not int or epoch < 1:
            raise ValueError("trace_epoch_invalid")
        if self.root is None:
            return "complete"
        with self._condition:
            session = self._session(identifier)
            if session is None:
                return "unavailable"
            session.epoch = max(epoch, session.epoch)
            session.capture_status = "revoked"
            if session.deletion_status == "complete":
                return "complete"
            session.deletion_status = "pending"
            session.gaps.clear()
            self._deletes.add(identifier)
            retained = deque()
            for event, size in self._queue:
                if event["test_session_id"] == identifier:
                    self._bytes -= size
                else:
                    retained.append((event, size))
            self._queue = retained
            self._condition.notify()
            return "pending"

    def status(self, identifier):
        with self._condition:
            session = self._sessions.get(identifier)
            if session is None:
                return {
                    "capture_status": "disabled"
                    if self.root is None
                    else "unavailable"
                    if not self._healthy
                    else "ready",
                    "deletion_status": "none",
                }
            return {
                "capture_status": session.capture_status,
                "deletion_status": session.deletion_status,
            }

    def close(self, timeout=None):
        budget = self.limits.shutdown_seconds if timeout is None else timeout
        budget = min(max(float(budget), 0), self.limits.shutdown_seconds)
        with self._condition:
            self._closed = True
            self._deadline = time.monotonic() + budget
            self._condition.notify()
        if self._thread is not None:
            self._thread.join(budget)

    def _allowed(self, event):
        with self._condition:
            session = self._sessions[event["test_session_id"]]
            return (
                session.epoch == event["epoch"]
                and session.deletion_status == "none"
                and (
                    self._deadline is None or time.monotonic() < self._deadline
                )
            )

    def _next(self):
        with self._condition:
            if (
                self._deadline is not None
                and time.monotonic() >= self._deadline
            ):
                return "stop", None
            if self._deletes:
                return "delete", self._deletes.pop()
            for identifier, session in self._sessions.items():
                if session.gaps and time.monotonic() >= session.retry_at:
                    gaps, session.gaps = session.gaps, {}
                    return "gaps", (identifier, gaps)
            if self._queue:
                event, size = self._queue.popleft()
                self._inflight_size = size
                self._inflight = event["test_session_id"]
                return "event", event
            if self._closed:
                if any(session.gaps for session in self._sessions.values()):
                    return "stop", None
                return "seal", None
            self._condition.wait(0.1)
            return "idle", None

    def _run(self):
        while True:
            if time.monotonic() >= self._next_prune:
                self._maintenance()
            operation, value = self._next()
            if operation == "stop":
                return
            if operation == "seal":
                self._seal()
                with self._condition:
                    if not self._deletes:
                        return
                continue
            try:
                self._perform(operation, value)
                if operation != "idle":
                    self._healthy = True
            except (OSError, ValueError, TypeError, RecursionError):
                self._failure(operation, value)
                self._healthy = False
            finally:
                with self._condition:
                    self._bytes -= self._inflight_size
                    self._inflight_size = 0
                    self._inflight = None

    def _perform(self, operation, value):
        if operation == "delete":
            self._delete(value)
        elif operation == "gaps":
            self._gaps(*value)
        elif operation == "event":
            self._write(value)

    def _maintenance(self):
        try:
            self.store.prune()
            self._healthy = True
            self._reap()
        except (OSError, ValueError, TypeError, RecursionError):
            self._healthy = False
        self._next_prune = time.monotonic() + 30

    def _write(self, event):
        identifier = event["test_session_id"]
        if not self._allowed(event):
            return
        status = self.store.append(event, allowed=lambda: self._allowed(event))
        with self._condition:
            session = self._sessions[identifier]
            if session.deletion_status == "none":
                session.capture_status = status
                if status != "captured":
                    self._gap(session, event["turn"], status)
                elif not event["capture"]["complete"] or session.has_gaps:
                    session.has_gaps = True
                    session.capture_status = "incomplete"
                elif any(
                    queued["test_session_id"] == identifier
                    for queued, _ in self._queue
                ):
                    session.capture_status = "queued"

    def _delete(self, identifier):
        self.store.delete(identifier)
        with self._condition:
            self._sessions[identifier].deletion_status = "complete"

    def _gaps(self, identifier, gaps):
        for turn, status in gaps.items():
            with self._condition:
                if self._sessions[identifier].deletion_status != "none":
                    return
            self.store.gap(identifier, turn, status)

    def _failure(self, operation, value):
        with self._condition:
            if operation == "delete":
                self._sessions[value].deletion_status = "unavailable"
            elif operation == "event":
                session = self._sessions[value["test_session_id"]]
                if session.deletion_status == "none":
                    session.capture_status = "unavailable"
                    self._gap(session, value["turn"], "unavailable")
            elif operation == "gaps":
                session = self._sessions[value[0]]
                if session.deletion_status == "none":
                    session.capture_status = "unavailable"
                    session.gaps.update(value[1])
                    session.retry_at = time.monotonic() + 1

    def _seal_allowed(self, identifier, epoch):
        with self._condition:
            session = self._sessions[identifier]
            return (
                session.epoch == epoch
                and session.deletion_status == "none"
                and time.monotonic() < self._deadline
            )

    def _seal(self):
        with self._condition:
            identifiers = [
                (identifier, session.epoch)
                for identifier, session in self._sessions.items()
                if session.deletion_status == "none"
                and not session.has_gaps
                and session.capture_status == "captured"
            ]
        for identifier, epoch in identifiers:
            if not self._seal_allowed(identifier, epoch):
                continue
            try:
                with self._condition:
                    self._inflight = identifier
                self.store.seal(
                    identifier,
                    allowed=lambda: self._seal_allowed(identifier, epoch),
                )
            except (OSError, ValueError, TypeError, RecursionError):
                with self._condition:
                    if self._sessions[identifier].deletion_status == "none":
                        self._sessions[
                            identifier
                        ].capture_status = "unavailable"
            finally:
                with self._condition:
                    self._inflight = None

    def _reap(self):
        cutoff = time.monotonic() - self.limits.ttl_seconds
        with self._condition:
            pending = {event["test_session_id"] for event, _ in self._queue}
            pending |= self._deletes
            for identifier, session in list(self._sessions.items()):
                if (
                    session.touched < cutoff
                    and identifier not in pending
                    and identifier != self._inflight
                    and session.deletion_status != "pending"
                ):
                    del self._sessions[identifier]
