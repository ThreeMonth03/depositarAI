"""FIFO rejection must happen without blocking before regular-file validation."""

from __future__ import annotations

from search.catalog.bundle import _open_sealed
from search.catalog.errors import BundleError

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.catalog.build import policy, storage

from search.operations.metadata import storage as metadata_storage, sync as catalog_sync


class CatalogSafeInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="depositar-safe-input-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fifo = self.root / "input.jsonl"
        os.mkfifo(self.fifo, 0o600)
        self.directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.directory_fd)
        self.real_open = os.open

    def guarded_open(self, path, flags, *args, **kwargs):
        # Fail before the real syscall if a regression would block the test.
        if Path(path).name == self.fifo.name:
            self.assertTrue(flags & os.O_NONBLOCK, "FIFO leaf must be nonblocking")
        return self.real_open(path, flags, *args, **kwargs)

    def test_source_reader_refuses_fifo_without_waiting_for_a_writer(self):
        with patch.object(os, "open", side_effect=self.guarded_open):
            with self.assertRaisesRegex(policy.BundleError, "regular file"):
                storage._safe_source_fd(str(self.fifo))

    def test_artifact_hasher_refuses_fifo_without_waiting_for_a_writer(self):
        with patch.object(os, "open", side_effect=self.guarded_open):
            with self.assertRaisesRegex(policy.BundleError, "unsafe"):
                storage._hash_file(self.directory_fd, self.fifo.name)

    def test_cache_reader_refuses_fifo_without_waiting_for_a_writer(self):
        with patch.object(os, "open", side_effect=self.guarded_open):
            with self.assertRaises(metadata_storage.SyncError):
                metadata_storage.file_bytes(
                    self.directory_fd, self.fifo.name, mode=0o600, limit=4096
                )

    def test_sealed_catalog_loader_refuses_fifo_before_blocking(self):
        with patch.object(os, "open", side_effect=self.guarded_open):
            with self.assertRaisesRegex(BundleError, "unsafe"):
                _open_sealed(self.fifo, expected_uid=os.geteuid(), max_bytes=4096)

    def test_offline_sync_refuses_fifo_and_does_not_publish_latest(self):
        cache = self.root / "cache"
        cache.mkdir(mode=0o700)
        with patch.object(os, "open", side_effect=self.guarded_open):
            with self.assertRaisesRegex(policy.BundleError, "regular file"):
                catalog_sync.sync(cache, snapshot=self.fifo)
        self.assertFalse((cache / "latest.json").exists())

    def test_nonblocking_flag_preserves_regular_file_hash_and_read(self):
        regular = self.root / "regular.jsonl"
        regular.write_bytes(b"public metadata\n")
        regular.chmod(0o400)
        descriptor, info = storage._safe_source_fd(str(regular))
        try:
            self.assertEqual(regular.stat().st_ino, info.st_ino)
            self.assertEqual(b"public metadata\n", os.read(descriptor, 4096))
        finally:
            os.close(descriptor)
        self.assertEqual(
            (hashlib.sha256(b"public metadata\n").hexdigest(), 16),
            storage._hash_file(self.directory_fd, regular.name),
        )
        self.assertEqual(
            b"public metadata\n",
            metadata_storage.file_bytes(
                self.directory_fd, regular.name, mode=0o400, limit=4096
            ),
        )


if __name__ == "__main__":
    unittest.main()
