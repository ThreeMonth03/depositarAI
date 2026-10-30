"""Verify bounded idle archival and deterministic recovery without a daemon."""

from copy import deepcopy
from contextlib import contextmanager, redirect_stdout
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import search_jobs
from search.operations.jobs import archive, queue, sources
from search.operations.jobs.pueue import ENVIRONMENT, JobError
from search.operations.shared import private_io


@contextmanager
def proof_locked(root, name):
    """Supply the agreed FD-proof lock API without changing another owner's file."""
    fd = os.open(root / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JobError("job_busy") from None
        yield private_io.identity(os.fstat(fd))
    finally:
        os.close(fd)


class FakePueue:
    def __init__(self):
        self.tasks = {}
        self.calls = []
        self.after_remove = False

    def state(self):
        return {
            "tasks": deepcopy(self.tasks),
            "groups": {
                name: {"status": "Running", "parallel_tasks": 1}
                for name in ("default", "heavy")
            },
        }

    def call(self, *args):
        self.calls.append(args)
        if args[0] != "remove":
            raise AssertionError(args)
        del self.tasks[args[1]]
        if self.after_remove:
            raise JobError("job_queue_command_failed")
        return b""


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="job-archive-fixture-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.base.chmod(0o700)
        self.root, self.repo = self.base / "queue", self.base / "repo"
        self.root.mkdir(mode=0o700)
        (self.root / "jobs").mkdir(mode=0o700)
        self.repo.mkdir(mode=0o700)
        self.git("init", "--quiet")
        (self.repo / "fixture.py").write_text("print('fixture')\n")
        (self.repo / ".gitignore").write_text("ignored.tmp\n")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        )
        self.revision = self.git("rev-parse", "HEAD").decode().strip()
        self.registry = {
            "root": str(self.root),
            "runtime_root": str(self.base),
            "operator_source": str(self.repo),
            "operator_revision": self.revision,
            "pueue": str(self.base / "pueue"),
            "machine_id_sha256": "a" * 64,
            "schema_version": 1,
            "profiles": {},
        }
        self.transport = FakePueue()
        self.evidence = {}
        self.count = 0
        self.addCleanup(patch.stopall)
        patch.object(queue, "locked", side_effect=proof_locked).start()
        patch.object(queue, "Pueue", return_value=self.transport).start()
        patch.object(archive, "Pueue", return_value=self.transport).start()
        patch.object(
            queue, "verified_evidence", side_effect=self.verified, create=True
        ).start()
        patch.object(queue, "job_root", side_effect=self.locate).start()

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "init.templateDir=", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
        ).stdout

    def locate(self, registry, identifier):
        source = self.root / "jobs" / identifier
        target = self.root / "archive" / identifier
        if source.exists() and target.exists():
            raise JobError("job_archive_duplicate_job")
        if target.exists():
            archive.verify_archived(registry, identifier, target)
            return target
        return source

    def verified(self, registry, identifier):
        # The typed owner supplies this API. Resource outer validation remains
        # real here; native parsing is exercised on the coordinator's merged head.
        queue.result(registry, identifier)
        return deepcopy(self.evidence[identifier])

    def job(self, *, cancelled=False, source=True):
        self.count += 1
        identifier, task_id = f"{self.count:032x}", self.count
        root = self.root / "jobs" / identifier
        root.mkdir(mode=0o700)
        frozen = root / "source"
        if source:
            self.git(
                "worktree",
                "add",
                "--detach",
                "--lock",
                "--reason",
                archive.LOCK_REASON,
                str(frozen),
                self.revision,
            )
            frozen.chmod(0o700)
        else:
            frozen.mkdir(mode=0o700)
        profile = {
            "type": "resource-pass",
            "bundle": str(self.base / "bundle"),
            "state_root": str(self.base / "profiles"),
            "plan_sha256": "b" * 64,
            "manifest_sha256": "c" * 64,
        }
        request = {
            "schema_version": 1,
            "issue": 69,
            "profile": "fixture",
            "source": str(self.repo),
            "revision": self.revision,
            "job_id": identifier,
            "created_at_ns": self.count,
            "queue_identity_sha256": queue.queue_identity(self.registry),
            "profile_definition": profile,
        }
        queue.write(root / "request.json", request)
        queue.write(
            root / "queue.json",
            {"task_id": task_id, "request_sha256": queue.digest(request)},
        )
        result = {
            "job_id": identifier,
            "request_sha256": queue.digest(request),
            "status": "cancelled" if cancelled else "completed",
        }
        if not cancelled:
            operation = {
                "schema_version": 1,
                "status": "completed",
                "plan_sha256": "b" * 64,
                "catalog_manifest_sha256": "c" * 64,
                "selected_count": 1,
                "pending_count": 0,
                "profile_count": 1,
                "failed_count": 0,
                "retryable_count": 0,
                "attempted_count": 1,
            }
            queue.write(root / "operation.json", operation)
            raw = (json.dumps(operation) + "\n").encode()
            (root / "stdout.bin").write_bytes(raw)
            (root / "stdout.bin").chmod(0o600)
            result.update(
                operation_sha256=queue.digest(operation),
                source_revision=self.revision,
                log_status="complete",
                artifacts={
                    "stdout.bin": {
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    },
                    "stderr.bin": {
                        "bytes": 0,
                        "sha256": hashlib.sha256(b"").hexdigest(),
                    },
                },
            )
            self.transport.tasks[str(task_id)] = {
                "label": "depositarai-" + identifier,
                "command": shlex.join(queue.launcher(self.registry, identifier)),
                "path": str(self.root),
                "group": "heavy",
                "envs": dict(ENVIRONMENT),
                "status": {"Done": {"result": "Success"}},
            }
            queue.write(
                self.root / "active.json",
                {"job_id": identifier, "status": "clean"},
                replace=(self.root / "active.json").exists(),
            )
        queue.write(root / "result.json", result)
        self.evidence[identifier] = archive._inventory(root, source=True)
        return identifier, root

    def test_terminal_archive_preserves_original_bytes_and_source_and_is_repeatable(
        self,
    ):
        identifier, original = self.job()
        before = {
            name: (original / name).read_bytes() for name in self.evidence[identifier]
        }
        source_before = (self.repo / "fixture.py").read_bytes()
        self.assertEqual(
            archive.archive(self.registry, identifier)["status"], "archived"
        )
        archived = self.root / "archive" / identifier
        self.assertFalse(original.exists())
        self.assertFalse((archived / "source").exists())
        self.assertFalse((self.root / "archive-intent.json").exists())
        for name, raw in before.items():
            self.assertEqual(raw, (archived / name).read_bytes())
        self.assertEqual(source_before, (self.repo / "fixture.py").read_bytes())
        self.assertEqual(queue.result(self.registry, identifier)["status"], "completed")
        self.assertEqual(
            archive.archive(self.registry, identifier)["status"], "archived"
        )
        self.assertEqual(self.transport.calls, [("remove", "1")])

    def test_cancelled_missing_entry_requires_exact_no_execution_evidence(self):
        identifier, _ = self.job(cancelled=True)
        self.assertEqual(
            archive.archive(self.registry, identifier)["status"], "archived"
        )
        self.assertEqual(self.transport.calls, [])

    def test_native_sidecars_are_preserved_by_the_verified_inventory_contract(self):
        identifier, root = self.job()
        queue.write(root / "native-config.json", {"fixture": "private"})
        queue.write(root / "quality-cases.json", {"fixture": "cases"})
        (root / "capture").mkdir(mode=0o700)
        for name in ("run.json", "capture.json", "outcomes.json"):
            queue.write(root / "capture" / name, {"fixture": name})
        self.evidence[identifier] = archive._inventory(root, source=True)
        archive.archive(self.registry, identifier)
        target = self.root / "archive" / identifier
        self.assertEqual(
            self.evidence[identifier],
            archive.verify_archived(self.registry, identifier, target)["files"],
        )
        (target / "capture/capture.json").write_bytes(b"changed")
        with self.assertRaises(JobError):
            archive.verify_archived(self.registry, identifier, target)

    def test_queued_running_unknown_and_unverified_peers_prevent_any_cleanup(self):
        target, root = self.job()
        peer, _ = self.job()
        row = self.transport.tasks["2"]
        for status in (
            {"Queued": {}},
            {"Stashed": {}},
            {"Running": {}},
            {"Paused": {}},
            {"Unknown": {}},
            {"Done": {"result": {"ExitCode": 1}}},
        ):
            row["status"] = status
            with self.subTest(status=status), self.assertRaises(JobError):
                archive.archive(self.registry, target)
            self.assertTrue((root / "source").exists())
            self.assertFalse((self.root / "archive-intent.json").exists())
            self.assertEqual(self.transport.calls, [])

    def test_unexpected_files_artifacts_and_private_permissions_are_refused(self):
        for mutation in ("unknown", "raw", "mode", "link", "ignored"):
            with self.subTest(mutation=mutation):
                identifier, root = self.job()
                if mutation == "unknown":
                    queue.write(root / "extra.json", {"extra": True})
                elif mutation == "raw":
                    (root / "stdout.bin").write_bytes(b"changed")
                elif mutation == "mode":
                    (root / "request.json").chmod(0o644)
                elif mutation == "link":
                    (root / "unknown-link").symlink_to(self.repo / "fixture.py")
                else:
                    (root / "source/ignored.tmp").write_text("private ignored input")
                with self.assertRaises(JobError):
                    archive.archive(self.registry, identifier)
                self.assertTrue((root / "source").exists())
                self.assertFalse((self.root / "archive-intent.json").exists())
                # Isolate the deliberate invalid peer before the next scenario.
                self.transport.tasks.pop(str(self.count))
                shutil.rmtree(root)

    def test_source_registration_dirty_or_wrong_lock_reason_is_refused(self):
        identifier, root = self.job()
        self.git("worktree", "unlock", str(root / "source"))
        for reason in (None, "some other owner"):
            if reason:
                self.git("worktree", "lock", "--reason", reason, str(root / "source"))
            with self.assertRaises(JobError):
                archive.archive(self.registry, identifier)
            self.assertFalse((self.root / "archive-intent.json").exists())
            if reason:
                self.git("worktree", "unlock", str(root / "source"))

    def test_unknown_or_recovery_terminal_is_not_archived(self):
        identifier, root = self.job()
        value = queue.read(root / "result.json")
        for status in ("blocked", "recovery_required"):
            value["status"] = status
            queue.write(root / "result.json", value, replace=True)
            with self.assertRaises(JobError):
                archive.archive(self.registry, identifier)
            self.assertFalse((self.root / "archive-intent.json").exists())

    def test_submissions_and_execution_locks_are_nonblocking(self):
        identifier, root = self.job()
        for name in ("submissions.lock", "execution.lock"):
            with queue.locked(self.root, name):
                with self.assertRaisesRegex(JobError, "^job_busy$"):
                    archive.archive(self.registry, identifier)
            self.assertTrue((root / "source").exists())

    def test_known_interruption_after_task_removal_can_resume_without_rewriting(self):
        identifier, root = self.job()
        original = (root / "result.json").read_bytes()
        self.transport.after_remove = True
        with self.assertRaisesRegex(JobError, "archive_recovery_required"):
            archive.archive(self.registry, identifier)
        self.assertTrue((self.root / "archive-intent.json").exists())
        self.assertTrue((root / "source").exists())
        self.transport.after_remove = False
        archive.archive(self.registry, identifier)
        self.assertEqual(
            original, (self.root / "archive" / identifier / "result.json").read_bytes()
        )

    def test_prepared_intent_does_not_authorize_an_unrecorded_task_disappearance(self):
        identifier, root = self.job()
        with patch.object(
            sources, "preserve_bundle", side_effect=OSError("fixture before bundle")
        ):
            with self.assertRaisesRegex(JobError, "archive_recovery_required"):
                archive.archive(self.registry, identifier)
        self.assertEqual(
            queue.read(self.root / "archive-intent.json")["phase"], "prepared"
        )
        self.transport.tasks.clear()
        with self.assertRaisesRegex(JobError, "archive_recovery_required"):
            archive.archive(self.registry, identifier)
        self.assertTrue((root / "source").exists())
        self.assertEqual(self.transport.calls, [])

    def test_missing_source_with_remaining_registration_requires_reconciliation(self):
        identifier, root = self.job()
        real_git = archive._git
        calls = []

        def git(registry, *args):
            calls.append(args)
            if args[:2] == ("worktree", "remove"):
                shutil.rmtree(root / "source")
                raise OSError("fixture interrupted Git before admin removal")
            return real_git(registry, *args)

        with patch.object(archive, "_git", side_effect=git):
            with self.assertRaisesRegex(JobError, "archive_recovery_required"):
                archive.archive(self.registry, identifier)
        with self.assertRaisesRegex(JobError, "archive_recovery_required"):
            archive.archive(self.registry, identifier)
        self.assertTrue((self.root / "archive-intent.json").exists())
        self.assertFalse((self.root / "archive" / identifier).exists())
        self.assertFalse(any("--force" in args or "prune" in args for args in calls))

    def test_new_busy_peer_during_bundle_verification_prevents_task_and_source_removal(
        self,
    ):
        identifier, root = self.job()
        self.job()
        preserve = sources.preserve_bundle

        def saved(*args):
            result = preserve(*args)
            self.transport.tasks["2"]["status"] = {"Queued": {}}
            return result

        with patch.object(sources, "preserve_bundle", side_effect=saved):
            with self.assertRaisesRegex(JobError, "archive_recovery_required"):
                archive.archive(self.registry, identifier)
        self.assertEqual(self.transport.calls, [])
        self.assertTrue((root / "source").exists())
        self.assertTrue((self.root / "archive-intent.json").exists())

    def test_task_disappearance_during_bundle_save_remains_unknown_recovery(self):
        identifier, root = self.job()
        preserve = sources.preserve_bundle

        def saved(*args):
            result = preserve(*args)
            self.transport.tasks.clear()
            return result

        with patch.object(sources, "preserve_bundle", side_effect=saved):
            with self.assertRaisesRegex(JobError, "archive_recovery_required"):
                archive.archive(self.registry, identifier)
        self.assertEqual(
            queue.read(self.root / "archive-intent.json")["phase"], "prepared"
        )
        self.assertTrue((root / "source").exists())
        self.assertEqual(self.transport.calls, [])

    def test_named_lock_replacement_during_bundle_save_prevents_cleanup(self):
        identifier, root = self.job()
        preserve = sources.preserve_bundle

        def saved(*args):
            result = preserve(*args)
            lock = self.root / "execution.lock"
            lock.unlink()
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            return result

        with patch.object(sources, "preserve_bundle", side_effect=saved):
            with self.assertRaisesRegex(JobError, "archive_recovery_required"):
                archive.archive(self.registry, identifier)
        self.assertTrue((root / "source").exists())
        self.assertTrue((self.root / "archive-intent.json").exists())
        self.assertEqual(self.transport.calls, [])
        with self.assertRaisesRegex(JobError, "archive_recovery_required"):
            archive.archive(self.registry, identifier)

    def test_initial_named_lock_cannot_replace_the_held_fd_as_authority(self):
        identifier, root = self.job()

        @contextmanager
        def changed(directory, name):
            with proof_locked(directory, name) as proof:
                if name == "execution.lock":
                    lock = directory / "submissions.lock"
                    lock.unlink()
                    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(fd)
                yield proof

        with patch.object(queue, "locked", side_effect=changed):
            with self.assertRaisesRegex(JobError, "^job_archive_lock_changed$"):
                archive.archive(self.registry, identifier)
        self.assertTrue((root / "source").exists())
        self.assertFalse((self.root / "archive-intent.json").exists())
        self.assertEqual(self.transport.calls, [])

    def test_unsafe_archive_destination_is_rejected_before_cleanup(self):
        identifier, root = self.job()
        external = self.base / "external"
        external.mkdir(mode=0o700)
        (self.root / "archive").symlink_to(external, target_is_directory=True)
        with self.assertRaises(JobError):
            archive.archive(self.registry, identifier)
        self.assertTrue((root / "source").exists())
        self.assertFalse((self.root / "archive-intent.json").exists())
        self.assertEqual(self.transport.calls, [])

    def test_cancelled_receipt_cannot_hide_execution_evidence(self):
        identifier, root = self.job(cancelled=True)
        queue.write(root / "operation.json", {"status": "completed"})
        self.evidence[identifier] = archive._inventory(root, source=True)
        source_before = self.git("worktree", "list", "--porcelain", "-z")
        content_before = (root / "source/fixture.py").read_bytes()
        tasks_before = self.transport.state()
        files_before = {
            name: (root / name).read_bytes() for name in self.evidence[identifier]
        }
        self.assertFalse((self.root / "archive-intent.json").exists())
        with self.assertRaisesRegex(JobError, "^job_artifact_changed$"):
            archive.archive(self.registry, identifier)
        self.assertEqual(
            source_before, self.git("worktree", "list", "--porcelain", "-z")
        )
        self.assertEqual(content_before, (root / "source/fixture.py").read_bytes())
        self.assertEqual(tasks_before, self.transport.state())
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(
            files_before,
            {name: (root / name).read_bytes() for name in self.evidence[identifier]},
        )
        self.assertFalse((self.root / "archive-intent.json").exists())

    def test_source_removal_and_rename_interruptions_resume_only_known_steps(self):
        for stage in ("unlock", "source", "rename-before", "rename-after"):
            with self.subTest(stage=stage):
                identifier, root = self.job()
                real_git, real_rename = archive._git, os.rename

                def git(registry, *args):
                    result = real_git(registry, *args)
                    if args[:2] == (
                        "worktree",
                        "unlock" if stage == "unlock" else "remove",
                    ):
                        raise OSError("fixture interruption after removal")
                    return result

                def rename(*args, **kwargs):
                    if stage == "rename-after":
                        real_rename(*args, **kwargs)
                    raise OSError("fixture rename interruption")

                with (
                    patch.object(archive, "_git", side_effect=git)
                    if stage in {"source", "unlock"}
                    else patch.object(archive.os, "rename", side_effect=rename)
                ):
                    with self.assertRaisesRegex(JobError, "archive_recovery_required"):
                        archive.archive(self.registry, identifier)
                self.assertTrue((self.root / "archive-intent.json").exists())
                archive.archive(self.registry, identifier)
                self.assertFalse((self.root / "archive-intent.json").exists())

    def test_changed_evidence_during_recovery_stays_blocked(self):
        identifier, root = self.job()
        self.transport.after_remove = True
        with self.assertRaises(JobError):
            archive.archive(self.registry, identifier)
        self.transport.after_remove = False
        (root / "stdout.bin").write_bytes(b"tampered during recovery")
        with self.assertRaisesRegex(JobError, "archive_recovery_required"):
            archive.archive(self.registry, identifier)
        self.assertTrue((root / "source").exists())

    def test_completed_missing_task_is_not_assumed_cancelled(self):
        identifier, root = self.job()
        self.transport.tasks.clear()
        with self.assertRaisesRegex(JobError, "archive_task_unverified"):
            archive.archive(self.registry, identifier)
        self.assertTrue((root / "source").exists())

    def test_more_than_128_cumulative_archives_keep_active_capacity_bounded(self):
        artifact = {
            "revision": self.revision,
            "bundle_name": "source.bundle",
            "bytes": 1,
            "sha256": "d" * 64,
        }
        proof = {
            "revision": self.revision,
            "inode": [1, 2],
            "repository_sha256": queue.digest(str(queue.common_directory(self.repo))),
        }

        def git(registry, *args):
            if args[:2] == ("worktree", "remove"):
                Path(args[2]).rmdir()
            return b""

        with (
            patch.object(archive, "_source", return_value=(proof, True)),
            patch.object(archive, "_git", side_effect=git),
            patch.object(sources, "preserve_bundle", return_value=artifact),
            patch.object(sources, "verify_bundle", return_value=artifact),
        ):
            for _ in range(129):
                identifier, _ = self.job(source=False)
                archive.archive(self.registry, identifier)
                self.assertEqual(len(list((self.root / "jobs").iterdir())), 0)
                self.assertEqual(len(self.transport.tasks), 0)
            self.assertEqual(len(list((self.root / "archive").iterdir())), 129)
            self.assertEqual(
                queue.result(self.registry, identifier)["status"], "completed"
            )


class ArchiveCLITests(unittest.TestCase):
    def test_archive_is_a_thin_fixed_command_with_closed_diagnostics(self):
        identifier = "a" * 32
        for error in (None, JobError("job_archive_recovery_required")):
            output = io.StringIO()
            with (
                patch.object(queue, "load_registry", return_value={"fixture": True}),
                patch.object(
                    archive,
                    "archive",
                    return_value={"job_id": identifier, "status": "archived"},
                    side_effect=error,
                ) as call,
                redirect_stdout(output),
            ):
                code = search_jobs.main(["archive", "--job", identifier])
            self.assertEqual(code, 1 if error else 0)
            call.assert_called_once_with({"fixture": True}, identifier)
            self.assertEqual(
                json.loads(output.getvalue()),
                {"error": str(error)}
                if error
                else {"job_id": identifier, "status": "archived"},
            )


if __name__ == "__main__":
    unittest.main()
