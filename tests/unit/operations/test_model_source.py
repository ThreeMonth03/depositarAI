"""Allocated-group model seeding stays private, descriptor-bound and pinned."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import search_model_source as model_source


class SearchModelSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="depositar-model-source-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.shared = self.root / "allocated"
        self.shared.mkdir(mode=0o1770)
        self.shared.chmod(0o1770)
        self.source = self.shared / "private-models"
        self.source.mkdir(mode=0o700)
        (self.source / "blobs").mkdir(mode=0o700)
        self.relative = "blobs/pinned-artifact"
        self.artifact = self.source / self.relative
        self.content = b"known pinned nonsecret model fixture\n"
        self.artifact.write_bytes(self.content)
        self.artifact.chmod(0o400)
        self.target = self.root / "private-target"
        self.target.mkdir(mode=0o700)
        self.destination = self.target / "staging"
        self.destination.write_bytes(b"")
        self.destination.chmod(0o600)
        self.digest = hashlib.sha256(self.content).hexdigest()

    @contextmanager
    def shared_metadata(self, *, uid=None, gid=None, source_uid=None):
        """Model a foreign allocated owner without needing chown or sudo."""
        shared = self.shared.stat()
        source = self.source.stat()
        real_stat, real_fstat = os.stat, os.fstat

        def altered(info):
            changes = {}
            if (info.st_dev, info.st_ino) == (shared.st_dev, shared.st_ino):
                changes["st_uid"] = os.geteuid() + 10000 if uid is None else uid
                changes["st_gid"] = os.getegid() if gid is None else gid
            if source_uid is not None and (info.st_dev, info.st_ino) == (
                source.st_dev,
                source.st_ino,
            ):
                changes["st_uid"] = source_uid
            if not changes:
                return info
            fields = {
                name: getattr(info, name)
                for name in dir(info)
                if name.startswith("st_")
            }
            return SimpleNamespace(**{**fields, **changes})

        with (
            patch.object(
                model_source.os,
                "stat",
                side_effect=lambda *args, **kw: altered(real_stat(*args, **kw)),
            ),
            patch.object(
                model_source.os,
                "fstat",
                side_effect=lambda *args, **kw: altered(real_fstat(*args, **kw)),
            ),
        ):
            yield

    def copy(self, **options):
        model_source.copy_verified_file(
            self.source,
            options.pop("relative", self.relative),
            options.pop("destination", self.destination),
            options.pop("digest", self.digest),
            options.pop("size", len(self.content)),
            **options,
        )

    def test_foreign_sticky_primary_group_ancestor_accepts_private_pinned_source(self):
        with self.shared_metadata():
            with model_source.open_source_root(self.source) as root:
                self.assertEqual(self.source.stat().st_ino, os.fstat(root.fd).st_ino)
            self.copy()
        self.assertEqual(self.content, self.destination.read_bytes())
        self.assertEqual(self.content, self.artifact.read_bytes())
        self.assertEqual(0o1770, stat.S_IMODE(self.shared.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(self.source.stat().st_mode))
        self.assertEqual(0o400, stat.S_IMODE(self.artifact.stat().st_mode))

    def test_nonsticky_group_writable_world_writable_and_wrong_gid_are_rejected(self):
        cases = (
            (0o770, os.getegid()),
            (0o1777, os.getegid()),
            (0o1770, os.getegid() + 1),
        )
        for mode, gid in cases:
            with self.subTest(mode=oct(mode), gid=gid):
                self.shared.chmod(mode)
                with self.shared_metadata(gid=gid):
                    with self.assertRaisesRegex(
                        model_source.ModelSourceError, "ancestor_unsafe"
                    ):
                        self.copy()
                self.assertEqual(b"", self.destination.read_bytes())

    def test_allocated_source_root_requires_operator_owner_and_exact_0700(self):
        self.source.chmod(0o750)
        with self.shared_metadata():
            with self.assertRaisesRegex(
                model_source.ModelSourceError, "root_not_private"
            ):
                self.copy()
        self.source.chmod(0o700)
        with self.shared_metadata(source_uid=0):
            with self.assertRaisesRegex(
                model_source.ModelSourceError, "root_not_private"
            ):
                self.copy()

    def test_root_owned_sticky_legacy_ancestor_still_accepts_read_only_source(self):
        self.shared.chmod(0o1777)
        self.source.chmod(0o755)
        with self.shared_metadata(uid=0):
            self.copy()
        self.assertEqual(self.content, self.destination.read_bytes())

    def test_retained_ancestor_fd_detects_path_replacement_during_copy(self):
        real_read = os.read
        replaced = False

        def replace_ancestor(fd, count):
            nonlocal replaced
            result = real_read(fd, count)
            if not replaced:
                replaced = True
                self.shared.rename(self.root / "allocated-saved")
                self.shared.mkdir(mode=0o1770)
                self.shared.chmod(0o1770)
            return result

        with (
            self.shared_metadata(),
            patch.object(model_source.os, "read", side_effect=replace_ancestor),
        ):
            with self.assertRaisesRegex(
                model_source.ModelSourceError, "directory_changed"
            ):
                self.copy()

    def test_relative_directory_fd_detects_replacement_during_copy(self):
        real_read = os.read
        replaced = False

        def replace_directory(fd, count):
            nonlocal replaced
            result = real_read(fd, count)
            if not replaced:
                replaced = True
                (self.source / "blobs").rename(self.source / "blobs-saved")
                (self.source / "blobs").mkdir(mode=0o700)
            return result

        with (
            self.shared_metadata(),
            patch.object(model_source.os, "read", side_effect=replace_directory),
        ):
            with self.assertRaisesRegex(
                model_source.ModelSourceError, "directory_changed"
            ):
                self.copy()

    def test_named_source_replacement_is_rejected_even_with_identical_bytes(self):
        real_read = os.read
        replaced = False

        def replace_file(fd, count):
            nonlocal replaced
            result = real_read(fd, count)
            if not replaced:
                replaced = True
                self.artifact.rename(self.artifact.with_name("old-pinned"))
                self.artifact.write_bytes(self.content)
                self.artifact.chmod(0o400)
            return result

        with (
            self.shared_metadata(),
            patch.object(model_source.os, "read", side_effect=replace_file),
        ):
            with self.assertRaisesRegex(model_source.ModelSourceError, "file_changed"):
                self.copy()

    def test_source_growth_is_bounded_and_never_writes_extra_bytes(self):
        real_read = os.read
        grew = False
        count = 0

        def grow(fd, limit):
            nonlocal grew, count
            count += 1
            result = real_read(fd, limit)
            if not grew:
                grew = True
                self.artifact.chmod(0o600)
                with self.artifact.open("ab") as stream:
                    stream.write(b"unbounded growth" * 100)
            return result

        with (
            self.shared_metadata(),
            patch.object(model_source.os, "read", side_effect=grow),
        ):
            with self.assertRaisesRegex(model_source.ModelSourceError, "file_changed"):
                self.copy()
        self.assertLessEqual(count, 2)
        self.assertLessEqual(self.destination.stat().st_size, len(self.content))

    def test_files_keep_owner_mode_link_size_and_sha_authority(self):
        for mutation in ("group-write", "hardlink", "sha", "size"):
            with self.subTest(mutation=mutation):
                self.artifact.chmod(0o400)
                duplicate = self.source / "duplicate"
                duplicate.unlink(missing_ok=True)
                if mutation == "group-write":
                    self.artifact.chmod(0o420)
                elif mutation == "hardlink":
                    os.link(self.artifact, duplicate)
                arguments = {"digest": "0" * 64} if mutation == "sha" else {}
                if mutation == "size":
                    arguments["size"] = len(self.content) + 1
                with self.shared_metadata():
                    with self.assertRaises(model_source.ModelSourceError):
                        self.copy(**arguments)

    def test_fifo_symlink_source_and_staging_links_are_rejected_before_blocking(self):
        self.artifact.unlink()
        os.mkfifo(self.artifact, 0o400)
        with self.shared_metadata():
            with self.assertRaisesRegex(model_source.ModelSourceError, "file_unsafe"):
                self.copy()
        self.artifact.unlink()
        self.artifact.symlink_to(self.destination)
        with self.shared_metadata():
            with self.assertRaisesRegex(model_source.ModelSourceError, "file_unsafe"):
                self.copy()
        self.artifact.unlink()
        self.artifact.write_bytes(self.content)
        self.artifact.chmod(0o400)
        os.link(self.destination, self.target / "other-link")
        with self.shared_metadata():
            with self.assertRaisesRegex(
                model_source.ModelSourceError, "staging_file_unsafe"
            ):
                self.copy()

    def test_destination_cannot_be_the_source_inode(self):
        self.artifact.chmod(0o600)
        with self.shared_metadata():
            with self.assertRaisesRegex(
                model_source.ModelSourceError, "overlaps_source"
            ):
                self.copy(destination=self.artifact)
        self.assertEqual(self.content, self.artifact.read_bytes())

    def test_destination_replacement_or_symlink_is_never_accepted(self):
        real_read = os.read
        replaced = False

        def replace_destination(fd, count):
            nonlocal replaced
            result = real_read(fd, count)
            if not replaced:
                replaced = True
                self.destination.rename(self.target / "old-staging")
                self.destination.write_bytes(self.content)
                self.destination.chmod(0o600)
            return result

        with (
            self.shared_metadata(),
            patch.object(model_source.os, "read", side_effect=replace_destination),
        ):
            with self.assertRaisesRegex(
                model_source.ModelSourceError, "staging_file_changed"
            ):
                self.copy()
        self.destination.unlink()
        self.destination.symlink_to(self.target / "old-staging")
        with self.shared_metadata():
            with self.assertRaisesRegex(
                model_source.ModelSourceError, "staging_file_unsafe"
            ):
                self.copy()

    def test_source_and_relative_paths_cannot_follow_links_or_escape(self):
        alias = self.root / "source-alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises((OSError, model_source.ModelSourceError)):
            with model_source.open_source_root(alias):
                pass
        for relative in ("../escape", "/absolute", "", None):
            with self.subTest(relative=relative), self.shared_metadata():
                with self.assertRaisesRegex(
                    model_source.ModelSourceError, "relative_path_invalid"
                ):
                    self.copy(relative=relative)

    def test_raw_path_types_and_nul_are_fixed_validation_errors(self):
        for root in (True, b"/invalid", "/invalid\x00path"):
            with self.subTest(root=root):
                with self.assertRaisesRegex(
                    model_source.ModelSourceError, "root_path_invalid"
                ):
                    with model_source.open_source_root(root):
                        pass
        for destination in (True, b"/invalid", "/invalid\x00path"):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(
                    model_source.ModelSourceError, "staging_path_invalid"
                ):
                    self.copy(destination=destination)

    def test_cli_check_and_copy_use_the_same_policy_and_hide_raw_io_errors(self):
        with self.shared_metadata():
            self.assertEqual(
                0, model_source.main(["check", "--source-root", str(self.source)])
            )
            self.assertEqual(
                0,
                model_source.main(
                    [
                        "copy",
                        "--source-root",
                        str(self.source),
                        "--relative",
                        self.relative,
                        "--destination",
                        str(self.destination),
                        "--sha256",
                        self.digest,
                        "--size",
                        str(len(self.content)),
                    ]
                ),
            )
        stderr = io.StringIO()
        with (
            patch.object(
                model_source.os, "open", side_effect=OSError("private raw path")
            ),
            patch.object(model_source.sys, "stderr", stderr),
        ):
            self.assertEqual(
                1, model_source.main(["check", "--source-root", str(self.source)])
            )
        self.assertEqual("Error: model_source_io_failed\n", stderr.getvalue())
        stderr = io.StringIO()
        with patch.object(model_source.sys, "stderr", stderr):
            self.assertEqual(
                1, model_source.main(["check", "--source-root", "/invalid-\ud800"])
            )
        self.assertEqual("Error: model_source_io_failed\n", stderr.getvalue())

    def test_private_parents_are_created_0700_at_every_depth_under_umask_0002(self):
        previous = os.umask(0o002)
        try:
            with model_source.create_private_root(self.target, "new-store") as root:
                first = root.relative_parent(
                    "manifests/registry/library/model/tag", private=True, create=True
                )
                self.assertEqual(
                    first[0],
                    root.relative_parent(
                        "manifests/registry/library/model/other",
                        private=True,
                        create=True,
                    )[0],
                )
                for relative in root.directory_fds:
                    directory = root.root / relative
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                self.assertEqual(
                    root.relative_parent(
                        "blobs/sha256-test", private=True, create=True
                    )[1],
                    "sha256-test",
                )
        finally:
            os.umask(previous)

    def test_private_parents_reject_existing_nonprivate_modes_without_repair(self):
        for mode in (0o750, 0o755, 0o770, 0o775):
            with self.subTest(mode=oct(mode)):
                with model_source.create_private_root(
                    self.target, "mode-" + oct(mode)
                ) as root:
                    unsafe = root.root / "existing"
                    unsafe.mkdir(mode=mode)
                    unsafe.chmod(mode)
                    with self.assertRaises(model_source.ModelSourceError):
                        root.relative_parent(
                            "existing/deeper/file", private=True, create=True
                        )
                    self.assertEqual(stat.S_IMODE(unsafe.stat().st_mode), mode)
                    self.assertEqual(list(unsafe.iterdir()), [])

    def test_private_parent_symlink_is_not_followed_or_modified(self):
        with model_source.create_private_root(self.target, "linked-store") as root:
            alias = root.root / "alias"
            alias.symlink_to(self.source, target_is_directory=True)
            with self.assertRaises((OSError, model_source.ModelSourceError)):
                root.relative_parent("alias/new/file", private=True, create=True)
            self.assertFalse((self.source / "new").exists())
            self.assertTrue(alias.is_symlink())

    def test_private_cached_parent_and_root_replacement_are_rejected(self):
        with model_source.create_private_root(self.target, "cached-store") as root:
            root.relative_parent("nested/file", private=True, create=True)
            (root.root / "nested").rename(root.root / "saved")
            (root.root / "nested").mkdir(mode=0o700)
            with self.assertRaisesRegex(
                model_source.ModelSourceError, "directory_changed"
            ):
                root.relative_parent("nested/other", private=True, create=True)
            (root.root / "nested").rmdir()
            (root.root / "saved").rename(root.root / "nested")
        with self.assertRaisesRegex(model_source.ModelSourceError, "directory_changed"):
            with model_source.create_private_root(
                self.target, "replaced-store"
            ) as root:
                root.root.rename(self.target / "saved-store")
                root.root.mkdir(mode=0o700)

    def test_private_creation_requires_explicit_policy_and_one_new_root_leaf(self):
        with model_source.open_private_root(self.target) as root:
            for options in (
                {"create": True},
                {"private": 1},
                {"private": True, "create": 1},
            ):
                with self.subTest(options=options):
                    with self.assertRaisesRegex(
                        model_source.ModelSourceError, "creation_policy_invalid"
                    ):
                        root.relative_parent("new/file", **options)
            self.assertFalse((self.target / "new").exists())
        for name in ("", ".", "..", "../escaped", "/absolute", "a\x00b", "a\nb", None):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    model_source.ModelSourceError, "root_name_invalid"
                ):
                    with model_source.create_private_root(self.target, name):
                        pass
        with self.assertRaises(FileExistsError):
            with model_source.create_private_root(self.root, self.target.name):
                pass
        self.assertEqual(self.destination.read_bytes(), b"")

    def test_default_source_reading_preserves_0755_intermediate_contract(self):
        (self.source / "blobs").chmod(0o755)
        with self.shared_metadata():
            with model_source.open_source_root(self.source) as root:
                parent, name = root.relative_parent(self.relative)
                self.assertEqual(stat.S_IMODE(os.fstat(parent).st_mode), 0o755)
                self.assertEqual(name, self.artifact.name)
            self.copy()
        self.assertEqual(self.destination.read_bytes(), self.content)


if __name__ == "__main__":
    unittest.main()
