"""One locked private startup with real metadata and receipts; no Docker/GPU.

Domain owners stand in for actual image, catalog and model execution. Their
results do not prove a deployed product; these tests exercise orchestration,
source/data boundaries and the existing granular operation journal.
"""

from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import errno
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.catalog.bundle import CatalogBundle
from search.operations import cli, lifecycle
from search.operations.catalog import backend, query, runtime, vectors
from search.operations.config import GPUConfig, ProductConfig
from search.operations.deployment import (
    composition,
    credentials,
    first_activation,
    image_staging,
    inference,
)
from search.operations.deployment.gateway import activation, preparation
from search.operations.deployment.release import controller as release
from search.operations.metadata import sync as metadata_sync
from search.operations.state import StateStore, locked_state, operation_receipts


REVISION = "a" * 40
CHANGED_REVISION = "c" * 40
PRIVATE = "private-error-must-not-enter-operation-receipts"
STAGE_STEPS = (
    "sync",
    "prepare-index",
    "build-catalog-images",
    "start-index",
    "import-index",
    "build-vectors",
)
STARTUP_STEPS = (
    "build-images",
    "prepare-secrets",
    "start-inference",
    "prepare-query",
    "start-release",
    "prepare-gateway",
    "activate-gateway",
)
ALL_STEPS = (*STAGE_STEPS, *STARTUP_STEPS)


def dataset(title="Public metadata"):
    return {
        "id": "00000000-0000-4000-8000-000000000001",
        "name": "test-dataset",
        "title": title,
        "private": False,
        "state": "active",
        "type": "dataset",
        "resources": [],
    }


class PrivateUpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-private-up-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.case_number = 0
        self.new_case()
        self.patches.enter_context(
            patch.object(lifecycle, "revision", side_effect=lambda *_: self.source)
        )
        owners = (
            (first_activation, "require_first_activation", self.barrier),
            (backend, "prepare", self.prepare),
            (runtime, "stage_images", self.catalog_images),
            (runtime, "start", self.start_index),
            (runtime, "admin", self.import_index),
            (vectors, "build", self.build_vectors),
            (image_staging, "stage_product_images", self.build_images),
            (credentials, "prepare_api_key", self.prepare_secrets),
            (inference, "start_private_inference", self.start_inference),
            (query, "provision", self.prepare_query),
            (release, "start_private_release", self.start_release),
            (preparation, "prepare_gateway", self.prepare_gateway),
            (activation, "activate_gateway", self.activate_gateway),
        )
        self.owners = {
            name: self.patches.enter_context(
                patch.object(module, name, side_effect=owner)
            )
            for module, name, owner in owners
        }

    def new_case(self):
        self.case_number += 1
        self.config = ProductConfig(
            2,
            self.root / f"product-{self.case_number}",
            self.root / "api-models",
            self.root / "embedding-models",
            self.root / "reranker-models",
            GPUConfig("GPU-a", ("GPU-b", "GPU-c"), "GPU-d"),
        )
        self.source = REVISION
        self.events = []
        self.fetch_calls = 0
        self.fail_step = self.drift_step = self.data_drift_step = None
        self.terminal_source_drift = False
        self.vector_status = "vectors-complete"
        self.changes = {}
        self.store = None
        self.catalog = None
        self.bound_candidate = None
        self.options_seen = {}

    def assert_locked(self, config=None, store=None):
        if config is not None:
            self.assertIs(config, self.config)
        if store is not None:
            self.assertIs(store, self.store)
        with self.assertRaisesRegex(ValueError, "product_operation_busy"):
            with locked_state(self.config):
                self.fail("product lock was released between steps")

    def step(self, name, *, config=None, store=None):
        self.assert_locked(config, store)
        self.events.append(name)
        started = [
            row["operation"]
            for row in operation_receipts(self.store)
            if row["status"] == "started"
        ]
        self.assertEqual(started, [name], "only the current granular step may be open")
        if self.fail_step == name:
            raise ValueError(PRIVATE)
        if self.drift_step == name:
            self.source = CHANGED_REVISION
        if self.data_drift_step == name:
            self.replace_candidate()

    def barrier(self, store, *, docker_runner=None):
        self.store = store
        self.assert_locked(store=store)
        self.events.append("barrier")
        self.assertFalse(operation_receipts(store))
        self.options_seen["barrier"] = docker_runner

    def fetch(self, start, rows, deadline):
        self.fetch_calls += 1
        if self.fetch_calls == 1:
            self.step("sync")
        else:
            self.assert_locked()
        return {"count": 1, "results": [dataset()][start : start + rows]}

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
        self.step("prepare-index", config=config)
        self.assertEqual(revision, REVISION)
        bundle = CatalogBundle.load(
            bundle_path, expected_manifest_sha256=manifest_sha256
        )
        root = config.state_root / ("catalog-" + bundle.bundle_id[:12])
        root.mkdir(mode=0o700, exist_ok=True)
        self.bound_candidate = self.store.read("candidate.json")
        self.catalog = {
            "schema_version": 1,
            "revision": catalog_revision or revision,
            "project": "depositar-ai-catalog-"
            + bundle.bundle_id[:12]
            + "-"
            + revision[:12],
            "bundle_path": str(bundle_path),
            "bundle_id": bundle.bundle_id,
            "manifest_sha256": manifest_sha256,
            "document_count": len(bundle.documents),
            "work_root": str(root),
        }
        return copy.deepcopy(self.catalog)

    def catalog_images(self, config, plan, *, repo_root, runner):
        self.step("build-catalog-images", config=config)
        self.assertEqual(plan, self.catalog)
        return {"status": "images_staged", "services_started": False}

    def start_index(self, config, plan, *, repo_root, runner):
        self.step("start-index", config=config)
        self.assertEqual(plan, self.catalog)
        return {"status": "running"}

    def import_index(self, config, plan, *, repo_root, operation, runner):
        self.step("import-index", config=config)
        self.assertEqual(operation, "import")
        self.assertEqual(plan, self.catalog)
        return {"status": "completed"}

    def build_vectors(
        self, config, plan, *, repo_root, runner, max_passes, wall_seconds
    ):
        self.step("build-vectors", config=config)
        self.assertEqual(plan, self.catalog)
        self.options_seen["vectors"] = (max_passes, wall_seconds)
        complete = self.vector_status == "vectors-complete"
        return {
            "schema_version": 1,
            "revision": plan["revision"],
            "bundle_id": plan["bundle_id"],
            "manifest_sha256": plan["manifest_sha256"],
            "document_count": plan["document_count"],
            "status": self.vector_status,
            "passes_this_invocation": 1,
            "done_count": plan["document_count"] if complete else 0,
            "pending_count": 0 if complete else plan["document_count"],
            "runtime_verified": True,
            "host_cleanup_verified": True,
            "active_changed": False,
        }

    def report(self, step, **fields):
        return {**fields, **self.changes.get(step, {})}

    def build_images(self, config, store, revision, **options):
        self.step("build-images", config=config, store=store)
        self.assertEqual(revision, REVISION)
        self.options_seen["images"] = options
        return self.report(
            "build-images",
            schema_version=1,
            revision=revision,
            status="images_staged",
            services_started=False,
            registry_published=False,
            active_changed=False,
        )

    def prepare_secrets(self, config, store, revision, **options):
        self.step("prepare-secrets", config=config, store=store)
        self.assertEqual(revision, REVISION)
        self.options_seen["secrets"] = options
        return self.report(
            "prepare-secrets",
            schema_version=1,
            source_revision=revision,
            status="secrets-ready",
            api_key_reused=False,
            secret_material_verified=True,
            services_started=False,
            active_changed=False,
        )

    def start_inference(self, config, store, revision, **options):
        self.step("start-inference", config=config, store=store)
        self.assertEqual(revision, REVISION)
        self.options_seen["inference"] = options
        return self.report(
            "start-inference",
            schema_version=2,
            revision=revision,
            status="runtime_running",
            runtime_verified=True,
            models_verified=True,
            gpu_ownership_snapshot_verified=True,
            model_warmup_verified=False,
            website_started=False,
            active_changed=False,
        )

    def prepare_query(self, config, plan, **options):
        self.step("prepare-query", config=config)
        self.assertEqual(plan, self.catalog)
        self.options_seen["query"] = options
        return self.report(
            "prepare-query",
            **plan,
            status="query-ready",
            active_changed=False,
            permissions_verified=True,
            runtime_verified=True,
            host_cleanup_verified=True,
            website_started=False,
        )

    def release_identity(self, revision):
        return {
            "schema_version": 1,
            "revision": revision,
            "project": "depositar-ai-release-"
            + self.catalog["bundle_id"][:12]
            + "-"
            + revision[:12],
            "bundle_id": self.catalog["bundle_id"],
            "manifest_sha256": self.catalog["manifest_sha256"],
            "active_changed": False,
        }

    def start_release(self, config, store, revision, plan, **options):
        self.step("start-release", config=config, store=store)
        self.assertEqual(revision, REVISION)
        self.assertEqual(plan, self.catalog)
        return self.report(
            "start-release",
            **self.release_identity(revision),
            status="private_release_running",
            runtime_verified=True,
            models_verified=True,
            query_permissions_verified=True,
            gpu_ownership_snapshot_verified=True,
            model_warmup_verified=True,
            readiness_verified=True,
            two_turn_search_verified=True,
            session_reset_verified=True,
            search_quality_verified=False,
            gateway_verified=False,
            ports_published=False,
            runtime_reused=False,
        )

    def prepare_gateway(self, config, store, revision, plan, **options):
        self.step("prepare-gateway", config=config, store=store)
        self.assertEqual(revision, REVISION)
        self.assertEqual(plan, self.catalog)
        identity = self.release_identity(revision)
        identity["release_project"] = identity.pop("project")
        return self.report(
            "prepare-gateway",
            **identity,
            project=composition.GATEWAY_PROJECT,
            status="gateway_prepared",
            activation_required=True,
            confers_runtime_authority=False,
            runtime_verified=False,
            gateway_verified=False,
            ports_published=False,
            active_route_published=False,
            activation_performed=False,
            loopback_verified=False,
            routed_two_turn_search_verified=False,
            routed_session_reset_verified=False,
            preparation_reused=False,
        )

    def activate_gateway(self, config, store, revision, plan, **options):
        self.step("activate-gateway", config=config, store=store)
        self.assertEqual(revision, REVISION)
        self.assertEqual(plan, self.catalog)
        self.options_seen["activation"] = options
        identity = self.release_identity(revision)
        identity["release_project"] = identity.pop("project")
        identity["active_changed"] = True
        report = {
            **identity,
            "project": composition.GATEWAY_PROJECT,
            "status": "gateway_active",
            "proof": {"container_ids": {"web-nginx": "1" * 64}},
            "runtime_verified": True,
            "gateway_verified": True,
            "loopback_verified": True,
            "two_turn_search_verified": True,
            "session_reset_verified": True,
            "ports_published": True,
            "private_release_acceptance_verified": True,
            "search_quality_verified": False,
            "activation_performed": True,
            "confers_runtime_authority": True,
        }
        store.write(
            "active.json",
            {
                **identity,
                "project": composition.GATEWAY_PROJECT,
                "status": "gateway_active",
                "activation_complete_sha256": "f" * 64,
            },
        )
        if self.terminal_source_drift:
            # Simulate a checkout change after native acceptance and publication.
            self.source = CHANGED_REVISION
        return report

    def replace_candidate(self):
        report = metadata_sync.sync(
            self.config.state_root / "cache",
            force=True,
            fetch=lambda start, rows, deadline: {
                "count": 1,
                "results": [dataset("Different sealed metadata")][start : start + rows],
            },
        )
        value = {
            key: report[key]
            for key in (
                "bundle_path",
                "bundle_id",
                "manifest_sha256",
                "document_count",
                "receipt_path",
            )
        }
        self.store.write(
            "candidate.json",
            {
                "schema_version": 1,
                "source_revision": REVISION,
                **value,
            },
            replace=True,
        )

    def up(self, **options):
        fetch = options.pop(
            "fetch", None if options.get("snapshot") is not None else self.fetch
        )
        return lifecycle.up_private(self.config, fetch=fetch, **options)

    def receipts(self):
        with locked_state(self.config) as store:
            return operation_receipts(store)

    def test_all_steps_share_one_lock_config_revision_and_sealed_bundle(self):
        with patch.object(lifecycle, "locked_state", wraps=locked_state) as lock:
            result = self.up(max_passes=2, wall_seconds=4800)
        lock.assert_called_once_with(self.config, create=True)
        self.assertEqual(self.events, ["barrier", *ALL_STEPS])
        self.assertEqual(result["operation"], "up-private")
        self.assertEqual(result["source_revision"], REVISION)
        self.assertEqual(result["catalog_revision"], REVISION)
        self.assertEqual(result["status"], "gateway_active")
        self.assertIs(result["active_changed"], True)
        self.assertIs(result["confers_runtime_authority"], True)
        self.assertEqual(result["bundle_id"], self.bound_candidate["bundle_id"])
        self.assertEqual(
            result["manifest_sha256"], self.bound_candidate["manifest_sha256"]
        )
        self.assertEqual(self.options_seen["vectors"], (2, 4800))
        receipts = self.receipts()
        self.assertEqual(len(receipts), len(ALL_STEPS))
        self.assertEqual({row["operation"] for row in receipts}, set(ALL_STEPS))
        for receipt in receipts:
            self.assertEqual(receipt["status"], "staged")
            self.assertEqual(receipt["source_revision"], REVISION)
            self.assertIs(
                receipt["active_changed"], receipt["operation"] == "activate-gateway"
            )
        self.assertNotIn(PRIVATE, json.dumps(receipts))

    def test_checkpoint_returns_before_any_app_image_gpu_query_or_gateway_step(self):
        self.vector_status = "checkpoint"
        result = self.up()
        self.assertEqual(result["status"], "checkpoint")
        self.assertIs(result["active_changed"], False)
        self.assertEqual(self.events, ["barrier", *STAGE_STEPS])
        self.assertFalse((self.config.state_root / "active.json").exists())
        self.assertEqual(
            {row["operation"] for row in self.receipts()}, set(STAGE_STEPS)
        )

    def test_runners_and_native_http_probe_reach_their_existing_owners(self):
        source_runner, docker_runner, gpu_runner, catalog_runner, http_probe = (
            object() for _ in range(5)
        )
        self.up(
            runner=catalog_runner,
            source_runner=source_runner,
            docker_runner=docker_runner,
            gpu_runner=gpu_runner,
            http_probe=http_probe,
        )
        self.assertIs(self.options_seen["barrier"], docker_runner)
        self.assertIs(self.options_seen["images"]["source_runner"], source_runner)
        self.assertIs(self.options_seen["images"]["docker_runner"], docker_runner)
        self.assertIs(self.options_seen["secrets"]["runner"], source_runner)
        self.assertIs(self.options_seen["inference"]["gpu_runner"], gpu_runner)
        self.assertIs(self.options_seen["query"]["runner"], catalog_runner)
        self.assertIs(self.options_seen["activation"]["catalog_runner"], catalog_runner)
        self.assertIs(self.options_seen["activation"]["http_probe"], http_probe)

    def test_offline_snapshot_uses_real_metadata_without_fetching(self):
        snapshot = self.root / "datasets.jsonl"
        snapshot.write_text(json.dumps(dataset()) + "\n", encoding="utf-8")
        snapshot.chmod(0o600)
        result = self.up(snapshot=snapshot, force=True)
        self.assertEqual(result["status"], "gateway_active")
        self.assertEqual(self.fetch_calls, 0)
        self.assertEqual(self.events, ["barrier", *ALL_STEPS[1:]])
        self.assertEqual(len(self.receipts()), len(ALL_STEPS))

    def test_invalid_profile_set_stops_before_index_models_or_activation(self):
        from search.operations.documents.sets import ProfileSetError

        invalid = self.root / ("profile-set-" + "a" * 64 + ".json")
        invalid.write_bytes(b"{}")
        invalid.chmod(0o600)
        with self.assertRaises(ProfileSetError):
            self.up(profile_set=invalid)
        self.assertEqual(self.events, ["barrier", "sync"])
        self.assertFalse((self.config.state_root / "candidate.json").exists())
        self.assertFalse((self.config.state_root / "active.json").exists())
        self.assertEqual([row["status"] for row in self.receipts()], ["failed"])

    def test_closed_early_barrier_errors_stop_before_metadata_or_other_steps(self):
        for code in first_activation.ERROR_CODES:
            with self.subTest(code=code):
                self.new_case()

                def blocked(store, *, docker_runner=None):
                    self.barrier(store, docker_runner=docker_runner)
                    raise first_activation.FirstActivationError(code)

                self.owners["require_first_activation"].side_effect = blocked
                with self.assertRaises(lifecycle.UpPrivateError) as caught:
                    self.up()
                self.assertEqual(caught.exception.args, (code,))
                self.assertEqual(self.events, ["barrier"])
                self.assertEqual(self.fetch_calls, 0)
                self.assertEqual(self.receipts(), [])
                self.assertFalse((self.config.state_root / "active.json").exists())

    def test_each_domain_failure_stops_without_retry_or_later_receipts(self):
        for index, step in enumerate(ALL_STEPS):
            with self.subTest(step=step):
                self.new_case()
                self.fail_step = step
                with self.assertRaisesRegex(ValueError, PRIVATE):
                    self.up()
                self.assertEqual(self.events, ["barrier", *ALL_STEPS[: index + 1]])
                receipts = self.receipts()
                self.assertEqual(len(receipts), index + 1)
                self.assertEqual(
                    [row["operation"] for row in receipts if row["status"] == "failed"],
                    [step],
                )
                self.assertFalse(any(row["status"] == "started" for row in receipts))
                self.assertFalse((self.config.state_root / "active.json").exists())
                self.assertNotIn(PRIVATE, json.dumps(receipts))

    def test_nonterminal_source_drift_stops_before_the_next_domain(self):
        for index, step in enumerate(ALL_STEPS[:-1]):
            with self.subTest(step=step):
                self.new_case()
                self.drift_step = step
                with self.assertRaisesRegex(ValueError, "invocation_source_changed"):
                    self.up()
                self.assertEqual(self.events, ["barrier", *ALL_STEPS[: index + 1]])
                self.assertEqual(
                    [
                        row["operation"]
                        for row in self.receipts()
                        if row["status"] == "failed"
                    ],
                    [step],
                )
                self.assertFalse((self.config.state_root / "active.json").exists())

    def test_valid_but_different_metadata_cannot_continue_into_startup(self):
        self.data_drift_step = "build-images"
        with self.assertRaisesRegex(ValueError, "catalog_unverified"):
            self.up()
        self.assertEqual(self.events, ["barrier", *STAGE_STEPS, "build-images"])
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_invalid_step_reports_fail_inside_their_granular_operation(self):
        for step in STARTUP_STEPS[:-1]:
            for changes in (
                {"status": "unverified"},
                {"active_changed": True},
                {"active_changed": 0},
            ):
                with self.subTest(step=step, changes=changes):
                    self.new_case()
                    self.changes[step] = changes
                    code = (
                        "product_query_preparation_not_verified"
                        if step == "prepare-query"
                        else "product_private_step_unverified"
                    )
                    with self.assertRaisesRegex(ValueError, "^" + code + "$"):
                        self.up()
                    index = ALL_STEPS.index(step)
                    self.assertEqual(self.events, ["barrier", *ALL_STEPS[: index + 1]])
                    self.assertEqual(
                        [
                            row["operation"]
                            for row in self.receipts()
                            if row["status"] == "failed"
                        ],
                        [step],
                    )
                    self.assertFalse((self.config.state_root / "active.json").exists())

    def test_unverified_catalog_summary_cannot_enter_startup(self):
        stage = lifecycle._stage_catalog_locked

        def unverified(*args, **options):
            return {**stage(*args, **options), "runtime_verified": 1}

        with patch.object(lifecycle, "_stage_catalog_locked", side_effect=unverified):
            with self.assertRaisesRegex(ValueError, "catalog_unverified"):
                self.up()
        self.assertEqual(self.events, ["barrier", *STAGE_STEPS])
        self.assertEqual(len(self.receipts()), len(STAGE_STEPS))

    def test_terminal_source_drift_keeps_the_successful_active_change(self):
        self.terminal_source_drift = True
        result = self.up()
        self.assertEqual(self.source, CHANGED_REVISION)
        self.assertEqual(result["source_revision"], REVISION)
        self.assertIs(result["active_changed"], True)
        self.assertIs(result["activation_performed"], True)
        with locked_state(self.config) as store:
            self.assertEqual(store.read("active.json")["revision"], REVISION)
        activation_receipt = next(
            row for row in self.receipts() if row["operation"] == "activate-gateway"
        )
        self.assertEqual(activation_receipt["status"], "staged")
        self.assertIs(activation_receipt["active_changed"], True)

    def test_final_receipt_failure_preserves_activation_without_repeating_steps(self):
        original = StateStore.write
        for error_number in (errno.ENOSPC, errno.EROFS):
            for committed in (False, True):
                with self.subTest(error_number=error_number, committed=committed):
                    self.new_case()
                    writes = []

                    def write(store, name, value, *, replace=False):
                        if (
                            name.startswith("op-")
                            and value.get("operation") == "activate-gateway"
                        ):
                            writes.append(value["status"])
                            if value["status"] == "staged":
                                if committed:
                                    original(store, name, value, replace=replace)
                                raise OSError(error_number, PRIVATE, str(self.root))
                        return original(store, name, value, replace=replace)

                    output, errors = io.StringIO(), io.StringIO()
                    with (
                        patch.object(StateStore, "write", new=write),
                        patch(
                            "search.operations.config.load_config",
                            return_value=self.config,
                        ),
                        patch.object(metadata_sync.source, "fetch_page", self.fetch),
                        redirect_stdout(output),
                        redirect_stderr(errors),
                    ):
                        code = cli.main(
                            ["--config", str(self.root / "private.json"), "up-private"]
                        )
                    self.assertEqual(code, 1)
                    self.assertEqual(output.getvalue(), "")
                    raw = errors.getvalue()
                    self.assertNotIn(PRIVATE, raw)
                    self.assertNotIn(str(self.root), raw)
                    self.assertNotIn("Traceback", raw)
                    report = json.loads(raw)
                    self.assertEqual(report["status"], "recovery_required")
                    self.assertEqual(report["code"], "product_operation_failed")
                    self.assertEqual(
                        report["error_code"], "product_activation_outcome_unconfirmed"
                    )
                    self.assertIs(report["active_changed"], True)
                    self.assertIs(report["activation_performed"], True)
                    self.assertIs(report["confers_runtime_authority"], False)
                    self.assertIs(report["runtime_verified"], False)
                    self.assertEqual(self.events, ["barrier", *ALL_STEPS])
                    self.assertEqual(writes, ["started", "staged"])
                    with locked_state(self.config) as store:
                        self.assertEqual(
                            store.read("active.json")["revision"], REVISION
                        )
                    receipts = self.receipts()
                    self.assertEqual(len(receipts), len(ALL_STEPS))
                    for row in receipts:
                        terminal = row["operation"] == "activate-gateway"
                        self.assertEqual(
                            row["status"],
                            "started" if terminal and not committed else "staged",
                        )
                        self.assertIs(row["active_changed"], terminal and committed)


if __name__ == "__main__":
    unittest.main()
