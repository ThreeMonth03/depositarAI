"""Check Unix directory-chain trust with synthetic owners and private
fixtures."""

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.shared import namespace


class NamespaceTests(unittest.TestCase):
    def test_shared_import_has_no_filesystem_io_or_domain_dependencies(self):
        root = Path(__file__).resolve().parents[3]
        script = (
            "import os, pathlib, sys\n"
            f"sys.path.insert(0, {str(root)!r})\n"
            "def forbidden(*args, **kwargs):\n"
            "    raise AssertionError('filesystem IO at import')\n"
            "os.open = os.stat = os.fstat = forbidden\n"
            "from search.operations.shared import namespace\n"
            "assert issubclass(namespace.NamespaceError, ValueError)\n"
            "loaded = {name for name in sys.modules "
            "if name == 'search' or name.startswith('search.')}\n"
            "assert loaded == {'search', 'search.operations', "
            "'search.operations.shared', "
            "'search.operations.shared.namespace'}, "
            "sorted(loaded)\n"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="namespace-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)

    def directory(self, name, mode=0o700):
        path = self.root / name
        path.mkdir(mode=0o700)
        path.chmod(mode)
        self.addCleanup(path.chmod, 0o700)
        return path

    @contextmanager
    def descriptors(self):
        real_open, real_close = os.open, os.close
        opened, closed = [], []

        def open_directory(path, flags, mode=0o777, *, dir_fd=None):
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            opened.append((descriptor, path, flags, dir_fd))
            return descriptor

        def close_directory(descriptor):
            real_close(descriptor)
            closed.append(descriptor)

        try:
            with (
                patch.object(namespace.os, "open", side_effect=open_directory),
                patch.object(
                    namespace.os, "close", side_effect=close_directory
                ),
            ):
                yield opened
        finally:
            self.assertCountEqual([row[0] for row in opened], closed)
            for descriptor in set(closed):
                with self.assertRaises(OSError) as caught:
                    fcntl.fcntl(descriptor, fcntl.F_GETFD)
                self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_directory_owner_and_write_permissions_form_one_trust_boundary(
        self,
    ):
        executing_uid, foreign_uid = 12345, 23456
        accepted = (
            (0, 0o755),
            (executing_uid, 0o555),
            (executing_uid, 0o700),
            (0, 0o1777),
            (executing_uid, 0o1777),
            (executing_uid, 0o1775),
        )
        refused = (
            (foreign_uid, 0o555),
            (foreign_uid, 0o700),
            (foreign_uid, 0o1777),
            (0, 0o775),
            (0, 0o777),
            (executing_uid, 0o775),
            (executing_uid, 0o777),
        )
        with patch.object(namespace.os, "geteuid", return_value=executing_uid):
            for owner, mode in accepted:
                with self.subTest(owner=owner, mode=oct(mode)):
                    info = SimpleNamespace(
                        st_uid=owner, st_mode=stat.S_IFDIR | mode
                    )
                    namespace._check_directory(info)
            for owner, mode in refused:
                with self.subTest(owner=owner, mode=oct(mode)):
                    info = SimpleNamespace(
                        st_uid=owner, st_mode=stat.S_IFDIR | mode
                    )
                    with self.assertRaisesRegex(
                        namespace.NamespaceError, "^namespace_unsafe$"
                    ):
                        namespace._check_directory(info)

    def test_trusted_owner_cannot_make_a_nondirectory_acceptable(self):
        for file_type in (
            stat.S_IFREG,
            stat.S_IFLNK,
            stat.S_IFIFO,
            stat.S_IFSOCK,
        ):
            with self.subTest(file_type=file_type):
                info = SimpleNamespace(st_uid=0, st_mode=file_type | 0o1777)
                with self.assertRaisesRegex(
                    namespace.NamespaceError, "^namespace_unsafe$"
                ):
                    namespace._check_directory(info)

    def test_walk_holds_parents_and_uses_nofollow_directory_descriptors(self):
        parent = self.directory("parent")
        child = parent / "private"
        child.mkdir(mode=0o700)
        with self.descriptors() as opened:
            namespace.check_directory(child, private=True)
        required = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        self.assertEqual(opened[0][1], "/")
        self.assertIsNone(opened[0][3])
        self.assertEqual([row[1] for row in opened[1:]], list(child.parts[1:]))
        for previous, current in zip(opened, opened[1:]):
            self.assertEqual(current[3], previous[0])
        for _, _, flags, _ in opened:
            self.assertEqual(flags & required, required)

    def test_sticky_trusted_parent_keeps_private_child_acceptable(self):
        parent = self.directory("sticky", 0o1777)
        child = parent / "private"
        child.mkdir(mode=0o700)
        with self.descriptors():
            namespace.check_directory(child, private=True)
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o1777)

    def test_nonsticky_writable_parent_is_refused_without_repair(self):
        for mode in (0o775, 0o777):
            with self.subTest(mode=oct(mode)):
                parent = self.directory(f"parent-{mode:o}", mode)
                child = parent / "private"
                child.mkdir(mode=0o700)
                with self.descriptors():
                    with self.assertRaisesRegex(
                        namespace.NamespaceError, "^namespace_unsafe$"
                    ):
                        namespace.check_directory(child, private=True)
                self.assertEqual(stat.S_IMODE(parent.stat().st_mode), mode)

    def test_readonly_trusted_parent_is_allowed(self):
        parent = self.directory("readonly")
        child = parent / "private"
        child.mkdir(mode=0o700)
        parent.chmod(0o555)
        with self.descriptors():
            namespace.check_directory(child, private=True)

    def test_sticky_directory_does_not_exempt_foreign_owners_anywhere(self):
        ancestor = self.directory("ancestor", 0o755)
        sticky = ancestor / "sticky"
        sticky.mkdir(mode=0o700)
        sticky.chmod(0o1777)
        child = sticky / "private"
        child.mkdir(mode=0o700)
        real_fstat = os.fstat
        foreign_uid = os.geteuid() + 1
        for target in (ancestor, sticky, child):
            identity = (target.stat().st_dev, target.stat().st_ino)

            def foreign_owner(descriptor):
                info = real_fstat(descriptor)
                if (info.st_dev, info.st_ino) == identity:
                    return SimpleNamespace(
                        st_dev=info.st_dev,
                        st_ino=info.st_ino,
                        st_uid=foreign_uid,
                        st_mode=info.st_mode,
                    )
                return info

            with self.subTest(target=target.name):
                with self.descriptors() as opened:
                    with patch.object(
                        namespace.os, "fstat", side_effect=foreign_owner
                    ):
                        with self.assertRaisesRegex(
                            namespace.NamespaceError, "^namespace_unsafe$"
                        ):
                            namespace.check_directory(child, private=True)
                self.assertEqual(opened[-1][1], target.name)

    def test_private_leaf_requires_exact_0700_even_when_owner_is_trusted(self):
        for mode in (0o755, 0o750, 0o500, 0o1700):
            leaf = self.directory(f"private-{mode:o}", mode)
            with self.subTest(mode=oct(mode)):
                namespace.check_directory(leaf)
                with self.descriptors():
                    with self.assertRaisesRegex(
                        namespace.NamespaceError,
                        "^namespace_private_directory_unsafe$",
                    ):
                        namespace.check_directory(leaf, private=True)
                self.assertEqual(stat.S_IMODE(leaf.stat().st_mode), mode)

    def test_root_owned_leaf_is_not_private_for_another_executing_account(
        self,
    ):
        leaf = self.directory("leaf")
        identity = (leaf.stat().st_dev, leaf.stat().st_ino)
        real_fstat, real_stat = os.fstat, os.stat

        def root_owned(info):
            if (info.st_dev, info.st_ino) == identity:
                return SimpleNamespace(
                    st_dev=info.st_dev,
                    st_ino=info.st_ino,
                    st_uid=0,
                    st_mode=info.st_mode,
                )
            return info

        with (
            patch.object(
                namespace.os, "geteuid", return_value=os.geteuid() or 12345
            ),
            patch.object(
                namespace.os,
                "fstat",
                side_effect=lambda fd: root_owned(real_fstat(fd)),
            ),
            patch.object(
                namespace.os,
                "stat",
                side_effect=lambda *a, **kw: root_owned(real_stat(*a, **kw)),
            ),
            self.descriptors(),
        ):
            with self.assertRaisesRegex(
                namespace.NamespaceError,
                "^namespace_private_directory_unsafe$",
            ):
                namespace.check_directory(leaf, private=True)

    def test_lexical_parent_and_relative_paths_are_rejected_before_open(self):
        for path in ("relative", self.root / "child" / ".." / "other"):
            with self.subTest(path=str(path)):
                with self.descriptors() as opened:
                    with self.assertRaisesRegex(
                        namespace.NamespaceError, "^namespace_path_invalid$"
                    ):
                        namespace.check_directory(path)
                self.assertEqual(opened, [])

    def test_missing_and_nondirectory_components_close_all_descriptors(self):
        regular = self.root / "regular"
        regular.write_bytes(b"fixture")
        regular.chmod(0o600)
        for path in (self.root / "missing", regular, regular / "child"):
            with self.subTest(path=str(path)):
                with self.descriptors():
                    with self.assertRaises(OSError):
                        namespace.check_directory(path)
        self.assertEqual(regular.read_bytes(), b"fixture")

    def test_symlink_leaf_and_ancestor_are_never_followed(self):
        target = self.directory("target")
        (target / "child").mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)
        before = target.stat()
        for path in (link, link / "child"):
            with self.subTest(path=str(path)):
                with self.descriptors():
                    with self.assertRaises(OSError):
                        namespace.check_directory(path, private=True)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.stat(), before)

    def test_named_entry_identity_drift_is_rejected_for_every_bound_field(
        self,
    ):
        leaf = self.directory("leaf")
        real_stat = os.stat
        for field in ("st_dev", "st_ino", "st_uid", "st_mode"):

            def changed_entry(path, **options):
                info = real_stat(path, **options)
                if path == leaf.name:
                    values = {
                        name: getattr(info, name)
                        for name in ("st_dev", "st_ino", "st_uid", "st_mode")
                    }
                    values[field] += 1
                    return SimpleNamespace(**values)
                return info

            with self.subTest(field=field):
                with self.descriptors():
                    with patch.object(
                        namespace.os, "stat", side_effect=changed_entry
                    ):
                        with self.assertRaisesRegex(
                            namespace.NamespaceError, "^namespace_changed$"
                        ):
                            namespace.check_directory(leaf, private=True)

    def test_entry_replaced_by_symlink_to_opened_inode_is_refused(self):
        leaf = self.directory("leaf")
        moved = self.root / "moved"
        real_stat = os.stat
        replaced = False

        def replace_entry(path, **options):
            nonlocal replaced
            if path == leaf.name and not replaced:
                leaf.rename(moved)
                leaf.symlink_to(moved, target_is_directory=True)
                replaced = True
            return real_stat(path, **options)

        try:
            with self.descriptors():
                with patch.object(
                    namespace.os, "stat", side_effect=replace_entry
                ):
                    with self.assertRaisesRegex(
                        namespace.NamespaceError, "^namespace_changed$"
                    ):
                        namespace.check_directory(leaf, private=True)
            self.assertTrue(replaced)
            self.assertTrue(leaf.is_symlink())
        finally:
            if replaced:
                leaf.unlink()
                moved.rename(leaf)

    def test_fstat_failure_closes_new_child_and_held_parent(self):
        leaf = self.directory("leaf")
        identity = (leaf.stat().st_dev, leaf.stat().st_ino)
        real_fstat = os.fstat

        def failed_fstat(descriptor):
            info = real_fstat(descriptor)
            if (info.st_dev, info.st_ino) == identity:
                raise OSError(errno.EIO, "synthetic fstat failure")
            return info

        with self.descriptors():
            with patch.object(namespace.os, "fstat", side_effect=failed_fstat):
                with self.assertRaises(OSError) as caught:
                    namespace.check_directory(leaf)
            self.assertEqual(caught.exception.errno, errno.EIO)

    def test_failed_root_fstat_closes_root_descriptor(self):
        with self.descriptors():
            with patch.object(
                namespace.os,
                "fstat",
                side_effect=OSError(errno.EIO, "fixture"),
            ):
                with self.assertRaises(OSError):
                    namespace.check_directory(self.root)

    def test_disappearing_named_entry_closes_new_child_and_parent(self):
        leaf = self.directory("leaf")
        real_stat = os.stat

        def missing_entry(path, **options):
            if path == leaf.name:
                raise FileNotFoundError(errno.ENOENT, "synthetic rename")
            return real_stat(path, **options)

        with self.descriptors():
            with patch.object(namespace.os, "stat", side_effect=missing_entry):
                with self.assertRaises(FileNotFoundError):
                    namespace.check_directory(leaf)


if __name__ == "__main__":
    unittest.main()
