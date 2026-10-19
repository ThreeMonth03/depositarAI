"""Tiny offline model fixtures exercise persistence without downloading weights."""

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_search_models as models


class ModelStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.dest = self.root / "copy"
        blob = b"tiny verified model"
        self.digest = hashlib.sha256(blob).hexdigest()
        self.blob = self.source / "blobs" / ("sha256-" + self.digest)
        self.blob.parent.mkdir()
        self.blob.write_bytes(blob)
        self.manifest = self.source / "manifests/registry.ollama.ai/library/example/one"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(
            json.dumps(
                {
                    "config": {"digest": "sha256:" + self.digest, "size": len(blob)},
                    "layers": [],
                }
            )
        )
        self.pins = {
            "example:one": hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        }
        self.patch = patch.object(models, "PINS", self.pins)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_exact_store_copy_and_full_verification(self):
        receipt = models.prepare(self.source, self.dest)
        self.assertEqual(models.verify(self.dest), receipt)
        self.assertEqual(len(receipt["files"]), 2)
        self.assertEqual(
            self.blob.read_bytes(),
            (self.dest / self.blob.relative_to(self.source)).read_bytes(),
        )
        self.assertEqual((self.dest / models.RECEIPT).stat().st_mode & 0o777, 0o400)

    def test_existing_destination_is_not_overwritten(self):
        self.dest.mkdir()
        with self.assertRaises(ValueError):
            models.prepare(self.source, self.dest)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_symlink_and_hardlink_sources_are_rejected(self):
        other = self.root / "other"
        self.blob.rename(other)
        self.blob.symlink_to(other)
        with self.assertRaises((ValueError, OSError)):
            models.prepare(self.source, self.dest)
        self.assertFalse((self.dest / models.RECEIPT).exists())
        self.blob.unlink()
        os.link(other, self.blob)
        with self.assertRaises(ValueError):
            models.checked_file(self.blob, self.digest)

    def test_corrupt_source_never_gets_a_verified_receipt(self):
        self.blob.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            models.prepare(self.source, self.dest)
        self.assertFalse((self.dest / models.RECEIPT).exists())

    def test_changed_manifest_is_rejected_before_creating_destination(self):
        self.manifest.write_text("{}")
        with self.assertRaises(ValueError):
            models.prepare(self.source, self.dest)
        self.assertFalse(self.dest.exists())

    def test_group_writable_artifact_is_rejected(self):
        self.blob.chmod(0o664)
        with self.assertRaises(ValueError):
            models.prepare(self.source, self.dest)
        self.assertFalse((self.dest / models.RECEIPT).exists())

    def test_corrupt_destination_or_extra_file_is_rejected(self):
        models.prepare(self.source, self.dest)
        extra = self.dest / "extra"
        extra.write_text("unexpected")
        with self.assertRaises(ValueError):
            models.verify(self.dest)
        extra.unlink()
        blob = self.dest / self.blob.relative_to(self.source)
        blob.chmod(0o600)
        blob.write_bytes(b"bad")
        with self.assertRaises(ValueError):
            models.verify(self.dest)

    def test_nonprivate_parent_and_symlink_ancestors_rejected(self):
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            models.prepare(self.source, self.dest)
        self.root.chmod(0o700)
        alias = self.root / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            models.plan(alias)

    def test_copy_under_group_writable_umask_creates_every_private_parent(self):
        previous = os.umask(0o002)
        try:
            receipt = models.prepare(self.source, self.dest)
        finally:
            os.umask(previous)
        self.assertEqual(models.verify(self.dest), receipt)
        for directory, subdirectories, files in os.walk(self.dest):
            self.assertEqual(Path(directory).stat().st_mode & 0o777, 0o700)
            for name in subdirectories:
                self.assertEqual((Path(directory) / name).stat().st_mode & 0o777, 0o700)
            for name in files:
                self.assertEqual((Path(directory) / name).stat().st_mode & 0o777, 0o400)

    def test_existing_unsafe_or_symlink_parent_is_not_repaired_or_followed(self):
        create = models.search_model_source.create_private_root
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        for case in ("group-writable", "symlink"):
            with self.subTest(case=case):
                destination = self.root / case

                @contextmanager
                def injected(parent, name):
                    with create(parent, name) as root:
                        intermediate = destination / "manifests"
                        if case == "group-writable":
                            intermediate.mkdir(mode=0o775)
                            intermediate.chmod(0o775)
                        else:
                            intermediate.symlink_to(outside, target_is_directory=True)
                        yield root

                checked = models.checked_file
                copied = []

                def counted(path, digest, size=None, output=None):
                    if output is not None and not isinstance(output, io.BytesIO):
                        copied.append(path)
                    return checked(path, digest, size, output)

                with (
                    patch.object(
                        models.search_model_source, "create_private_root", injected
                    ),
                    patch.object(models, "checked_file", side_effect=counted),
                    self.assertRaises((ValueError, OSError)),
                ):
                    models.prepare(self.source, destination)
                self.assertEqual(copied, [])
                self.assertFalse((destination / models.RECEIPT).exists())
                if case == "group-writable":
                    self.assertEqual(
                        (destination / "manifests").stat().st_mode & 0o777, 0o775
                    )
                else:
                    self.assertTrue((destination / "manifests").is_symlink())
                self.assertEqual(list(outside.iterdir()), [])

    def test_verify_rejects_unsafe_existing_intermediate_without_chmod(self):
        models.prepare(self.source, self.dest)
        parent = self.dest / "manifests/registry.ollama.ai"
        parent.chmod(0o775)
        with self.assertRaises((ValueError, OSError)):
            models.verify(self.dest)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o775)

    def test_verify_rejects_extra_empty_directory_or_symlink(self):
        models.prepare(self.source, self.dest)
        extra = self.dest / "extra"
        for kind in ("directory", "symlink"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    extra.mkdir(mode=0o700)
                else:
                    extra.symlink_to(self.source, target_is_directory=True)
                with self.assertRaises(ValueError):
                    models.verify(self.dest)
                if kind == "directory":
                    extra.rmdir()
                else:
                    extra.unlink()

    def test_verify_rejects_link_fifo_or_oversize_receipt_without_blocking(self):
        models.prepare(self.source, self.dest)
        receipt = self.dest / models.RECEIPT
        saved = self.root / "saved-receipt"
        receipt.rename(saved)
        for kind in ("symlink", "hardlink", "fifo", "oversize"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    receipt.symlink_to(saved)
                elif kind == "hardlink":
                    os.link(saved, receipt)
                elif kind == "fifo":
                    os.mkfifo(receipt, 0o400)
                else:
                    receipt.write_bytes(b" " * (128 * 1024 + 1))
                    receipt.chmod(0o400)
                with self.assertRaises((ValueError, OSError)):
                    models.verify(self.dest)
                receipt.unlink()

    def test_destination_parent_replacement_during_copy_is_rejected(self):
        parent = self.root / "publish"
        parent.mkdir(mode=0o700)
        destination = parent / "copy"
        checked = models.checked_file
        replaced = False

        def replace_parent(path, digest, size=None, output=None):
            nonlocal replaced
            if (
                output is not None
                and not isinstance(output, io.BytesIO)
                and not replaced
            ):
                replaced = True
                parent.rename(self.root / "publish-saved")
                parent.mkdir(mode=0o700)
            return checked(path, digest, size, output)

        with (
            patch.object(models, "checked_file", side_effect=replace_parent),
            self.assertRaisesRegex(ValueError, "directory_changed"),
        ):
            models.prepare(self.source, destination)
        self.assertEqual(list(parent.iterdir()), [])
        self.assertFalse((self.root / "publish-saved/copy" / models.RECEIPT).exists())

    def test_existing_artifact_is_never_overwritten(self):
        create = models.search_model_source.create_private_root
        sentinel = b"do not overwrite"

        @contextmanager
        def injected(parent, name):
            with create(parent, name) as root:
                parent_fd, leaf = root.relative_parent(
                    "blobs/sha256-" + self.digest, private=True, create=True
                )
                fd = os.open(
                    leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400, dir_fd=parent_fd
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(sentinel)
                yield root

        with (
            patch.object(models.search_model_source, "create_private_root", injected),
            self.assertRaises(FileExistsError),
        ):
            models.prepare(self.source, self.dest)
        self.assertEqual((self.dest / "blobs" / self.blob.name).read_bytes(), sentinel)
        self.assertFalse((self.dest / models.RECEIPT).exists())

    def test_receipt_leaf_replacement_before_success_is_rejected(self):
        fsync = os.fsync
        receipt = self.dest / models.RECEIPT
        replaced = False

        def replace_receipt(fd):
            nonlocal replaced
            if receipt.exists() and not replaced:
                opened = os.fstat(fd)
                named = receipt.stat()
                if (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino):
                    replaced = True
                    receipt.rename(self.root / "original-receipt")
                    receipt.write_bytes(b"{}\n")
                    receipt.chmod(0o400)
            return fsync(fd)

        with (
            patch.object(models.os, "fsync", side_effect=replace_receipt),
            self.assertRaisesRegex(ValueError, "receipt changed"),
        ):
            models.prepare(self.source, self.dest)
        self.assertTrue(replaced)
        with self.assertRaises(ValueError):
            models.verify(self.dest)

    def test_verify_detects_named_artifact_replacement_during_hashing(self):
        models.prepare(self.source, self.dest)
        artifact = self.dest / "blobs" / self.blob.name
        saved = self.root / "saved-artifact"
        for content in (self.blob.read_bytes(), b"wrong replacement bytes"):
            with self.subTest(same_bytes=content == self.blob.read_bytes()):
                identity = artifact.stat()
                fstat = os.fstat
                reads = 0

                def replace_on_final_fstat(fd):
                    nonlocal reads
                    info = fstat(fd)
                    if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                        reads += 1
                        if reads == 2:
                            artifact.rename(saved)
                            artifact.write_bytes(content)
                            artifact.chmod(0o400)
                    return info

                with (
                    patch.object(
                        models.os, "fstat", side_effect=replace_on_final_fstat
                    ),
                    self.assertRaisesRegex(ValueError, "source changed"),
                ):
                    models.verify(self.dest)
                self.assertEqual(reads, 2)
                artifact.unlink()
                saved.rename(artifact)

    def test_verify_rechecks_receipt_after_artifact_hashing(self):
        for mutation in ("changed-bytes", "same-bytes-replacement"):
            with self.subTest(mutation=mutation):
                destination = self.root / mutation
                models.prepare(self.source, destination)
                receipt = destination / models.RECEIPT
                checked = models.checked_file
                changed = False

                def replace_after_hash(path, digest, size=None, output=None):
                    nonlocal changed
                    count = checked(path, digest, size, output)
                    if path == destination / "blobs" / self.blob.name and not changed:
                        changed = True
                        original = receipt.read_bytes()
                        if mutation == "same-bytes-replacement":
                            receipt.rename(self.root / ("saved-" + mutation))
                            receipt.write_bytes(original)
                        else:
                            receipt.chmod(0o600)
                            receipt.write_bytes(b"{}\n")
                        receipt.chmod(0o400)
                    return count

                with (
                    patch.object(
                        models, "checked_file", side_effect=replace_after_hash
                    ),
                    self.assertRaisesRegex(ValueError, "receipt changed"),
                ):
                    models.verify(destination)
                self.assertTrue(changed)

    def test_verify_rechecks_inventory_after_artifact_hashing(self):
        for extra_kind in ("empty-directory", "file"):
            with self.subTest(extra_kind=extra_kind):
                destination = self.root / extra_kind
                models.prepare(self.source, destination)
                checked = models.checked_file
                changed = False

                def add_after_hash(path, digest, size=None, output=None):
                    nonlocal changed
                    count = checked(path, digest, size, output)
                    if path == destination / "blobs" / self.blob.name and not changed:
                        changed = True
                        extra = destination / "manifests/extra"
                        if extra_kind == "empty-directory":
                            extra.mkdir(mode=0o700)
                        else:
                            extra.write_bytes(b"unexpected")
                            extra.chmod(0o400)
                    return count

                with (
                    patch.object(models, "checked_file", side_effect=add_after_hash),
                    self.assertRaisesRegex(ValueError, "unexpected model-store"),
                ):
                    models.verify(destination)
                self.assertTrue(changed)
