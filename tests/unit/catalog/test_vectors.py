"""Host vector checkpoints: fake Docker only, real private receipt writes."""

from __future__ import annotations

from search.catalog.admin import AdminAccess
from search.catalog.errors import VectorBuildError
from search.catalog.vectors import VectorBuilder

import copy
import datetime
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search import vector_journal
from search.operations.catalog import execution, vectors
from search.operations.catalog.policy import admin_arguments
from search.operations.shared import private_io
from search.tests.catalog_fixtures import BuiltBundle, ManualClock, NONCE, PASSWORD
from search.tests.test_vector_pass import PartialTransport


def host_context(root, bundle, manifest_sha256, document_count):
    return {
        "root": root,
        "data": root,
        "plan": {
            "schema_version": 1,
            "project": "depositar-ai-catalog-" + "a" * 12 + "-" + "b" * 12,
            "revision": "b" * 40,
            "manifest_sha256": manifest_sha256,
            "bundle_path": str(root / "bundle"),
            "document_count": document_count,
            "compose_sha256": "d" * 64,
            "security_material_sha256": "e" * 64,
        },
        "bundle": bundle,
        "compose": {
            "services": {
                "fess-search": {
                    "environment": {
                        "FESS_JAVA_OPTS": " ".join(
                            (
                                "-Dfess.system.content_chunker.job.max_documents_per_run=256",
                                "-Dfess.system.content_chunker.job.concurrency=2",
                                "-Dfess.system.content_chunker.job.bulk_size=100",
                            )
                        )
                    }
                }
            }
        },
    }


class VectorCoordinatorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-vector-host-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.context = host_context(
            self.root,
            SimpleNamespace(bundle_id="f" * 64, expected_inventory_entries=()),
            "c" * 64,
            2,
        )
        self.image = {
            "image_id": "sha256:" + "a" * 64,
            "reference": "test:1",
            "labels": {},
        }
        self.secret = self.root / "secret"
        self.container_id = "a" * 64
        self.runtime = {"container_ids": {"fess-search": "b" * 64}}
        self.calls, self.last_identity, self.remaining = [], None, ""
        self.result_change, self.rm_failure, self.helper_failure = None, False, False
        self.addCleanup(patch.stopall)
        patch.object(
            execution, "require_runtime", return_value=(self.runtime, {})
        ).start()
        patch.object(execution, "verify_image").start()
        patch.object(
            vectors.administration, "_admin_create", side_effect=self.create
        ).start()
        patch.object(
            vectors.administration, "_inspect_admin", side_effect=self.inspect
        ).start()
        patch.object(vectors, "_clean_events", return_value=()).start()
        self.context["runner"] = self.runner
        self.history_root = self.root / "vector-operations"
        self.history_root.mkdir(mode=0o700)

    def report(self, identity):
        return {
            "schema_version": 2,
            "status": "complete-clean",
            **{
                key: identity[key]
                for key in ("run_id", "recipe_sha256", "bundle_id", "manifest_sha256")
            },
            "document_count": 2,
            "before_done_count": 0,
            "done_count": 2,
            "pending_count": 0,
            "progress_count": 2,
            "before_state_sha256": "1" * 64,
            "after_state_sha256": "2" * 64,
            "job_name": "Content Chunk Vector Indexer",
            "job_id": "job-1",
            "scheduler_id": "scheduler-1",
            "elapsed_seconds": 1.0,
            "scheduler_restored_disabled": True,
            "job_terminal_verified": True,
            "token_cleanup_verified": True,
            "progress_verified": True,
            "no_op": False,
            "generation_enabled": False,
        }

    def create(self, context, image, identity, fess, secret, *, journal):
        self.last_identity = identity
        self.assertEqual(self.history_root / identity["run_id"] / "events", journal)
        self.assertTrue((journal.parent / "started.json").is_file())
        return ["docker", "create"]

    def inspect(self, *args, **kwargs):
        started = any(argv[1] == "start" for argv in self.calls)
        return {
            "State": {
                "Status": "exited" if started else "created",
                "Running": False,
                "ExitCode": 0,
            }
        }

    def runner(self, argv, *, timeout):
        self.calls.append(argv)
        if argv[1] == "create":
            value = self.container_id
        elif argv[1] == "start":
            if self.helper_failure:
                raise subprocess.TimeoutExpired(argv, timeout)
            report = self.report(self.last_identity)
            if self.result_change:
                self.result_change(report)
            value = json.dumps(report) + "\n"
        elif argv[1] == "rm":
            self.assertEqual(["docker", "rm", self.container_id], argv)
            self.assertFalse(
                (
                    self.history_root / self.last_identity["run_id"] / "complete.json"
                ).exists()
            )
            if self.rm_failure:
                raise subprocess.TimeoutExpired(argv, timeout)
            value = self.container_id
        elif argv[1] == "ps":
            self.assertEqual("id=" + self.container_id, argv[-1])
            value = self.remaining
        else:
            self.fail("unexpected fake Docker operation")
        return subprocess.CompletedProcess(argv, 0, value, "")

    def run_pass(self, previous=None):
        return vectors._one_pass(
            self.context,
            self.image,
            self.secret,
            self.history_root,
            "3" * 64,
            1,
            previous,
        )

    def final_exists(self):
        return (
            self.history_root / self.last_identity["run_id"] / "complete.json"
        ).exists()

    def test_completion_follows_ordinary_removal_and_exact_absence(self):
        self.assertEqual("complete-clean", self.run_pass()["status"])
        self.assertTrue(self.final_exists())
        self.assertEqual(
            ["create", "start", "rm", "ps"], [argv[1] for argv in self.calls]
        )
        receipt = execution.read_record(
            self.history_root / self.last_identity["run_id"], "complete.json"
        )
        self.assertIs(receipt["helper_removed_verified"], True)

    def test_rm_failure_does_not_grant_resume_and_preserves_evidence(self):
        self.rm_failure = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_pass()
        self.assertFalse(self.final_exists())
        directory = self.history_root / self.last_identity["run_id"]
        self.assertTrue((directory / "failure.json").is_file())
        self.assertTrue((directory / "container.json").is_file())
        self.assertNotIn("ps", [argv[1] for argv in self.calls])

    def test_remaining_exact_container_blocks_completion(self):
        self.remaining = self.container_id + "\n"
        with self.assertRaisesRegex(ValueError, "removal_unverified"):
            self.run_pass()
        self.assertFalse(self.final_exists())

    def test_running_or_timed_out_helper_is_never_force_removed(self):
        self.helper_failure = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_pass()
        self.assertEqual(["create", "start"], [argv[1] for argv in self.calls])
        self.assertFalse(self.final_exists())

    def test_invalid_report_or_progress_cannot_trigger_removal(self):
        for mutate in (
            lambda report: report.update(token_cleanup_verified=False),
            lambda report: report.update(document_count=3),
            lambda report: report.update(progress_count=0),
            lambda report: report.update(elapsed_seconds=float("nan")),
        ):
            with self.subTest(mutate=mutate):
                self.calls.clear()
                self.result_change = mutate
                with self.assertRaises(ValueError):
                    self.run_pass()
                self.assertFalse(self.final_exists())
                self.assertNotIn("rm", [argv[1] for argv in self.calls])

    def test_missing_journal_cleanup_evidence_blocks_completion(self):
        with patch.object(
            vectors, "_clean_events", side_effect=ValueError("missing-proof")
        ):
            with self.assertRaises(ValueError):
                self.run_pass()
        self.assertNotIn("rm", [argv[1] for argv in self.calls])
        self.assertFalse(self.final_exists())

    def test_checkpoint_mismatch_is_detected_and_expected_digest_is_passed(self):
        with self.assertRaisesRegex(ValueError, "checkpoint_changed"):
            self.run_pass({"after_state_sha256": "4" * 64})
        self.assertEqual("4" * 64, self.last_identity["expected_before_state_sha256"])
        self.assertFalse(self.final_exists())

    def test_host_receipt_write_failure_after_removal_is_not_success(self):
        original = execution.write_record

        def fail_final(root, name, value, **kwargs):
            if name == "complete.json":
                raise OSError("fsync failed")
            return original(root, name, value, **kwargs)

        with patch.object(execution, "write_record", side_effect=fail_final):
            with self.assertRaises(OSError):
                self.run_pass()
        self.assertFalse(self.final_exists())
        self.assertIn("rm", [argv[1] for argv in self.calls])

    def test_unresolved_run_refuses_history_resume(self):
        vectors._history(self.context, self.image, {})
        directory = self.history_root / ("0" * 32)
        directory.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "explicit_recovery"):
            vectors._history(self.context, self.image, {})

    def test_recipe_change_is_not_accepted_as_noop(self):
        vectors._history(self.context, self.image, {"recipe": 1})
        with self.assertRaisesRegex(ValueError, "recipe_changed"):
            vectors._history(self.context, self.image, {"recipe": 2})

    def test_host_receipt_types_are_not_bool_integer_aliases(self):
        for schema, changed in ((True, False), (1, 0), (1.0, False)):
            with self.subTest(schema=schema, active_changed=changed):
                execution.write_record(
                    self.root,
                    "typed.json",
                    {"schema_version": schema, "active_changed": changed},
                    replace=True,
                )
                with self.assertRaisesRegex(ValueError, "host_receipt_invalid"):
                    vectors._host_record(self.root, "typed.json")

    def test_recipe_true_is_not_equivalent_to_integer_one(self):
        vectors._history(self.context, self.image, {"value": 1})
        with self.assertRaisesRegex(ValueError, "recipe_changed"):
            vectors._history(self.context, self.image, {"value": True})

    def test_budget_bounds_fail_before_context_or_docker(self):
        for max_passes, wall_seconds in (
            (0, 7200),
            (41, 7200),
            (True, 7200),
            (16, 1200),
            (16, 36001),
        ):
            with self.subTest(max_passes=max_passes, wall_seconds=wall_seconds):
                with self.assertRaisesRegex(ValueError, "budget_invalid"):
                    vectors.build(
                        None,
                        {},
                        repo_root=self.root,
                        max_passes=max_passes,
                        wall_seconds=wall_seconds,
                    )
        self.assertEqual([], self.calls)

    def test_command_mount_and_expected_checkpoint_are_closed(self):
        identity = {
            **execution.catalog_identity(self.context),
            "operation": "vector-pass",
            "run_id": "a" * 32,
            "recipe_sha256": "b" * 64,
            "expected_before_state_sha256": "c" * 64,
        }
        journal = self.history_root / identity["run_id"] / "events"
        command = admin_arguments(self.context, identity, journal=journal)
        self.assertIn("1200", command)
        self.assertEqual(["--expected-before-state-sha256", "c" * 64], command[-2:])
        with self.assertRaises(ValueError):
            admin_arguments(self.context, identity, journal=self.root)
        changed = copy.deepcopy(identity)
        changed["expected_before_state_sha256"] = "invalid"
        with self.assertRaises(ValueError):
            admin_arguments(self.context, changed, journal=journal)


