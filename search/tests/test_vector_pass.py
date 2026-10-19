"""Production vector-pass proofs using only local files and scripted Fess IO."""

from __future__ import annotations

from search.catalog.admin import AdminAccess
from search.catalog.errors import (
    ConfigurationError,
    ProtocolError,
    TokenCleanupError,
    VectorBuildError,
)
from search.catalog.inventory import InventorySnapshot
from search.catalog.vectors import (
    MAX_VECTOR_PASS_DOCUMENTS,
    VectorBuilder,
    VectorProgressSnapshot,
)

import copy
import datetime
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

from search import vector_journal as evidence
from search.tests.catalog_fixtures import (
    BuiltBundle,
    FakeTransport,
    ManualClock,
    NONCE,
    PASSWORD,
    TOKEN,
)


RUN_ID = "1" * 32
RECIPE = "2" * 64


class PartialTransport(FakeTransport):
    def __init__(self, limit=1):
        super().__init__()
        self.limit = limit

    def send(self, request, **kwargs):
        starting = urllib.parse.urlsplit(request.url).path.endswith(
            "/vector-scheduler/start"
        )
        before = copy.deepcopy(self.documents) if starting else None
        response = super().send(request, **kwargs)
        if before is not None and self.vector_job_status == "ok":
            self.documents = before
            pending = [
                row
                for row in self.documents.values()
                if row.get("content_chunk_status") is None
            ]
            for row in pending[: self.limit]:
                row["content_chunk_status"] = "done"
        return response


class ScriptedProgress:
    def __init__(self, values):
        self.values = iter(values)

    def snapshot(self, *_arguments):
        return next(self.values)


