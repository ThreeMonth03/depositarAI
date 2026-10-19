from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/search_dictionary_permissions.py"
SPEC = importlib.util.spec_from_file_location("search_dictionary_permissions", HELPER)
assert SPEC is not None and SPEC.loader is not None
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


class SearchDictionaryPermissionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="dictionary-policy-")
        self.root = Path(self.temporary.name) / "storage"
        self.dictionary = self.root / "opensearch" / "dictionary"
        self.dictionary.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_helper(self, action: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HELPER), action, str(self.root)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_converges_directories_and_regular_files_then_check_accepts(self) -> None:
        nested = self.dictionary / "nested"
        nested.mkdir(mode=0o755)
        first = self.dictionary / "synonym.txt"
        second = nested / "mapping.txt"
        first.write_text("one\n", encoding="utf-8")
        second.write_text("two\n", encoding="utf-8")
        self.dictionary.chmod(0o755)
        nested.chmod(0o750)
        first.chmod(0o644)
        second.chmod(0o660)
        opensearch_mode = stat.S_IMODE((self.root / "opensearch").stat().st_mode)

        converged = self.run_helper("--converge")
        self.assertEqual(0, converged.returncode, converged.stderr)
        self.assertEqual(0o700, stat.S_IMODE(self.dictionary.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(nested.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(first.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(second.stat().st_mode))
        self.assertEqual(
            opensearch_mode,
            stat.S_IMODE((self.root / "opensearch").stat().st_mode),
        )
        checked = self.run_helper("--check")
        self.assertEqual(0, checked.returncode, checked.stderr)

    def test_check_fails_closed_instead_of_repairing_wrong_mode(self) -> None:
        path = self.dictionary / "words.txt"
        path.write_text("words\n", encoding="utf-8")
        self.dictionary.chmod(0o700)
        path.chmod(0o644)

        result = self.run_helper("--check")

        self.assertNotEqual(0, result.returncode)
        self.assertIn("unsafe permissions", result.stderr)
        self.assertEqual(0o644, stat.S_IMODE(path.stat().st_mode))

    def test_symlink_special_file_and_hardlink_are_rejected(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        outside.chmod(0o644)
        (self.dictionary / "escape").symlink_to(outside)
        symlink_result = self.run_helper("--converge")
        self.assertNotEqual(0, symlink_result.returncode)
        self.assertEqual(0o644, stat.S_IMODE(outside.stat().st_mode))
        (self.dictionary / "escape").unlink()

        os.mkfifo(self.dictionary / "pipe")
        special_result = self.run_helper("--converge")
        self.assertNotEqual(0, special_result.returncode)
        (self.dictionary / "pipe").unlink()

        linked = self.dictionary / "linked.txt"
        os.link(outside, linked)
        hardlink_result = self.run_helper("--converge")
        self.assertNotEqual(0, hardlink_result.returncode)
        self.assertIn("multiply-linked", hardlink_result.stderr)
        self.assertEqual(0o644, stat.S_IMODE(outside.stat().st_mode))

    def test_wrong_owner_cross_device_and_bounds_fail_closed(self) -> None:
        self.dictionary.chmod(0o700)
        path = self.dictionary / "words.txt"
        path.write_text("words\n", encoding="utf-8")
        path.chmod(0o600)

        with mock.patch.object(POLICY.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(POLICY.PolicyError, "unsafe metadata"):
                POLICY.enforce(str(self.root), False)

        metadata = types.SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_uid=os.getuid(),
            st_dev=self.root.stat().st_dev + 1,
            st_nlink=1,
        )
        with self.assertRaisesRegex(POLICY.PolicyError, "filesystem boundary"):
            POLICY._validate_metadata(
                metadata,
                expected_uid=os.getuid(),
                expected_device=self.root.stat().st_dev,
                kind="file",
                require_single_link=True,
            )

        second = self.dictionary / "second.txt"
        second.write_text("second\n", encoding="utf-8")
        second.chmod(0o600)
        with mock.patch.object(POLICY, "MAX_ENTRIES", 1):
            with self.assertRaisesRegex(POLICY.PolicyError, "entry limit"):
                POLICY.enforce(str(self.root), False)

    def test_bootstrap_uses_the_shared_policy(self) -> None:
        bootstrap = (ROOT / "scripts/bootstrap-search-canary.sh").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            bootstrap.index("flock -n 9"),
            bootstrap.index('"$dictionary_permissions" --converge'),
        )
        self.assertEqual(1, bootstrap.count('"$dictionary_permissions" --converge'))
        self.assertEqual(
            1,
            bootstrap.count("timeout --signal=TERM --kill-after=5s 60s"),
        )


if __name__ == "__main__":
    unittest.main()