class DurableVectorHistoryTests(unittest.TestCase):
    """Real helper/journal/history chain; service and preflight IO are fake."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-vector-history-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.built = BuiltBundle(count=3)
        self.addCleanup(self.built.close)
        self.context = host_context(
            self.root, self.built.bundle, self.built.manifest_sha256, 3
        )
        self.context["plan"]["bundle_path"] = str(self.built.path)
        self.context["runner"] = self.runner
        self.image = {
            "image_id": "sha256:" + "a" * 64,
            "reference": "test:1",
            "labels": {},
        }
        self.secret = self.root / "unused-synthetic-secret-path"
        self.runtime = {"container_ids": {"fess-search": "b" * 64}}
        self.clock = ManualClock()
        seed_patch = patch.object(
            vectors.reuse, "seed", return_value={"reused_count": 0}
        )
        self.seed_mock = seed_patch.start()
        self.addCleanup(seed_patch.stop)
        self.transport = PartialTransport()
        self.transport.documents = {
            row["url"]: dict(row) for row in self.built.bundle.documents
        }
        self.client = VectorBuilder(
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
        )
        self.calls, self.helpers = [], {}
        self.recipe = vectors._recipe(self.context)
        self.history_root = self.root / "vector-operations"
        state = self.root / "state"
        state.mkdir(mode=0o700)
        execution.write_record(state, "operations.lock", {})
        for target, name, arguments in (
            (execution, "require_runtime", {"return_value": (self.runtime, {})}),
            (execution, "verify_image", {"return_value": None}),
            (
                vectors.administration,
                "_inspect_admin",
                {"side_effect": self.inspect},
            ),
        ):
            mock = patch.object(target, name, **arguments)
            mock.start()
            self.addCleanup(mock.stop)

    def runner(self, argv, *, timeout):
        self.calls.append(argv)
        if argv[1] == "create":
            run_id = argv[argv.index("--run-id") + 1]
            identity = execution.read_record(self.history_root / run_id, "started.json")
            container_id = run_id * 2
            self.helpers[container_id] = {
                "identity": identity,
                "arguments": argv,
                "status": "created",
                "removed": False,
            }
            value = container_id
        elif argv[1] == "start":
            self.assertEqual(vectors.HELPER_TIMEOUT, timeout)
            helper = self.helpers[argv[-1]]
            identity, arguments = helper["identity"], helper["arguments"]
            expected_before = (
                arguments[arguments.index("--expected-before-state-sha256") + 1]
                if "--expected-before-state-sha256" in arguments
                else None
            )
            journal_path = self.history_root / identity["run_id"] / "events"
            helper["status"] = "running"
            try:
                with vector_journal.DurableVectorJournal(
                    journal_path, vectors._journal_identity(identity)
                ) as journal:
                    report = self.client.build_vector_pass(
                        "admin",
                        PASSWORD,
                        journal=journal,
                        run_id=identity["run_id"],
                        recipe_sha256=identity["recipe_sha256"],
                        expected_before_state_sha256=expected_before,
                        nonce=NONCE,
                        deadline_seconds=float(
                            arguments[arguments.index("--deadline-seconds") + 1]
                        ),
                    )
                value = private_io.canonical(report.as_dict()).decode("utf-8")
            finally:
                helper["status"] = "exited"
        elif argv[1] == "rm":
            helper = self.helpers[argv[-1]]
            self.assertEqual("exited", helper["status"])
            self.assertFalse(
                (
                    self.history_root / helper["identity"]["run_id"] / "complete.json"
                ).exists()
            )
            helper["removed"] = True
            value = argv[-1]
        elif argv[1] == "ps":
            if argv[-1].startswith("label="):
                value = "\n".join(
                    identifier
                    for identifier, helper in self.helpers.items()
                    if not helper["removed"]
                )
            else:
                container_id = argv[-1].removeprefix("id=")
                value = "" if self.helpers[container_id]["removed"] else container_id
        else:
            self.fail("unexpected fake Docker operation")
        return subprocess.CompletedProcess(argv, 0, value, "")

    def inspect(self, _context, _image, identity, container_id, *_args, **_kwargs):
        helper = self.helpers[container_id]
        self.assertEqual(
            identity,
            {
                key: value
                for key, value in helper["identity"].items()
                if key != "status"
            },
        )
        return {
            "State": {
                "Status": helper["status"],
                "Running": False,
                "ExitCode": 0,
            }
        }

    def history(self):
        return vectors._history(self.context, self.image, self.recipe)

    def run_pass(self, limit):
        self.transport.limit = limit
        root, recipe_hash, ordinal, previous = self.history()
        return vectors._one_pass(
            self.context,
            self.image,
            self.secret,
            root,
            recipe_hash,
            ordinal + 1,
            previous,
        )

    def build(self, *, wall_seconds=3600):
        def preflight(*_args, **_kwargs):
            self.clock.sleep(1)

        with (
            patch.object(execution, "load_context", return_value=self.context),
            patch.object(vectors.administration, "_admin_ids", return_value=[]),
            patch.object(
                vectors.administration, "_admin_image", return_value=self.image
            ),
            patch.object(vectors.administration, "_secret", return_value=self.secret),
            patch("search.operations.doctor._embedding_model", side_effect=preflight),
        ):
            return vectors.build(
                None,
                self.context["plan"],
                repo_root=self.root,
                runner=self.runner,
                max_passes=1,
                wall_seconds=wall_seconds,
                clock=self.clock,
            )

    def directory(self, report):
        return self.history_root / report["run_id"]

    def assert_no_scheduler_mutation_since(self, request_count):
        self.assertFalse(
            any(
                request.url.endswith("/start")
                or (
                    request.method == "PUT"
                    and request.url.endswith("/scheduler/setting")
                )
                for request in self.transport.requests[request_count:]
            )
        )

    def replace_record(self, directory, name, mutate):
        value = execution.read_record(directory, name)
        mutate(value)
        execution.write_record(directory, name, value, replace=True)

    def rewrite_events(self, report, mutate):
        """Model canonical but untrusted evidence, not just broken JSON/hashes."""
        directory = self.directory(report) / "events"
        records = copy.deepcopy(list(vector_journal.read_events(directory)))
        mutate(records)
        previous = "0" * 64
        for record in records:
            record["previous_sha256"] = previous
            body = {key: value for key, value in record.items() if key != "sha256"}
            record["sha256"] = hashlib.sha256(private_io.canonical(body)).hexdigest()
            execution.write_record(
                directory,
                f"{record['sequence']:04d}-{record['event']}.json",
                record,
                replace=True,
            )
            previous = record["sha256"]
        return records

    def test_two_durable_clean_passes_resume_and_complete_history(self):
        first = self.run_pass(1)
        self.assertEqual(
            ("partial-clean", 1, 2),
            (first["status"], first["done_count"], first["pending_count"]),
        )
        self.assertEqual((1, first), self.history()[2:])
        second = self.run_pass(2)
        self.assertEqual(
            ("complete-clean", 3, 0),
            (second["status"], second["done_count"], second["pending_count"]),
        )
        self.assertEqual(first["after_state_sha256"], second["before_state_sha256"])
        self.assertEqual((2, second), self.history()[2:])
        for report in (first, second):
            records = vector_journal.read_events(self.directory(report) / "events")
            self.assertEqual(
                vector_journal.NORMAL_EVENTS, tuple(row["event"] for row in records)
            )
            self.assertEqual(report, records[-1]["payload"])
            self.assertTrue(self.helpers[report["run_id"] * 2]["removed"])

    def test_completed_history_accepts_verified_fresh_noop(self):
        completed = self.run_pass(3)
        request_count = len(self.transport.requests)
        noop = self.run_pass(0)
        self.assertTrue(noop["no_op"])
        self.assertIsNone(noop["job_id"])
        self.assertEqual(0, noop["progress_count"])
        self.assertEqual(completed["after_state_sha256"], noop["before_state_sha256"])
        self.assertEqual(noop["before_state_sha256"], noop["after_state_sha256"])
        self.assertEqual((2, noop), self.history()[2:])
        records = vector_journal.read_events(self.directory(noop) / "events")
        self.assertEqual(
            vector_journal.NOOP_EVENTS, tuple(row["event"] for row in records)
        )
        observed = next(
            row["payload"] for row in records if row["event"] == "scheduler-verified"
        )
        self.assertEqual(
            {
                "scheduler_id": noop["scheduler_id"],
                "available": "false",
                "running": False,
            },
            observed,
        )
        self.assert_no_scheduler_mutation_since(request_count)

    def test_build_reuses_durable_checkpoint_as_helper_preflight_digest(self):
        first = self.run_pass(1)
        self.transport.limit = 2
        result = self.build()
        self.assertEqual("vectors-complete", result["status"])
        self.assertEqual(
            (1, 2, 3, 0),
            (
                result["passes_this_invocation"],
                result["completed_passes"],
                result["done_count"],
                result["pending_count"],
            ),
        )
        self.assertTrue(result["host_cleanup_verified"])
        second = self.history()[3]
        arguments = self.helpers[second["run_id"] * 2]["arguments"]
        self.assertEqual(
            first["after_state_sha256"],
            arguments[arguments.index("--expected-before-state-sha256") + 1],
        )
        self.assertEqual(first["after_state_sha256"], second["before_state_sha256"])
        self.seed_mock.assert_not_called()

    def test_first_pass_observes_reused_prefix_and_builds_only_the_remainder(self):
        def seed(*_args, **_kwargs):
            for row in list(self.transport.documents.values())[:2]:
                row["content_chunk_status"] = "done"
            return {"reused_count": 2}

        self.seed_mock.side_effect = seed
        self.transport.limit = 1
        result = self.build()
        self.assertEqual("vectors-complete", result["status"])
        self.assertEqual({"reused_count": 2}, result["vector_reuse"])
        final = self.history()[3]
        self.assertEqual(2, final["before_done_count"])
        self.assertEqual(1, final["progress_count"])
        self.assertEqual(3, final["done_count"])
        self.seed_mock.assert_called_once()

    def test_all_reused_documents_keep_the_normal_no_op_cleanup_proof(self):
        def seed(*_args, **_kwargs):
            for row in self.transport.documents.values():
                row["content_chunk_status"] = "done"
            return {"reused_count": 3}

        self.seed_mock.side_effect = seed
        result = self.build()
        self.assertEqual("vectors-complete", result["status"])
        self.assertEqual(1, result["passes_this_invocation"])
        final = self.history()[3]
        self.assertTrue(final["no_op"])
        self.assertEqual(3, final["before_done_count"])
        self.assertEqual(0, final["progress_count"])
        self.assertTrue(result["host_cleanup_verified"])
        self.assertTrue(final["token_cleanup_verified"])
        self.assert_no_scheduler_mutation_since(0)

    def test_cache_preparation_cannot_consume_the_reserved_helper_wall_budget(self):
        self.seed_mock.side_effect = lambda *_args, **_kwargs: self.clock.sleep(1201)
        with patch.object(
            vectors, "_one_pass", side_effect=AssertionError("over budget")
        ):
            result = self.build(wall_seconds=3600)
        self.assertEqual("checkpoint", result["status"])
        self.assertEqual(0, result["passes_this_invocation"])
        self.assertEqual(0, result["completed_passes"])
        self.assertEqual({}, self.helpers)

    def test_build_complete_catalog_reverifies_without_a_new_helper_or_http(self):
        self.readonly_admin_evidence()
        completed = self.run_pass(3)
        before = self.filesystem_snapshot()
        request_count, command_count = len(self.transport.requests), len(self.calls)
        with patch.object(
            vectors, "_one_pass", side_effect=AssertionError("extra pass")
        ):
            result = self.build()
        self.assertEqual("vectors-complete", result["status"])
        self.assertEqual(0, result["passes_this_invocation"])
        self.assertEqual(1, result["completed_passes"])
        self.assertEqual(completed["after_state_sha256"], result["after_state_sha256"])
        self.assertTrue(result["runtime_verified"])
        self.assertTrue(result["host_cleanup_verified"])
        self.assertEqual(before, self.filesystem_snapshot())
        self.assertEqual(request_count, len(self.transport.requests))
        self.assertTrue(
            all(argv[:2] == ["docker", "ps"] for argv in self.calls[command_count:])
        )

    def test_cached_complete_catalog_still_rejects_runtime_drift_before_new_work(self):
        self.readonly_admin_evidence()
        self.run_pass(3)
        before = self.filesystem_snapshot()
        with (
            patch.object(
                execution, "require_runtime", side_effect=ValueError("runtime changed")
            ),
            patch.object(
                vectors, "_one_pass", side_effect=AssertionError("extra pass")
            ),
            self.assertRaisesRegex(ValueError, "runtime changed"),
        ):
            self.build()
        self.assertEqual(before, self.filesystem_snapshot())

    def test_drift_from_checkpoint_blocks_scheduler_enable_and_start(self):
        self.run_pass(1)
        pending = next(
            row
            for row in self.transport.documents.values()
            if row.get("content_chunk_status") is None
        )
        pending["content_chunk_status"] = "done"
        call_count, request_count = len(self.calls), len(self.transport.requests)
        with self.assertRaisesRegex(VectorBuildError, "resume checkpoint"):
            self.run_pass(1)
        self.assertFalse(self.transport.token_active)
        self.assert_no_scheduler_mutation_since(request_count)
        self.assertNotIn("rm", [argv[1] for argv in self.calls[call_count:]])
        failed_run = self.calls[-1][-1][:32]
        self.assertTrue((self.history_root / failed_run / "failure.json").is_file())
        self.assertFalse((self.history_root / failed_run / "complete.json").exists())
        with self.assertRaisesRegex(ValueError, "explicit_recovery"):
            self.history()

    def test_history_rejects_checkpoint_changed_consistently_in_host_receipts(self):
        self.run_pass(1)
        second = self.run_pass(1)
        for name in ("started.json", "container.json", "complete.json"):
            self.replace_record(
                self.directory(second),
                name,
                lambda value: value.update(expected_before_state_sha256="0" * 64),
            )
        with self.assertRaisesRegex(ValueError, "progress_checkpoint_changed"):
            self.history()

    def test_history_rejects_report_detached_from_durable_result(self):
        report = self.run_pass(1)
        self.replace_record(
            self.directory(report),
            "complete.json",
            lambda value: value["result"].update(
                elapsed_seconds=value["result"]["elapsed_seconds"] + 1
            ),
        )
        with self.assertRaisesRegex(ValueError, "journal_result_mismatch"):
            self.history()

    def test_history_rejects_durable_event_digest_tampering(self):
        report = self.run_pass(1)
        self.replace_record(
            self.directory(report) / "events",
            "0018-pass-result.json",
            lambda value: value["payload"].update(
                elapsed_seconds=value["payload"]["elapsed_seconds"] + 1
            ),
        )
        with self.assertRaisesRegex(vector_journal.JournalError, "digest_changed"):
            self.history()

    def test_history_rejects_removed_helper_proof_revocation(self):
        report = self.run_pass(1)
        self.replace_record(
            self.directory(report),
            "complete.json",
            lambda value: value.update(helper_removed_verified=False),
        )
        with self.assertRaisesRegex(ValueError, "host_cleanup_receipt_changed"):
            self.history()

    def test_history_host_proof_types_do_not_accept_boolean_integer_aliases(self):
        report = self.run_pass(1)
        directory = self.directory(report)
        for name, field, value in (
            ("complete.json", "helper_removed_verified", 1),
            ("complete.json", "helper_removed_verified", 1.0),
            ("complete.json", "pass_number", True),
            ("complete.json", "pass_number", 1.0),
            ("container.json", "pass_number", True),
            ("container.json", "pass_number", 1.0),
        ):
            with self.subTest(receipt=name, field=field, value=value):
                original = execution.read_record(directory, name)
                altered = copy.deepcopy(original)
                altered[field] = value
                execution.write_record(directory, name, altered, replace=True)
                try:
                    with self.assertRaisesRegex(
                        ValueError, "host_cleanup_receipt_changed"
                    ):
                        self.history()
                finally:
                    execution.write_record(directory, name, original, replace=True)
        self.assertEqual((1, report), self.history()[2:])

    def test_history_rejects_valid_chain_for_foreign_document_inventory(self):
        report = self.run_pass(1)

        def change_inventory(records):
            states = {}
            for record in records:
                if record["event"] in {"before-state", "after-state"}:
                    state = record["payload"]
                    for row in (*state["done_entries"], *state["pending_entries"]):
                        row[2] = "0" * 64
                    states[record["event"]] = state
            records[-1]["payload"].update(
                before_state_sha256=vector_journal.state_sha256(states["before-state"]),
                after_state_sha256=vector_journal.state_sha256(states["after-state"]),
            )

        records = self.rewrite_events(report, change_inventory)
        vector_journal.validate_all(records, require_result=True)
        self.replace_record(
            self.directory(report),
            "complete.json",
            lambda value: value.update(result=records[-1]["payload"]),
        )
        with self.assertRaisesRegex(ValueError, "journal_inventory_changed"):
            self.history()

    def test_noop_requires_observed_scheduler_identity_matching_report(self):
        self.run_pass(3)
        noop = self.run_pass(0)

        def change_observed_scheduler(records):
            for record in records:
                if record["event"] == "scheduler-verified":
                    record["payload"]["scheduler_id"] = "other-scheduler"

        self.rewrite_events(noop, change_observed_scheduler)
        with self.assertRaisesRegex(
            vector_journal.JournalError, "resource_identity_changed"
        ):
            self.history()

    def test_zero_pass_after_preflight_has_no_host_cleanup_proof(self):
        result = self.build(wall_seconds=2400)
        self.assertEqual("checkpoint", result["status"])
        self.assertEqual(
            (0, 0), (result["passes_this_invocation"], result["completed_passes"])
        )
        self.assertIsNone(result["done_count"])
        self.assertIsNone(result["pending_count"])
        self.assertFalse(result["host_cleanup_verified"])
        self.assertEqual([], self.calls)
        self.assertEqual([], self.transport.requests)

    def test_zero_pass_preserves_previous_counts_without_reasserting_cleanup(self):
        self.run_pass(1)
        call_count, request_count = len(self.calls), len(self.transport.requests)
        result = self.build(wall_seconds=2400)
        self.assertEqual("checkpoint", result["status"])
        self.assertEqual(
            (0, 1, 1, 2),
            (
                result["passes_this_invocation"],
                result["completed_passes"],
                result["done_count"],
                result["pending_count"],
            ),
        )
        self.assertFalse(result["host_cleanup_verified"])
        self.assertEqual(call_count, len(self.calls))
        self.assertEqual(request_count, len(self.transport.requests))

    def readonly_admin_evidence(self):
        """Real private receipts, with committed-source/image IO stubbed only."""
        source = b"synthetic committed importer source\n"
        stub = patch.object(execution, "sealed_source", return_value=source)
        stub.start()
        self.addCleanup(stub.stop)
        sources = {
            name: hashlib.sha256(source).hexdigest()
            for name in vectors.administration.ADMIN_FILES
        }
        self.image.update(
            reference=self.context["plan"]["project"] + "-catalog-admin:1",
            sources=sources,
            labels=execution.image_labels(
                self.context,
                "catalog-admin",
                sources,
                vectors.administration.ADMIN_BASE,
            ),
        )
        execution.write_record(self.root, "admin-image.json", self.image)
        directory = self.root / "admin-operations"
        directory.mkdir(mode=0o700)
        identity = {
            **execution.catalog_identity(self.context),
            "operation": "import",
            "run_id": "9" * 32,
            "admin_image_id": self.image["image_id"],
        }
        execution.write_record(
            directory,
            identity["run_id"] + "-started.json",
            {**identity, "status": "started"},
        )
        execution.write_record(
            directory,
            identity["run_id"] + "-container.json",
            {**identity, "container_id": "c" * 64},
        )
        execution.write_record(
            directory,
            identity["run_id"] + "-complete.json",
            {
                **identity,
                "status": "completed",
                "container_id": "c" * 64,
                "result": {
                    "schema_version": 1,
                    "status": "imported",
                    "bundle_id": self.context["bundle"].bundle_id,
                    "manifest_sha256": self.context["plan"]["manifest_sha256"],
                    "document_count": 3,
                    "batch_count": 1,
                    "elapsed_seconds": 1.0,
                    "inventory_verified": True,
                },
            },
        )
        return directory, identity

    def filesystem_snapshot(self):
        result = {}
        for path in (self.root, *sorted(self.root.rglob("*"))):
            info = path.lstat()
            result[str(path.relative_to(self.root))] = (
                private_io.identity(info),
                hashlib.sha256(path.read_bytes()).hexdigest()
                if stat.S_ISREG(info.st_mode)
                else None,
            )
        return result

    def observe_verify(self):
        """Every success/failure must leave bytes, identities and IO unchanged."""
        before = self.filesystem_snapshot()
        commands, requests = len(self.calls), len(self.transport.requests)
        original_open = private_io.open_directory

        def readonly_open(*args, **kwargs):
            self.assertIsNot(kwargs.get("create"), True)
            return original_open(*args, **kwargs)

        try:
            with (
                patch.object(private_io, "open_directory", side_effect=readonly_open),
                patch.object(
                    execution, "write_record", side_effect=AssertionError("write")
                ),
                patch.object(
                    private_io, "write_json", side_effect=AssertionError("publish")
                ),
                patch.object(
                    execution, "build_image", side_effect=AssertionError("build")
                ),
                patch.object(
                    vectors.administration,
                    "_secret",
                    side_effect=AssertionError("secret"),
                ),
                patch.object(
                    vectors.administration,
                    "_admin_create",
                    side_effect=AssertionError("helper"),
                ),
                patch.object(vectors, "_one_pass", side_effect=AssertionError("pass")),
            ):
                return vectors.verify_complete(self.context)
        finally:
            self.assertEqual(before, self.filesystem_snapshot())
            self.assertEqual(requests, len(self.transport.requests))
            self.assertTrue(
                all(argv[:2] == ["docker", "ps"] for argv in self.calls[commands:])
            )

    def test_readonly_complete_verifies_real_history_without_mutation(self):
        self.readonly_admin_evidence()
        self.run_pass(1)
        completed = self.run_pass(2)
        with patch.object(
            execution, "require_runtime", return_value=(self.runtime, {})
        ) as runtime:
            proof = self.observe_verify()
        self.assertEqual(2, runtime.call_count)
        self.assertEqual("vectors-complete", proof["status"])
        self.assertEqual(
            (2, 3, 0),
            (proof["completed_passes"], proof["done_count"], proof["pending_count"]),
        )
        self.assertEqual(completed["after_state_sha256"], proof["after_state_sha256"])
        self.assertEqual(execution.digest_record(self.runtime), proof["runtime_sha256"])
        self.assertIs(proof["read_only"], True)
        self.assertIs(proof["host_cleanup_verified"], True)
        self.assertIs(proof["active_changed"], False)

    def sealed_runtime_evidence(self):
        images = {
            role: {"image_id": "sha256:" + digit * 64}
            for role, digit in (
                ("fess-search", "1"),
                ("opensearch-search", "2"),
                ("ollama-embedding", "3"),
            )
        }
        overlay = {
            "services": {
                role: {"image": item["image_id"]} for role, item in images.items()
            }
        }
        self.runtime = {
            **execution.catalog_identity(self.context),
            "status": "running",
            "container_ids": {
                role: digit * 64
                for role, digit in (
                    ("fess-search", "4"),
                    ("opensearch-search", "5"),
                    ("ollama-embedding", "6"),
                )
            },
            "network_id": "7" * 64,
            "image_ids": {role: item["image_id"] for role, item in images.items()},
            "image_lock_sha256": execution.digest_record(overlay),
            "images_sha256": execution.digest_record(images),
        }
        for name, value in (
            ("runtime.json", self.runtime),
            ("images.json", images),
            ("image-lock.json", overlay),
        ):
            execution.write_record(self.root, name, value)
        stub = patch.object(execution, "load_images", return_value=images)
        stub.start()
        self.addCleanup(stub.stop)
        return images

    def test_read_complete_evidence_validates_real_history_without_baseline_topology(
        self,
    ):
        self.readonly_admin_evidence()
        self.run_pass(3)
        images = self.sealed_runtime_evidence()
        before = self.filesystem_snapshot()
        with (
            patch.object(
                execution,
                "require_runtime",
                side_effect=AssertionError("baseline cannot authorize leased peers"),
            ),
            patch.object(private_io, "write_file", side_effect=AssertionError("write")),
        ):
            gate, runtime, observed_images = vectors.read_complete_evidence(
                self.context
            )
        self.assertEqual(before, self.filesystem_snapshot())
        self.assertEqual(self.runtime, runtime)
        self.assertEqual(images, observed_images)
        self.assertEqual(3, gate["done_count"])
        self.assertIs(gate["runtime_verified"], False)
        self.assertIs(gate["host_cleanup_verified"], True)

    def test_read_complete_evidence_rejects_incomplete_journal_and_live_helper(self):
        self.readonly_admin_evidence()
        self.run_pass(3)
        self.sealed_runtime_evidence()
        pending = self.history_root / ("0" * 32)
        pending.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "explicit_recovery"):
            vectors.read_complete_evidence(self.context)
        pending.rmdir()
        with patch.object(
            vectors.administration, "_admin_ids", return_value=["a" * 64]
        ):
            with self.assertRaisesRegex(
                ValueError, "existing_helper_requires_recovery"
            ):
                vectors.read_complete_evidence(self.context)

    def test_read_complete_evidence_rejects_changed_runtime_or_image_lock(self):
        self.readonly_admin_evidence()
        self.run_pass(3)
        self.sealed_runtime_evidence()
        original = copy.deepcopy(self.runtime)
        for field, value in (
            ("schema_version", True),
            ("active_changed", 0),
            ("extra", True),
            ("network_id", "invalid"),
            ("image_ids", {}),
            ("container_ids", {"fess-search": "4" * 64}),
        ):
            execution.write_record(
                self.root, "runtime.json", {**original, field: value}, replace=True
            )
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, "runtime_receipt_changed"),
            ):
                vectors.read_complete_evidence(self.context)
        execution.write_record(self.root, "runtime.json", original, replace=True)
        execution.write_record(
            self.root, "image-lock.json", {"services": {}}, replace=True
        )
        with self.assertRaisesRegex(ValueError, "runtime_receipt_changed"):
            vectors.read_complete_evidence(self.context)

    def test_readonly_missing_vector_directory_is_not_created(self):
        self.readonly_admin_evidence()
        with self.assertRaises(FileNotFoundError):
            self.observe_verify()
        self.assertFalse(self.history_root.exists())

    def test_readonly_missing_recipe_is_not_written(self):
        self.readonly_admin_evidence()
        self.history_root.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "recipe_evidence_missing"):
            self.observe_verify()
        self.assertEqual([], list(self.history_root.iterdir()))

    def test_readonly_recipe_without_completed_pass_is_not_completion(self):
        self.readonly_admin_evidence()
        self.history()
        with self.assertRaisesRegex(ValueError, "completion_evidence_missing"):
            self.observe_verify()

    def test_readonly_partial_clean_history_is_not_completion(self):
        self.readonly_admin_evidence()
        self.run_pass(1)
        with self.assertRaisesRegex(ValueError, "completion_evidence_missing"):
            self.observe_verify()

    def test_readonly_missing_admin_image_never_builds(self):
        self.readonly_admin_evidence()
        self.run_pass(3)
        (self.root / "admin-image.json").unlink()
        with self.assertRaisesRegex(ValueError, "image_evidence_missing"):
            self.observe_verify()

    def test_readonly_changed_admin_image_receipt_is_rejected(self):
        self.readonly_admin_evidence()
        self.run_pass(3)
        self.replace_record(
            self.root, "admin-image.json", lambda value: value.update(sources={})
        )
        with self.assertRaisesRegex(ValueError, "image_receipt_changed"):
            self.observe_verify()

    def test_readonly_missing_admin_journal_is_not_created(self):
        directory, _ = self.readonly_admin_evidence()
        self.run_pass(3)
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()
        with self.assertRaises(FileNotFoundError):
            self.observe_verify()
        self.assertFalse(directory.exists())

    def test_readonly_empty_admin_journal_cannot_authorize_vectors(self):
        directory, _ = self.readonly_admin_evidence()
        self.run_pass(3)
        for path in directory.iterdir():
            path.unlink()
        with self.assertRaisesRegex(ValueError, "admin_completion_evidence_missing"):
            self.observe_verify()

    def test_readonly_unfinished_import_and_reconcile_are_not_retried(self):
        directory, identity = self.readonly_admin_evidence()
        self.run_pass(3)
        name = "8" * 32 + "-started.json"
        for operation, error in (
            ("import", "import_requires_reconcile"),
            ("reconcile", "operation_requires_recovery"),
        ):
            with self.subTest(operation=operation):
                execution.write_record(
                    directory,
                    name,
                    {
                        **identity,
                        "operation": operation,
                        "run_id": "8" * 32,
                        "status": "started",
                    },
                    replace=True,
                )
                with self.assertRaisesRegex(ValueError, error):
                    self.observe_verify()

    def test_readonly_admin_journal_is_bound_to_verified_image(self):
        directory, identity = self.readonly_admin_evidence()
        self.run_pass(3)
        for suffix in ("started", "container", "complete"):
            self.replace_record(
                directory,
                identity["run_id"] + "-" + suffix + ".json",
                lambda value: value.update(admin_image_id="sha256:" + "f" * 64),
            )
        with self.assertRaisesRegex(ValueError, "evidence_image_changed"):
            self.observe_verify()

    def test_readonly_image_inspection_failure_cannot_grant_completion(self):
        self.readonly_admin_evidence()
        self.run_pass(3)
        with patch.object(
            execution, "verify_image", side_effect=ValueError("retagged-image")
        ):
            with self.assertRaisesRegex(ValueError, "retagged-image"):
                self.observe_verify()

    def test_readonly_explicit_reconciliation_can_resolve_a_failed_import(self):
        directory, identity = self.readonly_admin_evidence()
        self.run_pass(3)
        failed = {**identity, "run_id": "8" * 32}
        execution.write_record(
            directory,
            failed["run_id"] + "-started.json",
            {**failed, "status": "started"},
        )
        execution.write_record(
            directory,
            failed["run_id"] + "-failure.json",
            {**failed, "status": "quarantined", "error_type": "TimeoutExpired"},
        )
        completed = execution.read_record(
            directory, identity["run_id"] + "-complete.json"
        )
        reconciled = {
            **completed,
            "operation": "reconcile",
            "run_id": "7" * 32,
            "result": {
                **completed["result"],
                "status": "reconciled-active",
                "batch_count": 0,
            },
        }
        fixed = {
            key: value
            for key, value in reconciled.items()
            if key not in {"container_id", "result", "status"}
        }
        execution.write_record(
            directory,
            reconciled["run_id"] + "-started.json",
            {**fixed, "status": "started"},
        )
        execution.write_record(
            directory,
            reconciled["run_id"] + "-container.json",
            {**fixed, "container_id": reconciled["container_id"]},
        )
        execution.write_record(
            directory, reconciled["run_id"] + "-complete.json", reconciled
        )
        execution.write_record(
            directory,
            "reconciled.json",
            {**reconciled, "resolved_import_runs": [failed["run_id"]]},
        )
        self.assertEqual("vectors-complete", self.observe_verify()["status"])

    def test_readonly_conflicting_admin_completion_and_failure_stays_quarantined(self):
        directory, identity = self.readonly_admin_evidence()
        self.run_pass(3)
        execution.write_record(
            directory,
            identity["run_id"] + "-failure.json",
            {**identity, "status": "quarantined", "error_type": "OSError"},
        )
        with self.assertRaisesRegex(ValueError, "operation_requires_recovery"):
            self.observe_verify()

    def test_readonly_admin_receipt_types_are_not_bool_integer_aliases(self):
        directory, identity = self.readonly_admin_evidence()
        self.run_pass(3)
        for suffix, field, changed in (
            ("started", "schema_version", True),
            ("complete", "schema_version", 1.0),
            ("container", "schema_version", True),
            ("container", "active_changed", 0),
        ):
            name = identity["run_id"] + "-" + suffix + ".json"
            original = execution.read_record(directory, name)
            execution.write_record(
                directory, name, {**original, field: changed}, replace=True
            )
            try:
                with (
                    self.subTest(suffix=suffix, field=field),
                    self.assertRaises(ValueError),
                ):
                    self.observe_verify()
            finally:
                execution.write_record(directory, name, original, replace=True)

    def test_readonly_pending_vector_run_cannot_be_ignored(self):
        self.readonly_admin_evidence()
        self.run_pass(3)
        (self.history_root / ("0" * 32)).mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "explicit_recovery"):
            self.observe_verify()

    def test_readonly_cleanup_flag_requires_actual_true(self):
        self.readonly_admin_evidence()
        report = self.run_pass(3)
        self.replace_record(
            self.directory(report),
            "complete.json",
            lambda value: value.update(helper_removed_verified=1),
        )
        with self.assertRaisesRegex(ValueError, "host_cleanup_receipt_changed"):
            self.observe_verify()

    def test_readonly_vector_counts_require_strict_integers(self):
        self.readonly_admin_evidence()
        report = self.run_pass(3)
        directory = self.directory(report)
        original = execution.read_record(directory, "complete.json")
        for field, changed in (
            ("done_count", 3.0),
            ("pending_count", False),
            ("document_count", True),
        ):
            altered = copy.deepcopy(original)
            altered["result"][field] = changed
            execution.write_record(directory, "complete.json", altered, replace=True)
            try:
                with (
                    self.subTest(field=field),
                    self.assertRaises(vector_journal.JournalError),
                ):
                    self.observe_verify()
            finally:
                execution.write_record(
                    directory, "complete.json", original, replace=True
                )

    def test_readonly_journal_digest_tampering_cannot_grant_completion(self):
        self.readonly_admin_evidence()
        report = self.run_pass(3)
        self.replace_record(
            self.directory(report) / "events",
            "0018-pass-result.json",
            lambda value: value["payload"].update(elapsed_seconds=2.0),
        )
        with self.assertRaisesRegex(vector_journal.JournalError, "digest_changed"):
            self.observe_verify()

    def test_readonly_helper_absence_is_checked_before_and_after_history(self):
        self.readonly_admin_evidence()
        self.run_pass(3)
        for observations in ((["d" * 64],), ([], ["d" * 64])):
            with (
                self.subTest(observations=observations),
                patch.object(
                    vectors.administration, "_admin_ids", side_effect=observations
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError, "existing_helper_requires_recovery"
                ):
                    self.observe_verify()

    def test_readonly_runtime_and_images_must_remain_identical(self):
        self.readonly_admin_evidence()
        self.run_pass(3)
        before = (self.runtime, {"fess-search": {"image_id": "sha256:" + "a" * 64}})
        for after in (
            ({"container_ids": {"fess-search": "f" * 64}}, before[1]),
            (self.runtime, {"fess-search": {"image_id": "sha256:" + "f" * 64}}),
        ):
            with (
                self.subTest(after=after),
                patch.object(execution, "require_runtime", side_effect=(before, after)),
            ):
                with self.assertRaisesRegex(ValueError, "runtime_changed"):
                    self.observe_verify()

    def test_readonly_runtime_snapshot_is_hashed_before_evidence_reads(self):
        self.readonly_admin_evidence()
        self.run_pass(3)
        original = vectors._history

        def mutate_after_snapshot(*args, **kwargs):
            self.runtime["container_ids"]["fess-search"] = "f" * 64
            return original(*args, **kwargs)

        with patch.object(vectors, "_history", side_effect=mutate_after_snapshot):
            with self.assertRaisesRegex(ValueError, "runtime_changed"):
                self.observe_verify()

    def test_readonly_accepts_full_completed_history_without_granting_write_capacity(
        self,
    ):
        self.readonly_admin_evidence()
        self.run_pass(3)
        with (
            patch.object(vectors, "MAX_HISTORY", 1),
            patch.object(vectors.administration, "MAX_OPERATIONS", 1),
        ):
            self.assertEqual("vectors-complete", self.observe_verify()["status"])
            with self.assertRaisesRegex(ValueError, "history_invalid_or_full"):
                self.history()
            with self.assertRaisesRegex(ValueError, "retention_limit"):
                vectors.administration._admin_records(self.context)


if __name__ == "__main__":
    unittest.main()
