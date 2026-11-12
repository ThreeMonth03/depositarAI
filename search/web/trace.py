"""Project consented website turns without exposing request credentials."""

from datetime import datetime, timezone
import re
import secrets
import time

from search.trace.contract import (
    NOTICE_VERSION,
    CAPTURE_STATUSES,
    DELETION_STATUSES,
    UNAVAILABLE_TRACE,
    validate_event,
)


SEARCH_FIELDS = {
    "request_id",
    "revision",
    "catalog_manifest_sha256",
    "status",
    "ranking",
    "rerank_status",
    "candidate_count",
    "condition_counts",
    "warnings",
    "filter_scope",
    "parsed_conditions",
    "applied_conditions",
}
CARD_FIELDS = {
    "rank",
    "dataset_id",
    "title",
    "resource_formats",
    "temporal_coverage",
    "condition_status",
}


def source_revision(value):
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _search_projection(result):
    if result is None or result.search is None:
        return None
    value = result.search.model_dump(include=SEARCH_FIELDS)
    value["cards"] = [
        card.model_dump(include=CARD_FIELDS) for card in result.search.cards
    ]
    return value


def _output(session, result, reason):
    if result is None:
        return {
            "version": session.version,
            "status": "unavailable",
            "reason": reason,
            "message": "",
            "state": session.state.model_dump(),
            "notices": [],
            "search": None,
        }
    value = result.model_dump(
        include={"status", "reason", "message", "state", "notices"}
    )
    return {
        **value,
        "version": session.version,
        "search": _search_projection(result),
    }


class SearchTrace:
    """Keep consent fencing on the event loop and all disk IO in the sink."""

    def __init__(self, settings, sink):
        self.settings, self.sink = settings, sink

    def policy(self):
        return {
            "available": self.settings.trace_root is not None,
            "notice_version": NOTICE_VERSION,
            "retention_days": self.settings.trace_limits.ttl_seconds / 86400,
        }

    def status(self, session):
        if session.test_session_id is not None:
            try:
                value = self.sink.status(session.test_session_id)
                if (
                    value["capture_status"] not in CAPTURE_STATUSES
                    or value["deletion_status"] not in DELETION_STATUSES
                ):
                    raise ValueError("trace_status_invalid")
                if session.capture_status in {
                    "ready",
                    "queued",
                    "captured",
                } and (value["capture_status"] != "disabled"):
                    session.capture_status = value["capture_status"]
                if value["deletion_status"] != "none":
                    session.deletion_status = value["deletion_status"]
            except Exception:
                session.capture_status = "unavailable"
        return {
            "enabled": session.capture_enabled,
            "test_session_id": session.test_session_id,
            "notice_version": NOTICE_VERSION,
            "retention_days": self.policy()["retention_days"],
            "capture_status": session.capture_status,
            "deletion_status": session.deletion_status,
        }

    def withdraw(self, session):
        if session.test_session_id is None:
            return self.status(session)
        # The event-loop fence is set before any sink operation. In-flight
        # upstream results cannot enqueue a snapshot from the previous epoch.
        session.capture_enabled = False
        session.trace_epoch += 1
        session.capture_status = "revoked"
        session.deletion_status = "pending"
        try:
            status = self.sink.revoke(
                session.test_session_id, session.trace_epoch
            )
            if status not in DELETION_STATUSES - {"none"}:
                raise ValueError("trace_deletion_status_invalid")
            session.deletion_status = status
        except Exception:
            session.deletion_status = "unavailable"
        return self.status(session)

    def begin(self, session, data):
        if not session.capture_enabled:
            return None
        session.trace_attempt += 1
        return {
            "trace_version": 1,
            "test_session_id": session.test_session_id,
            "epoch": session.trace_epoch,
            "turn": session.trace_attempt,
            "request_id": secrets.token_hex(16),
            "timestamp": utc_now(),
            "consent": {
                "notice_version": NOTICE_VERSION,
                "consented_at": session.consented_at,
            },
            "context": dict(data["context"]),
            "input": {"query": data["query"], "top_k": data["top_k"]},
            "state_before": session.state.model_dump(),
            "pending_before": session.pending,
        }

    def gap(self, session, code):
        session.capture_status = "unavailable"
        if not session.capture_enabled:
            return
        try:
            self.sink.note_gap(
                session.test_session_id,
                session.trace_epoch,
                max(1, session.trace_attempt),
                code,
            )
        except Exception:
            pass

    def finish(self, session, snapshot, result, started, reason):
        if snapshot is None:
            return
        if not session.capture_enabled or (
            snapshot["epoch"] != session.trace_epoch
        ):
            return
        try:
            event = validate_event(
                self._event(session, snapshot, result, started, reason)
            )
        except Exception:
            self.gap(session, "projection_invalid")
            return
        try:
            status = self.sink.enqueue(event)
            if status not in CAPTURE_STATUSES:
                raise ValueError("trace_capture_status_invalid")
            session.capture_status = status
        except Exception:
            # Diagnostics never include payloads or exception text.
            self.gap(session, "unavailable")

    def _event(self, session, snapshot, result, started, reason):
        output = _output(session, result, reason)
        search = output["search"]
        missing = list(UNAVAILABLE_TRACE)
        timing = {"bff_total": max(0.0, (time.monotonic() - started) * 1000)}
        if result is not None and "total" in result.timing_ms:
            timing["backend_total"] = result.timing_ms["total"]
        else:
            missing.append("backend_timing")
        if search is None:
            missing.extend(["catalog_identity", "legacy_conditions"])
        bff_revision = source_revision(self.settings.revision)
        backend_revision = source_revision(result.revision) if result else None
        if bff_revision is None:
            missing.append("bff_identity")
        if backend_revision is None:
            missing.append("backend_identity")
        gaps = [] if output["status"] != "unavailable" else [reason]
        return {
            **snapshot,
            "upstream_request_id": search["request_id"] if search else None,
            "state_after": session.state.model_dump(),
            "pending_after": session.pending,
            "execution": {
                "kind": "legacy_conditions",
                "parsed_conditions": search["parsed_conditions"]
                if search
                else None,
                "applied_conditions": search["applied_conditions"]
                if search
                else None,
            },
            "output": output,
            "identities": {
                "bff_revision": bff_revision,
                "backend_revision": backend_revision,
                "catalog_manifest_sha256": search["catalog_manifest_sha256"]
                if search
                else None,
                "capture_config_sha256": self.settings.trace_limits.digest(),
                "model_identity": None,
            },
            "timing_ms": timing,
            "capture": {"complete": not gaps, "gaps": gaps},
            "unavailable_trace": missing,
        }
