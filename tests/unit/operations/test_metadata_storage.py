"""Metadata record policy through real files, independent of sync orchestration."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from search.catalog.build.policy import BundleError
from search.operations.metadata import storage, sync
from search.operations.shared import private_io


class MetadataStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-metadata-storage-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fd = private_io.open_directory(self.root)
        self.addCleanup(os.close, self.fd)

    def record(self, raw, *, name="record.json", mode=0o600):
        path = self.root / name
        path.write_bytes(raw)
        path.chmod(mode)
        return path

    def test_sync_exports_the_same_storage_objects_and_error_name(self):
        aliases = {
            "_real_path": "real_path",
            "_same_file": "same_file",
            "_file_bytes": "file_bytes",
            "_json_record": "read_record",
            "_write_record": "write_record",
            "_private_directory": "private_directory",
            "SyncError": "SyncError",
            "MAX_RECORD_BYTES": "MAX_RECORD_BYTES",
        }
        for legacy, canonical in aliases.items():
            with self.subTest(name=legacy):
                self.assertIs(getattr(sync, legacy), getattr(storage, canonical))
        self.assertEqual("SyncError", storage.SyncError.__name__)
        self.assertEqual(4 * 1024**2, storage.MAX_RECORD_BYTES)

    def test_import_does_not_load_cache_or_catalog_orchestrators(self):
        root = Path(__file__).resolve().parents[3]
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(root)!r})\n"
            "import search.operations.metadata.storage\n"
            "for namespace in ('search.operations.metadata.sync', "
            "'search.operations.catalog', 'search.operations.lifecycle', "
            "'search.operations.deployment'):\n"
            "    assert not any(name == namespace or "
            "name.startswith(namespace + '.') for name in sys.modules), namespace\n"
            "print('metadata storage imports independently')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("metadata storage imports independently\n", completed.stdout)

    def test_valid_unicode_and_exact_json_depth_and_number_limits(self):
        nested = 0
        for _ in range(32):
            nested = [nested]
        for raw, expected in (
            ('{"主題":"水質"}'.encode(), {"主題": "水質"}),
            (b"[" * 32 + b"0" + b"]" * 32, nested),
            (b"9" * 128, int("9" * 128)),
            (b"1." + b"0" * 126, 1.0),
        ):
            with self.subTest(size=len(raw)):
                self.record(raw)
                self.assertEqual(expected, storage.read_record(self.fd, "record.json"))

    def test_strict_json_errors_are_not_replaced_with_sync_errors(self):
        for raw, exception in (
            (b'{"x":1,"x":2}', BundleError),
            (b'{"nested":{"x":1,"x":2}}', BundleError),
            (b"NaN", BundleError),
            (b"Infinity", BundleError),
            (b"1e999", BundleError),
            (b"9" * 129, BundleError),
            (b"1." + b"0" * 127, BundleError),
            (b"[" * 33 + b"0" + b"]" * 33, BundleError),
            (b'{"x":"\\ud800"}', BundleError),
            (b'{"\\ud800":1}', BundleError),
            (b'"\xff"', UnicodeDecodeError),
            (b"{", json.JSONDecodeError),
        ):
            with self.subTest(raw=raw[:30], size=len(raw)):
                self.record(raw)
                with self.assertRaises(exception) as caught:
                    storage.read_record(self.fd, "record.json")
                self.assertIs(type(caught.exception), exception)

    def test_record_byte_limit_accepts_exact_cap_and_rejects_one_more(self):
        raw = b"{}" + b" " * (storage.MAX_RECORD_BYTES - 2)
        self.record(raw)
        self.assertEqual({}, storage.read_record(self.fd, "record.json"))
        self.record(raw + b" ")
        with self.assertRaisesRegex(
            storage.SyncError, "^cache file ownership, mode, links or size are unsafe$"
        ) as caught:
            storage.read_record(self.fd, "record.json")
        self.assertIsInstance(caught.exception.__cause__, ValueError)

    def test_unsafe_file_mode_retains_cache_error_and_private_io_cause(self):
        self.record(b"{}", mode=0o644)
        with self.assertRaisesRegex(
            storage.SyncError, "^cache file ownership, mode, links or size are unsafe$"
        ) as caught:
            storage.read_record(self.fd, "record.json")
        self.assertEqual("private_io_file_unsafe", str(caught.exception.__cause__))

    def test_missing_and_symlink_files_propagate_os_errors(self):
        with self.assertRaises(FileNotFoundError):
            storage.read_record(self.fd, "missing.json")
        target = self.record(b"{}")
        (self.root / "symlink.json").symlink_to(target)
        with self.assertRaises(OSError) as caught:
            storage.read_record(self.fd, "symlink.json")
        self.assertEqual(errno.ELOOP, caught.exception.errno)

    def test_record_publication_preserves_no_clobber_and_explicit_replace(self):
        storage.write_record(self.fd, "record.json", {"topic": "水質"})
        self.assertEqual(0o600, (self.root / "record.json").stat().st_mode & 0o777)
        with self.assertRaises(FileExistsError):
            storage.write_record(self.fd, "record.json", {"topic": "降雨"})
        self.assertEqual({"topic": "水質"}, storage.read_record(self.fd, "record.json"))
        storage.write_record(self.fd, "record.json", {"topic": "降雨"}, replace=True)
        self.assertEqual({"topic": "降雨"}, storage.read_record(self.fd, "record.json"))

    def test_invalid_and_oversized_writes_keep_error_cause_and_publish_nothing(self):
        for value in (
            {"value": float("nan")},
            {"value": "x" * storage.MAX_RECORD_BYTES},
        ):
            with self.subTest(value_type=type(value["value"]).__name__):
                with self.assertRaisesRegex(
                    storage.SyncError, "^cache record publication invariant failed$"
                ) as caught:
                    storage.write_record(self.fd, "invalid.json", value)
                self.assertIsInstance(caught.exception.__cause__, ValueError)
                self.assertFalse((self.root / "invalid.json").exists())

    def test_private_directory_preserves_mode_cause_and_existing_path_error(self):
        descriptor = storage.private_directory(self.fd, "child", create=True)
        os.close(descriptor)
        self.assertEqual(0o700, (self.root / "child").stat().st_mode & 0o777)
        with self.assertRaises(FileExistsError):
            storage.private_directory(self.fd, "child", create=True)
        (self.root / "child").chmod(0o755)
        with self.assertRaisesRegex(
            storage.SyncError, "^cache directory must remain operator-owned mode 0700$"
        ) as caught:
            storage.private_directory(self.fd, "child")
        self.assertEqual("private_io_directory_unsafe", str(caught.exception.__cause__))

    def test_underlying_io_errors_pass_through_without_wrapping(self):
        for name, invoke in (
            (
                "read_file",
                lambda: storage.file_bytes(
                    self.fd, "record.json", mode=0o600, limit=64
                ),
            ),
            ("write_json", lambda: storage.write_record(self.fd, "record.json", {})),
            (
                "open_child_directory",
                lambda: storage.private_directory(self.fd, "child"),
            ),
        ):
            failure = OSError(errno.EIO, "fixture IO failure")
            with (
                self.subTest(operation=name),
                patch.object(private_io, name, side_effect=failure),
            ):
                with self.assertRaises(OSError) as caught:
                    invoke()
                self.assertIs(failure, caught.exception)

    def test_real_paths_and_file_identity_keep_their_existing_contract(self):
        path = self.record(b"{}")
        self.assertEqual(path, storage.real_path(path))
        link = self.root / "symlink"
        link.symlink_to(path)
        for unsafe in (Path("relative.json"), link):
            with self.subTest(path=unsafe), self.assertRaises(storage.SyncError):
                storage.real_path(unsafe)
        before = path.stat()
        self.assertTrue(storage.same_file(before, path.stat()))
        path.chmod(0o400)
        self.assertFalse(storage.same_file(before, path.stat()))


if __name__ == "__main__":
    unittest.main()
