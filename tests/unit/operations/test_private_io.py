"""Shared private IO policy using only isolated temporary files and stdlib."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from search.operations.shared import private_io as io


class PrivateIOTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-private-io-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fd = io.open_directory(self.root)
        self.addCleanup(os.close, self.fd)

    def private_file(self, name, content=b'{"value":1}\n', mode=0o600):
        path = self.root / name
        path.write_bytes(content)
        path.chmod(mode)
        return path

    def read(self, name, *, limit=256):
        return io.read_file(self.fd, name, mode=0o600, max_bytes=limit)

    def write(self, name, value, *, replace=False, limit=256):
        return io.write_json(self.fd, name, value, replace=replace, max_bytes=limit)

    def test_canonical_json_is_stable_unicode_and_rejects_nonfinite(self):
        self.assertEqual(b'{"a":1,"z":2}\n', io.canonical({"z": 2, "a": 1}))
        self.assertEqual('{"主題":"降雨"}\n'.encode(), io.canonical({"主題": "降雨"}))
        with self.assertRaises(ValueError):
            io.canonical({"value": float("nan")})

    def test_regular_read_is_bounded_nonblocking_and_closes_its_fd(self):
        self.private_file("safe.json")
        original = os.open
        opened = []

        def checked_open(name, flags, **kwargs):
            self.assertTrue(flags & os.O_NONBLOCK)
            self.assertTrue(flags & os.O_NOFOLLOW)
            fd = original(name, flags, **kwargs)
            opened.append(fd)
            return fd

        with patch.object(io.os, "open", side_effect=checked_open):
            self.assertEqual(b'{"value":1}\n', self.read("safe.json"))
        self.assertEqual(1, len(opened))
        with self.assertRaises(OSError) as error:
            os.fstat(opened[0])
        self.assertEqual(errno.EBADF, error.exception.errno)

    def test_invalid_names_cannot_escape_the_directory(self):
        for name in ("", ".", "..", "../outside.json", "/outside.json", "a/b", "a\0b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.read(name)
            with self.subTest(write=name), self.assertRaises(ValueError):
                self.write(name, {})

    def test_fifo_is_rejected_before_any_blocking_read(self):
        os.mkfifo(self.root / "fifo.json", mode=0o600)
        original = os.open

        def checked_open(name, flags, **kwargs):
            # Fail immediately if a regression removes NONBLOCK, not by hanging CI.
            self.assertTrue(flags & os.O_NONBLOCK)
            return original(name, flags, **kwargs)

        with (
            patch.object(io.os, "open", side_effect=checked_open),
            self.assertRaisesRegex(ValueError, "file_unsafe"),
        ):
            self.read("fifo.json")

    def test_symlinks_and_hardlinks_are_rejected(self):
        target = self.private_file("target.json")
        (self.root / "symbolic.json").symlink_to(target)
        with self.assertRaises(OSError):
            self.read("symbolic.json")
        os.link(target, self.root / "hard.json")
        for name in ("target.json", "hard.json"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsafe"):
                self.read(name)

    def test_file_mode_and_owner_are_not_relaxed(self):
        path = self.private_file("unsafe.json", mode=0o644)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.read(path.name)
        path.chmod(0o600)
        with (
            patch.object(io.os, "geteuid", return_value=os.geteuid() + 1),
            self.assertRaisesRegex(ValueError, "unsafe"),
        ):
            self.read(path.name)

    def test_empty_oversized_and_invalid_limits_are_rejected(self):
        self.private_file("empty.json", b"")
        self.private_file("large.json", b"x" * 257)
        for name in ("empty.json", "large.json"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "size"):
                self.read(name)
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.read("large.json", limit=limit)

    def test_named_file_replacement_during_read_is_rejected(self):
        target = self.private_file("race.json")
        original_read = os.read
        replaced = False

        def replace_after_read(fd, size):
            nonlocal replaced
            data = original_read(fd, size)
            if not replaced:
                replaced = True
                target.rename(self.root / "original.json")
                self.private_file("race.json", b'{"value":2}\n')
            return data

        with (
            patch.object(io.os, "read", side_effect=replace_after_read),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            self.read("race.json")
        self.assertTrue(replaced)

    def test_new_write_is_atomic_private_and_no_clobber(self):
        self.write("new.json", {"value": 1})
        info = (self.root / "new.json").lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(0o600, stat.S_IMODE(info.st_mode))
        self.assertEqual(1, info.st_nlink)
        with self.assertRaises(FileExistsError):
            self.write("new.json", {"value": 2})
        self.assertEqual({"value": 1}, json.loads(self.read("new.json")))
        self.assertEqual(["new.json"], os.listdir(self.root))

    def test_raw_write_uses_the_same_private_durable_no_clobber_policy(self):
        raw = b"a" * 64 + b"\n"
        io.write_file(self.fd, "query-secret", raw, max_bytes=65)
        self.assertEqual(raw, self.read("query-secret"))
        self.assertEqual(0o600, (self.root / "query-secret").stat().st_mode & 0o777)
        self.assertEqual(1, (self.root / "query-secret").stat().st_nlink)
        with self.assertRaises(FileExistsError):
            io.write_file(self.fd, "query-secret", b"b" * 65, max_bytes=65)
        self.assertEqual(raw, self.read("query-secret"))

    def test_raw_write_rejects_nonbytes_and_oversize_before_publication(self):
        for value in ("secret", bytearray(b"secret"), b"", b"x" * 66):
            with (
                self.subTest(value=type(value).__name__),
                self.assertRaises(ValueError),
            ):
                io.write_file(self.fd, "query-secret", value, max_bytes=65)
        self.assertEqual([], os.listdir(self.root))

    def test_explicit_replace_updates_record_and_preserves_old_open_fd(self):
        self.write("record.json", {"value": 1})
        previous = os.open(self.root / "record.json", os.O_RDONLY)
        try:
            self.write("record.json", {"value": 2}, replace=True)
            self.assertEqual({"value": 1}, json.loads(os.read(previous, 256)))
        finally:
            os.close(previous)
        self.assertEqual({"value": 2}, json.loads(self.read("record.json")))
        self.assertEqual(["record.json"], os.listdir(self.root))

    def test_replace_refuses_unsafe_existing_target(self):
        target = self.private_file("target.json")
        (self.root / "replace.json").symlink_to(target)
        with self.assertRaises(OSError):
            self.write("replace.json", {"value": 2}, replace=True)
        self.assertEqual(b'{"value":1}\n', target.read_bytes())
        (self.root / "replace.json").unlink()
        os.link(target, self.root / "replace.json")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.write("replace.json", {"value": 2}, replace=True)
        self.assertEqual(b'{"value":1}\n', target.read_bytes())

    def test_write_size_is_checked_before_publishing(self):
        with self.assertRaisesRegex(ValueError, "size"):
            self.write("large.json", {"value": "x" * 300})
        self.assertEqual([], os.listdir(self.root))

    def test_successful_publication_fsyncs_file_then_directory(self):
        original = os.fsync
        kinds = []

        def tracked_fsync(fd):
            kinds.append(stat.S_IFMT(os.fstat(fd).st_mode))
            return original(fd)

        with patch.object(io.os, "fsync", side_effect=tracked_fsync):
            self.write("receipt.json", {"ready": True})
        self.assertEqual([stat.S_IFREG, stat.S_IFDIR], kinds)

    def test_failed_file_fsync_cleans_temporary_and_preserves_old_record(self):
        self.write("receipt.json", {"value": 1})
        with (
            patch.object(io.os, "fsync", side_effect=OSError("injected failure")),
            self.assertRaises(OSError),
        ):
            self.write("receipt.json", {"value": 2}, replace=True)
        self.assertEqual({"value": 1}, json.loads(self.read("receipt.json")))
        self.assertEqual(["receipt.json"], os.listdir(self.root))

    def test_directory_creation_is_one_private_leaf_and_fsyncs_parent(self):
        child = self.root / "created"
        with patch.object(io.os, "fsync", wraps=os.fsync) as fsync:
            fd = io.open_directory(child, create=True)
        try:
            self.assertEqual(0o700, stat.S_IMODE(os.fstat(fd).st_mode))
            io.verify_directory(fd, child)
        finally:
            os.close(fd)
        self.assertEqual(1, fsync.call_count)
        with self.assertRaises(FileNotFoundError):
            io.open_directory(self.root / "missing" / "leaf", create=True)
        self.assertFalse((self.root / "missing").exists())

    def test_directory_paths_permissions_and_symlink_ancestors_are_rejected(self):
        for path in (Path("relative"), Path("/"), self.root / ".." / self.root.name):
            with self.subTest(path=path), self.assertRaises(ValueError):
                io.open_directory(path)
        unsafe = self.root / "public"
        unsafe.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            io.open_directory(unsafe)
        link = self.root / "linked"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            io.open_directory(link / "child", create=True)
        self.assertFalse((self.root / "child").exists())

    def test_directory_replacement_after_fd_open_is_rejected(self):
        target = self.root / "race"
        target.mkdir(mode=0o700)
        inode = target.stat().st_ino
        original = os.fstat
        replaced = False

        def replace_after_open(fd):
            nonlocal replaced
            info = original(fd)
            if info.st_ino == inode and not replaced:
                replaced = True
                target.rename(self.root / "old-race")
                target.mkdir(mode=0o700)
            return info

        with (
            patch.object(io.os, "fstat", side_effect=replace_after_open),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            io.open_directory(target)
        self.assertTrue(replaced)

    def test_directory_verification_rejects_detached_fd(self):
        target = self.root / "verify"
        target.mkdir(mode=0o700)
        fd = io.open_directory(target)
        try:
            target.rename(self.root / "old-verify")
            target.mkdir(mode=0o700)
            with self.assertRaisesRegex(ValueError, "changed"):
                io.verify_directory(fd, target)
        finally:
            os.close(fd)

    def test_directory_verification_rechecks_mode_and_operator_owner(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "directory_unsafe"):
            io.verify_directory(self.fd, self.root)
        self.root.chmod(0o700)
        operator = os.geteuid()
        with (
            patch.object(io.os, "geteuid", return_value=operator + 1),
            self.assertRaisesRegex(ValueError, "directory_unsafe"),
        ):
            io.verify_directory(self.fd, self.root)
        io.verify_directory(self.fd, self.root)

    def test_child_creation_is_private_fd_relative_and_no_clobber(self):
        with patch.object(io.os, "fsync", wraps=os.fsync) as fsync:
            fd = io.open_child_directory(self.fd, "child", create=True)
        try:
            self.assertEqual(0o700, stat.S_IMODE(os.fstat(fd).st_mode))
            self.assertEqual((self.root / "child").stat().st_ino, os.fstat(fd).st_ino)
        finally:
            os.close(fd)
        self.assertEqual(1, fsync.call_count)
        with self.assertRaises(FileExistsError):
            io.open_child_directory(self.fd, "child", create=True)
        with self.assertRaises(ValueError):
            io.open_child_directory(self.fd, "../escape", create=True)

    def test_child_directory_rejects_links_permissions_and_replacement(self):
        target = self.root / "child"
        target.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            io.open_child_directory(self.fd, "child")
        target.chmod(0o700)
        (self.root / "child-link").symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            io.open_child_directory(self.fd, "child-link")
        inode = target.stat().st_ino
        original = os.fstat
        replaced = False

        def replace_after_open(fd):
            nonlocal replaced
            info = original(fd)
            if info.st_ino == inode and not replaced:
                replaced = True
                target.rename(self.root / "old-child")
                target.mkdir(mode=0o700)
            return info

        with (
            patch.object(io.os, "fstat", side_effect=replace_after_open),
            self.assertRaisesRegex(ValueError, "unsafe"),
        ):
            io.open_child_directory(self.fd, "child")
        self.assertTrue(replaced)


if __name__ == "__main__":
    unittest.main()
