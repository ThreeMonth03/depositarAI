"""Strict vector evidence, private publication and isolated admin package imports."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from search import vector_journal as journal
from search.operations.shared import private_io


IDENTITY = {
    "schema_version": 1,
    "run_id": "1" * 32,
    "bundle_id": "a" * 64,
    "manifest_sha256": "b" * 64,
    "recipe_sha256": "c" * 64,
}
ENTRIES = [
    ["https://data.depositar.io/dataset/" + name, "a" * 64, "d" * 64]
    for name in ("a", "b")
]


def clean_payloads(*, no_op=False):
    before = {
        "document_count": 2,
        "done_entries": copy.deepcopy(ENTRIES) if no_op else [],
        "pending_entries": [] if no_op else copy.deepcopy(ENTRIES),
    }
    after = (
        copy.deepcopy(before)
        if no_op
        else {
            "document_count": 2,
            "done_entries": copy.deepcopy(ENTRIES[:1]),
            "pending_entries": copy.deepcopy(ENTRIES[1:]),
        }
    )
    report = {
        "schema_version": 2,
        "status": "complete-clean" if no_op else "partial-clean",
        **{key: value for key, value in IDENTITY.items() if key != "schema_version"},
        "document_count": 2,
        "before_done_count": 2 if no_op else 0,
        "done_count": 2 if no_op else 1,
        "pending_count": 0 if no_op else 1,
        "progress_count": 0 if no_op else 1,
        "before_state_sha256": journal.state_sha256(before),
        "after_state_sha256": journal.state_sha256(after),
        "job_name": None if no_op else "Content Chunk Vector Indexer",
        "job_id": None if no_op else "job-1",
        "scheduler_id": "scheduler-1",
        "elapsed_seconds": 61.0,
        "scheduler_restored_disabled": True,
        "job_terminal_verified": True,
        "token_cleanup_verified": True,
        "progress_verified": True,
        "no_op": no_op,
        "generation_enabled": False,
    }
    token = {"token_name": "depositar-import-0123456789ab"}
    scheduler = {"scheduler_id": "scheduler-1"}
    job = {**scheduler, "job_id": report["job_id"]}
    return {
        "pass-started": {"deadline_seconds": 1200.0, "poll_seconds": 1.0},
        "token-create-intent": token,
        "token-created": {**token, "token_id": "token-id"},
        "scheduler-verified": {**scheduler, "available": "false", "running": False},
        "before-state": before,
        "scheduler-enable-intent": scheduler,
        "scheduler-enabled": scheduler,
        "job-start-intent": scheduler,
        "job-started": job,
        "job-completed": job,
        "scheduler-restore-intent": job,
        "scheduler-restored": job,
        "after-state": after,
        "token-cleanup-intent": token,
        "token-revoked": token,
        "logout-intent": {},
        "session-logged-out": {},
        "pass-result": report,
    }


def records(payloads=None, *, no_op=False, names=None):
    payloads = payloads or clean_payloads(no_op=no_op)
    names = names or (journal.NOOP_EVENTS if no_op else journal.NORMAL_EVENTS)
    result, previous = [], "0" * 64
    for number, event in enumerate(names, 1):
        body = {
            **IDENTITY,
            "sequence": number,
            "event": event,
            "payload": copy.deepcopy(payloads[event]),
            "previous_sha256": previous,
        }
        record = {
            **body,
            "sha256": hashlib.sha256(private_io.canonical(body)).hexdigest(),
        }
        result.append(record)
        previous = record["sha256"]
    return result


class VectorJournalValidationTests(unittest.TestCase):
    def test_clean_normal_and_noop_chains_validate(self):
        for no_op in (False, True):
            with self.subTest(no_op=no_op):
                value = records(no_op=no_op)
                self.assertEqual(
                    tuple(value),
                    journal.validate_all(value, IDENTITY, require_result=True),
                )

    def test_prefixes_are_diagnostic_only_and_cannot_grant_completion(self):
        value = records()
        for prefix in ([], value[:1], value[:-1]):
            self.assertEqual(tuple(prefix), journal.validate_all(prefix, IDENTITY))
            with self.assertRaisesRegex(journal.JournalError, "incomplete"):
                journal.validate_all(prefix, IDENTITY, require_result=True)

    def test_order_gap_digest_mutation_and_identity_change_are_rejected(self):
        original = records()
        mutations = (
            lambda rows: rows[3].update(sequence=99),
            lambda rows: rows[3].update(previous_sha256="f" * 64),
            lambda rows: rows[3].update(sha256="f" * 64),
            lambda rows: rows[3].update(run_id="2" * 32),
            lambda rows: rows[3]["payload"].update(running=True),
        )
        for mutation in mutations:
            value = copy.deepcopy(original)
            mutation(value)
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(journal.JournalError),
            ):
                journal.validate_all(value, IDENTITY, require_result=True)
        names = list(journal.NORMAL_EVENTS)
        names[4], names[5] = names[5], names[4]
        with self.assertRaisesRegex(journal.JournalError, "clean_sequence"):
            journal.validate_all(records(names=names), require_result=True)

    def test_rehashed_resource_changes_still_fail_closed(self):
        for event, key, value in (
            ("job-completed", "job_id", "different-job"),
            ("scheduler-verified", "scheduler_id", "different-scheduler"),
            ("token-revoked", "token_name", "depositar-import-ffffffffffff"),
            ("pass-result", "recipe_sha256", "f" * 64),
        ):
            payloads = clean_payloads()
            payloads[event][key] = value
            with self.subTest(event=event), self.assertRaises(journal.JournalError):
                journal.validate_all(records(payloads), require_result=True)

    def test_same_counts_cannot_hide_replacement_or_regression(self):
        payloads = clean_payloads()
        payloads["before-state"]["done_entries"] = [ENTRIES[0]]
        payloads["before-state"]["pending_entries"] = [ENTRIES[1]]
        payloads["after-state"]["done_entries"] = [ENTRIES[1]]
        payloads["after-state"]["pending_entries"] = [ENTRIES[0]]
        payloads["pass-result"].update(
            before_done_count=1,
            progress_count=1,
            before_state_sha256=journal.state_sha256(payloads["before-state"]),
            after_state_sha256=journal.state_sha256(payloads["after-state"]),
        )
        with self.assertRaises(journal.JournalError):
            journal.validate_all(records(payloads), require_result=True)
        payloads = clean_payloads()
        payloads["after-state"]["done_entries"][0][2] = "f" * 64
        payloads["pass-result"]["after_state_sha256"] = journal.state_sha256(
            payloads["after-state"]
        )
        with self.assertRaisesRegex(journal.JournalError, "progress_changed"):
            journal.validate_all(records(payloads), require_result=True)

    def test_noop_cannot_omit_scheduler_proof_or_claim_other_scheduler(self):
        value = records(no_op=True)
        names = tuple(
            event for event in journal.NOOP_EVENTS if event != "scheduler-verified"
        )
        with self.assertRaisesRegex(journal.JournalError, "clean_sequence"):
            journal.validate_all(records(no_op=True, names=names), require_result=True)
        payloads = clean_payloads(no_op=True)
        payloads["pass-result"]["scheduler_id"] = "other"
        with self.assertRaisesRegex(journal.JournalError, "resource_identity"):
            journal.validate_all(records(payloads, no_op=True), require_result=True)
        self.assertEqual(len(value), len(journal.NOOP_EVENTS))

    def test_state_partition_is_sorted_unique_exhaustive_and_bounded(self):
        for state in (
            {
                "document_count": 2,
                "done_entries": ENTRIES,
                "pending_entries": [ENTRIES[0]],
            },
            {"document_count": True, "done_entries": [], "pending_entries": ENTRIES},
            {
                "document_count": 2,
                "done_entries": [],
                "pending_entries": list(reversed(ENTRIES)),
            },
            {
                "document_count": 2,
                "done_entries": [],
                "pending_entries": [ENTRIES[0], ENTRIES[0]],
            },
            {"document_count": 2, "done_entries": [], "pending_entries": [ENTRIES[0]]},
        ):
            with self.subTest(state=state), self.assertRaises(journal.JournalError):
                journal.validate_state(state)

    def test_unknown_events_extra_secret_fields_and_false_boolean_proofs_fail(self):
        for event, key, value in (
            ("pass-started", "token", "secret-not-allowed"),
            ("pass-started", "deadline_seconds", float("nan")),
            ("pass-started", "deadline_seconds", 1801),
            ("scheduler-verified", "running", 0),
            ("pass-result", "token_cleanup_verified", 1),
            ("pass-result", "elapsed_seconds", -0.5),
            ("pass-result", "progress_count", True),
        ):
            payloads = clean_payloads()
            payloads[event][key] = value
            with (
                self.subTest(event=event, key=key),
                self.assertRaises((journal.JournalError, ValueError)),
            ):
                journal.validate_all(records(payloads), require_result=True)
        value = records()
        value[0]["event"] = "publish-site"
        with self.assertRaises(journal.JournalError):
            journal.validate_all(value)

    def test_record_count_and_byte_caps_are_enforced(self):
        with self.assertRaisesRegex(journal.JournalError, "event_limit"):
            journal.validate_all([{}] * (journal.MAX_EVENTS + 1))
        with (
            patch.object(journal, "MAX_RECORD_BYTES", 10),
            self.assertRaisesRegex(journal.JournalError, "byte_limit"),
        ):
            journal.validate_all(records())
        with (
            patch.object(journal, "MAX_TOTAL_BYTES", 10),
            self.assertRaisesRegex(journal.JournalError, "byte_limit"),
        ):
            journal.validate_all(records())


class VectorJournalFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-journal-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def save_records(self, value):
        fd = private_io.open_directory(self.root)
        try:
            for record in value:
                private_io.write_json(
                    fd,
                    f"{record['sequence']:04d}-{record['event']}.json",
                    record,
                    max_bytes=journal.MAX_RECORD_BYTES,
                )
        finally:
            os.close(fd)

    def test_private_append_read_validate_and_immutable_final_record(self):
        with journal.DurableVectorJournal(self.root, IDENTITY) as writer:
            payloads = clean_payloads()
            for event in journal.NORMAL_EVENTS:
                writer.append(event, payloads[event])
            value = journal.read_events(self.root, IDENTITY)
            journal.validate_all(value, IDENTITY, require_result=True)
            for path in self.root.iterdir():
                self.assertEqual(0o600, path.stat().st_mode & 0o777)
                self.assertEqual(1, path.stat().st_nlink)
            with self.assertRaisesRegex(journal.JournalError, "writer_identity"):
                writer.append("logout-intent", {})
        with self.assertRaisesRegex(journal.JournalError, "closed"):
            writer.append("logout-intent", {})
        with self.assertRaisesRegex(journal.JournalError, "empty"):
            journal.DurableVectorJournal(self.root, IDENTITY)

    def test_permissions_links_and_unrecognized_crash_files_are_rejected(self):
        self.save_records(records()[:1])
        path = next(self.root.iterdir())
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            journal.read_events(self.root)
        path.chmod(0o600)
        os.link(path, self.root / "0002-token-create-intent.json")
        with self.assertRaises(ValueError):
            journal.read_events(self.root)
        (self.root / "0002-token-create-intent.json").unlink()
        symbolic = self.root / "0002-token-create-intent.json"
        symbolic.symlink_to(path)
        with self.assertRaises((ValueError, OSError)):
            journal.read_events(self.root)
        symbolic.unlink()
        (self.root / ".record-interrupted-write").write_bytes(b"unfinished")
        with self.assertRaisesRegex(journal.JournalError, "file_identity"):
            journal.read_events(self.root)

    def test_noncanonical_duplicate_key_wrong_filename_and_record_change_rejected(self):
        self.save_records(records()[:1])
        path = next(self.root.iterdir())
        raw = path.read_bytes()
        path.write_bytes(b" " + raw)
        with self.assertRaisesRegex(journal.JournalError, "noncanonical"):
            journal.read_events(self.root)
        path.write_bytes(
            raw.replace(b'{"bundle_id"', b'{"sequence":1,"sequence":2,"bundle_id"', 1)
        )
        with self.assertRaisesRegex(journal.JournalError, "duplicate_key"):
            journal.read_events(self.root)
        path.write_bytes(raw)
        other = path.with_name("0001-token-created.json")
        path.rename(other)
        with self.assertRaisesRegex(journal.JournalError, "file_identity"):
            journal.read_events(self.root)
        other.rename(path)
        value = json.loads(raw)
        value["payload"]["deadline_seconds"] = 600
        path.write_bytes(private_io.canonical(value))
        with self.assertRaisesRegex(journal.JournalError, "digest_changed"):
            journal.read_events(self.root)

    def test_directory_replacement_blocks_publication_through_retained_fd(self):
        with journal.DurableVectorJournal(self.root, IDENTITY) as writer:
            moved = self.root.with_name(self.root.name + "-moved")
            self.root.rename(moved)
            self.root.mkdir(mode=0o700)
            try:
                with self.assertRaises(ValueError):
                    writer.append("pass-started", clean_payloads()["pass-started"])
                self.assertEqual([], list(moved.iterdir()))
            finally:
                self.root.rmdir()
                moved.rename(self.root)

    def test_other_writer_or_tampering_cannot_be_accepted_as_our_own_records(self):
        with journal.DurableVectorJournal(self.root, IDENTITY) as writer:
            self.save_records(records()[:1])
            with self.assertRaisesRegex(journal.JournalError, "writer_identity"):
                writer.append("logout-intent", {})

    def test_unsafe_directory_mode_alias_and_wrong_owner_are_rejected(self):
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            journal.DurableVectorJournal(self.root, IDENTITY)
        self.root.chmod(0o700)
        with patch.object(private_io.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(ValueError):
                journal.DurableVectorJournal(self.root, IDENTITY)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            journal.read_events(alias)

    def test_admin_package_closure_supports_durable_journals_without_app_dependencies(
        self,
    ):
        from search.operations.catalog.administration import ADMIN_FILES

        staging = self.root / "admin-source"
        staging.mkdir(mode=0o700)
        repository = Path(__file__).resolve().parents[2]
        for name in ADMIN_FILES:
            if name.endswith(".py"):
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((repository / name).read_bytes())
        script = """