class VectorPassTests(unittest.TestCase):
    def setUp(self):
        self.built = BuiltBundle(count=3)
        self.addCleanup(self.built.close)
        self.temporary = tempfile.TemporaryDirectory(prefix="depositar-vector-pass-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.transport = PartialTransport()
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        self.clock = ManualClock()
        self.client = self.make_client()
        self.identity = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "bundle_id": self.built.bundle.bundle_id,
            "manifest_sha256": self.built.manifest_sha256,
            "recipe_sha256": RECIPE,
        }

    def make_client(self, **kwargs):
        return VectorBuilder(
            self.built.bundle,
            AdminAccess(
                "http://127.0.0.1:8080",
                transport=self.transport,
                clock=self.clock,
                sleep=self.clock.sleep,
                utcnow=lambda: datetime.datetime(
                    2026, 9, 9, 12, tzinfo=datetime.timezone.utc
                ),
            ),
            **kwargs,
        )

    def journal(self, name="journal"):
        path = self.root / name
        path.mkdir(mode=0o700)
        return evidence.DurableVectorJournal(path, self.identity)

    def run_pass(self, journal, **kwargs):
        return self.client.build_vector_pass(
            "admin",
            PASSWORD,
            journal=journal,
            run_id=RUN_ID,
            recipe_sha256=RECIPE,
            nonce=NONCE,
            **kwargs,
        )

    def progress(self, done):
        entries = self.built.bundle.expected_inventory_entries
        return VectorProgressSnapshot(
            InventorySnapshot(entries),
            tuple(row for row in entries if row[0] in done),
            tuple(row for row in entries if row[0] not in done),
        )

    def assert_clean(self, journal, report):
        rows = evidence.read_events(journal.path, self.identity)
        evidence.validate_all(rows, self.identity, require_result=True)
        self.assertEqual(report.as_dict(), rows[-1]["payload"])
        encoded = json.dumps(rows)
        for secret in (PASSWORD, TOKEN):
            self.assertNotIn(secret, encoded)
        self.assertFalse(self.transport.token_active)
        self.assertFalse(self.transport.scheduler_available)
        self.assertFalse(self.transport.vector_job_running)

    def test_partial_clean_has_document_identity_progress_and_durable_cleanup(self):
        with self.journal() as journal:
            report = self.run_pass(journal)
            self.assertEqual("partial-clean", report.status)
            self.assertEqual(
                (0, 1, 2, 1),
                (
                    report.before_done_count,
                    report.done_count,
                    report.pending_count,
                    report.progress_count,
                ),
            )
            self.assertFalse(report.no_op)
            self.assert_clean(journal, report)
            rows = evidence.read_events(journal.path)
            self.assertEqual(
                evidence.NORMAL_EVENTS, tuple(row["event"] for row in rows)
            )
            self.assertEqual(
                "2026-09-09T12:35:00", self.transport.created_token_expiries[0]
            )
            self.assertEqual(1200.0, rows[0]["payload"]["deadline_seconds"])

    def test_complete_clean_and_all_done_noop_do_not_launch_another_job(self):
        self.transport.limit = 3
        with self.journal("first") as journal:
            report = self.run_pass(journal)
            self.assertEqual("complete-clean", report.status)
            self.assertEqual(3, report.progress_count)
            self.assert_clean(journal, report)
        starts = len(self.transport.scheduler_updates)
        with self.journal("noop") as journal:
            report = self.run_pass(journal)
            self.assertTrue(report.no_op)
            self.assertEqual((None, 0), (report.job_id, report.progress_count))
            self.assertEqual(report.before_state_sha256, report.after_state_sha256)
            self.assert_clean(journal, report)
            self.assertEqual(
                evidence.NOOP_EVENTS,
                tuple(row["event"] for row in evidence.read_events(journal.path)),
            )
        self.assertEqual(starts, len(self.transport.scheduler_updates))

    def test_expected_checkpoint_is_proved_before_any_scheduler_write(self):
        with (
            self.journal() as journal,
            self.assertRaisesRegex(VectorBuildError, "checkpoint"),
        ):
            self.run_pass(journal, expected_before_state_sha256="0" * 64)
        self.assertEqual([], self.transport.scheduler_updates)
        self.assertFalse(self.transport.token_active)

    def test_correct_expected_checkpoint_can_run(self):
        with self.journal() as journal:
            report = self.run_pass(
                journal, expected_before_state_sha256=self.progress(set()).sha256
            )
            self.assert_clean(journal, report)

    def test_evidence_identity_and_invalid_budgets_are_rejected_before_login(self):
        with self.journal() as journal:
            journal.identity["recipe_sha256"] = "f" * 64
            with self.assertRaises(ConfigurationError):
                self.run_pass(journal)
        self.assertEqual([], self.transport.requests)
        for value in (True, 0, 1800.001, float("nan"), float("inf"), 10**400):
            with self.journal(
                "budget-" + str(len(list(self.root.iterdir())))
            ) as journal:
                with self.assertRaises(ConfigurationError):
                    self.run_pass(journal, deadline_seconds=value)
        self.assertEqual([], self.transport.requests)

    def test_nonresumable_status_is_rejected_before_enable_not_guessed_pending(self):
        for status in ("fail", "skipped", "pending", "", "processing", "DONE"):
            with self.subTest(status=status):
                row = next(iter(self.transport.documents.values()))
                row["content_chunk_status"] = status
                with self.journal(
                    "status-" + str(len(list(self.root.iterdir())))
                ) as journal:
                    with self.assertRaisesRegex(ProtocolError, "non-resumable"):
                        self.run_pass(journal)
                self.assertEqual([], self.transport.scheduler_updates)
                self.assertFalse(self.transport.token_active)

    def test_zero_progress_and_success_marker_only_do_not_create_clean_result(self):
        self.transport.limit = 0
        with self.journal() as journal:
            with self.assertRaisesRegex(VectorBuildError, "empty"):
                self.run_pass(journal)
            rows = evidence.read_events(journal.path)
            self.assertNotIn("pass-result", [row["event"] for row in rows])
            with self.assertRaises(evidence.JournalError):
                evidence.validate_all(rows, require_result=True)
        self.assertFalse(self.transport.token_active)
        self.assertFalse(self.transport.scheduler_available)

    def test_done_count_growth_cannot_hide_regression_or_changed_after_reads(self):
        entries = self.built.bundle.expected_inventory_entries
        before = self.progress({entries[0][0]})
        after = self.progress({entries[1][0], entries[2][0]})
        cases = (
            ([before, after, after], "non-monotonic"),
            ([self.progress(set()), before, after], "proof reads"),
        )
        for number, (values, error) in enumerate(cases):
            self.client = self.make_client(vector_progress=ScriptedProgress(values))
            with self.journal("regression-" + str(number)) as journal:
                with self.assertRaisesRegex(VectorBuildError, error):
                    self.run_pass(journal)
            self.assertFalse(self.transport.token_active)
            self.assertFalse(self.transport.scheduler_available)

    def test_progress_adapter_must_return_typed_exhaustive_partition(self):
        with self.assertRaises(ProtocolError):
            VectorProgressSnapshot(
                self.progress(set()).inventory,
                self.built.bundle.expected_inventory_entries[:1],
                (),
            )
        self.client = self.make_client(
            vector_progress=ScriptedProgress([self.progress(set()).as_dict()])
        )
        with (
            self.journal() as journal,
            self.assertRaisesRegex(VectorBuildError, "sealed inventory"),
        ):
            self.run_pass(journal)
        self.assertEqual([], self.transport.scheduler_updates)

    def test_progress_larger_than_the_fixed_production_pass_is_not_success(self):
        entries = tuple(
            (f"https://data.depositar.io/dataset/{number:04d}", "a" * 64, "b" * 64)
            for number in range(MAX_VECTOR_PASS_DOCUMENTS + 1)
        )
        inventory = InventorySnapshot(entries)
        before = VectorProgressSnapshot(inventory, (), entries)
        after = VectorProgressSnapshot(inventory, entries, ())
        with self.assertRaisesRegex(VectorBuildError, "cap"):
            self.client._verify_vector_progress(before, after, no_op=False)

    def test_scheduler_detail_running_state_is_checked_not_just_the_listing(self):
        # The listing fixture says false; the exact detail says running. Do not
        # turn that inconsistent observation into a scheduler-verified record.
        self.transport.vector_job_running = True
        with self.journal() as journal:
            with self.assertRaisesRegex(VectorBuildError, "disabled and stopped"):
                self.run_pass(journal)
            self.assertNotIn(
                "scheduler-verified",
                [row["event"] for row in evidence.read_events(journal.path)],
            )
        self.assertEqual([], self.transport.scheduler_updates)
        self.assertFalse(self.transport.token_active)

    def test_timeout_known_job_stops_once_but_never_grants_resume(self):
        self.transport.vector_job_status = "running"
        with self.journal() as journal:
            with self.assertRaises(VectorBuildError) as error:
                self.run_pass(journal, deadline_seconds=61)
            self.assertFalse(error.exception.possibly_polluted)
            rows = evidence.read_events(journal.path)
            self.assertIn("scheduler-stop-intent", [row["event"] for row in rows])
            self.assertNotIn("pass-result", [row["event"] for row in rows])
        self.assertEqual(1, self.transport.stop_calls)
        self.assertFalse(self.transport.vector_job_running)
        self.assertFalse(self.transport.token_active)

    def test_lost_start_keeps_quarantine_without_restarting_or_stopping_unknown_child(
        self,
    ):
        self.transport.start_commits_then_times_out = True
        with self.journal() as journal:
            with self.assertRaises(VectorBuildError) as error:
                self.run_pass(journal)
            self.assertTrue(error.exception.possibly_polluted)
            rows = evidence.read_events(journal.path)
            self.assertNotIn("job-started", [row["event"] for row in rows])
            self.assertNotIn("pass-result", [row["event"] for row in rows])
        self.assertEqual(0, self.transport.stop_calls)
        self.assertEqual(
            1,
            sum(
                request.url.endswith("/vector-scheduler/start")
                for request in self.transport.requests
            ),
        )
        self.assertFalse(self.transport.token_active)

    def test_durable_intents_precede_every_initial_remote_mutation(self):
        with self.journal() as journal:
            original = self.transport.send

            def inspect_intent(request, **kwargs):
                path = urllib.parse.urlsplit(request.url).path
                expected = None
                if path == "/admin/accesstoken/" and request.method == "POST":
                    expected = "token-create-intent"
                elif path == "/api/admin/scheduler/setting" and request.method == "PUT":
                    body = json.loads(request.body)
                    expected = (
                        "scheduler-enable-intent"
                        if body["available"] == "true"
                        else "scheduler-restore-intent"
                    )
                elif path.endswith("/vector-scheduler/start"):
                    expected = "job-start-intent"
                elif (
                    path.startswith("/api/admin/accesstoken/setting/")
                    and request.method == "DELETE"
                ):
                    expected = "token-cleanup-intent"
                elif path == "/api/v2/auth/logout":
                    expected = "logout-intent"
                if expected:
                    names = [row["event"] for row in evidence.read_events(journal.path)]
                    self.assertIn(expected, names)
                return original(request, **kwargs)

            self.transport.send = inspect_intent
            report = self.run_pass(journal)
            self.assert_clean(journal, report)

    def test_failed_start_intent_does_not_start_and_still_cleans_existing_resources(
        self,
    ):
        with self.journal() as journal:
            append = journal.append

            def broken(event, payload):
                if event == "job-start-intent":
                    raise OSError("a private path or secret must not escape")
                append(event, payload)

            journal.append = broken
            with self.assertRaises(VectorBuildError) as error:
                self.run_pass(journal)
            self.assertNotIn("private path", str(error.exception))
        self.assertFalse(self.transport.scheduler_started)
        self.assertFalse(self.transport.scheduler_available)
        self.assertFalse(self.transport.token_active)

    def test_cleanup_evidence_write_failure_does_not_strand_a_token(self):
        with self.journal() as journal:
            append = journal.append

            def broken(event, payload):
                if event == "token-cleanup-intent":
                    raise OSError("private-path")
                append(event, payload)

            journal.append = broken
            with self.assertRaises(TokenCleanupError):
                self.run_pass(journal)
            self.assertNotIn(
                "pass-result",
                [row["event"] for row in evidence.read_events(journal.path)],
            )
        self.assertFalse(self.transport.token_active)
        self.assertFalse(self.transport.scheduler_available)

    def test_failed_restore_intent_still_disables_scheduler_but_cannot_return_clean(
        self,
    ):
        with self.journal() as journal:
            append = journal.append

            def broken(event, payload):
                if event == "scheduler-restore-intent":
                    raise OSError("private-path")
                append(event, payload)

            journal.append = broken
            with self.assertRaises(VectorBuildError):
                self.run_pass(journal)
            self.assertNotIn(
                "pass-result",
                [row["event"] for row in evidence.read_events(journal.path)],
            )
        self.assertFalse(self.transport.token_active)
        self.assertFalse(self.transport.scheduler_available)

    def test_failed_token_creation_intent_never_creates_remote_credential(self):
        with self.journal() as journal:
            append = journal.append

            def broken(event, payload):
                if event == "token-create-intent":
                    raise OSError("private-path")
                append(event, payload)

            journal.append = broken
            with self.assertRaises(VectorBuildError):
                self.run_pass(journal)
        self.assertEqual([], self.transport.created_token_expiries)
        self.assertFalse(self.transport.token_active)

    def test_cleanup_token_failure_never_returns_partial_clean(self):
        self.transport.revoke_fails = True
        with self.journal() as journal:
            with self.assertRaises(TokenCleanupError):
                self.run_pass(journal)
            self.assertNotIn(
                "pass-result",
                [row["event"] for row in evidence.read_events(journal.path)],
            )

    def test_new_cli_has_private_identity_arguments_and_1200_default(self):
        script = Path(__file__).resolve().parents[1] / "catalog_admin.py"
        spec = importlib.util.spec_from_file_location("vector_pass_cli", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        prefix = [
            "--bundle",
            str(self.built.path),
            "--manifest-sha256",
            self.built.manifest_sha256,
            "vector-pass",
            "--journal-dir",
            str(self.root / "journal"),
            "--run-id",
            RUN_ID,
            "--recipe-sha256",
            RECIPE,
        ]
        self.assertEqual(1200.0, module.parse_args(prefix).deadline_seconds)
        self.assertEqual(
            "0" * 64,
            module.parse_args(
                prefix + ["--expected-before-state-sha256", "0" * 64]
            ).expected_before_state_sha256,
        )
        with (
            patch.object(module, "read_admin_secret", return_value=PASSWORD),
            patch.object(module, "VectorBuilder", return_value=self.client),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            (self.root / "journal").mkdir(mode=0o700)
            self.assertEqual(0, module.main(prefix))
            report = json.loads(output.getvalue())
            evidence.validate_report(report)
            self.assertEqual("partial-clean", report["status"])
        for value in ("nan", "0", "1800.001"):
            outcome = subprocess.run(
                [
                    sys.executable,
                    "-E",
                    "-s",
                    "-B",
                    "-u",
                    "-m",
                    "search.catalog_admin",
                    *prefix,
                    "--deadline-seconds",
                    value,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(2, outcome.returncode)
            self.assertIn(b"deadline-seconds", outcome.stderr)


if __name__ == "__main__":
    unittest.main()
