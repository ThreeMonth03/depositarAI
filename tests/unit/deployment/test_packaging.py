"""Product collection binds real metadata to verified same-source authorities."""

from contextlib import nullcontext
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from search.operations.config import GPUConfig, ProductConfig
from search.operations.deployment import packaging, product_package
from search.operations.shared import private_io


REVISION = "a" * 40
BUNDLE = "b" * 64


def digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


class ProductPackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-product-collection-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir(mode=0o700)
        self.config = ProductConfig(
            1,
            self.root / "state",
            self.root / "api",
            self.root / "embedding",
            self.root / "reranker",
            GPUConfig("GPU-a", ("GPU-b", "GPU-c"), "GPU-d"),
        )
        self.catalog = {
            "project": "depositar-ai-catalog-" + BUNDLE[:12] + "-" + REVISION[:12],
            "revision": REVISION,
            "bundle_path": str(self.root / "bundle"),
            "manifest_sha256": "c" * 64,
            "document_count": 1,
            "compose_sha256": "d" * 64,
            "source_sha256": {
                name: "e" * 64
                for name in packaging.execution.service_sources("opensearch-search")
            },
        }
        self.context = {
            "root": self.root / "catalog",
            "plan": self.catalog,
            "bundle": SimpleNamespace(bundle_id=BUNDLE),
        }
        self.helper = {
            "image_id": "sha256:" + "f" * 64,
            "reference": packaging.backend.HASH_IMAGE_PREFIX + REVISION,
            "revision": REVISION,
            "sources": self.catalog["source_sha256"],
            "base": packaging.backend.APPROVED_OPENSEARCH_BASE,
        }

    def package(self, **options):
        return packaging.package_product(
            self.config,
            object(),
            REVISION,
            self.catalog,
            repo_root=self.repo,
            destination=self.root / "output",
            scanner=self.root / "gitleaks",
            **options,
        )

    def test_unsafe_destination_fails_before_source_images_or_scanner(self):
        for path in (
            self.repo / "output",
            self.config.state_root / "output",
            self.config.api_model_root,
            Path("relative"),
            self.root,
        ):
            with (
                self.subTest(path=path),
                patch.object(packaging, "_source") as source,
                patch.object(packaging.source_artifact, "package") as scanner,
                self.assertRaises(ValueError),
            ):
                packaging.package_product(
                    self.config,
                    object(),
                    REVISION,
                    self.catalog,
                    repo_root=self.repo,
                    destination=path,
                    scanner=self.root / "gitleaks",
                )
            source.assert_not_called()
            scanner.assert_not_called()

    def test_missing_image_authority_does_not_scan_or_export(self):
        with (
            patch.object(packaging, "_source"),
            patch.object(
                packaging.source_bundle, "committed_source_plan", return_value={}
            ),
            patch.object(
                packaging.image_evidence,
                "read_completed_images",
                side_effect=ValueError("incomplete"),
            ),
            patch.object(packaging.source_artifact, "package") as scanner,
            patch.object(product_package, "export_product_package") as export,
            self.assertRaisesRegex(ValueError, "incomplete"),
        ):
            self.package()
        scanner.assert_not_called()
        export.assert_not_called()
        self.assertFalse((self.root / "output").exists())

    def test_source_scan_or_roundtrip_failure_does_not_publish(self):
        for failing in ("package", "restore"):
            with (
                self.subTest(stage=failing),
                patch.object(packaging, "_source"),
                patch.object(
                    packaging.source_bundle, "committed_source_plan", return_value={}
                ),
                patch.object(
                    packaging.image_evidence,
                    "read_completed_images",
                    return_value={"images": {}},
                ),
                patch.object(
                    packaging.execution, "load_context", return_value=self.context
                ),
                patch.object(
                    packaging.execution, "operation_lock", return_value=nullcontext()
                ),
                patch.object(packaging, "_catalog_images", return_value={}),
                patch.object(packaging.source_artifact, "package"),
                patch.object(packaging.source_artifact, "restore"),
                patch.object(
                    packaging.source_artifact,
                    failing,
                    side_effect=ValueError("source_rejected"),
                ),
                patch.object(product_package, "export_product_package") as export,
                self.assertRaisesRegex(ValueError, "source_rejected"),
            ):
                self.package()
            export.assert_not_called()

    def test_collection_roundtrips_source_and_rechecks_authority_before_completion(
        self,
    ):
        self.collect()

    def test_collection_preserves_origin_images_and_current_application_source(self):
        self.collect(catalog_revision="1" * 40)

    def collect(self, *, catalog_revision=REVISION):
        self.catalog["revision"] = catalog_revision
        self.catalog["project"] = (
            "depositar-ai-catalog-" + BUNDLE[:12] + "-" + catalog_revision[:12]
        )
        authority = {"images": {}}
        images = {}
        for index, role in enumerate(product_package.IMAGE_ROLES, 1):
            proof = {
                "revision": catalog_revision
                if role in product_package.CATALOG_ROLES
                else REVISION,
                "source_sha256": "d" * 64,
                "image_id": "sha256:" + format(index, "x") * 64,
                "image_environment_sha256": "e" * 64,
            }
            if role in packaging.source_bundle.RECIPES:
                authority["images"][role] = {
                    **proof,
                    "schema_version": 1,
                    "service": role,
                }
            else:
                images[role] = proof
        images["ollama"] = {
            "image_id": "sha256:" + "9" * 64,
            "reference": packaging.composition.OLLAMA_IMAGE,
            "image_environment_sha256": "e" * 64,
        }
        metadata_root = Path(self.catalog["bundle_path"])
        metadata_root.mkdir(mode=0o700)
        for name in (*packaging.ARTIFACT_NAMES, packaging.MANIFEST_NAME):
            (metadata_root / name).write_bytes(b"public fixture\n")
        self.catalog["manifest_sha256"] = hashlib.sha256(
            b"public fixture\n"
        ).hexdigest()
        models = {
            role: {"weights": {"bytes": 1, "sha256": "f" * 64}}
            for role in product_package.MODEL_ROLES
        }

        def source_package(path, revision, scanner, *, repo_root):
            self.assertEqual(REVISION, revision)
            path.mkdir(mode=0o700)
            names = product_package.source_names(revision)
            for name in names[:-1]:
                (path / name).write_bytes(
                    (revision + "\n").encode()
                    if name == "SOURCE_REVISION"
                    else b"fixture\n"
                )
            sums = "".join(
                hashlib.sha256((path / name).read_bytes()).hexdigest()
                + "  "
                + name
                + "\n"
                for name in names[:-1]
            )
            (path / "SHA256SUMS").write_text(sums)

        def export(plan, destination, **options):
            self.assertEqual(REVISION, plan["revision"])
            self.assertEqual(catalog_revision, plan["catalog"]["revision"])
            self.assertEqual(self.catalog["project"], plan["catalog"]["project"])
            self.assertTrue(plan["models"]["delivery"] == "bundled")
            self.assertFalse(plan["install_complete"])
            self.assertEqual(set(product_package.IMAGE_ROLES), set(plan["images"]))
            for role in product_package.BUILT_ROLES:
                self.assertEqual(
                    catalog_revision
                    if role in product_package.CATALOG_ROLES
                    else REVISION,
                    plan["images"][role]["revision"],
                )
            self.assertNotIn(str(self.root), str(plan))
            options["before_publish"]()
            return {**plan, "status": "packaged"}

        with (
            patch.object(packaging, "_source") as source_check,
            patch.object(
                packaging.source_bundle, "committed_source_plan", return_value={}
            ),
            patch.object(
                packaging.image_evidence,
                "read_completed_images",
                return_value=authority,
            ) as image_check,
            patch.object(
                packaging.execution, "load_context", return_value=self.context
            ),
            patch.object(
                packaging.execution, "operation_lock", return_value=nullcontext()
            ),
            patch.object(
                packaging, "_catalog_images", return_value=images
            ) as catalog_check,
            patch.object(
                packaging.source_artifact, "package", side_effect=source_package
            ),
            patch.object(packaging.source_artifact, "restore") as restore,
            patch.object(
                packaging, "_models", return_value=(models, {"approved": "paths"})
            ),
            patch.object(
                packaging.execution, "read_record", return_value={"verified": "images"}
            ),
            patch.object(product_package, "export_product_package", side_effect=export),
        ):
            self.assertEqual("packaged", self.package()["status"])
        restore.assert_called_once()
        self.assertEqual(3, source_check.call_count)
        self.assertEqual(2, image_check.call_count)
        self.assertEqual(2, catalog_check.call_count)

    def catalog_images(self, mutation=None):
        entries = {}
        for index, role in enumerate(
            ("fess-search", "opensearch-search", "catalog-admin", "ollama-embedding"), 1
        ):
            entries[role] = {
                "image_id": "sha256:" + str(index) * 64,
                "reference": "reviewed-" + role,
                "labels": {"org.opencontainers.image.revision": REVISION},
                "sources": {"reviewed-source": "e" * 64},
            }
        helper = copy.deepcopy(self.helper)
        if mutation:
            mutation(helper)

        def inspected(_context, reference, image_id=None, labels=None):
            return {"Id": image_id or reference, "Config": {"Env": ["PATH=/usr/bin"]}}

        with (
            patch.object(
                packaging.runtime,
                "_require_staged_images",
                return_value={
                    "images": entries,
                    "admin_image": entries["catalog-admin"],
                },
            ),
            patch.object(packaging.execution, "read_record", return_value=helper),
            patch.object(
                packaging.execution, "verify_image", side_effect=inspected
            ) as verify,
            patch.object(packaging.execution, "inspect_image", side_effect=inspected),
        ):
            result = packaging._catalog_images(self.context)
        self.assertEqual(4, verify.call_count)
        return result

    def test_helper_uses_prepare_receipt_and_exact_committed_sources(self):
        result = self.catalog_images()
        self.assertEqual(
            {
                "fess-search",
                "opensearch-search",
                "catalog-admin",
                "opensearch-hash",
                "ollama",
            },
            set(result),
        )
        self.assertEqual(
            digest(self.helper["sources"]), result["opensearch-hash"]["source_sha256"]
        )
        self.assertEqual(REVISION, result["opensearch-hash"]["revision"])
        self.assertEqual(
            packaging.composition.OLLAMA_IMAGE, result["ollama"]["reference"]
        )
        self.assertNotIn(str(self.root), str(result))

    def test_helper_from_another_source_or_base_is_rejected(self):
        mutations = (
            lambda row: row.update(revision="b" * 40),
            lambda row: row.update(reference="unreviewed:latest"),
            lambda row: row.update(base="unreviewed@sha256:" + "0" * 64),
            lambda row: row.update(sources={}),
            lambda row: row.update(secret="must-not-export"),
        )
        for mutation in mutations:
            with (
                self.subTest(mutation=mutation),
                self.assertRaisesRegex(ValueError, "hash_image_receipt_changed"),
            ):
                self.catalog_images(mutation)


if __name__ == "__main__":
    unittest.main()
