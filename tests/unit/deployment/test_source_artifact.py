"""Real-Git source handoff tests; no Docker, models or external repository."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/ci/source_artifact.py"
spec = importlib.util.spec_from_file_location("depositar_source_artifact", SCRIPT)
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)


class SourceArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="depositar-source-artifact-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "checkout"
        self.repo.mkdir(mode=0o700)
        for directory in (
            "search/operations/shared",
            "scripts/ci",
            ".github/workflows",
        ):
            (self.repo / directory).mkdir(parents=True)
        source = ROOT / "search/operations/shared/source_identity.py"
        (self.repo / "search/operations/shared/source_identity.py").write_bytes(
            source.read_bytes()
        )
        for relative in (
            "search/__init__.py",
            "search/operations/__init__.py",
            "search/operations/shared/__init__.py",
        ):
            (self.repo / relative).write_text('"""Fixture package."""\n')
        (self.repo / "scripts/depositarai.py").write_text(
            "import argparse\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('--config', required=True)\n"
            "parser.parse_args()\n"
        )
        (self.repo / "README.md").write_text("Public source fixture.\n")
        (self.repo / "scripts/ci/tracked-helper.sh").write_text("#!/bin/bash\nexit 0\n")
        self.git("init", "--quiet")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "--quiet",
            "-m",
            "Fixture",
        )
        self.revision = self.git("rev-parse", "HEAD").strip()
        self.scanner = self.root / "gitleaks"
        self.scanner.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "if sys.argv[1:] == ['version']:\n"
            "    print(os.environ.get('TEST_SCANNER_VERSION', '8.30.1'))\n"
            "    sys.exit(0)\n"
            "with open(os.environ['TEST_SCAN_LOG'], 'w') as output:\n"
            "    ignore = sys.argv[sys.argv.index('--gitleaks-ignore-path') + 1]\n"
            "    with open(ignore) as ignored:\n"
            "        ignore_contents = ignored.read()\n"
            "    json.dump({'argv': sys.argv[1:], 'env_keys': list(os.environ), 'ignore': ignore_contents}, output)\n"
            "sys.exit(int(os.environ.get('TEST_SCAN_STATUS', '0')))\n"
        )
        self.scanner.chmod(0o700)
        self.scan_log = self.root / "scan.json"
        self.environment = mock.patch.dict(os.environ, TEST_SCAN_LOG=str(self.scan_log))
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def git(self, *args):
        result = subprocess.run(
            ["git", "-C", str(self.repo), *map(str, args)],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def package(self):
        path = self.root / "package"
        result = artifact.package(
            path, self.revision, self.scanner, repo_root=self.repo
        )
        return path, result

    def update_digests(self, package):
        records = []
        for name in artifact._names(self.revision):
            digest = hashlib.sha256((package / name).read_bytes()).hexdigest()
            records.append(digest + "  " + name + "\n")
        (package / "SHA256SUMS").write_text("".join(records))

    def assert_rejected(self, code, action):
        with self.assertRaisesRegex(artifact.ArtifactError, code):
            action()

    def reviewed_entry(self):
        return {
            "fingerprint": self.revision + ":README.md:generic-api-key:1",
            "reasonCode": "public_source_revision",
        }

    def commit_policy(self, value):
        path = self.repo / artifact.REVIEWED_MANIFEST
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        self.git("add", artifact.REVIEWED_MANIFEST)
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "--quiet",
            "-m",
            "Reviewed fixture policy",
        )
        self.revision = self.git("rev-parse", "HEAD").strip()
        return path

    def test_real_bundle_archive_restore_is_exact_clean_and_source_only(self):
        # Neither tracked-only packaging nor history scanning reads this file.
        (self.repo / ".env.private").write_text("not-a-source-file\n")
        package, report = self.package()
        self.assertEqual(report["source_revision"], self.revision)
        self.assertEqual(report["secret_scan"], "selected_reachable_history")
        self.assertFalse(report["runtime_included"])
        self.assertEqual(
            set(package.iterdir()),
            {
                package / name
                for name in (*artifact._names(self.revision), "SHA256SUMS")
            },
        )
        self.assertEqual(0o700, package.stat().st_mode & 0o777)
        with tarfile.open(package / artifact._names(self.revision)[0]) as archive:
            self.assertNotIn("depositar-ai/.env.private", archive.getnames())
        destination = self.root / "restored"
        restored = artifact.restore(package, self.revision, destination)
        self.assertEqual(restored["status"], "restored")
        self.assertTrue(restored["archive_matches_git_tree"])
        self.assertTrue(restored["clean_git_checkout"])
        self.assertTrue(restored["cli_help_verified"])
        self.assertFalse(restored["runtime_restored"])
        self.assertFalse((destination / ".env.private").exists())

    def test_private_caller_restores_git_modes_without_relaxing_its_umask(self):
        helper = self.repo / "scripts/ci/tracked-helper.sh"
        helper.chmod(0o755)
        self.git("update-index", "--chmod=+x", "scripts/ci/tracked-helper.sh")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "--quiet",
            "-m",
            "Executable fixture",
        )
        self.revision = self.git("rev-parse", "HEAD").strip()
        previous = os.umask(0o077)
        try:
            package, _ = self.package()
            destination = self.root / "restored-private"
            report = artifact.restore(package, self.revision, destination)
            tree = artifact._tree(self.repo, self.revision)
            self.assertTrue(tree["scripts/ci/tracked-helper.sh"][1])
            for relative, (_, executable) in tree.items():
                restored = destination / relative
                with self.subTest(source=relative):
                    self.assertEqual(
                        0o755 if executable else 0o644,
                        restored.stat().st_mode & 0o777,
                    )
                    self.assertEqual(
                        (self.repo / relative).read_bytes(), restored.read_bytes()
                    )
            for path in destination.rglob("*"):
                if path.is_dir() and ".git" not in path.relative_to(destination).parts:
                    self.assertEqual(0o755, path.stat().st_mode & 0o777, str(path))
            self.assertEqual(0o700, destination.stat().st_mode & 0o777)
            self.assertEqual(0o700, (destination / ".git").stat().st_mode & 0o777)
            self.assertEqual(0o700, package.stat().st_mode & 0o777)
            for path in package.iterdir():
                self.assertEqual(0o600, path.stat().st_mode & 0o777, str(path))
            secret = self.root / "operator-secret"
            secret.write_text("private fixture\n")
            self.assertEqual(0o600, secret.stat().st_mode & 0o777)
            self.assertEqual(0o077, os.umask(0o077))
            self.assertTrue(report["clean_git_checkout"])
            self.assertEqual(
                b"",
                artifact._git(
                    destination, "status", "--porcelain", "--untracked-files=all"
                ),
            )
        finally:
            os.umask(previous)

    def test_history_scan_is_bound_to_expected_commit_and_cannot_inherit_ignores(self):
        with mock.patch.dict(
            os.environ, GITLEAKS_CONFIG="/do/not/use", GITLEAKS_LICENSE="not-needed"
        ):
            self.package()
        scan = json.loads(self.scan_log.read_text())
        self.assertIn("--log-opts=" + self.revision, scan["argv"])
        self.assertIn("--ignore-gitleaks-allow", scan["argv"])
        self.assertIn("--redact=100", scan["argv"])
        self.assertFalse(any(name.startswith("GITLEAKS_") for name in scan["env_keys"]))
        self.assertNotIn("--all", scan["argv"])
        self.assertEqual("", scan["ignore"])

    def test_committed_exact_policy_is_reported_with_digest_and_not_a_rule_skip(self):
        entry = self.reviewed_entry()
        path = self.commit_policy({"schema_version": 1, "entries": [entry]})
        _, report = self.package()
        self.assertEqual(self.revision, report["reviewed_policy"]["source_revision"])
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(),
            report["reviewed_policy"]["sha256"],
        )
        self.assertEqual(1, report["reviewed_policy"]["fingerprint_count"])
        scan = json.loads(self.scan_log.read_text())
        self.assertEqual(entry["fingerprint"] + "\n", scan["ignore"])
        self.assertNotIn("--enable-rule", scan["argv"])

    def test_empty_committed_policy_still_scans_history_with_no_exceptions(self):
        path = self.commit_policy({"schema_version": 1, "entries": []})
        _, report = self.package()
        self.assertEqual(self.revision, report["reviewed_policy"]["source_revision"])
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(),
            report["reviewed_policy"]["sha256"],
        )
        self.assertEqual(0, report["reviewed_policy"]["fingerprint_count"])
        scan = json.loads(self.scan_log.read_text())
        self.assertEqual("", scan["ignore"])
        self.assertIn("--log-opts=" + self.revision, scan["argv"])
        self.assertNotIn("--enable-rule", scan["argv"])

    def test_uncommitted_policy_cannot_override_selected_commit(self):
        path = self.repo / artifact.REVIEWED_MANIFEST
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"schema_version": 1, "entries": [self.reviewed_entry()]})
        )
        _, report = self.package()
        self.assertIsNone(report["reviewed_policy"]["sha256"])
        self.assertEqual(0, report["reviewed_policy"]["fingerprint_count"])
        self.assertEqual("", json.loads(self.scan_log.read_text())["ignore"])

    def test_same_path_line_in_new_commit_does_not_match_old_exception(self):
        entry = self.reviewed_entry()
        self.commit_policy({"schema_version": 1, "entries": [entry]})
        fingerprints, _ = artifact._reviewed_entries(
            self.repo, self.revision, artifact._tree(self.repo, self.revision)
        )
        self.assertIn(entry["fingerprint"], fingerprints)
        self.assertNotIn(self.revision + ":README.md:generic-api-key:1", fingerprints)
        with mock.patch.dict(os.environ, TEST_SCAN_STATUS="1"):
            self.assert_rejected("source_secret_scan_failed", self.package)
        self.assertFalse((self.root / "package").exists())

    def test_reviewed_manifest_rejects_unknown_duplicate_and_nonfinite_schema(self):
        cases = (
            {"schema_version": True, "entries": []},
            {"schema_version": 1, "entries": [], "extra": []},
            '{"schema_version":1,"schema_version":1,"entries":[]}',
            '{"schema_version":1,"entries":NaN}',
        )
        for value in cases:
            with self.subTest(value=value):
                self.commit_policy(value)
                self.assert_rejected("source_review_manifest_invalid", self.package)
        self.assertFalse(self.scan_log.exists())

    def test_policy_cannot_allow_rules_paths_patterns_or_duplicate_fingerprints(self):
        entry = self.reviewed_entry()
        cases = (
            [{**entry, "reasonCode": "ignore_all"}],
            [{**entry, "fingerprint": self.revision + ":README.md:github-pat:1"}],
            [{**entry, "fingerprint": self.revision + ":../outside:generic-api-key:1"}],
            [{**entry, "fingerprint": self.revision + ":README.md:generic-api-key:0"}],
            [{**entry, "rule": "generic-api-key"}],
            [entry, entry],
        )
        for entries in cases:
            with self.subTest(entries=entries):
                self.commit_policy({"schema_version": 1, "entries": entries})
                with self.assertRaises(artifact.ArtifactError):
                    self.package()
        self.assertFalse(self.scan_log.exists())

    def test_policy_requires_real_reachable_commit_file_and_line(self):
        entry = self.reviewed_entry()
        cases = (
            [{**entry, "fingerprint": "0" * 40 + ":README.md:generic-api-key:1"}],
            [
                {
                    **entry,
                    "fingerprint": self.revision + ":missing.txt:generic-api-key:1",
                }
            ],
            [{**entry, "fingerprint": self.revision + ":README.md:generic-api-key:2"}],
        )
        for entries in cases:
            with self.subTest(entries=entries):
                self.commit_policy({"schema_version": 1, "entries": entries})
                self.assert_rejected("source_review_position_unavailable", self.package)
        self.assertFalse(self.scan_log.exists())

    def test_scanner_failure_prevents_artifact_directory_creation(self):
        with mock.patch.dict(os.environ, TEST_SCAN_STATUS="1"):
            self.assert_rejected("source_secret_scan_failed", self.package)
        self.assertFalse((self.root / "package").exists())

    def test_scanner_version_mismatch_prevents_history_scan(self):
        with mock.patch.dict(os.environ, TEST_SCANNER_VERSION="8.18.4"):
            self.assert_rejected("source_scanner_version_invalid", self.package)
        self.assertFalse(self.scan_log.exists())

    def test_dirty_tracked_docs_prevent_packaging(self):
        (self.repo / "README.md").write_text("Uncommitted modification.\n")
        self.assert_rejected("source_command_failed", self.package)
        self.assertFalse(self.scan_log.exists())

    def test_untracked_ci_helper_prevents_packaging(self):
        (self.repo / "scripts/ci/new-helper.sh").write_text("#!/bin/bash\n")
        self.assert_rejected("source_package_helpers_dirty", self.package)
        self.assertFalse(self.scan_log.exists())

    def test_untracked_workflow_prevents_packaging(self):
        (self.repo / ".github/workflows/untracked.yml").write_text("name: Untracked\n")
        self.assert_rejected("source_package_helpers_dirty", self.package)
        self.assertFalse(self.scan_log.exists())

    def test_wrong_expected_revision_prevents_packaging(self):
        self.assert_rejected(
            "source_revision_mismatch",
            lambda: artifact.package(
                self.root / "package", "0" * 40, self.scanner, repo_root=self.repo
            ),
        )
        self.assertFalse(self.scan_log.exists())

    def test_existing_output_and_checkout_paths_are_not_overwritten(self):
        output = self.root / "package"
        output.mkdir()
        sentinel = output / "user-file"
        sentinel.write_text("preserve\n")
        self.assert_rejected("source_destination_exists", self.package)
        self.assertEqual("preserve\n", sentinel.read_text())
        self.assert_rejected(
            "source_destination_not_dedicated",
            lambda: artifact.package(
                self.repo / "release", self.revision, self.scanner, repo_root=self.repo
            ),
        )

    def test_digest_tampering_fails_before_restore_directory_exists(self):
        package, _ = self.package()
        archive = package / artifact._names(self.revision)[0]
        with archive.open("ab") as output:
            output.write(b"corruption")
        target = self.root / "restored"
        self.assert_rejected(
            "source_artifact_digest_mismatch",
            lambda: artifact.restore(package, self.revision, target),
        )
        self.assertFalse(target.exists())

    def test_manifest_cannot_add_duplicate_or_arbitrary_filename(self):
        package, _ = self.package()
        manifest = package / "SHA256SUMS"
        content = manifest.read_text()
        for invalid in (
            content + content.splitlines()[0] + "\n",
            content.replace("SOURCE_REVISION", "../outside"),
        ):
            with self.subTest(manifest=invalid[-90:]):
                manifest.write_text(invalid)
                self.assert_rejected(
                    "source_digest_manifest_invalid",
                    lambda: artifact.restore(
                        package, self.revision, self.root / "restored"
                    ),
                )
        self.assertFalse((self.root / "restored").exists())

    def test_manifest_update_cannot_hide_wrong_source_revision(self):
        package, _ = self.package()
        (package / "SOURCE_REVISION").write_text("0" * 40 + "\n")
        self.update_digests(package)
        self.assert_rejected(
            "source_revision_mismatch",
            lambda: artifact.restore(package, self.revision, self.root / "restored"),
        )
        self.assertFalse((self.root / "restored").exists())

    def test_archive_corresponds_to_bundle_not_only_manifest_digest(self):
        package, _ = self.package()
        archive = package / artifact._names(self.revision)[0]
        with tarfile.open(archive, "w:gz") as output:
            entry = tarfile.TarInfo("depositar-ai/README.md")
            content = b"Not the committed source.\n"
            entry.size = len(content)
            output.addfile(entry, io.BytesIO(content))
        self.update_digests(package)
        self.assert_rejected(
            "source_archive_inventory_mismatch",
            lambda: artifact.restore(package, self.revision, self.root / "restored"),
        )

    def test_symlink_artifact_is_rejected_without_following_it(self):
        package, _ = self.package()
        archive = package / artifact._names(self.revision)[0]
        original = self.root / "archive"
        archive.rename(original)
        archive.symlink_to(original)
        with self.assertRaises(OSError):
            artifact.restore(package, self.revision, self.root / "restored")
        self.assertTrue(original.exists())
        self.assertFalse((self.root / "restored").exists())

    def test_unsafe_archive_paths_and_links_are_not_extracted(self):
        tree = {"README.md": ("0" * 40, False)}
        for name, kind in (
            ("depositar-ai/../../outside", tarfile.REGTYPE),
            ("other/README.md", tarfile.REGTYPE),
            ("depositar-ai/README.md", tarfile.SYMTYPE),
            ("depositar-ai/README.md", tarfile.LNKTYPE),
        ):
            with self.subTest(name=name, kind=kind):
                archive = self.root / "unsafe.tar.gz"
                with tarfile.open(archive, "w:gz") as output:
                    entry = tarfile.TarInfo(name)
                    entry.type = kind
                    entry.linkname = "../../outside"
                    output.addfile(entry)
                with self.assertRaises(artifact.ArtifactError):
                    artifact._verify_archive(archive, tree)
        self.assertFalse((self.root / "outside").exists())

    def test_restore_wrapper_emits_sanitized_json_on_bad_revision(self):
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts/ci/source-restore.sh"),
                str(self.root),
                "bad",
                str(self.root / "restored"),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(1, result.returncode)
        self.assertEqual(
            {"status": "failed", "code": "source_revision_invalid"},
            json.loads(result.stderr),
        )
        self.assertEqual("", result.stdout)
        self.assertNotIn(str(self.root), result.stderr)


if __name__ == "__main__":
    unittest.main()
