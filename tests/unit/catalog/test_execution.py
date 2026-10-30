"""Real harmless subprocess and module-boundary tests, never Docker calls."""

import ast
import copy
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.catalog import commands, execution
from tests.unit.catalog.test_module_boundaries import (
    dependencies,
    imported_module,
    imports_namespace,
    source_tree,
)


class CatalogExecutionTests(unittest.TestCase):
    def test_volume_observation_reads_sealed_metadata_once_without_a_helper(self):
        from search.operations.catalog import volumes
        from tests.unit.catalog.volume_fixtures import IMAGE, CatalogVolumeFixture

        fixture = CatalogVolumeFixture(self)
        fixture.start_intent()
        expected = volumes.ensure(fixture.context)
        execution.write_record(fixture.root, "admin-image.json", {"image_id": IMAGE})
        fixture.calls.clear()
        image_calls = fixture.image.call_count
        self.assertEqual(expected, execution.require_storage(fixture.context))
        self.assertEqual(1, len(fixture.calls))
        self.assertEqual(["docker", "volume", "inspect"], fixture.calls[0][:3])
        self.assertEqual(image_calls, fixture.image.call_count)
        for field, value in (
            ("mountpoint", "/different/volume"),
            ("created_at", "later"),
            ("driver", "nfs"),
        ):
            with self.subTest(field=field):
                old = fixture.metadata[field]
                fixture.metadata[field] = value
                with self.assertRaises(ValueError):
                    execution.require_storage(fixture.context)
                fixture.metadata[field] = old

    def test_volume_binding_uses_the_complete_same_container_snapshot(self):
        receipt = {"name": "fixed-volume", "metadata": {"mountpoint": "/opaque/volume"}}
        mount = {
            "Type": "volume",
            "Name": "fixed-volume",
            "Source": "/opaque/volume",
            "Driver": "local",
            "Destination": "/usr/share/opensearch/data",
            "RW": True,
        }
        row = {
            "Config": {"Labels": {"com.docker.compose.service": "opensearch-search"}},
            "Mounts": [mount],
        }
        execution.validate_storage_mounts(receipt, [row])
        for field, value in (
            ("Source", "/other/volume"),
            ("Name", "another"),
            ("Driver", "nfs"),
            ("RW", 1),
            ("Destination", "/other"),
        ):
            with self.subTest(field=field):
                old = mount[field]
                mount[field] = value
                with self.assertRaises(ValueError):
                    execution.validate_storage_mounts(receipt, [row])
                mount[field] = old
        for rows in ([], [row, row]):
            with self.assertRaises(ValueError):
                execution.validate_storage_mounts(receipt, rows)
        other = copy.deepcopy(row)
        other["Config"]["Labels"]["com.docker.compose.service"] = "fess-search"
        with self.assertRaises(ValueError):
            execution.validate_storage_mounts(receipt, [row, other])

    def test_missing_volume_evidence_never_skips_mount_validation(self):
        with self.assertRaisesRegex(ValueError, "volume_binding_changed"):
            execution.validate_storage_mounts(None, [])

    def test_real_child_recreates_iid_with_private_mode_without_parent_umask_changes(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="catalog-child-umask-") as directory:
            path = Path(directory) / "iid"
            path.touch(mode=0o600)
            script = "import os,sys; p=sys.argv[1]; os.unlink(p); fd=os.open(p, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o644); os.write(fd, b'sha256:example'); os.close(fd); print(oct(os.stat(p).st_mode & 0o777))"
            outcome = commands.default_runner(Path(directory))(
                [sys.executable, "-c", script, str(path)], timeout=10
            )
            self.assertEqual(0, outcome.returncode)
            self.assertEqual("0o600\n", outcome.stdout)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_registry_digest_is_canonical_and_keeps_the_exact_sha(self):
        digest = "sha256:" + "a" * 64
        self.assertEqual(
            "ollama/ollama@" + digest,
            execution.registry_digest("ollama/ollama:0.32.14@" + digest),
        )
        for value in (
            "other/repository:0.32.14@" + digest,
            "ollama/ollama:latest",
            "ollama/ollama:0.32.14@sha256:short",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                execution.registry_digest(value)

    def test_shared_execution_has_no_circular_or_cross_private_imports(self):
        prohibited = {
            "execution": {
                "search.operations.catalog.runtime",
                "search.operations.catalog.administration",
                "search.operations.lifecycle",
            },
            "administration": {"search.operations.catalog.runtime"},
        }
        for module, banned in prohibited.items():
            found = dependencies(module)
            for namespace in banned:
                self.assertFalse(imports_namespace(found, namespace))
            for node in ast.walk(source_tree(module)):
                if isinstance(node, ast.ImportFrom):
                    if imported_module(node) == "search.operations.catalog.execution":
                        self.assertTrue(
                            all(not alias.name.startswith("_") for alias in node.names)
                        )


class CatalogPrebuiltImageReceiptTests(unittest.TestCase):
    def setUp(self):
        self.sources = {
            name: {path: "a" * 64 for path in execution.service_sources(name)}
            for name in ("fess-search", "opensearch-search")
        }
        self.reference = "ollama/ollama:0.32.14@sha256:" + "f" * 64
        self.context = {
            "plan": {
                "project": "depositar-ai-catalog-" + "1" * 12 + "-" + "2" * 12,
                "revision": "2" * 40,
                "source_sha256": {
                    path: digest
                    for sources in self.sources.values()
                    for path, digest in sources.items()
                },
            },
            "compose": {
                "services": {
                    name: {
                        "build": {
                            "args": {"APP_BASE_IMAGE": "reviewed@sha256:" + "b" * 64}
                        }
                    }
                    for name in self.sources
                }
            },
        }
        self.context["compose"]["services"]["ollama-embedding"] = {
            "image": self.reference
        }
        self.receipt = {
            "schema_version": 1,
            "project": self.context["plan"]["project"],
            "revision": self.context["plan"]["revision"],
            "images": {},
        }
        self.local_images = {}
        for index, (name, sources) in enumerate(self.sources.items(), start=1):
            base = self.context["compose"]["services"][name]["build"]["args"][
                "APP_BASE_IMAGE"
            ]
            labels = execution.image_labels(self.context, name, sources, base)
            reference = self.context["plan"]["project"] + "-" + name + ":1"
            image_id = "sha256:" + str(index) * 64
            self.receipt["images"][name] = {
                "image_id": image_id,
                "reference": reference,
                "labels": labels,
                "sources": sources,
            }
            self.local_images[reference] = self.local_images[image_id] = {
                "Id": image_id,
                "Config": {"Labels": labels},
            }
        ollama_id = "sha256:" + "3" * 64
        self.receipt["images"]["ollama-embedding"] = {
            "image_id": ollama_id,
            "reference": self.reference,
            "labels": {},
            "sources": {},
        }
        self.local_images[self.reference] = self.local_images[ollama_id] = {
            "Id": ollama_id,
            "RepoDigests": [execution.registry_digest(self.reference)],
            "Config": {"Labels": {}},
        }

    def test_explicit_image_proof_retains_security_preflight_and_does_not_write(self):
        with (
            patch.object(execution, "verify_security_material") as security,
            patch.object(
                execution,
                "inspect_image",
                side_effect=lambda context, ref: self.local_images[ref],
            ),
            patch.object(execution, "write_record") as writer,
            patch.object(execution, "sealed_source") as source_reader,
        ):
            images = execution.verify_images(self.context, self.receipt)
        security.assert_called_once_with(self.context)
        writer.assert_not_called()
        source_reader.assert_not_called()
        self.assertEqual(self.receipt["images"], images)
        images["fess-search"]["sources"].clear()
        self.assertTrue(self.receipt["images"]["fess-search"]["sources"])

    def test_self_consistent_local_labels_cannot_replace_sealed_source_identity(self):
        supplied = copy.deepcopy(self.receipt)
        entry = supplied["images"]["fess-search"]
        entry["sources"][next(iter(entry["sources"]))] = "9" * 64
        entry["labels"][execution.LABEL_PREFIX + "source-sha256"] = (
            execution.digest_record(entry["sources"])
        )
        self.local_images[entry["reference"]]["Config"]["Labels"] = entry["labels"]
        with (
            patch.object(execution, "verify_security_material"),
            patch.object(
                execution,
                "inspect_image",
                side_effect=lambda context, ref: self.local_images[ref],
            ),
            self.assertRaisesRegex(ValueError, "catalog_image_source_receipt_changed"),
        ):
            execution.verify_images(self.context, supplied)

    def test_changed_security_material_stops_before_any_image_inspection(self):
        with (
            patch.object(
                execution,
                "verify_security_material",
                side_effect=execution.RuntimeError("catalog_security_material_changed"),
            ),
            patch.object(execution, "inspect_image") as inspect,
            self.assertRaisesRegex(ValueError, "catalog_security_material_changed"),
        ):
            execution.verify_images(self.context, self.receipt)
        inspect.assert_not_called()


class CatalogImageIdFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="catalog-image-id-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "slot"
        self.root.mkdir(mode=0o700)
        self.path = self.root / "image-fess-search.iid"
        self.image_id = "sha256:" + "a" * 64

    def write_iid(self, *, mode=0o600, raw=None):
        self.path.unlink(missing_ok=True)
        self.path.write_bytes(self.image_id.encode("ascii") if raw is None else raw)
        self.path.chmod(mode)

    def read_iid(self):
        return execution._read_iid(self.root, self.path.name)

    def test_exact_600_and_builder_644_with_optional_single_lf_are_valid(self):
        for mode in (0o600, 0o644):
            for suffix in (b"", b"\n"):
                with self.subTest(mode=oct(mode), suffix=suffix):
                    self.write_iid(
                        mode=mode, raw=self.image_id.encode("ascii") + suffix
                    )
                    self.assertEqual(self.image_id, self.read_iid())

    def test_other_modes_are_rejected(self):
        for mode in (0o400, 0o640, 0o664, 0o666):
            with self.subTest(mode=oct(mode)):
                self.write_iid(mode=mode)
                with self.assertRaisesRegex(ValueError, "catalog_image_id_file_unsafe"):
                    self.read_iid()

    def test_fifo_is_rejected_before_open_and_cannot_block(self):
        os.mkfifo(self.path, 0o600)
        with (
            patch.object(execution.private_io, "read_file") as reader,
            self.assertRaisesRegex(ValueError, "catalog_image_id_file_unsafe"),
        ):
            self.read_iid()
        reader.assert_not_called()

    def test_symlink_and_multiple_links_are_rejected(self):
        target = self.root / "target"
        target.write_bytes(self.image_id.encode("ascii"))
        target.chmod(0o600)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "catalog_image_id_file_unsafe"):
            self.read_iid()
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaisesRegex(ValueError, "catalog_image_id_file_unsafe"):
            self.read_iid()

    def test_nonowner_file_stat_is_rejected(self):
        self.write_iid()
        original = os.stat

        def changed_owner(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.path.name:
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_uid=info.st_uid + 1,
                    st_nlink=info.st_nlink,
                    st_size=info.st_size,
                )
            return info

        with (
            patch.object(execution.os, "stat", side_effect=changed_owner),
            self.assertRaisesRegex(ValueError, "catalog_image_id_file_unsafe"),
        ):
            self.read_iid()

    def test_wrong_sizes_and_nonimmutable_values_are_rejected(self):
        for raw in (
            b"",
            b"sha256:short",
            self.image_id.encode("ascii") + b"\n\n",
            ("sha256:" + "A" * 64).encode("ascii"),
            b" " + self.image_id.encode("ascii"),
            self.image_id.encode("ascii") + b" ",
            b"\xff" * 71,
        ):
            with self.subTest(raw=raw):
                self.write_iid(raw=raw)
                with self.assertRaises(ValueError):
                    self.read_iid()

    def test_private_parent_is_required(self):
        self.write_iid(mode=0o644)
        self.root.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private_io_directory_unsafe"):
            self.read_iid()

    def test_replacement_between_stat_and_read_is_rejected(self):
        self.write_iid(mode=0o644)
        original = execution.private_io.read_file

        def replace_iid(*args, **kwargs):
            replacement = self.root / "replacement"
            replacement.write_bytes(self.image_id.encode("ascii"))
            replacement.chmod(0o644)
            os.replace(replacement, self.path)
            return original(*args, **kwargs)

        with (
            patch.object(execution.private_io, "read_file", side_effect=replace_iid),
            self.assertRaisesRegex(ValueError, "catalog_image_id_file_changed"),
        ):
            self.read_iid()

    def test_changed_mode_between_stat_and_read_is_rejected(self):
        self.write_iid(mode=0o644)
        original = execution.private_io.read_file

        def change_mode(*args, **kwargs):
            self.path.chmod(0o664)
            return original(*args, **kwargs)

        with (
            patch.object(execution.private_io, "read_file", side_effect=change_mode),
            self.assertRaisesRegex(ValueError, "private_io_file_unsafe"),
        ):
            self.read_iid()

    def test_replaced_parent_is_rejected(self):
        self.write_iid(mode=0o644)
        original = execution.private_io.read_file

        def replace_parent(*args, **kwargs):
            self.root.rename(self.parent / "old-slot")
            self.root.mkdir(mode=0o700)
            return original(*args, **kwargs)

        with (
            patch.object(execution.private_io, "read_file", side_effect=replace_parent),
            self.assertRaisesRegex(ValueError, "private_io_directory_changed"),
        ):
            self.read_iid()

    def test_non_iid_names_cannot_select_other_files(self):
        self.write_iid()
        for name in ("secrets.json", "../image-fess-search.iid", None):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    ValueError, "catalog_image_id_file_name_invalid"
                ),
            ):
                execution._read_iid(self.root, name)

    def test_generic_private_reader_still_rejects_644_receipts(self):
        self.write_iid(mode=0o644)
        descriptor = execution.private_io.open_directory(self.root)
        try:
            with self.assertRaisesRegex(ValueError, "private_io_file_unsafe"):
                execution.private_io.read_file(
                    descriptor, self.path.name, mode=0o600, max_bytes=72
                )
        finally:
            os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
