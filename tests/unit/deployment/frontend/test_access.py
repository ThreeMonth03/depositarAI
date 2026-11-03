"""Exercise intranet admission publication using private, local CPU
fixtures.
"""

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from search.operations.deployment.frontend import access


RULES = (
    b"# Direct TCP peers only. Loopback is for container health checks.\n"
    b"allow 127.0.0.1;\nallow ::1;\nallow 10.20.30.0/24;\ndeny all;\n"
)


class AccessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="intranet-access-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.parent = self.root / "private"
        self.parent.mkdir(mode=0o700)
        self.path = self.parent / "intranet-allow.conf"
        self.config = {
            "services": {
                "web": {
                    "environment": {
                        "WEB_ACCESS_MODE": "intranet-http",
                        "WEB_ORIGIN": "http://10.20.30.40:18082",
                    }
                },
                "web-nginx": {
                    "environment": {"WEB_ALLOWED_CIDRS": "10.20.30.0/24"},
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(self.path),
                            "target": access.ACCESS_TARGET,
                            "read_only": True,
                            "bind": {"create_host_path": False},
                        }
                    ],
                },
            }
        }

    def write(self, raw=RULES, mode=0o444):
        self.path.unlink(missing_ok=True)
        self.path.write_bytes(raw)
        self.path.chmod(mode)

    @contextmanager
    def descriptors(self):
        real_open, opened = os.open, []

        def tracked_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        try:
            with patch.object(access.os, "open", side_effect=tracked_open):
                yield
        finally:
            for descriptor in set(opened):
                with self.assertRaises(OSError) as caught:
                    fcntl.fcntl(descriptor, fcntl.F_GETFD)
                self.assertEqual(caught.exception.errno, errno.EBADF)

    def denied(self):
        with self.descriptors():
            for operation in (access.prepare_access, access.verify_access):
                with self.assertRaises((ValueError, OSError)):
                    operation(self.config)

    def test_create_exact_readonly_policy_and_repeated_prepare(self):
        with self.descriptors():
            access.prepare_access(self.config)
            original = self.path.stat()
            access.verify_access(self.config)
            access.prepare_access(self.config)
        self.assertEqual(self.path.read_bytes(), RULES)
        self.assertTrue(stat.S_ISREG(original.st_mode))
        self.assertEqual(
            (original.st_uid, original.st_nlink), (os.geteuid(), 1)
        )
        self.assertEqual(stat.S_IMODE(original.st_mode), 0o444)
        self.assertEqual(self.path.stat().st_ino, original.st_ino)

    def test_valid_cidr_update_replaces_only_the_dedicated_file(self):
        self.write()
        sentinel = self.parent / "unrelated"
        sentinel.write_bytes(b"untouched")
        self.config["services"]["web-nginx"]["environment"][
            "WEB_ALLOWED_CIDRS"
        ] = "192.168.2.0/24,10.20.31.0/24"
        with self.descriptors():
            with self.assertRaises(ValueError):
                access.verify_access(self.config)
            access.prepare_access(self.config)
            access.verify_access(self.config)
        expected = RULES.replace(
            b"allow 10.20.30.0/24;\n",
            b"allow 192.168.2.0/24;\nallow 10.20.31.0/24;\n",
        )
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o444)
        self.assertEqual(sentinel.read_bytes(), b"untouched")

    def test_wrong_mode_empty_and_oversized_files_are_not_repaired(self):
        fixtures = [(RULES, mode) for mode in (0o600, 0o644, 0o400)]
        fixtures += [(b"", 0o444), (b"x" * 4097, 0o444)]
        for raw, mode in fixtures:
            with self.subTest(length=len(raw), mode=oct(mode)):
                self.write(raw, mode)
                self.denied()
                self.assertEqual(self.path.read_bytes(), raw)
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), mode)

    def test_verify_requires_exact_approved_ascii_bytes(self):
        for raw in (
            RULES + b"\n",
            RULES.replace(b"deny all;", b"allow all;"),
            b"\xff",
        ):
            with self.subTest(raw=raw):
                self.write(raw)
                with self.descriptors():
                    with self.assertRaises(ValueError):
                        access.verify_access(self.config)

    def test_private_parent_and_higher_ancestor_refusals_do_not_write(self):
        self.write()
        parents = ((self.parent, (0o775, 0o777, 0o755)), (self.root, (0o775,)))
        for directory, modes in parents:
            for mode in modes:
                with self.subTest(directory=directory.name, mode=oct(mode)):
                    directory.chmod(mode)
                    try:
                        self.denied()
                        self.assertEqual(self.path.read_bytes(), RULES)
                    finally:
                        directory.chmod(0o700)

    def test_symlink_destination_and_hardlink_never_overwrite_another_file(
        self,
    ):
        external = self.root / "external"
        external.write_bytes(b"external sentinel")
        external.chmod(0o444)
        for link in ("symlink", "hardlink"):
            with self.subTest(link=link):
                if link == "symlink":
                    self.path.symlink_to(external)
                else:
                    os.link(external, self.path)
                try:
                    self.denied()
                    self.assertEqual(
                        external.read_bytes(), b"external sentinel"
                    )
                finally:
                    self.path.unlink()

    def test_symlink_ancestor_does_not_rebind_the_approved_parent(self):
        self.write()
        moved = self.root / "moved"
        self.parent.rename(moved)
        self.parent.symlink_to(moved, target_is_directory=True)
        try:
            self.denied()
            self.assertEqual((moved / self.path.name).read_bytes(), RULES)
        finally:
            self.parent.unlink()
            moved.rename(self.parent)

    def test_fifo_is_refused_without_a_blocking_open(self):
        os.mkfifo(self.path, 0o444)
        real_open = os.open

        def nonblocking_open(name, flags, *args, **kwargs):
            if name == self.path.name:
                self.assertTrue(flags & os.O_NONBLOCK)
            return real_open(name, flags, *args, **kwargs)

        with patch.object(access.os, "open", side_effect=nonblocking_open):
            self.denied()
        self.assertTrue(stat.S_ISFIFO(self.path.lstat().st_mode))

    def test_foreign_owned_regular_file_is_refused_without_chown(self):
        self.write()
        identity = (self.path.stat().st_dev, self.path.stat().st_ino)
        real_fstat = os.fstat

        def foreign_file(descriptor):
            info = real_fstat(descriptor)
            if (info.st_dev, info.st_ino) == identity:
                values = list(info)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return info

        with patch.object(access.os, "fstat", side_effect=foreign_file):
            self.denied()
        self.assertEqual(self.path.read_bytes(), RULES)

    def test_file_and_directory_fsync_failures_never_report_success(self):
        real_fsync = os.fsync
        for directory_failure in (False, True):
            with self.subTest(directory_failure=directory_failure):
                self.write(b"old policy\n")

                def failing_fsync(descriptor):
                    if (
                        stat.S_ISDIR(os.fstat(descriptor).st_mode)
                        == directory_failure
                    ):
                        raise OSError(errno.EIO, "synthetic fsync failure")
                    return real_fsync(descriptor)

                with self.descriptors():
                    with patch.object(
                        access.os, "fsync", side_effect=failing_fsync
                    ):
                        with self.assertRaises(OSError):
                            access.prepare_access(self.config)
                self.assertEqual(list(self.parent.iterdir()), [self.path])
                expected = RULES if directory_failure else b"old policy\n"
                self.assertEqual(self.path.read_bytes(), expected)