import json
from pathlib import Path
import sys

source = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(source))
from search import catalog_admin, vector_journal
from search.catalog.journal import _vector_event

assert catalog_admin.VectorBuilder.__module__ == "search.catalog.vectors"
identity = dict(schema_version=1, run_id='1'*32, bundle_id='a'*64,
                manifest_sha256='b'*64, recipe_sha256='c'*64)
vector_journal.validate_identity(identity)
directory = source.parent / "isolated-journal"
directory.mkdir(mode=0o700)
with vector_journal.DurableVectorJournal(directory, identity) as writer:
    _vector_event(writer, "pass-started",
                  {"deadline_seconds": 1200.0, "poll_seconds": 1.0})
rows = vector_journal.read_events(directory, identity)
assert len(rows) == 1 and rows[0]["event"] == "pass-started"
vector_journal.validate_all(rows, identity)

reviewed = set(json.loads(sys.argv[2]))
for name, module in tuple(sys.modules.items()):
    if name == "search" or name.startswith("search."):
        relative = Path(module.__file__).resolve().relative_to(source).as_posix()
        assert relative in reviewed, (name, relative)
for forbidden in ("torch", "fastapi", "django", "rest_framework", "adrf", "pydantic", "search.api", "search.web",
                  "search.planning", "search.operations.config"):
    assert not any(name == forbidden or name.startswith(forbidden + ".")
                   for name in sys.modules), forbidden
"""
        outcome = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-S",
                "-c",
                script,
                str(staging),
                json.dumps(ADMIN_FILES),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        self.assertEqual(0, outcome.returncode, outcome.stderr.decode())


if __name__ == "__main__":
    unittest.main()
