"""Catalog recovery boundaries with real metadata, locks and operation receipts.

Domain owners are fake; these tests do not start Docker or prove runtime health.
"""

from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.catalog.bundle import CatalogBundle
from search.operations import lifecycle
from search.operations.catalog import backend, runtime, vectors
from search.operations.config import GPUConfig, ProductConfig
from search.operations.state import OPERATIONS, StateStore, locked_state, operation


REVISION = "a" * 40


class RecoveryBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-recovery-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = ProductConfig(
            2,
            self.root / "product",
            self.root / "api-models",
            self.root / "embedding-models",
            self.root / "reranker-models",
            GPUConfig("GPU-a", ("GPU-b", "GPU-c"), "GPU-d"),
        )
        self.snapshot = self.root / "datasets.jsonl"
        self.snapshot.write_text(
            json.dumps(
                {
                    "id": "00000000-0000-4000-8000-000000000001",
                    "name": "test-dataset",
                    "title": "Public metadata",
                    "private": False,
                    "state": "active",
                    "type": "dataset",
                    "resources": [],
                }
            )
            + "\n"
        )
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(
            patch.object(lifecycle, "revision", return_value=REVISION)
        )
        self.admin_failure = False
        self.vector_status = "vectors-complete"
        self.vector_changed = False
        owners = (
            (backend, "prepare", self.prepare),
            (runtime, "stage_images", self.catalog_images),
            (runtime, "start", self.start),
            (runtime, "admin", self.admin),
            (vectors, "build", self.vectors),
        )
        self.owners = {
            name: self.patches.enter_context(
                patch.object(module, name, side_effect=owner)
            )
            for module, name, owner in owners
        }

    def assert_locked(self):
        with self.assertRaisesRegex(ValueError, "product_operation_busy"):
            with locked_state(self.config):
                self.fail("catalog owner ran without the product lock")

    def prepare(
        self,
        config,
        *,
        repo_root,
        bundle_path,
        manifest_sha256,
        revision,
        runner,
        catalog_revision=None,
    ):
        self.assert_locked()
        bundle = CatalogBundle.load(
            bundle_path, expected_manifest_sha256=manifest_sha256
        )
        work_root = config.state_root / ("catalog-" + bundle.bundle_id[:12])
        work_root.mkdir(mode=0o700, exist_ok=True)
        return {
            "schema_version": 1,
            "revision": catalog_revision or revision,
            "bundle_path": str(bundle_path),
            "manifest_sha256": manifest_sha256,
            "document_count": len(bundle.documents),
            "work_root": str(work_root),
        }

    def catalog_images(self, *args, **kwargs):
        self.assert_locked()
        return {"status": "images_staged", "services_started": False}

    def start(self, *args, **kwargs):
        self.assert_locked()
        return {"status": "running"}

    def admin(self, *args, operation, **kwargs):
        self.assert_locked()
        if self.admin_failure:
            raise ValueError("fake admin failure")
        return {"status": "completed", "operation": operation}

    def vectors(self, config, plan, **kwargs):
        self.assert_locked()
        bundle = CatalogBundle.load(
            Path(plan["bundle_path"]),
            expected_manifest_sha256=plan["manifest_sha256"],
        )
        complete = self.vector_status == "vectors-complete"
        return {
            "schema_version": 1,
            "revision": plan["revision"],
            "bundle_id": bundle.bundle_id,
            "manifest_sha256": plan["manifest_sha256"],
            "document_count": len(bundle.documents),
            "status": self.vector_status,
            "passes_this_invocation": 1,
            "done_count": len(bundle.documents) if complete else 0,
            "pending_count": 0 if complete else len(bundle.documents),
            "runtime_verified": True,
            "host_cleanup_verified": True,
            "active_changed": self.vector_changed,
        }

    def stage(self):
        return lifecycle.stage_catalog(self.config, snapshot=self.snapshot)

    def prepare_candidate(self):
        lifecycle.sync_metadata(self.config, snapshot=self.snapshot)
        lifecycle.prepare_index(self.config)

    def test_failed_import_reconciles_then_stage_skips_write_but_rechecks_vectors(self):
        self.admin_failure = True
        with self.assertRaisesRegex(ValueError, "fake admin failure"):
            self.stage()
        failed = {
            path: path.read_bytes()
            for path in self.config.state_root.glob("op-*.json")
            if json.loads(path.read_text())["status"] == "failed"
        }
        self.assertEqual(1, len(failed))
        self.owners["build"].assert_not_called()
        self.admin_failure = False
        lifecycle.reconcile_index(self.config)
        recovered = self.stage()
        self.assertIs(recovered["import_skipped"], True)
        self.assertEqual("vectors-complete", recovered["status"])
        self.assertEqual(
            ["import", "reconcile"],
            [call.kwargs["operation"] for call in self.owners["admin"].call_args_list],
        )
        self.owners["build"].assert_called_once()
        self.assertEqual(failed, {path: path.read_bytes() for path in failed})
        self.assertEqual("recovery_required", lifecycle.status(self.config)["status"])
        with self.assertRaisesRegex(ValueError, "product_private_recovery_required"):
            lifecycle.up_private(self.config, snapshot=self.snapshot)

    def test_reconciliation_does_not_bypass_vector_result_validation(self):
        self.prepare_candidate()
        lifecycle.reconcile_index(self.config)
        self.vector_changed = True
        with self.assertRaisesRegex(ValueError, "product_stage_vector_outcome_invalid"):
            self.stage()
        self.assertEqual(
            "reconcile", self.owners["admin"].call_args.kwargs["operation"]
        )
        self.owners["admin"].assert_called_once()
        self.owners["build"].assert_called_once()

    def test_only_completed_matching_inventory_receipts_skip_import(self):
        for case in (
            "none",
            "missing_identity",
            "revision",
            "bundle",
            "manifest",
            "failed",
        ):
            with self.subTest(case=case):
                self.config = replace(self.config, state_root=self.root / case)
                self.prepare_candidate()
                with locked_state(self.config) as store:
                    candidate = store.read("candidate.json")
                    if case != "none":
                        value = {
                            key: candidate[key]
                            for key in ("bundle_id", "manifest_sha256")
                        }
                        if case == "missing_identity":
                            value = {}
                        elif case in {"bundle", "manifest"}:
                            key = "bundle_id" if case == "bundle" else "manifest_sha256"
                            value[key] = "f" * 64
                        if case == "failed":
                            self.admin_failure = True
                        else:
                            with operation(
                                store,
                                "reconcile-index",
                                "c" * 40 if case == "revision" else REVISION,
                            ) as outcome:
                                outcome.update(value)
                if case == "failed":
                    with self.assertRaisesRegex(ValueError, "fake admin failure"):
                        lifecycle.reconcile_index(self.config)
                    self.admin_failure = False
                self.owners["admin"].reset_mock()
                report = self.stage()
                self.assertIs(report["import_skipped"], False)
                self.owners["admin"].assert_called_once()
                self.assertEqual(
                    "import", self.owners["admin"].call_args.kwargs["operation"]
                )

    def test_started_intent_blocks_all_operation_names_and_catalog_entrypoints(self):
        self.prepare_candidate()
        with locked_state(self.config) as store:
            store.write(
                "op-" + "1" * 32 + ".json",
                {
                    "schema_version": 1,
                    "operation": "import-index",
                    "source_revision": REVISION,
                    "status": "started",
                    "active_changed": False,
                },
            )
            for name in OPERATIONS:
                with self.subTest(operation=name):
                    with self.assertRaisesRegex(
                        ValueError, "product_operation_recovery_required"
                    ):
                        with operation(store, name, REVISION):
                            self.fail("interrupted import permitted another operation")
        for owner in self.owners.values():
            owner.reset_mock()
        actions = (
            lifecycle.prepare_index,
            lifecycle.build_catalog_images,
            lifecycle.start_index,
            lifecycle.import_index,
            lifecycle.reconcile_index,
            lifecycle.build_vectors,
        )
        for action in actions:
            with self.assertRaisesRegex(
                ValueError, "product_operation_recovery_required"
            ):
                action(self.config)
        with self.assertRaisesRegex(ValueError, "product_operation_recovery_required"):
            lifecycle.sync_metadata(self.config, snapshot=self.snapshot)
        with self.assertRaisesRegex(ValueError, "product_operation_recovery_required"):
            self.stage()
        for owner in self.owners.values():
            owner.assert_not_called()
        before = {
            path: path.read_bytes()
            for path in self.config.state_root.rglob("*")
            if path.is_file()
        }
        with patch.object(
            StateStore, "write", side_effect=AssertionError("status wrote state")
        ):
            report = lifecycle.status(self.config)
        self.assertEqual("recovery_required", report["status"])
        self.assertFalse(report["runtime_verified"])
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_clean_checkpoint_resumes_vectors_without_sync_or_import(self):
        self.vector_status = "checkpoint"
        self.assertEqual("checkpoint", self.stage()["status"])
        for owner in self.owners.values():
            owner.reset_mock()
        self.vector_status = "vectors-complete"
        with patch.object(
            lifecycle.catalog_sync,
            "sync",
            side_effect=AssertionError("resynced metadata"),
        ):
            report = lifecycle.build_vectors(self.config)
        self.assertEqual("vectors-complete", report["status"])
        self.owners["build"].assert_called_once()
        for name in ("prepare", "stage_images", "start", "admin"):
            self.owners[name].assert_not_called()


if __name__ == "__main__":
    unittest.main()
