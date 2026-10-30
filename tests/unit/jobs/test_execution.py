"""Verify native typed evidence with CPU fixtures, never models or services."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from search.operations.jobs import execution, profiles, queue
from search.operations.jobs.pueue import JobError
from search.operations.shared import private_io
from search.quality.contract import plan_digest
from tests.unit.quality.fixtures import capture, turn
from tests.unit.quality.test_batches import encoded, make_record, rewrite


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)
        self.root = self.directory / "job"
        self.root.mkdir(mode=0o700)
        self.config = b'{"state_root":"/private/fixture/native-state"}\n'
        self.config_sha = hashlib.sha256(self.config).hexdigest()
        self.record = make_record(
            capture(
                turn(None),
                turn({"start": "2024", "end": "2024"}, version=2, retain_topic=True),
            )
        )
        self.plan = self.record["plan"]
        self.cases = encoded(self.plan)
        run = json.loads(self.record["files"]["run.json"])
        self.quality = {
            "type": "quality-batch",
            "config": str(self.directory / "config.json"),
            "config_sha256": self.config_sha,
            "source_revision": "c" * 40,
            "candidate_revision": "a" * 40,
            "cases": str(self.directory / "cases.json"),
            "cases_sha256": hashlib.sha256(self.cases).hexdigest(),
            "plan_sha256": plan_digest(self.plan),
            "expected_identity_sha256": queue.digest(run["identity"]),
        }
        self.vector = {
            "type": "vector-pass",
            "config": str(self.directory / "config.json"),
            "config_sha256": self.config_sha,
            "source_revision": "c" * 40,
            "catalog_revision": "a" * 40,
            "bundle_id": "1" * 64,
            "manifest_sha256": "b" * 64,
            "expected_inputs_sha256": "2" * 64,
        }
        self.vector_report = {
            "schema_version": 1,
            "status": "checkpoint",
            "revision": "a" * 40,
            "bundle_id": "1" * 64,
            "manifest_sha256": "b" * 64,
            "active_changed": False,
            "runtime_verified": True,
            "host_cleanup_verified": True,
            "document_count": 10,
            "done_count": 4,
            "pending_count": 6,
            "completed_passes": 1,
            "passes_this_invocation": 1,
            "recipe_sha256": "3" * 64,
        }

    def request(self, profile):
        return {"revision": "c" * 40, "profile_definition": profile}

    def freeze(self, profile):
        files = {"native-config.json": self.config}
        if profile["type"] == "quality-batch":
            files["quality-cases.json"] = self.cases
        profiles.freeze(self.root, profile, files)

    def capture_files(self):
        directory = self.root / "capture"
        directory.mkdir(mode=0o700, exist_ok=True)
        fd = private_io.open_directory(directory)
        try:
            for name, raw in self.record["files"].items():
                private_io.write_file(
                    fd,
                    name,
                    raw,
                    replace=(directory / name).exists(),
                    max_bytes=8 * 1024**2,
                )
        finally:
            os.close(fd)

    def quality_summary(self):
        return {
            key: value
            for key, value in json.loads(self.record["files"]["run.json"]).items()
            if key != "identity"
        }

    def test_fixed_argv_uses_frozen_executing_source_and_required_native_hash(self):
        for profile, script, flag, value in (
            (
                self.quality,
                "share_search_backend.py",
                "--expected-identity-sha256",
                self.quality["expected_identity_sha256"],
            ),
            (
                self.vector,
                "depositarai.py",
                "--expected-inputs-sha256",
                self.vector["expected_inputs_sha256"],
            ),
        ):
            with self.subTest(profile=profile["type"]):
                argv = execution.argv(self.root, self.request(profile))
                self.assertEqual(argv[:4], ["/usr/bin/python3", "-I", "-S", "-B"])
                self.assertEqual(argv[4], self.root / "source/scripts" / script)
                self.assertEqual(argv[argv.index(flag) + 1], value)
                self.assertEqual(
                    argv[argv.index("--config") + 1], self.root / "native-config.json"
                )
                self.assertNotIn(profile["config"], argv)
        vector = execution.argv(self.root, self.request(self.vector))
        self.assertEqual(vector[vector.index("--max-passes") + 1], "1")
        self.assertEqual(vector[vector.index("--wall-seconds") + 1], "3600")
        self.assertGreater(execution.timeout(self.vector), 3600)

    def test_quality_complete_requires_all_private_native_artifacts(self):
        self.freeze(self.quality)
        self.capture_files()
        self.assertEqual(
            execution.verify(
                self.quality_summary(), 0, self.root, self.request(self.quality)
            ),
            "completed",
        )
        (self.root / "capture/capture.json").write_bytes(b"{}\n")
        with self.assertRaises(ValueError):
            execution.verify(
                self.quality_summary(), 0, self.root, self.request(self.quality)
            )

    def test_stdout_alone_is_not_verified_quality_evidence(self):
        self.freeze(self.quality)
        with self.assertRaises((OSError, ValueError)):
            execution.verify(
                self.quality_summary(), 0, self.root, self.request(self.quality)
            )

    def test_quality_assertion_failure_does_not_become_collection_failure(self):
        captured = json.loads(self.record["files"]["capture.json"])
        captured["cases"][0]["turns"][1]["response"]["result"]["state"]["topic"] = (
            "different topic"
        )
        rewrite(self.record, captured=captured)
        self.freeze(self.quality)
        self.capture_files()
        self.assertEqual(
            execution.verify(
                self.quality_summary(), 0, self.root, self.request(self.quality)
            ),
            "completed",
        )

    def test_missing_turn_cannot_shrink_the_frozen_quality_denominator(self):
        captured = json.loads(self.record["files"]["capture.json"])
        captured["cases"][0]["turns"].pop()
        rewrite(self.record, captured=captured)
        self.freeze(self.quality)
        self.capture_files()
        with self.assertRaises(ValueError):
            execution.verify(
                self.quality_summary(), 0, self.root, self.request(self.quality)
            )

    def test_quality_wrong_source_catalog_model_and_future_identity_are_rejected(self):
        original = deepcopy(self.record)
        self.freeze(self.quality)
        for key, value in (
            ("operator_revision", "d" * 40),
            ("candidate_revision", "d" * 40),
            ("catalog_manifest_sha256", "d" * 64),
            ("model_manifest_sha256", "d" * 64),
            ("future_selected_catalog", {"manifest_sha256": "d" * 64}),
        ):
            with self.subTest(key=key):
                self.record = deepcopy(original)
                run = json.loads(self.record["files"]["run.json"])
                run["identity"][key] = value
                rewrite(self.record, run=run)
                self.capture_files()
                with self.assertRaises(JobError):
                    execution.verify(
                        self.quality_summary(), 0, self.root, self.request(self.quality)
                    )

    def test_future_identity_fields_are_preserved_when_the_full_hash_matches(self):
        run = json.loads(self.record["files"]["run.json"])
        run["identity"]["future_selected_catalog"] = {"manifest_sha256": "d" * 64}
        self.quality["expected_identity_sha256"] = queue.digest(run["identity"])
        rewrite(self.record, run=run)
        self.freeze(self.quality)
        self.capture_files()
        self.assertEqual(
            execution.verify(
                self.quality_summary(), 0, self.root, self.request(self.quality)
            ),
            "completed",
        )

    def test_quality_failed_quarantined_or_changed_stdout_are_not_completed(self):
        self.freeze(self.quality)
        original = deepcopy(self.record)
        for status in ("failed", "quarantined"):
            with self.subTest(status=status):
                self.record = deepcopy(original)
                run = json.loads(self.record["files"]["run.json"])
                run.update(
                    status=status,
                    error="shared_batch_failed",
                    identity_verified=status != "quarantined",
                )
                rewrite(self.record, run=run)
                self.capture_files()
                with self.assertRaises(JobError):
                    execution.verify(
                        self.quality_summary(), 1, self.root, self.request(self.quality)
                    )
        self.record = original
        self.capture_files()
        with self.assertRaises(JobError):
            execution.verify(
                {**self.quality_summary(), "completed_turns": 0},
                0,
                self.root,
                self.request(self.quality),
            )

    def test_vector_checkpoint_requires_runtime_target_counts_and_clean_new_pass(self):
        self.freeze(self.vector)
        self.assertEqual(
            execution.verify(
                self.vector_report, 2, self.root, self.request(self.vector)
            ),
            "checkpoint",
        )
        for key, value in (
            ("revision", "d" * 40),
            ("bundle_id", "d" * 64),
            ("manifest_sha256", "d" * 64),
            ("host_cleanup_verified", False),
            ("runtime_verified", False),
            ("passes_this_invocation", 2),
            ("done_count", 1),
        ):
            with self.subTest(key=key), self.assertRaises(JobError):
                execution.verify(
                    {**self.vector_report, key: value},
                    2,
                    self.root,
                    self.request(self.vector),
                )

    def test_zero_new_pass_checkpoint_is_not_completion_or_claimed_helper_cleanup(self):
        self.freeze(self.vector)
        value = {
            **self.vector_report,
            "passes_this_invocation": 0,
            "completed_passes": 0,
            "done_count": None,
            "pending_count": None,
            "host_cleanup_verified": False,
        }
        self.assertEqual(
            execution.verify(value, 2, self.root, self.request(self.vector)),
            "checkpoint",
        )
        for change in (
            {"status": "vectors-complete"},
            {"host_cleanup_verified": True},
            {"completed_passes": 1},
        ):
            with self.subTest(change=change), self.assertRaises(JobError):
                execution.verify(
                    {**value, **change}, 2, self.root, self.request(self.vector)
                )

    def test_vector_new_completion_and_already_complete_readonly_are_distinct(self):
        self.freeze(self.vector)
        value = {
            **self.vector_report,
            "status": "vectors-complete",
            "done_count": 10,
            "pending_count": 0,
        }
        self.assertEqual(
            execution.verify(value, 0, self.root, self.request(self.vector)),
            "completed",
        )
        value.update(
            passes_this_invocation=0,
            read_only=True,
            after_state_sha256="7" * 64,
            runtime_sha256="8" * 64,
            admin_image_id="sha256:" + "9" * 64,
        )
        self.assertEqual(
            execution.verify(value, 0, self.root, self.request(self.vector)),
            "completed",
        )
        value.pop("runtime_sha256")
        with self.assertRaises(JobError):
            execution.verify(value, 0, self.root, self.request(self.vector))


if __name__ == "__main__":
    unittest.main()
