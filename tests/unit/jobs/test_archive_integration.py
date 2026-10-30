"""Verify typed evidence survives archival through the actual queue interfaces."""

import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from search.operations.jobs import archive, profiles, queue, sources
from search.operations.jobs.pueue import JobError
from search.operations.shared import private_io
from search.quality.contract import plan_digest
from tests.unit.jobs import test_archive as fixtures
from tests.unit.quality.fixtures import capture, turn
from tests.unit.quality.test_batches import encoded, make_record, rewrite


class ArchiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        actual = {
            name: getattr(queue, name)
            for name in ("job_root", "locked", "verified_evidence")
        }
        self.fixture = fixtures.ArchiveTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        # Reuse the isolated Git/Pueue fixture, exercising its real consumers.
        for name, function in actual.items():
            restored = patch.object(queue, name, function)
            restored.start()
            self.addCleanup(restored.stop)

    def typed_job(self, kind, *, cancelled=False, source=True):
        fixture = self.fixture
        identifier, root = fixture.job(cancelled=cancelled, source=source)
        config = b'{"state_root":"/private/fixture/native-state"}\n'
        profile = {
            "type": kind,
            "config": str(fixture.base / "config.json"),
            "config_sha256": hashlib.sha256(config).hexdigest(),
            "source_revision": fixture.revision,
        }
        frozen, native = {"native-config.json": config}, {}
        if kind == "quality-batch":
            record = make_record(
                capture(
                    turn(None),
                    turn({"start": "2024", "end": "2024"}, version=2),
                )
            )
            run = json.loads(record["files"]["run.json"])
            run["operator_revision"] = fixture.revision
            run["identity"]["operator_revision"] = fixture.revision
            rewrite(record, run=run)
            cases = encoded(record["plan"])
            profile.update(
                candidate_revision="a" * 40,
                cases=str(fixture.base / "cases.json"),
                cases_sha256=hashlib.sha256(cases).hexdigest(),
                plan_sha256=plan_digest(record["plan"]),
                expected_identity_sha256=queue.digest(run["identity"]),
            )
            frozen["quality-cases.json"] = cases
            native = {"capture/" + name: raw for name, raw in record["files"].items()}
            operation = {key: value for key, value in run.items() if key != "identity"}
            status = "completed"
        else:
            profile.update(
                catalog_revision="a" * 40,
                bundle_id="1" * 64,
                manifest_sha256="b" * 64,
                expected_inputs_sha256="2" * 64,
            )
            operation = {
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
            status = "checkpoint"
        profiles.freeze(root, profile, frozen)
        request = queue.read(root / "request.json")
        request["profile_definition"] = profile
        queue.write(root / "request.json", request, replace=True)
        queued = queue.read(root / "queue.json")
        queued["request_sha256"] = queue.digest(request)
        queue.write(root / "queue.json", queued, replace=True)
        result = queue.read(root / "result.json")
        result["request_sha256"] = queue.digest(request)
        if not cancelled:
            for name, raw in native.items():
                target = root / name
                target.parent.mkdir(mode=0o700, exist_ok=True)
                target.write_bytes(raw)
                target.chmod(0o600)
            queue.write(root / "operation.json", operation, replace=True)
            raw = private_io.canonical(operation)
            (root / "stdout.bin").write_bytes(raw)
            result.update(status=status, operation_sha256=queue.digest(operation))
            result["artifacts"]["stdout.bin"] = {
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        queue.write(root / "result.json", result, replace=True)
        return identifier, root

    def archive_and_read(self, identifier):
        registry = self.fixture.registry
        original = queue.job_root(registry, identifier)
        inventory = queue.verified_evidence(registry, identifier)
        before = {name: (original / name).read_bytes() for name in inventory}
        outcome = queue.result(registry, identifier)
        self.assertEqual(archive.archive(registry, identifier)["status"], "archived")
        saved = queue.job_root(registry, identifier)
        self.assertEqual(saved, self.fixture.root / "archive" / identifier)
        self.assertFalse(original.exists())
        self.assertEqual(queue.result(registry, identifier), outcome)
        self.assertEqual(queue.verified_evidence(registry, identifier), inventory)
        self.assertEqual(
            archive.verify_archived(registry, identifier, saved)["files"], inventory
        )
        for name, raw in before.items():
            self.assertEqual((saved / name).read_bytes(), raw)
        return saved

    def test_resource_and_typed_native_evidence_remains_readable_by_job_id(self):
        for kind in ("resource-pass", "quality-batch", "vector-pass"):
            with self.subTest(kind=kind):
                identifier, _ = (
                    self.fixture.job()
                    if kind == "resource-pass"
                    else self.typed_job(kind)
                )
                saved = self.archive_and_read(identifier)
                if kind == "quality-batch":
                    self.assertTrue(
                        {
                            "native-config.json",
                            "quality-cases.json",
                            "capture/run.json",
                            "capture/capture.json",
                            "capture/outcomes.json",
                        }
                        <= queue.verified_evidence(
                            self.fixture.registry, identifier
                        ).keys()
                    )
                self.assertFalse((saved / "source").exists())
                self.assertEqual(
                    queue.read(self.fixture.root / "active.json"),
                    {"job_id": identifier, "status": "clean"},
                )
                with patch.object(
                    queue, "Pueue", side_effect=AssertionError("unexpected daemon")
                ):
                    self.assertIn(
                        queue.result(self.fixture.registry, identifier)["status"],
                        {"completed", "checkpoint"},
                    )

    def test_typed_cancellation_retains_inputs_and_archived_active_reference(self):
        previous, _ = self.fixture.job()
        self.archive_and_read(previous)
        active = queue.read(self.fixture.root / "active.json")
        for kind in ("resource-pass", "quality-batch", "vector-pass"):
            with self.subTest(kind=kind):
                identifier, _ = (
                    self.fixture.job(cancelled=True)
                    if kind == "resource-pass"
                    else self.typed_job(kind, cancelled=True)
                )
                saved = self.archive_and_read(identifier)
                self.assertFalse((saved / "capture").exists())
                self.assertEqual(queue.read(self.fixture.root / "active.json"), active)
        next_job, _ = self.fixture.job()
        self.archive_and_read(next_job)

    def test_native_job_recovers_after_rename_with_actual_archived_locator(self):
        identifier, original = self.typed_job("quality-batch")
        before = queue.verified_evidence(self.fixture.registry, identifier)
        rename = archive.os.rename

        def interrupted(*args, **kwargs):
            rename(*args, **kwargs)
            raise OSError("fixture interruption after rename")

        with patch.object(archive.os, "rename", side_effect=interrupted):
            with self.assertRaisesRegex(JobError, "archive_recovery_required"):
                archive.archive(self.fixture.registry, identifier)
        self.assertFalse(original.exists())
        self.assertEqual(
            queue.read(self.fixture.root / "archive-intent.json")["phase"], "moving"
        )
        self.assertEqual(
            queue.verified_evidence(self.fixture.registry, identifier), before
        )
        archive.archive(self.fixture.registry, identifier)
        self.assertFalse((self.fixture.root / "archive-intent.json").exists())
        self.assertEqual(
            queue.verified_evidence(self.fixture.registry, identifier), before
        )

    def test_result_lookup_rejects_duplicate_tampered_and_symlink_archives(self):
        identifier, original = self.typed_job("quality-batch")
        saved = self.archive_and_read(identifier)
        original.mkdir(mode=0o700)
        with self.assertRaisesRegex(JobError, "archive_duplicate_job"):
            queue.result(self.fixture.registry, identifier)
        original.rmdir()
        capture_file = saved / "capture/outcomes.json"
        raw = capture_file.read_bytes()
        capture_file.write_bytes(b"{}\n")
        with self.assertRaises(JobError):
            queue.result(self.fixture.registry, identifier)
        capture_file.write_bytes(raw)
        external = self.fixture.base / "moved-evidence"
        saved.rename(external)
        saved.symlink_to(external, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            queue.result(self.fixture.registry, identifier)

    def test_mixed_cumulative_archives_reclaim_capacity_and_keep_old_ids(self):
        fixture = self.fixture
        proof = {
            "revision": fixture.revision,
            "inode": [1, 2],
            "repository_sha256": queue.digest(
                str(queue.common_directory(fixture.repo))
            ),
        }
        artifact = {
            "revision": fixture.revision,
            "bundle_name": "source.bundle",
            "bytes": 1,
            "sha256": "d" * 64,
        }

        def git(_registry, *args):
            if args[:2] == ("worktree", "remove"):
                Path(args[2]).rmdir()
            return b""

        # Capacity uses synthetic source proof; the single-job tests use real Git.
        with (
            patch.object(archive, "_source", return_value=(proof, True)),
            patch.object(archive, "_git", side_effect=git),
            patch.object(sources, "preserve_bundle", return_value=artifact),
            patch.object(sources, "verify_bundle", return_value=artifact),
        ):
            retained = []
            for number in range(129):
                kind = ("resource-pass", "quality-batch", "vector-pass")[number % 3]
                identifier, _ = (
                    fixture.job(source=False)
                    if kind == "resource-pass"
                    else self.typed_job(kind, source=False)
                )
                inventory = queue.verified_evidence(fixture.registry, identifier)
                archive.archive(fixture.registry, identifier)
                self.assertEqual(list((fixture.root / "jobs").iterdir()), [])
                self.assertEqual(fixture.transport.tasks, {})
                if number in {0, 64, 128}:
                    retained.append((identifier, inventory))
            self.assertEqual(len(list((fixture.root / "archive").iterdir())), 129)
            for identifier, inventory in retained:
                self.assertEqual(
                    queue.verified_evidence(fixture.registry, identifier), inventory
                )


if __name__ == "__main__":
    unittest.main()
