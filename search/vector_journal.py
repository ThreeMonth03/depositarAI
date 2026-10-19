"""Private, bounded evidence for one vector helper; no service or Docker IO.

The same package validators are used inside the importer image and by the host.
A valid prefix is diagnostic only. Only a complete clean chain can support
the host's separate exact-helper removal and resume decision.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re

from search.operations.shared import private_io


MAX_EVENTS = 32
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_DOCUMENTS = 10_000
MAX_PASS_DOCUMENTS = 256
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,1000}\Z")
TOKEN_NAME = re.compile(r"depositar-import-[0-9a-f]{12}\Z")
IDENTITY_KEYS = frozenset(
    {"schema_version", "run_id", "bundle_id", "manifest_sha256", "recipe_sha256"}
)
RECORD_KEYS = IDENTITY_KEYS | {
    "sequence",
    "event",
    "payload",
    "previous_sha256",
    "sha256",
}
EVENTS = frozenset(
    {
        "pass-started",
        "token-create-intent",
        "token-created",
        "scheduler-verified",
        "before-state",
        "scheduler-enable-intent",
        "scheduler-enabled",
        "job-start-intent",
        "job-started",
        "job-completed",
        "scheduler-restore-intent",
        "scheduler-stop-intent",
        "scheduler-restored",
        "after-state",
        "token-cleanup-intent",
        "token-revoked",
        "logout-intent",
        "session-logged-out",
        "pass-result",
    }
)
NORMAL_EVENTS = (
    "pass-started",
    "token-create-intent",
    "token-created",
    "scheduler-verified",
    "before-state",
    "scheduler-enable-intent",
    "scheduler-enabled",
    "job-start-intent",
    "job-started",
    "job-completed",
    "scheduler-restore-intent",
    "scheduler-restored",
    "after-state",
    "token-cleanup-intent",
    "token-revoked",
    "logout-intent",
    "session-logged-out",
    "pass-result",
)
NOOP_EVENTS = (
    "pass-started",
    "token-create-intent",
    "token-created",
    "scheduler-verified",
    "before-state",
    "scheduler-restored",
    "after-state",
    "token-cleanup-intent",
    "token-revoked",
    "logout-intent",
    "session-logged-out",
    "pass-result",
)
REPORT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "run_id",
        "recipe_sha256",
        "bundle_id",
        "manifest_sha256",
        "document_count",
        "before_done_count",
        "done_count",
        "pending_count",
        "progress_count",
        "before_state_sha256",
        "after_state_sha256",
        "job_name",
        "job_id",
        "scheduler_id",
        "elapsed_seconds",
        "scheduler_restored_disabled",
        "job_terminal_verified",
        "token_cleanup_verified",
        "progress_verified",
        "no_op",
        "generation_enabled",
    }
)


class JournalError(ValueError):
    """Fixed errors intentionally omit file paths, secrets and supplied values."""


def _sha(value):
    return type(value) is str and SHA256.fullmatch(value) is not None


def _id(value):
    return type(value) is str and SAFE_ID.fullmatch(value) is not None


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _number(value, minimum, maximum):
    if type(value) not in (float, int):
        return False
    try:
        return math.isfinite(value) and minimum < value <= maximum
    except (OverflowError, ValueError):
        return False


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise JournalError("vector_journal_schema_invalid")


def validate_identity(value):
    _keys(value, IDENTITY_KEYS)
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["run_id"]) is not str
        or RUN_ID.fullmatch(value["run_id"]) is None
        or not all(
            _sha(value[key])
            for key in ("bundle_id", "manifest_sha256", "recipe_sha256")
        )
    ):
        raise JournalError("vector_journal_identity_invalid")
    return dict(value)


def _entries(value):
    if type(value) is not list or len(value) > MAX_DOCUMENTS:
        raise JournalError("vector_journal_entries_invalid")
    result, previous = [], None
    for row in value:
        if (
            type(row) is not list
            or len(row) != 3
            or type(row[0]) is not str
            or not 1 <= len(row[0]) <= 4096
            or any(character in row[0] for character in ("\x00", "\n", "\r"))
            or not _sha(row[1])
            or not _sha(row[2])
            or (previous is not None and row[0] <= previous)
        ):
            raise JournalError("vector_journal_entries_invalid")
        result.append(tuple(row))
        previous = row[0]
    return tuple(result)


def validate_state(value):
    _keys(value, {"document_count", "done_entries", "pending_entries"})
    done, pending = _entries(value["done_entries"]), _entries(value["pending_entries"])
    if (
        not _integer(value["document_count"], 1, MAX_DOCUMENTS)
        or len(done) + len(pending) != value["document_count"]
        or {row[0] for row in done} & {row[0] for row in pending}
    ):
        raise JournalError("vector_journal_state_invalid")
    return done, pending


def state_sha256(value):
    validate_state(value)
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def validate_report(value):
    _keys(value, REPORT_KEYS)
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 2
        or type(value["run_id"]) is not str
        or RUN_ID.fullmatch(value["run_id"]) is None
        or not all(
            _sha(value[key])
            for key in (
                "recipe_sha256",
                "bundle_id",
                "manifest_sha256",
                "before_state_sha256",
                "after_state_sha256",
            )
        )
        or not _integer(value["document_count"], 1, MAX_DOCUMENTS)
        or any(
            not _integer(value[key], 0, value["document_count"])
            for key in (
                "before_done_count",
                "done_count",
                "pending_count",
                "progress_count",
            )
        )
        or value["done_count"] + value["pending_count"] != value["document_count"]
        or value["done_count"] - value["before_done_count"] != value["progress_count"]
        or value["progress_count"] > MAX_PASS_DOCUMENTS
        or type(value["no_op"]) is not bool
        or not _id(value["scheduler_id"])
        or not _number(value["elapsed_seconds"], -1, 2220)
        or value["elapsed_seconds"] < 0
        or any(
            value[key] is not True
            for key in (
                "scheduler_restored_disabled",
                "job_terminal_verified",
                "token_cleanup_verified",
                "progress_verified",
            )
        )
        or value["generation_enabled"] is not False
        or value["status"]
        != ("partial-clean" if value["pending_count"] else "complete-clean")
    ):
        raise JournalError("vector_journal_report_invalid")
    if value["no_op"]:
        if (
            value["job_name"] is not None
            or value["job_id"] is not None
            or value["progress_count"] != 0
            or value["pending_count"] != 0
            or value["before_done_count"] != value["document_count"]
            or value["before_state_sha256"] != value["after_state_sha256"]
        ):
            raise JournalError("vector_journal_noop_invalid")
    elif (
        value["job_name"] != "Content Chunk Vector Indexer"
        or not _id(value["job_id"])
        or value["progress_count"] < 1
    ):
        raise JournalError("vector_journal_job_invalid")
    return value


def _payload(event, value):
    if event == "pass-started":
        _keys(value, {"deadline_seconds", "poll_seconds"})
        if not _number(value["deadline_seconds"], 0, 1800) or not _number(
            value["poll_seconds"], 0, 10
        ):
            raise JournalError("vector_journal_budget_invalid")
    elif event in {"before-state", "after-state"}:
        validate_state(value)
    elif event == "pass-result":
        validate_report(value)
    elif event in {
        "token-create-intent",
        "token-cleanup-intent",
        "token-revoked",
        "token-created",
    }:
        expected = (
            {"token_name", "token_id"} if event == "token-created" else {"token_name"}
        )
        _keys(value, expected)
        if (
            type(value["token_name"]) is not str
            or TOKEN_NAME.fullmatch(value["token_name"]) is None
            or (event == "token-created" and not _id(value["token_id"]))
        ):
            raise JournalError("vector_journal_token_identity_invalid")
    elif event in {"logout-intent", "session-logged-out"}:
        _keys(value, set())
    elif event == "scheduler-verified":
        _keys(value, {"scheduler_id", "available", "running"})
        if (
            not _id(value["scheduler_id"])
            or value["available"] != "false"
            or value["running"] is not False
        ):
            raise JournalError("vector_journal_scheduler_not_stopped")
    elif event in {"scheduler-enable-intent", "scheduler-enabled", "job-start-intent"}:
        _keys(value, {"scheduler_id"})
        if not _id(value["scheduler_id"]):
            raise JournalError("vector_journal_scheduler_invalid")
    else:
        _keys(value, {"scheduler_id", "job_id"})
        if not _id(value["scheduler_id"]) or (
            not _id(value["job_id"])
            and not (
                event in {"scheduler-restore-intent", "scheduler-restored"}
                and value["job_id"] is None
            )
        ):
            raise JournalError("vector_journal_job_identity_invalid")


def _complete(records, identity):
    report = records[-1]["payload"]
    names = tuple(row["event"] for row in records)
    if names != (NOOP_EVENTS if report["no_op"] else NORMAL_EVENTS):
        raise JournalError("vector_journal_clean_sequence_invalid")
    payloads = {row["event"]: row["payload"] for row in records}
    if any(report[key] != identity[key] for key in IDENTITY_KEYS - {"schema_version"}):
        raise JournalError("vector_journal_report_identity_changed")
    token_names = {
        value["token_name"] for value in payloads.values() if "token_name" in value
    }
    scheduler_ids = {
        value["scheduler_id"] for value in payloads.values() if "scheduler_id" in value
    }
    job_ids = {value["job_id"] for value in payloads.values() if "job_id" in value}
    if (
        len(token_names) != 1
        or scheduler_ids != {report["scheduler_id"]}
        or job_ids != {report["job_id"]}
    ):
        raise JournalError("vector_journal_resource_identity_changed")
    before, after = payloads["before-state"], payloads["after-state"]
    before_done, before_pending = validate_state(before)
    after_done, after_pending = validate_state(after)
    if (
        set(before_done) | set(before_pending) != set(after_done) | set(after_pending)
        or any(row[1] != identity["bundle_id"] for row in before_done + before_pending)
        or not set(before_done) <= set(after_done)
        or not set(after_pending) <= set(before_pending)
        or before["document_count"] != report["document_count"]
        or after["document_count"] != report["document_count"]
        or len(before_done) != report["before_done_count"]
        or len(after_done) != report["done_count"]
        or len(after_pending) != report["pending_count"]
        or state_sha256(before) != report["before_state_sha256"]
        or state_sha256(after) != report["after_state_sha256"]
    ):
        raise JournalError("vector_journal_progress_changed")


def validate_all(events, expected_identity=None, *, require_result=False):
    """Validate a bounded diagnostic prefix, or demand a complete clean chain."""
    if type(events) not in (tuple, list) or len(events) > MAX_EVENTS:
        raise JournalError("vector_journal_event_limit")
    expected = (
        validate_identity(expected_identity) if expected_identity is not None else None
    )
    previous, total, identity = "0" * 64, 0, expected
    records = []
    for number, record in enumerate(events, 1):
        _keys(record, RECORD_KEYS)
        current_identity = validate_identity(
            {key: record[key] for key in IDENTITY_KEYS}
        )
        if identity is None:
            identity = current_identity
        if (
            current_identity != identity
            or type(record["sequence"]) is not int
            or record["sequence"] != number
            or type(record["event"]) is not str
            or record["event"] not in EVENTS
            or record["previous_sha256"] != previous
            or not _sha(record["sha256"])
        ):
            raise JournalError("vector_journal_chain_invalid")
        _payload(record["event"], record["payload"])
        body = {key: value for key, value in record.items() if key != "sha256"}
        encoded = private_io.canonical(body)
        if hashlib.sha256(encoded).hexdigest() != record["sha256"]:
            raise JournalError("vector_journal_digest_changed")
        size = len(private_io.canonical(record))
        total += size
        if size > MAX_RECORD_BYTES or total > MAX_TOTAL_BYTES:
            raise JournalError("vector_journal_byte_limit")
        records.append(record)
        previous = record["sha256"]
    if require_result:
        if not records or records[-1]["event"] != "pass-result":
            raise JournalError("vector_journal_incomplete")
        _complete(records, identity)
    return tuple(records)


def _strict_object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise JournalError("vector_journal_duplicate_key")
        value[key] = child
    return value


def _read_events(fd, path, expected_identity):
    private_io.verify_directory(fd, path)
    names = sorted(os.listdir(fd))
    if len(names) > MAX_EVENTS:
        raise JournalError("vector_journal_event_limit")
    records, total = [], 0
    for number, name in enumerate(names, 1):
        if re.fullmatch(r"[0-9]{4}-[a-z-]+\.json", name) is None or not name.startswith(
            f"{number:04d}-"
        ):
            raise JournalError("vector_journal_file_identity_invalid")
        raw = private_io.read_file(fd, name, mode=0o600, max_bytes=MAX_RECORD_BYTES)
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise JournalError("vector_journal_byte_limit")
        try:
            record = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise JournalError("vector_journal_json_invalid") from None
        if (
            type(record) is not dict
            or name != f"{number:04d}-{record.get('event')}.json"
        ):
            raise JournalError("vector_journal_file_identity_invalid")
        if private_io.canonical(record) != raw:
            raise JournalError("vector_journal_noncanonical_record")
        records.append(record)
    if names != sorted(os.listdir(fd)):
        raise JournalError("vector_journal_inventory_changed")
    private_io.verify_directory(fd, path)
    return validate_all(records, expected_identity)


def read_events(path, expected_identity=None):
    """Read and validate private files; incomplete prefixes grant no rights."""
    fd = private_io.open_directory(path)
    try:
        return _read_events(fd, path, expected_identity)
    finally:
        os.close(fd)


class DurableVectorJournal:
    def __init__(self, path, identity):
        self.path = Path(path)
        self.identity = validate_identity(identity)
        self.fd = private_io.open_directory(self.path)
        try:
            if os.listdir(self.fd):
                raise JournalError("vector_journal_requires_empty_directory")
            private_io.verify_directory(self.fd, self.path)
        except BaseException:
            self.close()
            raise
        self.records = ()

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def append(self, event, payload):
        if self.fd is None:
            raise JournalError("vector_journal_closed")
        current = _read_events(self.fd, self.path, self.identity)
        if current != self.records or (
            current and current[-1]["event"] == "pass-result"
        ):
            raise JournalError("vector_journal_writer_identity_changed")
        body = {
            **self.identity,
            "sequence": len(current) + 1,
            "event": event,
            "payload": payload,
            "previous_sha256": current[-1]["sha256"] if current else "0" * 64,
        }
        record = {
            **body,
            "sha256": hashlib.sha256(private_io.canonical(body)).hexdigest(),
        }
        validated = validate_all(
            [*current, record], self.identity, require_result=event == "pass-result"
        )
        private_io.write_json(
            self.fd,
            f"{record['sequence']:04d}-{event}.json",
            record,
            max_bytes=MAX_RECORD_BYTES,
        )
        if _read_events(self.fd, self.path, self.identity) != validated:
            raise JournalError("vector_journal_publish_identity_changed")
        self.records = validated
