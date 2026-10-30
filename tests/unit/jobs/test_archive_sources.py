"""Keep archived Git source independently restorable before removing a worktree."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from search.operations.jobs import sources
from search.operations.jobs.pueue import JobError
from search.operations.shared import private_io


class SourceBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="archive-source-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.source = self.root / "source"
        self.source.mkdir(mode=0o700)
        self.git("init", "--quiet")
        (self.source / "program.py").write_text("print('fixture source')\n")
        (self.source / ".gitignore").write_text("private-*\n")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--quiet",
            "-m",
            "First fixture source",
        )
        (self.source / "other.txt").write_text("second revision\n")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--quiet",
            "-m",
            "Second fixture source",
        )
        self.revision = self.git("rev-parse", "HEAD").decode().strip()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(mode=0o700)
        self.slot = self.artifacts / self.revision

    def git(self, *arguments):
        return subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(self.source),
                *arguments,
            ],
            check=True,
            capture_output=True,
            timeout=15,
        ).stdout

    def preserve(self):
        return sources.preserve_bundle(self.source, self.slot, self.revision)

    def verify(self):
        return sources.verify_bundle(self.source, self.slot, self.revision)

    def rejected(self, operation):
        with self.assertRaises(JobError) as caught:
            operation()
        self.assertRegex(str(caught.exception), r"^job_source_artifact_[a-z_]+$")
        self.assertNotIn(str(self.root), str(caught.exception))

    def rebind(self, raw):
        bundle = self.slot / "source.bundle"
        bundle.write_bytes(raw)
        bundle.chmod(0o600)
        record = {
            "revision": self.revision,
            "bundle_name": "source.bundle",
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        (self.slot / "source.json").write_bytes(private_io.canonical(record))
        (self.slot / "source.json").chmod(0o600)

    def test_preserve_and_restore_proof_keep_private_original_source(self):
        before = (self.source / "program.py").read_bytes()
        record = self.preserve()
        self.assertEqual(record, self.verify())
        self.assertEqual(set(record), {"revision", "bundle_name", "bytes", "sha256"})
        self.assertEqual(record["revision"], self.revision)
        self.assertEqual((self.slot / "source.bundle").stat().st_size, record["bytes"])
        self.assertEqual(
            hashlib.sha256((self.slot / "source.bundle").read_bytes()).hexdigest(),
            record["sha256"],
        )
        self.assertEqual(self.slot.stat().st_mode & 0o777, 0o700)
        for name in ("source.bundle", "source.json"):
            self.assertEqual((self.slot / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, (self.source / "program.py").read_bytes())
        self.assertEqual(self.git("status", "--porcelain"), b"")
        self.assertEqual(list(self.artifacts.iterdir()), [self.slot])

    def test_same_revision_deduplicates_without_overwriting_evidence(self):
        first = self.preserve()
        identities = {
            name: private_io.identity((self.slot / name).stat())
            for name in ("source.bundle", "source.json")
        }
        self.assertEqual(self.preserve(), first)
        self.assertEqual(
            identities,
            {
                name: private_io.identity((self.slot / name).stat())
                for name in identities
            },
        )

    def test_locked_detached_frozen_worktree_can_be_preserved_without_removing_it(self):
        frozen = self.root / "frozen"
        self.git(
            "worktree",
            "add",
            "--detach",
            "--lock",
            "--reason",
            "queued resource pass",
            str(frozen),
            self.revision,
        )
        frozen.chmod(0o700)
        record = sources.preserve_bundle(frozen, self.slot, self.revision)
        self.assertEqual(record, self.verify())
        self.assertTrue(frozen.is_dir())
        listing = self.git("worktree", "list", "--porcelain", "-z")
        self.assertIn(b"locked queued resource pass\0", listing)
        self.assertIn(str(frozen).encode(), listing)

    def test_bundle_restore_proof_does_not_depend_on_original_object_database(self):
        record = self.preserve()
        independent = self.root / "empty-repository"
        independent.mkdir(mode=0o700)
        subprocess.run(
            ["git", "init", "--quiet", str(independent)],
            check=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(
            record, sources.verify_bundle(independent, self.slot, self.revision)
        )

    def test_wrong_or_malformed_revision_never_creates_artifact(self):
        for value in ("a" * 40, "A" * 40, "a" * 39, True, b"a" * 40):
            with self.subTest(value_type=type(value).__name__):
                self.rejected(
                    lambda: sources.preserve_bundle(self.source, self.slot, value)
                )
                self.assertFalse(self.slot.exists())

    def test_dirty_untracked_and_ignored_content_prevents_preservation(self):
        for name in ("program.py", "untracked.txt", "private-secret"):
            target = self.source / name
            original = target.read_bytes() if target.exists() else None
            target.write_bytes(b"PRIVATE_CONTENT_DO_NOT_REMOVE")
            with self.subTest(name=name):
                self.rejected(self.preserve)
                self.assertFalse(self.slot.exists())
                self.assertEqual(target.read_bytes(), b"PRIVATE_CONTENT_DO_NOT_REMOVE")
            if original is None:
                target.unlink()
            else:
                target.write_bytes(original)

    def test_source_symlink_or_nonprivate_source_is_refused(self):
        linked = self.root / "linked-source"
        linked.symlink_to(self.source, target_is_directory=True)
        self.rejected(lambda: sources.preserve_bundle(linked, self.slot, self.revision))
        self.source.chmod(0o755)
        self.rejected(self.preserve)
        self.assertFalse(self.slot.exists())

    def test_overlapping_or_symlinked_artifact_paths_are_refused(self):
        self.rejected(
            lambda: sources.preserve_bundle(
                self.source, self.source / "artifact", self.revision
            )
        )
        linked = self.root / "linked-artifacts"
        linked.symlink_to(self.artifacts, target_is_directory=True)
        self.rejected(
            lambda: sources.preserve_bundle(
                self.source, linked / self.revision, self.revision
            )
        )

    def test_existing_partial_or_unknown_artifact_is_never_replaced(self):
        self.slot.mkdir(mode=0o700)
        target = self.slot / "source.bundle"
        target.write_bytes(b"PRIVATE_PARTIAL_BUNDLE")
        target.chmod(0o600)
        self.rejected(self.preserve)
        self.assertEqual(target.read_bytes(), b"PRIVATE_PARTIAL_BUNDLE")
        self.assertFalse((self.slot / "source.json").exists())

    def test_extra_file_is_not_silently_discarded(self):
        self.preserve()
        extra = self.slot / "unrecognized"
        extra.write_bytes(b"fixture")
        extra.chmod(0o600)
        self.rejected(self.verify)
        self.assertTrue(extra.exists())

    def test_tampered_bytes_or_manifest_are_refused(self):
        self.preserve()
        raw = (self.slot / "source.bundle").read_bytes()
        (self.slot / "source.bundle").write_bytes(raw + b"changed")
        self.rejected(self.verify)
        self.rebind(raw)
        record = json.loads((self.slot / "source.json").read_bytes())
        for key, value in (
            ("revision", "a" * 40),
            ("bytes", True),
            ("sha256", "0" * 64),
            ("bundle_name", "../other.bundle"),
        ):
            changed = record | {key: value}
            (self.slot / "source.json").write_bytes(private_io.canonical(changed))
            with self.subTest(key=key):
                self.rejected(self.verify)
        self.rebind(raw)

    def test_header_only_bundle_checks_do_not_replace_actual_restore_proof(self):
        self.preserve()
        raw = (self.slot / "source.bundle").read_bytes()
        header = raw[: raw.index(b"\n\n") + 2]
        self.rebind(header + b"PACK\0\0\0\2\0\0\0\1")
        self.assertTrue(
            self.git("bundle", "list-heads", str(self.slot / "source.bundle"))
        )
        self.git("bundle", "verify", str(self.slot / "source.bundle"))
        self.rejected(self.verify)
        self.assertTrue((self.source / "program.py").exists())

    def test_bundle_symlink_and_hardlink_are_refused(self):
        self.preserve()
        bundle = self.slot / "source.bundle"
        original = self.root / "original.bundle"
        original.write_bytes(bundle.read_bytes())
        original.chmod(0o600)
        for link in ("symlink", "hardlink"):
            bundle.unlink()
            if link == "symlink":
                bundle.symlink_to(original)
            else:
                os.link(original, bundle)
            with self.subTest(link=link):
                self.rejected(self.verify)
        self.assertTrue(original.exists())

    def test_manifest_or_artifact_directory_permission_drift_is_refused(self):
        self.preserve()
        for target, mode, restored in (
            (self.slot / "source.json", 0o644, 0o600),
            (self.slot / "source.bundle", 0o660, 0o600),
            (self.slot, 0o750, 0o700),
        ):
            target.chmod(mode)
            with self.subTest(name=target.name):
                self.rejected(self.verify)
            target.chmod(restored)

    def test_size_bound_refuses_before_using_large_bundle(self):
        self.preserve()
        with patch.object(sources, "MAX_BUNDLE_BYTES", 1):
            self.rejected(self.verify)

    def test_interrupted_manifest_publication_preserves_partial_evidence_for_recovery(
        self,
    ):
        with patch.object(
            private_io, "write_json", side_effect=OSError("fixture interruption")
        ):
            self.rejected(self.preserve)
        raw = (self.slot / "source.bundle").read_bytes()
        self.assertFalse((self.slot / "source.json").exists())
        self.rejected(self.preserve)
        self.assertEqual(raw, (self.slot / "source.bundle").read_bytes())
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), self.revision)
        self.assertTrue((self.source / "program.py").exists())

    def test_verification_does_not_execute_checkout_hooks_or_source(self):
        marker = self.root / "hook-ran"
        hook = self.source / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
        hook.chmod(0o700)
        self.preserve()
        self.verify()
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
