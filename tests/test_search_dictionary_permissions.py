from __future__ import annotations

import errno
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
SPEC = importlib.util.spec_from_file_location(
    "search_dictionary_permissions", HELPER
)
assert SPEC is not None and SPEC.loader is not None
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


class SearchDictionaryPermissionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="dictionary-policy-"
        )
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

    def test_converges_directories_and_regular_files_then_check_accepts(
        self,
    ) -> None:
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
        opensearch_mode = stat.S_IMODE(
            (self.root / "opensearch").stat().st_mode
        )

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

        with mock.patch.object(
            POLICY.os, "getuid", return_value=os.getuid() + 1
        ):
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
        bootstrap = (ROOT / "scripts/bootstrap-catalog.sh").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            bootstrap.index("flock -n 9"),
            bootstrap.index('"$dictionary_permissions" --converge'),
        )
        self.assertEqual(
            1, bootstrap.count('"$dictionary_permissions" --converge')
        )
        self.assertEqual(
            1,
            bootstrap.count("timeout --signal=TERM --kill-after=5s 60s"),
        )

    def test_limits_keep_global_count_and_partial_modes(self) -> None:
        nested = self.dictionary / "nested"
        nested.mkdir(mode=0o755)
        leaf = nested / "words.txt"
        leaf.write_bytes(b"original\n")
        leaf.chmod(0o644)
        self.dictionary.chmod(0o755)
        parents = (self.root, self.root / "opensearch")
        parent_modes = [stat.S_IMODE(path.stat().st_mode) for path in parents]

        with mock.patch.object(POLICY, "MAX_ENTRIES", 1):
            with self.assertRaisesRegex(POLICY.PolicyError, "entry limit"):
                POLICY.enforce(str(self.root), True)
        self.assertEqual(0o700, stat.S_IMODE(self.dictionary.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(nested.stat().st_mode))
        self.assertEqual(0o644, stat.S_IMODE(leaf.stat().st_mode))
        self.assertEqual(b"original\n", leaf.read_bytes())

        outside = self.root / "outside.txt"
        outside.write_bytes(b"outside\n")
        outside.chmod(0o644)
        leaf.unlink()
        leaf.symlink_to(outside)
        self.dictionary.chmod(0o755)
        nested.chmod(0o755)
        with mock.patch.object(POLICY, "MAX_DEPTH", 1):
            with self.assertRaisesRegex(POLICY.PolicyError, "depth limit"):
                POLICY.enforce(str(self.root), True)
        self.assertEqual(0o700, stat.S_IMODE(nested.stat().st_mode))
        self.assertEqual(0o644, stat.S_IMODE(outside.stat().st_mode))
        self.assertEqual(b"outside\n", outside.read_bytes())
        self.assertEqual(
            parent_modes,
            [stat.S_IMODE(path.stat().st_mode) for path in parents],
        )

    def test_open_identity_change_refuses_before_mode_and_closes_fds(
        self,
    ) -> None:
        self.dictionary.chmod(0o700)
        leaf = self.dictionary / "words.txt"
        saved = self.root / "original.txt"
        leaf.write_bytes(b"original\n")
        leaf.chmod(0o644)
        original_open, original_close = os.open, os.close
        opened_fds, closed_fds = [], []

        def opened(path, flags, *args, **kwargs):
            if path == leaf.name and not flags & os.O_PATH:
                leaf.rename(saved)
                leaf.write_bytes(b"replacement\n")
                leaf.chmod(0o644)
            descriptor = original_open(path, flags, *args, **kwargs)
            opened_fds.append(descriptor)
            return descriptor

        def closed(descriptor):
            closed_fds.append(descriptor)
            return original_close(descriptor)

        with (
            mock.patch.object(POLICY.os, "open", side_effect=opened),
            mock.patch.object(POLICY.os, "close", side_effect=closed),
        ):
            with self.assertRaisesRegex(
                POLICY.PolicyError, "changed while it was opened"
            ) as raised:
                POLICY.enforce(str(self.root), True)
        self.assertEqual(5, len(opened_fds))
        self.assertEqual(list(reversed(opened_fds)), closed_fds)
        for descriptor in opened_fds:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertIs(type(raised.exception), POLICY.PolicyError)
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(0o644, stat.S_IMODE(leaf.stat().st_mode))
        self.assertEqual(0o644, stat.S_IMODE(saved.stat().st_mode))
        self.assertEqual(b"original\n", saved.read_bytes())
        self.assertEqual(b"replacement\n", leaf.read_bytes())

    def test_post_name_change_preserves_close_error_priority(self) -> None:
        self.dictionary.chmod(0o700)
        original_open, original_close = os.open, os.close
        leaf = self.dictionary / "words.txt"
        saved = self.root / "original.txt"
        for close_fault in (False, True):
            with self.subTest(close_fault=close_fault):
                leaf.write_bytes(b"original\n")
                leaf.chmod(0o644)
                opened_fds, closed_fds = [], []
                close_error = OSError(errno.EIO, "synthetic close fault")

                def opened(path, flags, *args, **kwargs):
                    descriptor = original_open(path, flags, *args, **kwargs)
                    opened_fds.append(descriptor)
                    return descriptor

                def closed(descriptor):
                    original_close(descriptor)
                    closed_fds.append(descriptor)
                    if descriptor == opened_fds[-1]:
                        leaf.rename(saved)
                        leaf.write_bytes(b"replacement\n")
                        leaf.chmod(0o644)
                    if close_fault and descriptor == opened_fds[-2]:
                        raise close_error

                expected = OSError if close_fault else POLICY.PolicyError
                with (
                    mock.patch.object(POLICY.os, "open", side_effect=opened),
                    mock.patch.object(POLICY.os, "close", side_effect=closed),
                ):
                    with self.assertRaises(expected) as raised:
                        POLICY.enforce(str(self.root), True)
                self.assertEqual(5, len(opened_fds))
                self.assertEqual(list(reversed(opened_fds)), closed_fds)
                for descriptor in opened_fds:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)
                error = raised.exception
                if close_fault:
                    self.assertIs(error, close_error)
                    error = error.__context__
                self.assertIs(type(error), POLICY.PolicyError)
                self.assertEqual(
                    "the dictionary tree changed while it was inspected",
                    str(error),
                )
                self.assertEqual(0o600, stat.S_IMODE(saved.stat().st_mode))
                self.assertEqual(0o644, stat.S_IMODE(leaf.stat().st_mode))
                self.assertEqual(b"original\n", saved.read_bytes())
                self.assertEqual(b"replacement\n", leaf.read_bytes())
                saved.unlink()
                leaf.unlink()


if __name__ == "__main__":
    unittest.main()
