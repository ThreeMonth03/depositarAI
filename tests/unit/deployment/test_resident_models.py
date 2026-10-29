"""Resident metadata reuse retains the original model-content and FD authority."""

import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from search.operations.config import GPUConfig, ProductConfig
from search.operations.deployment import inference
from search.operations.shared import private_io


class ResidentModelFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="resident-model-unit-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = ProductConfig(
            2,
            root / "state",
            root / "api",
            root / "embedding",
            root / "reranker",
            GPUConfig("scope", ("planner-a", "planner-b"), "reranker"),
        )
        for folder in (
            self.config.api_model_root,
            self.config.embedding_model_root,
            self.config.reranker_model_root,
        ):
            folder.mkdir(mode=0o700)
        self.large = set()
        pins = {}
        for name in ("fixture-scope:tiny", "fixture-planner:tiny"):
            pins[name] = self.ollama(self.config.api_model_root, name)
        embedding = "fixture-embedding:tiny"
        embedding_digest = self.ollama(self.config.embedding_model_root, embedding)
        reranker = {}
        for name, raw in (
            ("model.safetensors", b"reranker-data" * 1024),
            ("config.json", b"{}"),
            ("tokenizer.json", b'{"version":1}'),
        ):
            destination = self.config.reranker_model_root / name
            self.write(destination, raw)
            reranker[name] = hashlib.sha256(raw).hexdigest()
            if len(raw) > 1024:
                self.large.add(destination)
        self.reranker_pins = reranker
        for patch in (
            mock.patch.object(inference.prepare_search_models, "PINS", pins),
            mock.patch("search.reranker.model.MODEL_HASHES", reranker),
            mock.patch("search.catalog.embedding.MODEL", embedding),
            mock.patch("search.catalog.embedding.MODEL_DIGEST", embedding_digest),
            mock.patch.object(inference, "RESIDENT_METADATA_BYTES", 1024),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def write(path, raw):
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.exists():
            assert path.read_bytes() == raw
            return
        path.write_bytes(raw)
        path.chmod(0o400)

    def ollama(self, root, name):
        raw = (name.encode() + b"-weights") * 512
        layers = []
        for content in (b"{}", raw):
            sha = hashlib.sha256(content).hexdigest()
            destination = root / "blobs" / ("sha256-" + sha)
            self.write(destination, content)
            layers.append({"digest": "sha256:" + sha, "size": len(content)})
            if len(content) > 1024:
                self.large.add(destination)
        manifest = private_io.canonical(
            {"schemaVersion": 2, "config": layers[0], "layers": layers[1:]}
        )
        self.write(
            root / "manifests/registry.ollama.ai/library" / name.replace(":", "/"),
            manifest,
        )
        return hashlib.sha256(manifest).hexdigest()

    def deep(self, *, include_embedding=True):
        with inference._verified_models(
            self.config, include_embedding=include_embedding
        ) as (plans, verify):
            self.assertRegex(verify.file_identity_sha256, r"^[0-9a-f]{64}$")
            verify()
            return plans, verify.file_identity_sha256

    def cached(self, stamp, **options):
        return inference._resident_models(self.config, stamp, **options)

    def test_deep_reads_all_weights_and_resident_reuse_skips_only_large_data(self):
        original = os.read
        reads = []

        def read(fd, size):
            reads.append(Path(os.readlink(f"/proc/self/fd/{fd}")))
            return original(fd, size)

        with mock.patch.object(inference.os, "read", side_effect=read):
            plans, stamp = self.deep()
        self.assertTrue(self.large <= set(reads))
        reads.clear()

        def metadata_read(fd, size):
            self.assertNotIn(Path(os.readlink(f"/proc/self/fd/{fd}")), self.large)
            return read(fd, size)

        with mock.patch.object(inference.os, "read", side_effect=metadata_read):
            with self.cached(stamp) as (cached_plans, verify):
                self.assertEqual(cached_plans, plans)
                self.assertEqual(verify.file_identity_sha256, stamp)
                verify()
        self.assertTrue(reads, "small config/tokenizer artifacts still need hashing")

    def test_original_deep_default_never_reuses_a_cached_weight_digest(self):
        self.deep()
        target = next(iter(self.large))
        target.chmod(0o600)
        target.write_bytes(b"x" * target.stat().st_size)
        target.chmod(0o400)
        with self.assertRaisesRegex(inference.InferenceError, "model_digest_changed"):
            self.deep()

    def test_same_size_write_with_original_mtime_still_invalidates_ctime(self):
        _, stamp = self.deep()
        target = next(iter(self.large))
        before = target.stat()
        target.chmod(0o600)
        target.write_bytes(b"x" * before.st_size)
        target.chmod(0o400)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(target.stat().st_ino, before.st_ino)
        self.assertEqual(target.stat().st_size, before.st_size)
        self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertNotEqual(target.stat().st_ctime_ns, before.st_ctime_ns)
        with self.assertRaisesRegex(inference.InferenceError, "file_identity_changed"):
            with self.cached(stamp):
                self.fail("changed weights reused")

    def test_same_bytes_replaced_inode_cannot_reuse_the_prior_identity(self):
        _, stamp = self.deep()
        target = next(iter(self.large))
        replacement = target.with_name(target.name + ".replacement")
        self.write(replacement, target.read_bytes())
        replacement.replace(target)
        with self.assertRaisesRegex(inference.InferenceError, "file_identity_changed"):
            with self.cached(stamp):
                self.fail("replaced inode reused")

    def test_expected_weight_sha_is_part_of_the_cache_authority(self):
        _, stamp = self.deep()
        changed = {**self.reranker_pins, "model.safetensors": "f" * 64}
        with mock.patch("search.reranker.model.MODEL_HASHES", changed):
            with self.assertRaisesRegex(
                inference.InferenceError, "file_identity_changed"
            ):
                with self.cached(stamp):
                    self.fail("changed expected pin reused")

    def test_pinned_manifest_is_rehashed_before_cache_admission(self):
        _, stamp = self.deep()
        changed = {name: "f" * 64 for name in inference.prepare_search_models.PINS}
        with mock.patch.object(inference.prepare_search_models, "PINS", changed):
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                with self.cached(stamp):
                    self.fail("changed manifest pin accepted")

    def test_small_artifacts_still_require_their_actual_digest(self):
        _, stamp = self.deep()
        target = self.config.reranker_model_root / "config.json"
        target.chmod(0o600)
        target.write_bytes(b"[]")
        target.chmod(0o400)
        with self.assertRaisesRegex(inference.InferenceError, "model_digest_changed"):
            with self.cached(stamp):
                self.fail("small changed metadata was not hashed")

    def test_retained_file_and_named_path_are_checked_on_context_exit(self):
        _, stamp = self.deep()
        target = next(iter(self.large))
        with self.assertRaisesRegex(inference.InferenceError, "model_file_changed"):
            with self.cached(stamp):
                replacement = target.with_name(target.name + ".replacement")
                self.write(replacement, target.read_bytes())
                replacement.replace(target)

    def test_file_replaced_between_named_stat_and_open_is_rejected(self):
        _, stamp = self.deep()
        target = self.config.reranker_model_root / "model.safetensors"
        replacement = target.with_name("replacement.safetensors")
        self.write(replacement, target.read_bytes())
        original, swapped = os.open, False

        def racing_open(name, flags, *args, dir_fd=None):
            nonlocal swapped
            if name == target.name and dir_fd is not None and not swapped:
                swapped = True
                replacement.replace(target)
            return original(name, flags, *args, dir_fd=dir_fd)

        with mock.patch.object(inference.os, "open", side_effect=racing_open):
            with self.assertRaisesRegex(inference.InferenceError, "model_file_changed"):
                with self.cached(stamp):
                    self.fail("file replacement while opening was accepted")
        self.assertTrue(swapped)

    def test_parent_ancestry_remains_pinned_during_cached_context(self):
        _, stamp = self.deep()
        with self.assertRaisesRegex(ValueError, "model_source_directory_changed"):
            with self.cached(stamp):
                root = self.config.api_model_root
                root.rename(root.with_name("previous-api"))
                root.mkdir(mode=0o700)

    def test_cache_does_not_weaken_modes_or_widen_model_roles(self):
        _, stamp = self.deep(include_embedding=False)
        with self.assertRaisesRegex(inference.InferenceError, "file_identity_changed"):
            with self.cached(stamp):
                self.fail("embedding authority was added to a smaller cache")
        _, stamp = self.deep()
        target = next(iter(self.large))
        target.chmod(0o660)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            with self.cached(stamp):
                self.fail("group-writable file accepted")

    def test_atime_is_excluded_from_both_deep_and_cached_identity(self):
        _, stamp = self.deep()
        original = os.fstat

        class AtimeChanged:
            def __init__(self, info):
                self.info = info

            def __getattr__(self, name):
                return getattr(self.info, name) + (
                    1 if name in {"st_atime", "st_atime_ns"} else 0
                )

        with mock.patch.object(
            inference.os, "fstat", side_effect=lambda fd: AtimeChanged(original(fd))
        ):
            self.assertEqual(self.deep()[1], stamp)
            with self.cached(stamp) as (_, verify):
                self.assertEqual(verify.file_identity_sha256, stamp)

    def test_cached_deadline_and_invalid_fingerprint_fail_before_admission(self):
        _, stamp = self.deep()
        for value in (None, True, "", "f" * 63, "g" * 64):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    inference.InferenceError, "file_identity_invalid"
                ),
            ):
                with self.cached(value):
                    self.fail("invalid cache fingerprint accepted")
        with self.assertRaisesRegex(inference.InferenceError, "verification_deadline"):
            with self.cached(stamp, deadline=time.monotonic() - 1):
                self.fail("expired deadline accepted")
        with mock.patch.object(inference.time, "monotonic", return_value=10) as clock:
            with self.assertRaisesRegex(
                inference.InferenceError, "verification_deadline"
            ):
                with self.cached(stamp, deadline=20) as (_, verify):
                    clock.return_value = 21
                    verify()


if __name__ == "__main__":
    unittest.main()
