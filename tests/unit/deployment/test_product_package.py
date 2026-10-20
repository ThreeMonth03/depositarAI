"""Product payload closure and corrupt/unsafe export tests, without Docker."""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest

from search.operations.deployment import product_package as package
from search.operations.shared import private_io
from search.catalog.build import builder
from search.catalog.bundle import CatalogBundle
from search.reranker.model import MODEL_HASHES
from tests.unit.operations.test_metadata_sync import dataset


REVISION = "a" * 40


def record(raw):
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def oci_fixture(*, architecture="amd64", corrupt_blob=False, wrong_compatibility=False):
    """Docker 29's observed index ID, config blob, and gzip-layer layout."""
    blobs = {}

    def descriptor(raw, media_type):
        digest = hashlib.sha256(raw).hexdigest()
        blobs["blobs/sha256/" + digest] = raw
        return {"digest": "sha256:" + digest, "size": len(raw), "mediaType": media_type}

    uncompressed = b"synthetic fixed layer bytes"
    layer = descriptor(
        gzip.compress(uncompressed, mtime=0),
        "application/vnd.oci.image.layer.v1.tar+gzip",
    )
    config_value = {
        "architecture": architecture,
        "os": "linux",
        "config": {"Env": ["PATH=/usr/bin"], "Labels": {}},
        "rootfs": {
            "type": "layers",
            "diff_ids": ["sha256:" + hashlib.sha256(uncompressed).hexdigest()],
        },
    }
    config = descriptor(
        private_io.canonical(config_value), "application/vnd.oci.image.config.v1+json"
    )
    manifest = descriptor(
        private_io.canonical(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": config,
                "layers": [layer],
            }
        ),
        "application/vnd.oci.image.manifest.v1+json",
    )
    manifest["platform"] = {"architecture": architecture, "os": "linux"}
    # Real save retains other-platform index references without their blobs.
    uncached = {
        "digest": "sha256:" + "f" * 64,
        "size": 200,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "platform": {"architecture": "arm64", "os": "linux"},
    }
    root = descriptor(
        private_io.canonical(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [manifest, uncached],
            }
        ),
        "application/vnd.oci.image.index.v1+json",
    )
    blobs["index.json"] = private_io.canonical(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [root],
        }
    )
    blobs["oci-layout"] = private_io.canonical({"imageLayoutVersion": "1.0.0"})
    blobs["manifest.json"] = private_io.canonical(
        [
            {
                "Config": "blobs/sha256/" + config["digest"][7:],
                "Layers": [
                    "blobs/sha256/"
                    + ("e" * 64 if wrong_compatibility else layer["digest"][7:])
                ],
                "RepoTags": ["fixture:test"],
            }
        ]
    )
    if corrupt_blob:
        blobs["blobs/sha256/" + layer["digest"][7:]] += b"corruption"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, raw in blobs.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    environment_digest = hashlib.sha256(
        private_io.canonical({"PATH": "/usr/bin"})
    ).hexdigest()
    return stream.getvalue(), {
        "fixture": {
            "image_id": root["digest"],
            "image_environment_sha256": environment_digest,
        }
    }


class FakeDocker:
    def __init__(self, plan, images, configs, layer):
        self.plan, self.images, self.configs, self.layer = plan, images, configs, layer
        self.calls = []
        self.corrupt_layer = False
        self.omit_image = False
        self.fail_save = False
        self.after_save = None

    def __call__(self, argv, *, timeout, environment):
        self.calls.append(list(argv))
        assert environment == {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        assert not list(Path(argv[4]).iterdir())
        arguments = argv[5:]
        if arguments[:2] == ["image", "inspect"]:
            inspected = self.images[arguments[-1]]
            return subprocess.CompletedProcess(argv, 0, json.dumps(inspected), "")
        assert arguments[:2] == ["image", "save"]
        assert set(arguments[4:]) == set(self.images)
        assert not Path(arguments[3]).exists()
        if self.fail_save:
            return subprocess.CompletedProcess(argv, 1, b"", b"private-output-sentinel")
        rows = []
        with tarfile.open(arguments[3], mode="w") as archive:

            def add(name, raw):
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))

            for index, (identifier, config) in enumerate(self.configs.items()):
                if self.omit_image and index == 0:
                    continue
                name = identifier[7:] + ".json"
                add(name, config)
                rows.append(
                    {"Config": name, "RepoTags": None, "Layers": ["layer/layer.tar"]}
                )
            add(
                "layer/layer.tar",
                self.layer + (b"corrupt" if self.corrupt_layer else b""),
            )
            add("manifest.json", json.dumps(rows).encode())
        if self.after_save:
            self.after_save()
        return subprocess.CompletedProcess(argv, 0, b"", b"")


class ProductPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-product-package-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source_raw = {
            package.source_names(REVISION)[0]: b"reviewed-source-archive",
            package.source_names(REVISION)[1]: b"reviewed-git-bundle",
            "SOURCE_REVISION": (REVISION + "\n").encode(),
        }
        self.source_raw["SHA256SUMS"] = "".join(
            record(raw)["sha256"] + "  " + name + "\n"
            for name, raw in self.source_raw.items()
        ).encode()
        self.source = {name: record(raw) for name, raw in self.source_raw.items()}
        self.metadata_raw = {
            name: (name + " public metadata").encode()
            for name in (*package.ARTIFACT_NAMES, package.MANIFEST_NAME)
        }
        self.metadata = {
            "bundle_id": "b" * 64,
            "manifest_sha256": record(self.metadata_raw[package.MANIFEST_NAME])[
                "sha256"
            ],
            "document_count": 1,
            "files": {name: record(raw) for name, raw in self.metadata_raw.items()},
        }
        self.catalog = {
            "project": "depositar-ai-catalog-" + "b" * 12 + "-" + REVISION[:12],
            "revision": REVISION,
            "bundle_id": self.metadata["bundle_id"],
            "manifest_sha256": self.metadata["manifest_sha256"],
            "compose_sha256": "c" * 64,
            "image_receipt_sha256": "d" * 64,
        }
        self.model_raw = {
            role: {"weights/model.bin": (role + " pinned model bytes").encode()}
            for role in package.MODEL_ROLES
        }
        self.models = {
            role: {name: record(raw) for name, raw in files.items()}
            for role, files in self.model_raw.items()
        }
        self.images, self.inspections, self.configs = {}, {}, {}
        layer_stream = io.BytesIO()
        with tarfile.open(fileobj=layer_stream, mode="w"):
            pass
        self.layer = layer_stream.getvalue()
        environment = {"PATH": "/usr/bin"}
        environment_digest = hashlib.sha256(
            private_io.canonical(environment)
        ).hexdigest()
        for index, role in enumerate(package.IMAGE_ROLES):
            source_digest = hashlib.sha256(role.encode()).hexdigest()
            if role in {"fess-search", "opensearch-search"}:
                sources = {
                    name: hashlib.sha256(name.encode()).hexdigest()
                    for name in package.catalog_execution.service_sources(role)
                }
                source_digest = package.catalog_execution.digest_record(sources)
            labels = {"fixture-role": role}
            if role != "ollama":
                labels["org.opencontainers.image.revision"] = REVISION
                labels["io.depositar.ai.source.sha256"] = source_digest
                labels["io.depositar.ai.catalog.source-sha256"] = source_digest
                labels["io.depositar.ai.catalog.project"] = self.catalog["project"]
            config = {
                "architecture": "amd64",
                "os": "linux",
                "config": {"Env": ["PATH=/usr/bin"], "Labels": labels},
                "rootfs": {
                    "type": "layers",
                    "diff_ids": ["sha256:" + hashlib.sha256(self.layer).hexdigest()],
                },
            }
            raw = private_io.canonical(config)
            identifier = "sha256:" + hashlib.sha256(raw).hexdigest()
            self.configs[identifier] = raw
            self.inspections[identifier] = {
                "Id": identifier,
                "Architecture": "amd64",
                "Os": "linux",
                "Config": config["config"],
                "RepoDigests": [
                    "ollama/ollama@" + package.composition.OLLAMA_IMAGE.split("@", 1)[1]
                ]
                if role == "ollama"
                else [],
            }
            self.images[role] = {
                "image_id": identifier,
                "image_environment_sha256": environment_digest,
            }
            self.images[role].update(
                {"reference": package.composition.OLLAMA_IMAGE}
                if role == "ollama"
                else {"revision": REVISION, "source_sha256": source_digest}
            )
        self.catalog_images = {
            "schema_version": 1,
            "project": self.catalog["project"],
            "revision": REVISION,
            "images": {},
        }
        for service, base in (
            ("fess-search", package.validate_catalog_compose.FESS_BASE_IMAGE),
            (
                "opensearch-search",
                package.validate_catalog_compose.OPENSEARCH_BASE_IMAGE,
            ),
            ("ollama-embedding", None),
        ):
            role = "ollama" if base is None else service
            sources = (
                {
                    name: hashlib.sha256(name.encode()).hexdigest()
                    for name in package.catalog_execution.service_sources(service)
                }
                if base is not None
                else {}
            )
            self.catalog_images["images"][service] = {
                "reference": self.catalog["project"] + "-" + service + ":1"
                if base is not None
                else package.composition.OLLAMA_IMAGE,
                "image_id": self.images[role]["image_id"],
                "labels": package.catalog_execution.image_labels(
                    {"plan": self.catalog}, service, sources, base
                )
                if base is not None
                else {},
                "sources": sources,
            }
        self.catalog["image_receipt_sha256"] = package.catalog_execution.digest_record(
            self.catalog_images
        )
        self.source_files = self.write_inputs("source-input", self.source_raw)
        self.metadata_files = self.write_inputs("metadata-input", self.metadata_raw)
        self.model_files = {
            role: self.write_inputs("model-input-" + role, files)
            for role, files in self.model_raw.items()
        }
        self.destination = self.root / "output"
        self.plan = self.make_plan()
        self.docker = FakeDocker(self.plan, self.inspections, self.configs, self.layer)

    def write_inputs(self, directory, files):
        result = {}
        for name, raw in files.items():
            path = self.root / directory / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # mkdir(parents=True) does not control intermediate modes.
            for parent in path.parents:
                if parent == self.root:
                    break
                parent.chmod(0o700)
            path.write_bytes(raw)
            path.chmod(0o600)
            result[name] = path
        return result

    def make_plan(self, **options):
        return package.plan_product_package(
            REVISION,
            source=self.source,
            images=self.images,
            models=self.models,
            metadata=self.metadata,
            catalog=self.catalog,
            **options,
        )

    def export(self, **options):
        return package.export_product_package(
            self.plan,
            self.destination,
            source_files=self.source_files,
            metadata_files=self.metadata_files,
            model_files=self.model_files
            if self.plan["models"]["delivery"] == "bundled"
            else None,
            runner=self.docker,
            **options,
        )

    def assert_incomplete(self):
        self.assertFalse((self.destination / "package.json").exists())

    def use_origin_catalog(self, revision="1" * 40):
        """Give the catalog its own real config IDs, not application aliases."""
        self.catalog["revision"] = revision
        self.catalog["project"] = (
            "depositar-ai-catalog-"
            + self.metadata["bundle_id"][:12]
            + "-"
            + revision[:12]
        )
        self.catalog_images.update(project=self.catalog["project"], revision=revision)
        for role in package.CATALOG_ROLES:
            entry = self.images[role]
            old_id = entry["image_id"]
            config = json.loads(self.configs.pop(old_id))
            labels = config["config"]["Labels"]
            labels["org.opencontainers.image.revision"] = revision
            labels["io.depositar.ai.catalog.project"] = self.catalog["project"]
            raw = private_io.canonical(config)
            image_id = "sha256:" + hashlib.sha256(raw).hexdigest()
            self.configs[image_id] = raw
            inspected = self.inspections.pop(old_id)
            inspected.update(Id=image_id, Config=config["config"])
            self.inspections[image_id] = inspected
            entry.update(revision=revision, image_id=image_id)
        for service in ("fess-search", "opensearch-search"):
            receipt = self.catalog_images["images"][service]
            receipt["reference"] = self.catalog["project"] + "-" + service + ":1"
            receipt["image_id"] = self.images[service]["image_id"]
            receipt["labels"] = package.catalog_execution.image_labels(
                {"plan": self.catalog},
                service,
                receipt["sources"],
                receipt["labels"]["io.depositar.ai.catalog.base"],
            )
        self.catalog["image_receipt_sha256"] = package.catalog_execution.digest_record(
            self.catalog_images
        )
        self.plan = self.make_plan()
        self.docker = FakeDocker(self.plan, self.inspections, self.configs, self.layer)

    def test_mixed_package_keeps_catalog_provenance_and_actual_exported_image_ids(self):
        self.use_origin_catalog()
        result = self.export(catalog_images=self.catalog_images)
        self.assertEqual(REVISION, result["revision"])
        self.assertEqual("1" * 40, result["catalog"]["revision"])
        self.assertFalse(result["install_complete"])
        receipt = json.loads((self.destination / "catalog-images.json").read_bytes())
        self.assertEqual("1" * 40, receipt["revision"])
        self.assertEqual(self.catalog["project"], receipt["project"])
        for role in package.BUILT_ROLES:
            self.assertEqual(
                "1" * 40 if role in package.CATALOG_ROLES else REVISION,
                result["images"][role]["revision"],
            )
        self.assertEqual(
            (REVISION + "\n").encode(),
            (self.destination / "source/SOURCE_REVISION").read_bytes(),
        )

    def test_mixed_package_cannot_relabel_catalog_or_application_images(self):
        self.use_origin_catalog()
        for role, wrong in (("web", "1" * 40), ("catalog-admin", REVISION)):
            with self.subTest(role=role):
                entry = self.images[role]
                original = entry["revision"]
                entry["revision"] = wrong
                with self.assertRaisesRegex(
                    package.ProductPackageError, "image_source_mismatch"
                ):
                    self.make_plan()
                entry["revision"] = original
        receipt = copy.deepcopy(self.catalog_images)
        receipt["revision"] = REVISION
        with self.assertRaisesRegex(
            package.ProductPackageError, "catalog_images_identity_mismatch"
        ):
            self.export(catalog_images=receipt)
        self.assertFalse(self.destination.exists())

    def test_origin_image_label_drift_still_blocks_export(self):
        self.use_origin_catalog()
        row = self.inspections[self.images["catalog-admin"]["image_id"]]
        row["Config"]["Labels"]["org.opencontainers.image.revision"] = REVISION
        with self.assertRaisesRegex(
            package.ProductPackageError, "image_source_mismatch"
        ):
            self.export(catalog_images=self.catalog_images)
        self.assert_incomplete()

    def test_complete_payload_uses_docker_save_and_copies_only_approved_files(self):
        result = self.export()
        self.assertEqual("packaged", result["status"])
        self.assertTrue(result["payload_self_contained"])
        self.assertIsNone(result["catalog_image_receipt"])
        for field in (
            "install_complete",
            "runtime_install_verified",
            "registry_identity_restore_verified",
            "credentials_included",
            "active_changed",
        ):
            self.assertIs(False, result[field])
        for group, files in (
            ("source", self.source_raw),
            ("metadata", self.metadata_raw),
            *(("models/" + role, files) for role, files in self.model_raw.items()),
        ):
            for name, expected in files.items():
                path = self.destination / group / name
                self.assertEqual(expected, path.read_bytes())
                self.assertEqual(0o600, path.stat().st_mode & 0o777)
        for path in self.destination.rglob("*"):
            if path.is_dir():
                self.assertEqual(0o700, path.stat().st_mode & 0o777)
        checksums = (self.destination / "SHA256SUMS").read_text().splitlines()
        for line in checksums:
            digest, name = line.split("  ", 1)
            self.assertEqual(
                digest,
                hashlib.sha256((self.destination / name).read_bytes()).hexdigest(),
            )
        self.assertEqual(19, len(self.docker.calls))
        self.assertTrue(
            all(
                call[5:7] in (["image", "inspect"], ["image", "save"])
                for call in self.docker.calls
            )
        )
        encoded = (self.destination / "package.json").read_text()
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("PATH=", encoded)

    def test_catalog_prebuilt_receipt_is_bundled_with_matching_checksum(self):
        result = self.export(catalog_images=self.catalog_images)
        raw = (self.destination / "catalog-images.json").read_bytes()
        expected = private_io.canonical(self.catalog_images)
        self.assertEqual(expected, raw)
        self.assertEqual(
            {
                "path": "catalog-images.json",
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
            result["catalog_image_receipt"],
        )
        self.assertEqual(
            0o600, (self.destination / "catalog-images.json").stat().st_mode & 0o777
        )
        self.assertIn(
            hashlib.sha256(raw).hexdigest() + "  catalog-images.json\n",
            (self.destination / "SHA256SUMS").read_text(),
        )

    def test_catalog_receipt_rejects_source_secret_id_and_identity_changes(self):
        def change_source(value):
            sources = value["images"]["fess-search"]["sources"]
            sources[next(iter(sources))] = "f" * 64

        mutations = (
            change_source,
            lambda value: value["images"]["fess-search"]["labels"].update(
                password="private-sentinel"
            ),
            lambda value: value["images"]["fess-search"].update(
                image_id=self.images["web"]["image_id"]
            ),
            lambda value: value.update(project="private-sentinel"),
            lambda value: value["images"]["ollama-embedding"].update(
                reference="ollama:latest"
            ),
            lambda value: value.update(password="private-sentinel"),
            lambda value: value["images"]["fess-search"].update(
                credential="private-sentinel"
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                receipt = copy.deepcopy(self.catalog_images)
                mutation(receipt)
                with self.assertRaises(package.ProductPackageError) as error:
                    self.export(catalog_images=receipt)
                self.assertNotIn("private-sentinel", str(error.exception))
                self.assertFalse(self.destination.exists())

    def test_catalog_receipt_cannot_change_during_authority_reproof(self):
        with self.assertRaisesRegex(
            package.ProductPackageError, "copied_asset_changed"
        ):
            self.export(
                catalog_images=self.catalog_images,
                before_publish=lambda: (
                    self.destination / "catalog-images.json"
                ).write_bytes(b"changed"),
            )
        self.assert_incomplete()

    def test_explicit_external_models_are_reported_without_copy(self):
        self.plan = self.make_plan(include_models=False)
        result = self.export()
        self.assertEqual("external", result["models"]["delivery"])
        self.assertFalse(result["payload_self_contained"])
        self.assertFalse((self.destination / "models").exists())

    def test_missing_image_role_mixed_source_and_unreviewed_registry_rejected(self):
        for mutation in (
            lambda: self.images.pop("catalog-admin"),
            lambda: self.images["web"].update(revision="f" * 40),
            lambda: self.images["ollama"].update(reference="ollama:latest"),
        ):
            previous = copy.deepcopy(self.images)
            mutation()
            with self.assertRaises(package.ProductPackageError):
                self.make_plan()
            self.images = previous

    def test_catalog_project_must_bind_the_same_bundle_and_revision(self):
        self.catalog["project"] = (
            "depositar-ai-catalog-" + "f" * 12 + "-" + REVISION[:12]
        )
        with self.assertRaisesRegex(
            package.ProductPackageError, "catalog_identity_mismatch"
        ):
            self.make_plan()

    def test_unknown_fields_cannot_smuggle_credentials_into_manifest(self):
        self.catalog["password"] = "private-sentinel"
        with self.assertRaises(package.ProductPackageError) as error:
            self.make_plan()
        self.assertNotIn("private-sentinel", str(error.exception))

    def test_unsafe_model_relative_paths_rejected(self):
        for name in (
            "../model.bin",
            "/model.bin",
            ".env",
            "secrets/key",
            "config\\model.bin",
        ):
            self.models["api"] = {name: record(b"fixture")}
            with self.assertRaises(package.ProductPackageError):
                self.make_plan()

    def test_pinned_reranker_inventory_accepts_reviewed_tokenizer_metadata(self):
        self.models["reranker"] = {
            name: {"bytes": 1, "sha256": digest}
            for name, digest in MODEL_HASHES.items()
        }
        plan = self.make_plan()
        self.assertEqual(self.models["reranker"], plan["models"]["files"]["reranker"])
        self.assertIn("special_tokens_map.json", plan["models"]["files"]["reranker"])

    def test_reviewed_model_name_exception_requires_exact_role_path_and_digest(self):
        original = copy.deepcopy(self.models)
        for role, name, digest in (
            ("api", "special_tokens_map.json", MODEL_HASHES["special_tokens_map.json"]),
            ("reranker", "special_tokens_map.json", "f" * 64),
            (
                "reranker",
                "other/special_tokens_map.json",
                MODEL_HASHES["special_tokens_map.json"],
            ),
        ):
            with self.subTest(role=role, name=name, digest=digest):
                self.models = copy.deepcopy(original)
                self.models[role] = {name: {"bytes": 1, "sha256": digest}}
                with self.assertRaisesRegex(
                    package.ProductPackageError, "asset_name_invalid"
                ):
                    self.make_plan()
        # Archive/source paths retain the general secret-name policy.
        with self.assertRaisesRegex(package.ProductPackageError, "asset_name_invalid"):
            package._relative("special_tokens_map.json")

    def test_credential_token_filenames_still_rejected_in_every_model_role(self):
        original = copy.deepcopy(self.models)
        for role in package.MODEL_ROLES:
            for name in (
                "credentials.json",
                "token.json",
                "tokens.json",
                "access_token.json",
                "refresh-token.json",
                "private_key.pem",
                "api_key.json",
                "tokens/model.bin",
            ):
                with self.subTest(role=role, name=name):
                    self.models = copy.deepcopy(original)
                    self.models[role] = {name: record(b"private-sentinel")}
                    with self.assertRaisesRegex(
                        package.ProductPackageError, "asset_name_invalid"
                    ):
                        self.make_plan()

    def test_tampered_plan_cannot_assert_runtime_install_success(self):
        self.plan["runtime_install_verified"] = True
        with self.assertRaisesRegex(package.ProductPackageError, "plan_changed"):
            self.export()
        self.assertFalse(self.destination.exists())
        self.assertEqual([], self.docker.calls)

    def test_existing_or_symlink_destination_is_not_adopted(self):
        self.destination.mkdir(mode=0o700)
        with self.assertRaises(package.ProductPackageError):
            self.export()
        self.assertEqual([], self.docker.calls)
        self.destination.rmdir()
        self.destination.symlink_to(
            self.root / "source-input", target_is_directory=True
        )
        with self.assertRaises(package.ProductPackageError):
            self.export()
        self.assertEqual([], self.docker.calls)

    def test_source_file_corruption_or_symlink_is_rejected_before_docker(self):
        path = self.source_files["SOURCE_REVISION"]
        original = path.read_bytes()
        path.write_bytes(b"f" * 40 + b"\n")
        with self.assertRaises(package.ProductPackageError):
            self.export()
        self.assert_incomplete()
        self.assertEqual([], self.docker.calls)
        self.destination = self.root / "output-second"
        path.unlink()
        target = path.parent / "actual-revision"
        target.write_bytes(original)
        target.chmod(0o600)
        path.symlink_to(target)
        with self.assertRaises(package.ProductPackageError):
            self.export()
        self.assert_incomplete()

    def test_model_corruption_is_not_a_successful_package(self):
        next(iter(self.model_files["api"].values())).write_bytes(b"wrong pinned model")
        with self.assertRaises(package.ProductPackageError):
            self.export()
        self.assert_incomplete()
        self.assertEqual([], self.docker.calls)

    def test_sealed_metadata_under_allocated_ancestor_keeps_modes_and_bytes(self):
        source = self.root / "snapshot.jsonl"
        source.write_text(json.dumps(dataset()) + "\n")
        parent = self.root / "bundles"
        parent.mkdir(mode=0o700)
        built = builder.build_bundle(source, parent)
        allocated = self.root / "allocated"
        allocated.mkdir(mode=0o1770)
        allocated.chmod(0o1770)
        parent.rename(allocated / "bundles")
        sealed = allocated / "bundles" / Path(built["bundle_path"]).name
        bundle = CatalogBundle.load(
            sealed, expected_manifest_sha256=built["manifest_sha256"]
        )
        inputs = {
            name: sealed / name
            for name in (*package.ARTIFACT_NAMES, package.MANIFEST_NAME)
        }
        original = {name: path.read_bytes() for name, path in inputs.items()}
        expected = {name: record(raw) for name, raw in original.items()}
        target = self.root / "metadata-copy"
        target.mkdir(mode=0o700)
        self.assertEqual(1, len(bundle.documents))
        self.assertEqual(0o500, stat.S_IMODE(sealed.stat().st_mode))
        # This is the original failure on the supported non-root product host.
        if os.geteuid() != 0:
            with self.assertRaisesRegex(
                package.search_model_source.ModelSourceError,
                "model_source_allocated_root_not_private",
            ):
                with package.search_model_source.open_source_root(sealed):
                    pass
        package._copy_files(inputs, expected, target, sealed_metadata=True)
        self.assertEqual(0o1770, stat.S_IMODE(allocated.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(sealed.parent.stat().st_mode))
        self.assertEqual(0o500, stat.S_IMODE(sealed.stat().st_mode))
        for name, raw in original.items():
            with self.subTest(name=name):
                self.assertEqual(raw, inputs[name].read_bytes())
                self.assertEqual(raw, (target / name).read_bytes())
                self.assertEqual(0o400, stat.S_IMODE(inputs[name].stat().st_mode))
                self.assertEqual(0o600, stat.S_IMODE((target / name).stat().st_mode))

    def test_sealed_metadata_anchor_requires_operator_private_parent(self):
        parent = self.root / "nonprivate-bundles"
        parent.mkdir(mode=0o750)
        parent.chmod(0o750)
        sealed = parent / "sealed"
        sealed.mkdir(mode=0o700)
        inputs = self.write_inputs("nonprivate-bundles/sealed", self.metadata_raw)
        sealed.chmod(0o500)
        parent.chmod(0o750)  # write_inputs intentionally creates private parents.
        target = self.root / "metadata-copy"
        target.mkdir(mode=0o700)
        with self.assertRaises(package.search_model_source.ModelSourceError):
            package._copy_files(
                inputs,
                self.metadata["files"],
                target,
                sealed_metadata=True,
            )
        self.assertEqual(0o500, stat.S_IMODE(sealed.stat().st_mode))
        self.assertEqual(0o750, stat.S_IMODE(parent.stat().st_mode))

    def test_export_refuses_secret_image_configuration_without_echoing_it(self):
        identifier = self.images["web"]["image_id"]
        self.inspections[identifier]["Config"]["Env"].append("API_KEY=private-sentinel")
        with self.assertRaisesRegex(
            package.ProductPackageError, "image_secret_configuration"
        ) as error:
            self.export()
        self.assertNotIn("private-sentinel", str(error.exception))
        self.assert_incomplete()

    def test_save_failure_retains_partial_output_without_complete_manifest(self):
        self.docker.fail_save = True
        with self.assertRaises(package.ProductPackageError) as error:
            self.export()
        self.assertNotIn("private-output-sentinel", str(error.exception))
        self.assert_incomplete()
        self.assertTrue(self.destination.exists())

    def test_archive_missing_image_or_corrupt_layer_is_rejected(self):
        self.docker.omit_image = True
        with self.assertRaises(package.ProductPackageError):
            self.export()
        self.assert_incomplete()
        self.destination = self.root / "second-output"
        self.docker.omit_image = False
        self.docker.corrupt_layer = True
        with self.assertRaisesRegex(
            package.ProductPackageError, "archive_layer_digest_mismatch"
        ):
            self.export()
        self.assert_incomplete()

    def test_post_save_image_and_copied_asset_rechecks(self):
        identifier = self.images["web"]["image_id"]
        self.docker.after_save = lambda: self.inspections[identifier]["Config"][
            "Labels"
        ].update({"org.opencontainers.image.revision": "f" * 40})
        with self.assertRaisesRegex(
            package.ProductPackageError, "image_source_mismatch"
        ):
            self.export()
        self.assert_incomplete()
        self.destination = self.root / "second-output"
        self.inspections[identifier]["Config"]["Labels"][
            "org.opencontainers.image.revision"
        ] = REVISION
        self.docker.after_save = lambda: (
            self.destination / "metadata" / package.MANIFEST_NAME
        ).write_bytes(b"mutated-after-copy")
        with self.assertRaisesRegex(
            package.ProductPackageError, "copied_asset_changed"
        ):
            self.export()
        self.assert_incomplete()

    def test_missing_registry_repodigest_is_not_replaced_by_image_id(self):
        self.inspections[self.images["ollama"]["image_id"]]["RepoDigests"] = []
        with self.assertRaisesRegex(
            package.ProductPackageError, "registry_digest_mismatch"
        ):
            self.export()
        self.assert_incomplete()

    def test_publication_callback_runs_before_any_completion_marker(self):
        def verify():
            self.assertTrue((self.destination / "images.tar").exists())
            self.assertFalse((self.destination / "SHA256SUMS").exists())
            self.assert_incomplete()

        self.assertEqual("packaged", self.export(before_publish=verify)["status"])

    def test_publication_authority_failure_does_not_publish_success(self):
        def failed_authority():
            raise RuntimeError("private-authority-sentinel")

        with self.assertRaisesRegex(
            package.ProductPackageError, "publication_authority_failed"
        ) as error:
            self.export(before_publish=failed_authority)
        self.assertNotIn("private-authority-sentinel", str(error.exception))
        self.assert_incomplete()
        self.assertFalse((self.destination / "SHA256SUMS").exists())

    def test_docker_output_symlink_hardlink_or_unsafe_mode_is_rejected(self):
        def linked_output():
            archive = self.destination / "images.tar"
            real = self.destination / "linked-output.tar"
            archive.rename(real)
            archive.symlink_to(real)

        def hardlink_output():
            os.link(self.destination / "images.tar", self.destination / "hardlink.tar")

        for index, mutation in enumerate(
            (
                linked_output,
                hardlink_output,
                lambda: (self.destination / "images.tar").chmod(0o666),
            )
        ):
            self.destination = self.root / ("unsafe-output-" + str(index))
            self.docker.after_save = mutation
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(package.ProductPackageError),
            ):
                self.export()
            self.assert_incomplete()

    def test_publication_rechecks_archive_after_authority_callback(self):
        def changed_payload():
            with (self.destination / "images.tar").open("ab") as stream:
                stream.write(b"after-verification-change")

        with self.assertRaisesRegex(package.ProductPackageError, "archive_changed"):
            self.export(before_publish=changed_payload)
        self.assert_incomplete()

    def check_oci_fixture(self, **options):
        raw, images = oci_fixture(**options)
        path = self.root / "oci-fixture.tar"
        path.write_bytes(raw)
        descriptor = os.open(path, os.O_RDONLY)
        try:
            return package._check_archive(descriptor, images)
        finally:
            os.close(descriptor)

    def test_containerd_index_id_and_gzip_layers_differ_from_config_id(self):
        self.assertEqual("docker-save-oci-linux-amd64", self.check_oci_fixture())

    def test_oci_blob_corruption_missing_platform_and_wrong_compatibility(self):
        for options, error in (
            ({"corrupt_blob": True}, "archive_blob_digest_mismatch"),
            ({"architecture": "arm64"}, "archive_platform_incomplete"),
            ({"wrong_compatibility": True}, "archive_compatibility_mismatch"),
        ):
            with (
                self.subTest(options=options),
                self.assertRaisesRegex(package.ProductPackageError, error),
            ):
                self.check_oci_fixture(**options)


if __name__ == "__main__":
    unittest.main()
