"""Real sealed metadata, private receipts and CLI contracts without Docker."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from search.operations import cli
from search.catalog import errors as catalog
from search.operations import lifecycle
from search.operations.config import GPUConfig, ProductConfig
from search.operations.state import locked_state, operation


REVISION = "a" * 40


def dataset():
    return {
        "id": "00000000-0000-4000-8000-000000000001",
        "name": "test-dataset",
        "title": "Public metadata",
        "private": False,
        "state": "active",
        "type": "dataset",
        "resources": [],
    }


class ProductLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-product-lifecycle-")
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
        self.rows, self.fetch_calls = [dataset()], 0
        self.addCleanup(patch.stopall)
        patch.object(lifecycle, "revision", return_value=REVISION).start()

    def fetch(self, start, rows, deadline):
        self.fetch_calls += 1
        return {"count": len(self.rows), "results": self.rows[start : start + rows]}

    def sync(self, **options):
        return lifecycle.sync_metadata(self.config, fetch=self.fetch, **options)

    def test_first_and_repeat_sync_do_not_invent_an_active_release(self):
        self.assertEqual("not_initialized", lifecycle.status(self.config)["status"])
        first, second = self.sync(), self.sync()
        self.assertEqual(4, self.fetch_calls)
        self.assertFalse(first["active_changed"])
        self.assertTrue(second["unchanged"])
        self.assertEqual("previous_staged_metadata", second["comparison_baseline"])
        value = lifecycle.status(self.config)
        self.assertEqual("staged", value["status"])
        self.assertIsNone(value["active_release"])
        self.assertFalse(value["runtime_verified"])
        self.assertEqual(REVISION, value["candidate"]["source_revision"])
        self.assertTrue(all(item["status"] == "staged" for item in value["operations"]))

    def test_download_failure_keeps_previous_candidate(self):
        first = self.sync()
        with self.assertRaises(ValueError):
            lifecycle.sync_metadata(
                self.config, fetch=lambda *args: {"count": 2, "results": []}
            )
        value = lifecycle.status(self.config)
        self.assertEqual(first["bundle_id"], value["candidate"]["bundle_id"])
        self.assertIn("failed", {item["status"] for item in value["operations"]})

    def test_config_identity_change_rejected_but_ttl_change_allowed(self):
        self.sync()
        with self.assertRaisesRegex(ValueError, "configuration_mismatch"):
            lifecycle.status(
                replace(self.config, api_model_root=self.root / "different")
            )
        self.assertEqual(
            "staged",
            lifecycle.status(replace(self.config, cache_ttl_seconds=60))["status"],
        )

    def test_unmarked_nonempty_directory_is_not_adopted(self):
        self.config.state_root.mkdir(mode=0o700)
        (self.config.state_root / "user-data").touch()
        with self.assertRaisesRegex(ValueError, "unmarked_nonempty"):
            self.sync()
        self.assertTrue((self.config.state_root / "user-data").is_file())

    def test_concurrent_operation_is_rejected(self):
        with locked_state(self.config, create=True):
            with self.assertRaisesRegex(ValueError, "busy"):
                with locked_state(self.config):
                    self.fail("second lock was accepted")

    def test_started_receipt_requires_crash_recovery(self):
        with locked_state(self.config, create=True) as store:
            store.write(
                "op-" + "a" * 32 + ".json",
                {
                    "schema_version": 1,
                    "operation": "sync",
                    "source_revision": REVISION,
                    "status": "started",
                    "active_changed": False,
                },
            )
        self.assertEqual("recovery_required", lifecycle.status(self.config)["status"])
        with self.assertRaisesRegex(ValueError, "recovery_required"):
            self.sync()

    def test_linked_private_records_are_rejected(self):
        self.sync()
        candidate = self.config.state_root / "candidate.json"
        os.link(candidate, self.root / "hardlink")
        with self.assertRaisesRegex(ValueError, "file_unsafe"):
            lifecycle.status(self.config)

    def test_fifo_receipt_is_rejected_without_open_blocking(self):
        with locked_state(self.config, create=True) as store:
            os.mkfifo(self.config.state_root / "fifo.json", mode=0o600)
            with self.assertRaisesRegex(ValueError, "file_unsafe"):
                store.read("fifo.json")

    def test_state_write_fsyncs_file_and_directory(self):
        with locked_state(self.config, create=True) as store:
            with patch("search.operations.state.os.fsync", wraps=os.fsync) as fsync:
                store.write("test.json", {"value": 1})
            self.assertGreaterEqual(fsync.call_count, 2)
            self.assertEqual({"value": 1}, store.read("test.json"))
            with self.assertRaises(FileExistsError):
                store.write("test.json", {"value": 2})
            self.assertEqual({"value": 1}, store.read("test.json"))

    def test_non_object_operation_receipt_is_rejected(self):
        with locked_state(self.config, create=True) as store:
            store.write("op-" + "a" * 32 + ".json", [])
        with self.assertRaisesRegex(ValueError, "receipt_invalid"):
            lifecycle.status(self.config)

    def test_changed_directory_identity_is_rejected(self):
        with locked_state(self.config, create=True) as store:
            old = self.config.state_root.with_name("old-state")
            self.config.state_root.rename(old)
            self.config.state_root.mkdir(mode=0o700)
            with self.assertRaisesRegex(ValueError, "directory_changed"):
                store.write("test.json", {})

    def test_dirty_source_does_not_invent_a_sealed_revision(self):
        patch.stopall()
        from subprocess import CompletedProcess

        with patch(
            "search.operations.shared.source_identity.subprocess.run",
            side_effect=[
                CompletedProcess([], 0, REVISION + "\n"),
                CompletedProcess([], 0, " M search/operations/lifecycle.py\n"),
            ],
        ):
            with self.assertRaisesRegex(ValueError, "source_dirty"):
                lifecycle.revision()

    def test_duplicate_json_fields_are_rejected(self):
        self.sync()
        path = self.config.state_root / "candidate.json"
        path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(ValueError, "duplicate_key"):
            lifecycle.status(self.config)

    def test_private_state_json_uses_utf8_and_preserves_fixed_parse_errors(self):
        with locked_state(self.config, create=True) as store:
            value = {"title": "海洋資料"}
            store.write("record.json", value)
            self.assertEqual(value, store.read("record.json"))
            path = self.config.state_root / "record.json"
            for raw, expected in (
                (b'{"value":1,"value":2}', "product_state_duplicate_key"),
                (b'{"value":NaN}', "product_state_nonfinite"),
                (b'{"value":Infinity}', "product_state_nonfinite"),
                (b'{"value":-Infinity}', "product_state_nonfinite"),
            ):
                path.write_bytes(raw)
                with (
                    self.subTest(raw=raw),
                    self.assertRaisesRegex(ValueError, "^" + expected + "$"),
                ):
                    store.read("record.json")
            for raw in (
                b'{"value":"\xff"}',
                json.dumps(value, ensure_ascii=False).encode("utf-16"),
                json.dumps(value, ensure_ascii=False).encode("utf-32"),
                json.dumps(value, ensure_ascii=False).encode("utf-8-sig"),
            ):
                path.write_bytes(raw)
                with self.subTest(raw=raw[:8]), self.assertRaises(ValueError):
                    store.read("record.json")
            path.write_bytes(b'{"value":0}')
            with (
                patch(
                    "search.operations.state.strict_json", side_effect=RecursionError
                ),
                self.assertRaises(RecursionError),
            ):
                store.read("record.json")
            path.write_bytes(b'{"value":"over the byte cap"}')
            with patch("search.operations.state.MAX_BYTES", 8):
                with self.assertRaisesRegex(ValueError, "file_size_invalid"):
                    store.read("record.json")

    def test_unimplemented_operation_names_cannot_start_or_be_accepted_as_receipts(
        self,
    ):
        from search.operations.state import operation_receipts

        with locked_state(self.config, create=True) as store:
            for name in ("install", "update"):
                before = set(os.listdir(store.fd))
                with (
                    self.subTest(operation=name),
                    self.assertRaisesRegex(ValueError, "^product_operation_invalid$"),
                ):
                    with operation(store, name, REVISION):
                        self.fail("unimplemented operation started")
                self.assertEqual(before, set(os.listdir(store.fd)))
                receipt = "op-" + "a" * 32 + ".json"
                store.write(
                    receipt,
                    {
                        "schema_version": 1,
                        "operation": name,
                        "source_revision": REVISION,
                        "status": "staged",
                        "active_changed": False,
                    },
                )
                with (
                    self.subTest(receipt_operation=name),
                    self.assertRaisesRegex(
                        ValueError, "^product_operation_receipt_invalid$"
                    ),
                ):
                    operation_receipts(store)
                os.unlink(receipt, dir_fd=store.fd)

    def test_failed_operation_never_records_exception_message(self):
        with locked_state(self.config, create=True) as store:
            with self.assertRaises(ValueError):
                with operation(store, "sync", REVISION):
                    raise ValueError("secret-value-do-not-log")
        raw = "".join(
            path.read_text() for path in self.config.state_root.glob("op-*.json")
        )
        self.assertNotIn("secret-value", raw)
        self.assertEqual("failed", json.loads(raw)["status"])

    def test_cli_requires_baseline_path_and_manifest_pair(self):
        with (
            patch("search.operations.config.load_config", return_value=self.config),
            patch.object(lifecycle, "sync_metadata") as sync,
            patch("builtins.print"),
        ):
            self.assertEqual(
                1,
                cli.main(
                    [
                        "--config",
                        "/unused/config.json",
                        "sync",
                        "--baseline-bundle",
                        "/bundle",
                    ]
                ),
            )
        sync.assert_not_called()

    def test_cli_does_not_offer_unimplemented_install_or_publish(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            cli.parser().parse_args(["--config", "/config.json", "install"])

    def test_image_builder_runs_inside_marked_product_lock_and_receipt(self):
        from search.operations.deployment import image_staging

        def stage(config, store, source_revision, **options):
            self.assertIs(config, self.config)
            self.assertEqual(REVISION, source_revision)
            self.assertEqual(
                {
                    "repo_root": lifecycle.ROOT,
                    "source_runner": None,
                    "docker_runner": None,
                },
                options,
            )
            with self.assertRaisesRegex(ValueError, "busy"):
                with locked_state(config):
                    self.fail("image build did not retain product lock")
            self.assertEqual(
                "started",
                json.loads(next(store.root.glob("op-*.json")).read_text())["status"],
            )
            return {"status": "images_staged", "active_changed": False}

        with patch.object(image_staging, "stage_product_images", side_effect=stage):
            self.assertEqual(
                "images_staged", lifecycle.build_images(self.config)["status"]
            )
        state = lifecycle.status(self.config)
        self.assertIsNone(state["active_release"])
        self.assertEqual("build-images", state["operations"][0]["operation"])
        self.assertEqual("staged", state["operations"][0]["status"])

    def test_prepare_secrets_creates_and_reuses_real_private_key_without_metadata(self):
        from search.operations.deployment import credentials

        with patch.object(credentials, "require_clean_revision", return_value=REVISION):
            first = lifecycle.prepare_secrets(self.config)
            original = (self.config.state_root / "secrets/api_key").read_bytes()
            second = lifecycle.prepare_secrets(self.config)
        self.assertFalse(first["api_key_reused"])
        self.assertTrue(second["api_key_reused"])
        self.assertEqual(
            original, (self.config.state_root / "secrets/api_key").read_bytes()
        )
        state = lifecycle.status(self.config)
        self.assertIsNone(state["candidate"])
        self.assertIsNone(state["active_release"])
        self.assertEqual(2, len(state["operations"]))
        self.assertTrue(
            all(
                row["operation"] == "prepare-secrets" and row["status"] == "staged"
                for row in state["operations"]
            )
        )

    def test_prepare_secrets_retains_product_lock_and_started_receipt(self):
        from search.operations.deployment import credentials

        def prepare(config, store, revision, **options):
            self.assertIs(config, self.config)
            self.assertEqual(REVISION, revision)
            self.assertEqual({"repo_root": lifecycle.ROOT, "runner": None}, options)
            with self.assertRaisesRegex(ValueError, "busy"):
                with locked_state(config):
                    self.fail("secret creation did not retain product lock")
            self.assertEqual(
                "started",
                json.loads(next(store.root.glob("op-*.json")).read_text())["status"],
            )
            return {"status": "secrets-ready", "active_changed": False}

        with patch.object(credentials, "prepare_api_key", side_effect=prepare):
            self.assertEqual(
                "secrets-ready", lifecycle.prepare_secrets(self.config)["status"]
            )

    def test_prepare_secrets_failure_keeps_private_intent_and_failed_operation(self):
        from search.operations.deployment import credentials

        with (
            patch.object(credentials, "require_clean_revision", return_value=REVISION),
            patch.object(credentials.secrets, "token_hex", return_value="bad"),
            self.assertRaises(credentials.CredentialError),
        ):
            lifecycle.prepare_secrets(self.config)
        value = lifecycle.status(self.config)
        self.assertEqual("failed", value["operations"][0]["status"])
        self.assertIsNone(value["active_release"])
        self.assertEqual(
            {credentials.STARTED}, set(os.listdir(self.config.state_root / "secrets"))
        )

    def test_success_history_prevents_key_regeneration_if_anchor_and_folder_are_lost(
        self,
    ):
        from search.operations.deployment import credentials

        with patch.object(credentials, "require_clean_revision", return_value=REVISION):
            lifecycle.prepare_secrets(self.config)
            saved = self.root / "saved-secrets"
            (self.config.state_root / "secrets").rename(saved)
            (self.config.state_root / credentials.ANCHOR).unlink()
            with (
                patch.object(credentials.secrets, "token_hex") as generate,
                self.assertRaisesRegex(
                    credentials.CredentialError, "recovery_required"
                ),
            ):
                lifecycle.prepare_secrets(self.config)
            generate.assert_not_called()
        self.assertTrue((saved / credentials.KEY).exists())
        self.assertFalse((self.config.state_root / "secrets").exists())
        self.assertEqual(
            {"staged", "failed"},
            {row["status"] for row in lifecycle.status(self.config)["operations"]},
        )

    def test_inference_start_retains_product_lock_and_durable_operation(self):
        from search.operations.deployment import inference

        self.sync()

        def start(config, store, revision, **options):
            self.assertIs(config, self.config)
            self.assertEqual(REVISION, revision)
            self.assertEqual(
                {
                    "repo_root": lifecycle.ROOT,
                    "source_runner": None,
                    "docker_runner": None,
                    "gpu_runner": None,
                },
                options,
            )
            with self.assertRaisesRegex(ValueError, "busy"):
                with locked_state(config):
                    self.fail("inference startup did not retain product lock")
            records = [
                json.loads(path.read_text()) for path in store.root.glob("op-*.json")
            ]
            self.assertEqual(
                ["started"],
                [
                    row["status"]
                    for row in records
                    if row["operation"] == "start-inference"
                ],
            )
            return {
                "status": "runtime_running",
                "active_changed": False,
                "model_warmup_verified": False,
            }

        with patch.object(inference, "start_private_inference", side_effect=start):
            report = lifecycle.start_inference(self.config)
        self.assertEqual("runtime_running", report["status"])
        self.assertFalse(report["model_warmup_verified"])
        state = lifecycle.status(self.config)
        self.assertIsNone(state["active_release"])
        self.assertEqual(
            ["staged"],
            [
                row["status"]
                for row in state["operations"]
                if row["operation"] == "start-inference"
            ],
        )

    def test_failed_inference_start_is_not_success_or_live_activation(self):
        from search.operations.deployment import inference

        self.sync()
        with (
            patch.object(
                inference,
                "start_private_inference",
                side_effect=inference.InferenceError("private_fixed_failure"),
            ),
            self.assertRaises(inference.InferenceError),
        ):
            lifecycle.start_inference(self.config)
        state = lifecycle.status(self.config)
        self.assertIsNone(state["active_release"])
        self.assertEqual(
            ["failed"],
            [
                row["status"]
                for row in state["operations"]
                if row["operation"] == "start-inference"
            ],
        )

    def test_query_preparation_requires_bound_candidate_and_product_lock(self):
        from search.operations.catalog import query

        plan = self.stage_index_plan()

        def prepare(config, actual_plan, **options):
            self.assertIs(config, self.config)
            self.assertEqual(plan, actual_plan)
            self.assertEqual({"repo_root": lifecycle.ROOT, "runner": None}, options)
            with self.assertRaisesRegex(ValueError, "busy"):
                with locked_state(config):
                    self.fail("query preparation did not retain product lock")
            return {"status": "query-ready", "active_changed": False}

        with patch.object(query, "provision", side_effect=prepare):
            self.assertEqual(
                "query-ready", lifecycle.prepare_query(self.config)["status"]
            )
        value = lifecycle.status(self.config)
        self.assertIsNone(value["active_release"])
        self.assertFalse(value["runtime_verified"])
        self.assertEqual(
            "query-ready",
            json.loads((self.config.state_root / "query-candidate.json").read_text())[
                "status"
            ],
        )
        self.assertIn(
            "prepare-query", {item["operation"] for item in value["operations"]}
        )

    def test_release_start_retains_lock_binds_candidate_and_passes_runners(self):
        from search.operations.deployment.release import controller as release

        plan = self.stage_index_plan()
        runners = {
            name: object()
            for name in (
                "source_runner",
                "docker_runner",
                "catalog_runner",
                "gpu_runner",
            )
        }

        def start(config, store, revision, actual_plan, **options):
            self.assertIs(config, self.config)
            self.assertEqual(REVISION, revision)
            self.assertEqual(plan, actual_plan)
            self.assertEqual({"repo_root": lifecycle.ROOT, **runners}, options)
            with self.assertRaisesRegex(ValueError, "busy"):
                with locked_state(config):
                    self.fail("release startup did not retain product lock")
            receipts = [
                json.loads(path.read_text()) for path in store.root.glob("op-*.json")
            ]
            self.assertEqual(
                ["started"],
                [
                    row["status"]
                    for row in receipts
                    if row["operation"] == "start-release"
                ],
            )
            return {
                "status": "private_release_running",
                "active_changed": False,
                "gateway_started": False,
            }

        with patch.object(release, "start_private_release", side_effect=start):
            report = lifecycle.start_release(self.config, **runners)
        self.assertEqual("private_release_running", report["status"])
        self.assertFalse(report["gateway_started"])
        state = lifecycle.status(self.config)
        self.assertIsNone(state["active_release"])
        self.assertFalse(state["runtime_verified"])
        receipt = next(
            row for row in state["operations"] if row["operation"] == "start-release"
        )
        self.assertEqual("staged", receipt["status"])
        self.assertEqual(state["candidate"]["bundle_id"], receipt["bundle_id"])
        self.assertEqual(
            state["candidate"]["manifest_sha256"], receipt["manifest_sha256"]
        )

    def test_release_rejects_old_candidate_or_plan_before_start(self):
        from search.operations.deployment.release import controller as release

        for filename, key in (
            ("candidate.json", "source_revision"),
            ("index-candidate.json", "revision"),
        ):
            with self.subTest(filename=filename):
                self.sync()
                candidate = lifecycle.status(self.config)["candidate"]
                plan = {
                    "schema_version": 1,
                    "revision": REVISION,
                    **{
                        key: candidate[key]
                        for key in ("bundle_path", "manifest_sha256", "document_count")
                    },
                }
                with locked_state(self.config) as store:
                    store.write("index-candidate.json", plan, replace=True)
                    value = store.read(filename)
                    value[key] = "b" * 40
                    store.write(filename, value, replace=True)
                with patch.object(release, "start_private_release") as start:
                    with self.assertRaises(ValueError):
                        lifecycle.start_release(self.config)
                start.assert_not_called()
                self.assertIsNone(lifecycle.status(self.config)["active_release"])

    def test_failed_release_start_leaves_failed_operation_and_no_activation(self):
        from search.operations.deployment.release import controller as release

        self.stage_index_plan()
        with (
            patch.object(
                release,
                "start_private_release",
                side_effect=ValueError("private_failure"),
            ),
            self.assertRaises(ValueError),
        ):
            lifecycle.start_release(self.config)
        state = lifecycle.status(self.config)
        self.assertIsNone(state["active_release"])
        self.assertEqual(
            ["failed"],
            [
                row["status"]
                for row in state["operations"]
                if row["operation"] == "start-release"
            ],
        )

    def test_release_source_change_after_start_cannot_publish_success(self):
        from search.operations.deployment.release import controller as release

        self.stage_index_plan()
        with (
            patch.object(
                release,
                "start_private_release",
                return_value={"status": "runtime_running"},
            ),
            patch.object(
                lifecycle, "revision", side_effect=[REVISION, REVISION, "b" * 40]
            ),
            self.assertRaisesRegex(ValueError, "invocation_source_changed"),
        ):
            lifecycle.start_release(self.config)
        self.assertEqual(
            ["failed"],
            [
                row["status"]
                for row in lifecycle.status(self.config)["operations"]
                if row["operation"] == "start-release"
            ],
        )

    def test_gateway_prepare_retains_lock_and_only_stages_candidate_receipt(self):
        from search.operations.deployment.gateway import preparation as gateway

        plan = self.stage_index_plan()

        def prepare(config, store, revision, actual_plan, **options):
            self.assertEqual(
                (self.config, REVISION, plan), (config, revision, actual_plan)
            )
            with self.assertRaisesRegex(ValueError, "busy"):
                with locked_state(config):
                    self.fail("gateway preparation did not retain product lock")
            rows = [
                json.loads(path.read_bytes()) for path in store.root.glob("op-*.json")
            ]
            self.assertEqual(
                ["started"],
                [
                    row["status"]
                    for row in rows
                    if row["operation"] == "prepare-gateway"
                ],
            )
            return {
                "status": "gateway_prepared",
                "activation_performed": False,
                "active_changed": False,
            }

        with patch.object(gateway, "prepare_gateway", side_effect=prepare):
            result = lifecycle.prepare_gateway(self.config)
        self.assertFalse(result["activation_performed"])
        receipts = lifecycle.status(self.config)["operations"]
        receipt = next(row for row in receipts if row["operation"] == "prepare-gateway")
        self.assertEqual("staged", receipt["status"])
        self.assertEqual(plan["manifest_sha256"], receipt["manifest_sha256"])
        self.assertFalse(receipt["active_changed"])

    def test_gateway_wrong_candidate_source_is_rejected_before_prepare(self):
        from search.operations.deployment.gateway import preparation as gateway

        self.stage_index_plan()
        with locked_state(self.config) as store:
            candidate = store.read("candidate.json")
            candidate["source_revision"] = "b" * 40
            store.write("candidate.json", candidate, replace=True)
        with patch.object(gateway, "prepare_gateway") as prepare:
            with self.assertRaisesRegex(ValueError, "gateway_candidate_source_changed"):
                lifecycle.prepare_gateway(self.config)
        prepare.assert_not_called()
        receipt = next(
            row
            for row in lifecycle.status(self.config)["operations"]
            if row["operation"] == "prepare-gateway"
        )
        self.assertEqual("failed", receipt["status"])

    def test_gateway_activation_retains_product_lock_and_records_changed_pointer(self):
        from search.operations.deployment.gateway import (
            activation as gateway_activation,
        )

        plan = self.stage_index_plan()

        def activate(config, store, revision, actual_plan, **options):
            self.assertEqual(
                (self.config, REVISION, plan), (config, revision, actual_plan)
            )
            with self.assertRaisesRegex(ValueError, "busy"):
                with locked_state(config):
                    self.fail("activation did not retain product lock")
            store.write("active.json", {"revision": revision})
            return {
                "status": "gateway_active",
                "active_changed": True,
                "activation_performed": True,
                "confers_runtime_authority": True,
            }

        with patch.object(gateway_activation, "activate_gateway", side_effect=activate):
            result = lifecycle.activate_gateway(self.config)
        self.assertTrue(result["active_changed"])
        receipt = next(
            row
            for row in lifecycle.status(self.config)["operations"]
            if row["operation"] == "activate-gateway"
        )
        self.assertEqual("staged", receipt["status"])
        self.assertTrue(receipt["active_changed"])

    def test_gateway_activation_failure_preserves_false_changed_receipt(self):
        from search.operations.deployment.gateway import (
            activation as gateway_activation,
        )

        self.stage_index_plan()
        with patch.object(
            gateway_activation, "activate_gateway", side_effect=ValueError("blocked")
        ):
            with self.assertRaises(ValueError):
                lifecycle.activate_gateway(self.config)
        receipt = next(
            row
            for row in lifecycle.status(self.config)["operations"]
            if row["operation"] == "activate-gateway"
        )
        self.assertEqual("failed", receipt["status"])
        self.assertFalse(receipt["active_changed"])
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_gateway_activation_result_requires_exact_true_before_success_receipt(self):
        from search.operations.deployment.gateway import (
            activation as gateway_activation,
        )

        self.stage_index_plan()
        for changed in (False, 1):
            with (
                self.subTest(changed=changed),
                patch.object(
                    gateway_activation,
                    "activate_gateway",
                    return_value={
                        "status": "gateway_active",
                        "active_changed": changed,
                        "activation_performed": True,
                        "confers_runtime_authority": True,
                    },
                ),
            ):
                with self.assertRaisesRegex(ValueError, "activation_result_invalid"):
                    lifecycle.activate_gateway(self.config)
        receipts = [
            row
            for row in lifecycle.status(self.config)["operations"]
            if row["operation"] == "activate-gateway"
        ]
        self.assertTrue(
            all(
                row["status"] == "failed" and row["active_changed"] is False
                for row in receipts
            )
        )

    def test_non_activation_receipt_cannot_claim_changed_pointer(self):
        self.stage_index_plan()
        with locked_state(self.config) as store:
            with operation(store, "prepare-gateway", REVISION) as outcome:
                outcome["active_changed"] = True
        with self.assertRaisesRegex(ValueError, "operation_receipt_invalid"):
            lifecycle.status(self.config)

    def test_query_failure_never_publishes_candidate_or_success_receipt(self):
        from search.operations.catalog import query

        self.stage_index_plan()
        with (
            patch.object(
                query, "provision", side_effect=ValueError("query_not_verified")
            ),
            self.assertRaises(ValueError),
        ):
            lifecycle.prepare_query(self.config)
        self.assertFalse((self.config.state_root / "query-candidate.json").exists())
        self.assertIn(
            "failed",
            {item["status"] for item in lifecycle.status(self.config)["operations"]},
        )

    def test_new_cli_commands_dispatch_without_exposing_public_install(self):
        for command, action in (
            ("prepare-secrets", "prepare_secrets"),
            ("start-inference", "start_inference"),
            ("start-release", "start_release"),
            ("prepare-gateway", "prepare_gateway"),
            ("activate-gateway", "activate_gateway"),
            ("build-images", "build_images"),
            ("build-catalog-images", "build_catalog_images"),
            ("prepare-query", "prepare_query"),
        ):
            with (
                self.subTest(command=command),
                patch("search.operations.config.load_config", return_value=self.config),
                patch.object(
                    lifecycle, action, return_value={"status": "staged"}
                ) as execute,
                patch("builtins.print"),
            ):
                self.assertEqual(0, cli.main(["--config", "/config.json", command]))
                execute.assert_called_once_with(self.config)

    def test_prebuilt_start_cli_reads_only_a_private_receipt(self):
        receipt = self.root / "images.json"
        receipt.write_text('{"schema_version":1}\n')
        receipt.chmod(0o600)
        with (
            patch("search.operations.config.load_config", return_value=self.config),
            patch.object(
                lifecycle, "start_index", return_value={"status": "running"}
            ) as start,
            patch("builtins.print"),
        ):
            self.assertEqual(
                0,
                cli.main(
                    [
                        "--config",
                        "/config.json",
                        "start-index",
                        "--prebuilt-images",
                        str(receipt),
                    ]
                ),
            )
        start.assert_called_once_with(
            self.config, prebuilt_images={"schema_version": 1}
        )
        for unsafe in ("images.json", str(self.root / "link.json")):
            with self.subTest(path=unsafe):
                if unsafe.endswith("link.json"):
                    (self.root / "link.json").symlink_to(receipt)
                with (
                    patch(
                        "search.operations.config.load_config", return_value=self.config
                    ),
                    patch.object(lifecycle, "start_index") as rejected,
                    patch("builtins.print"),
                ):
                    self.assertEqual(
                        1,
                        cli.main(
                            [
                                "--config",
                                "/config.json",
                                "start-index",
                                "--prebuilt-images",
                                unsafe,
                            ]
                        ),
                    )
                rejected.assert_not_called()

    def test_package_cli_bundles_models_by_default_and_allows_explicit_external(self):
        for flags, bundled in (([], True), (["--external-models"], False)):
            with (
                self.subTest(flags=flags),
                patch("search.operations.config.load_config", return_value=self.config),
                patch.object(
                    lifecycle, "package_product", return_value={"status": "packaged"}
                ) as package,
                patch("builtins.print"),
            ):
                self.assertEqual(
                    0,
                    cli.main(
                        [
                            "--config",
                            "/config.json",
                            "package-product",
                            "--destination",
                            "/private/product-v1",
                            "--scanner",
                            "/tools/gitleaks",
                            *flags,
                        ]
                    ),
                )
                package.assert_called_once_with(
                    self.config,
                    destination=Path("/private/product-v1"),
                    scanner=Path("/tools/gitleaks"),
                    include_models=bundled,
                )

    def test_prebuilt_cli_rejects_deep_json_without_traceback_or_start(self):
        receipt = self.root / "deep.json"
        receipt.write_text("[" * 2000 + "]" * 2000)
        receipt.chmod(0o600)
        with (
            patch("search.operations.config.load_config", return_value=self.config),
            patch.object(lifecycle, "start_index") as start,
            patch("builtins.print") as output,
        ):
            self.assertEqual(
                1,
                cli.main(
                    [
                        "--config",
                        "/config.json",
                        "start-index",
                        "--prebuilt-images",
                        str(receipt),
                    ]
                ),
            )
        start.assert_not_called()
        self.assertEqual(
            "product_operation_failed", json.loads(output.call_args.args[0])["code"]
        )

    def test_prebuilt_cli_rejects_nested_objects_and_nonobject_receipts(self):
        receipt = self.root / "invalid-receipt.json"
        for raw in (
            '{"nested":' * 32 + "{}" + "}" * 32,
            "[]",
            "true",
            '"private-response-not-for-output"',
        ):
            with self.subTest(raw=raw):
                receipt.write_text(raw)
                receipt.chmod(0o600)
                with (
                    patch(
                        "search.operations.config.load_config", return_value=self.config
                    ),
                    patch.object(lifecycle, "start_index") as start,
                    patch("builtins.print") as output,
                ):
                    self.assertEqual(
                        1,
                        cli.main(
                            [
                                "--config",
                                "/config.json",
                                "start-index",
                                "--prebuilt-images",
                                str(receipt),
                            ]
                        ),
                    )
                start.assert_not_called()
                printed = output.call_args.args[0]
                self.assertNotIn("private-response-not-for-output", printed)
                self.assertEqual(
                    "product_operation_failed", json.loads(printed)["code"]
                )

    def test_cli_sanitizes_query_protocol_failures_without_traceback_or_body(self):
        from search.catalog.errors import ProtocolError

        with (
            patch("search.operations.config.load_config", return_value=self.config),
            patch.object(
                lifecycle,
                "prepare_query",
                side_effect=ProtocolError("private-response-not-for-output"),
            ),
            patch("builtins.print") as output,
        ):
            self.assertEqual(1, cli.main(["--config", "/config.json", "prepare-query"]))
        printed = output.call_args.args[0]
        self.assertNotIn("private-response-not-for-output", printed)
        self.assertEqual("product_operation_failed", json.loads(printed)["code"])

    def test_cli_reports_fixed_operator_error_codes_without_rendering_exceptions(self):
        from search.operations.catalog.access import AccessError
        from search.operations.catalog.policy import RuntimeError as CatalogRuntimeError
        from search.operations.catalog.projection import ProjectionError

        for error, code in (
            (
                AccessError("catalog_access_permissions_changed"),
                "catalog_access_permissions_changed",
            ),
            (
                ProjectionError("catalog_projection_mapping_changed"),
                "catalog_projection_mapping_changed",
            ),
            *(
                (CatalogRuntimeError(code), code)
                for code in (
                    "catalog_admin_inventory_conflict",
                    "catalog_admin_deadline_exceeded",
                    "catalog_admin_authentication_failed",
                    "catalog_admin_transport_failed",
                )
            ),
        ):
            with self.subTest(error_type=type(error).__name__):
                with (
                    patch(
                        "search.operations.config.load_config", return_value=self.config
                    ),
                    patch.object(lifecycle, "prepare_query", side_effect=error),
                    patch.object(
                        type(error),
                        "__str__",
                        side_effect=AssertionError(
                            "exception text must not be rendered"
                        ),
                    ) as render,
                    patch("builtins.print") as output,
                ):
                    self.assertEqual(
                        1,
                        cli.main(["--config", "/config.json", "prepare-query"]),
                    )
                render.assert_not_called()
                self.assertEqual(
                    {
                        "schema_version": 1,
                        "status": "failed",
                        "code": "product_operation_failed",
                        "error_type": type(error).__name__,
                        "active_changed": False,
                        "error_code": code,
                    },
                    json.loads(output.call_args.args[0]),
                )
                self.assertIs(cli.sys.stderr, output.call_args.kwargs["file"])

    def test_cli_emits_only_revalidated_closed_catalog_failure_record(self):
        from search.operations.catalog.policy import RuntimeError as CatalogRuntimeError

        primary = catalog._diagnosed(
            catalog.ImportFailed("private-response-not-for-output"),
            stage="bulk_dispatched",
            cause="http_503",
        )
        cleanup = catalog.TokenCleanupError(
            "private-response-not-for-output", possibly_polluted=True
        )
        cleanup.primary_diagnostic = catalog._failure_detail(primary)
        cleanup.cleanup_diagnostic = catalog._failure_detail(
            catalog.TransportError("private-response-not-for-output"),
            stage="token_cleanup",
        )
        catalog._diagnosed(cleanup, stage="token_cleanup", cause="transport")
        record = catalog.failure_record(cleanup, possibly_polluted=True)
        error = CatalogRuntimeError("catalog_admin_diagnostic_failed")
        error.catalog_diagnostic = record
        with (
            patch("search.operations.config.load_config", return_value=self.config),
            patch.object(lifecycle, "prepare_query", side_effect=error),
            patch.object(
                CatalogRuntimeError,
                "__str__",
                side_effect=AssertionError("must not render exception"),
            ),
            patch("builtins.print") as output,
        ):
            self.assertEqual(1, cli.main(["--config", "/config.json", "prepare-query"]))
        printed = output.call_args.args[0]
        report = json.loads(printed)
        self.assertEqual("catalog_admin_diagnostic_failed", report["error_code"])
        self.assertEqual(record, report["catalog_diagnostic"])
        self.assertNotIn("private-response-not-for-output", printed)

    def test_cli_rejects_forged_catalog_failure_attributes_and_downgrade(self):
        from search.operations.catalog.policy import RuntimeError as CatalogRuntimeError

        class RuntimeSubclass(CatalogRuntimeError):
            pass

        class PrivateDiagnostic:
            def __str__(self):
                raise AssertionError("private value must not be rendered")

        record = catalog.failure_record(
            catalog.TransportError("private-response-not-for-output"),
            possibly_polluted=False,
        )
        bad_detail = json.loads(json.dumps(record))
        bad_detail["diagnostic"]["cause"] = PrivateDiagnostic()
        primary = catalog._diagnosed(
            catalog.ImportFailed("private-response-not-for-output"),
            stage="bulk_dispatched",
            cause="http_503",
        )
        cleanup = catalog.TokenCleanupError(
            "private-response-not-for-output", possibly_polluted=True
        )
        cleanup.primary_diagnostic = catalog._failure_detail(primary)
        cleanup.cleanup_diagnostic = catalog._failure_detail(
            catalog.TransportError("private-value"), stage="token_cleanup"
        )
        catalog._diagnosed(cleanup, stage="token_cleanup", cause="transport")
        cleanup_record = catalog.failure_record(cleanup, possibly_polluted=True)
        downgrade = json.loads(json.dumps(cleanup_record))
        downgrade["possibly_polluted"] = False
        contradictory = json.loads(json.dumps(cleanup_record))
        contradictory["diagnostic"]["cause"] = "http_503"
        spoofed = type("RuntimeError", (ValueError,), {})
        for kind, detail in (
            (CatalogRuntimeError, None),
            (CatalogRuntimeError, PrivateDiagnostic()),
            (CatalogRuntimeError, bad_detail),
            (CatalogRuntimeError, downgrade),
            (CatalogRuntimeError, contradictory),
            (RuntimeSubclass, record),
            (spoofed, record),
        ):
            with self.subTest(kind=kind.__name__, detail_type=type(detail).__name__):
                error = kind("catalog_admin_diagnostic_failed")
                error.catalog_diagnostic = detail
                with (
                    patch(
                        "search.operations.config.load_config", return_value=self.config
                    ),
                    patch.object(lifecycle, "prepare_query", side_effect=error),
                    patch("builtins.print") as output,
                ):
                    self.assertEqual(
                        1,
                        cli.main(["--config", "/config.json", "prepare-query"]),
                    )
                report = json.loads(output.call_args.args[0])
                self.assertNotIn("error_code", report)
                self.assertNotIn("catalog_diagnostic", report)
                self.assertEqual("product_operation_failed", report["code"])

    def test_cli_rejects_spoofed_operator_diagnostics_without_rendering_values(self):
        from search.operations.catalog.access import AccessError
        from search.operations.catalog.policy import RuntimeError as CatalogRuntimeError
        from search.operations.catalog.projection import ProjectionError

        class AccessSubclass(AccessError):
            pass

        class ProjectionSubclass(ProjectionError):
            pass

        class CatalogRuntimeSubclass(CatalogRuntimeError):
            pass

        class PrivateDiagnostic:
            def __str__(self):
                raise AssertionError("private diagnostic must not be rendered")

        class DiagnosticString(str):
            def __hash__(self):
                raise AssertionError("nonliteral string must not be inspected")

            def __str__(self):
                raise AssertionError("nonliteral string must not be rendered")

        spoofed_access_error = type("AccessError", (ValueError,), {})
        spoofed_runtime_error = type("RuntimeError", (ValueError,), {})
        errors = (
            ValueError("catalog_access_permissions_changed"),
            spoofed_access_error("catalog_access_permissions_changed"),
            AccessSubclass("catalog_access_permissions_changed"),
            ProjectionSubclass("catalog_projection_mapping_changed"),
            AccessError("catalog_access_private-response-not-for-output"),
            ProjectionError("catalog_projection_private-response-not-for-output"),
            AccessError(
                "catalog_access_permissions_changed", "private-response-not-for-output"
            ),
            ProjectionError(),
            AccessError(PrivateDiagnostic()),
            ProjectionError(DiagnosticString("catalog_projection_mapping_changed")),
            ValueError("catalog_admin_inventory_conflict"),
            spoofed_runtime_error("catalog_admin_inventory_conflict"),
            CatalogRuntimeSubclass("catalog_admin_inventory_conflict"),
            CatalogRuntimeError("catalog_admin_private-response-not-for-output"),
            CatalogRuntimeError("catalog_admin_inventory_conflict", "private-value"),
            CatalogRuntimeError(DiagnosticString("catalog_admin_inventory_conflict")),
            CatalogRuntimeError(PrivateDiagnostic()),
            CatalogRuntimeError(),
        )
        for position, error in enumerate(errors):
            with self.subTest(position=position):
                with (
                    patch(
                        "search.operations.config.load_config", return_value=self.config
                    ),
                    patch.object(lifecycle, "prepare_query", side_effect=error),
                    patch("builtins.print") as output,
                ):
                    self.assertEqual(
                        1,
                        cli.main(["--config", "/config.json", "prepare-query"]),
                    )
                printed = output.call_args.args[0]
                self.assertNotIn("private-response-not-for-output", printed)
                self.assertEqual(
                    {
                        "schema_version": 1,
                        "status": "failed",
                        "code": "product_operation_failed",
                        "error_type": type(error).__name__,
                        "active_changed": False,
                    },
                    json.loads(printed),
                )

    def test_cli_sanitizes_release_failure_without_private_diagnostic(self):
        with (
            patch("search.operations.config.load_config", return_value=self.config),
            patch.object(
                lifecycle,
                "start_release",
                side_effect=ValueError("private-response-not-for-output"),
            ),
            patch("builtins.print") as output,
        ):
            self.assertEqual(1, cli.main(["--config", "/config.json", "start-release"]))
        printed = output.call_args.args[0]
        self.assertNotIn("private-response-not-for-output", printed)
        self.assertEqual("product_operation_failed", json.loads(printed)["code"])

    def test_cli_dispatches_vector_budget_and_checkpoint_has_nonzero_exit(self):
        for result_status, expected_exit in (
            ("checkpoint", 2),
            ("vectors-complete", 0),
        ):
            with self.subTest(status=result_status):
                with (
                    patch(
                        "search.operations.config.load_config", return_value=self.config
                    ),
                    patch.object(
                        lifecycle,
                        "build_vectors",
                        return_value={"status": result_status},
                    ) as build,
                    patch("builtins.print"),
                ):
                    self.assertEqual(
                        expected_exit,
                        cli.main(
                            [
                                "--config",
                                "/config.json",
                                "build-vectors",
                                "--max-passes",
                                "2",
                                "--wall-seconds",
                                "4800",
                            ]
                        ),
                    )
                build.assert_called_once_with(
                    self.config, max_passes=2, wall_seconds=4800
                )

    def stage_index_plan(self):
        candidate = self.sync()
        plan = {
            "schema_version": 1,
            "revision": REVISION,
            "bundle_path": candidate["bundle_path"],
            "manifest_sha256": candidate["manifest_sha256"],
            "document_count": candidate["document_count"],
        }
        with locked_state(self.config) as store:
            store.write("index-candidate.json", plan)
        return plan

    def test_index_commands_share_candidate_binding_and_keep_active_unchanged(self):
        from search.operations.catalog import runtime as catalog_runtime

        plan = self.stage_index_plan()
        with (
            patch.object(
                catalog_runtime, "start", return_value={"status": "running"}
            ) as start,
            patch.object(
                catalog_runtime, "admin", return_value={"status": "completed"}
            ) as admin,
        ):
            lifecycle.start_index(self.config)
            lifecycle.import_index(self.config)
            lifecycle.reconcile_index(self.config)
        start.assert_called_once_with(
            self.config, plan, repo_root=lifecycle.ROOT, runner=None
        )
        self.assertEqual(
            ["import", "reconcile"],
            [call.kwargs["operation"] for call in admin.call_args_list],
        )
        value = lifecycle.status(self.config)
        self.assertIsNone(value["active_release"])
        self.assertFalse(value["runtime_verified"])
        self.assertTrue(
            all(item["active_changed"] is False for item in value["operations"])
        )
        self.assertEqual(
            {"sync", "start-index", "import-index", "reconcile-index"},
            {item["operation"] for item in value["operations"]},
        )

    def test_catalog_image_build_and_prebuilt_start_keep_candidate_and_product_lock(
        self,
    ):
        from search.operations.catalog import runtime as catalog_runtime

        plan = self.stage_index_plan()
        receipt = {"schema_version": 1, "project": "sealed-project"}

        def build(config, actual_plan, **options):
            self.assertEqual(self.config, config)
            self.assertEqual(plan, actual_plan)
            with self.assertRaisesRegex(ValueError, "busy"):
                with locked_state(config):
                    self.fail("catalog image build released product lock")
            return {"status": "images_staged", "services_started": False}

        with (
            patch.object(catalog_runtime, "stage_images", side_effect=build),
            patch.object(
                catalog_runtime, "start", return_value={"status": "running"}
            ) as start,
        ):
            lifecycle.build_catalog_images(self.config)
            lifecycle.start_index(self.config, prebuilt_images=receipt)
        start.assert_called_once_with(
            self.config,
            plan,
            repo_root=lifecycle.ROOT,
            runner=None,
            prebuilt_images=receipt,
        )
        self.assertIsNone(lifecycle.status(self.config)["active_release"])

    def test_product_package_holds_lock_and_records_failure_without_activation(self):
        from search.operations.deployment import packaging

        plan = self.stage_index_plan()

        def export(config, store, revision, actual_plan, **options):
            self.assertEqual(plan, actual_plan)
            self.assertEqual(REVISION, revision)
            with self.assertRaisesRegex(ValueError, "busy"):
                with locked_state(config):
                    self.fail("product export released product lock")
            raise ValueError("payload_verification_failed")

        with (
            patch.object(packaging, "package_product", side_effect=export),
            self.assertRaisesRegex(ValueError, "payload_verification_failed"),
        ):
            lifecycle.package_product(
                self.config,
                destination=self.root / "output",
                scanner=self.root / "gitleaks",
            )
        status = lifecycle.status(self.config)
        self.assertIsNone(status["active_release"])
        self.assertTrue(
            any(
                row["operation"] == "package-product" and row["status"] == "failed"
                for row in status["operations"]
            )
        )

    def test_missing_plan_is_not_automatically_prepared_or_started(self):
        from search.operations.catalog import backend as catalog_backend
        from search.operations.catalog import runtime as catalog_runtime

        self.sync()
        with (
            patch.object(catalog_backend, "prepare") as prepare,
            patch.object(catalog_runtime, "start") as start,
            self.assertRaises(FileNotFoundError),
        ):
            lifecycle.start_index(self.config)
        prepare.assert_not_called()
        start.assert_not_called()
        self.assertFalse((self.config.state_root / "index-candidate.json").exists())

    def test_changed_or_untyped_plan_cannot_reach_docker(self):
        from search.operations.catalog import runtime as catalog_runtime

        original = self.stage_index_plan()
        changes = (
            {"schema_version": True},
            {"document_count": True},
            {"revision": "b" * 40},
            {"manifest_sha256": "c" * 64},
            {"bundle_path": "/other/private/bundle"},
        )
        for change in changes:
            with locked_state(self.config) as store:
                store.write(
                    "index-candidate.json", {**original, **change}, replace=True
                )
            with (
                self.subTest(change=change),
                patch.object(catalog_runtime, "start") as start,
                self.assertRaisesRegex(ValueError, "prepare_required"),
            ):
                lifecycle.start_index(self.config)
            start.assert_not_called()

    def test_new_metadata_invalidates_the_old_prepared_plan(self):
        from search.operations.catalog import runtime as catalog_runtime

        self.stage_index_plan()
        self.rows[0]["title"] = "Changed public metadata"
        self.sync()
        with (
            patch.object(catalog_runtime, "admin") as admin,
            self.assertRaisesRegex(ValueError, "prepare_required"),
        ):
            lifecycle.import_index(self.config)
        admin.assert_not_called()

    def test_failed_index_operation_never_stores_secret_error_text(self):
        from search.operations.catalog import runtime as catalog_runtime

        self.stage_index_plan()
        with (
            patch.object(
                catalog_runtime, "admin", side_effect=ValueError("private-key")
            ),
            self.assertRaises(ValueError),
        ):
            lifecycle.import_index(self.config)
        records = [
            path.read_text() for path in self.config.state_root.glob("op-*.json")
        ]
        self.assertTrue(any(json.loads(raw)["status"] == "failed" for raw in records))
        self.assertTrue(all("private-key" not in raw for raw in records))

    def test_cli_dispatches_only_explicit_candidate_steps(self):
        for command in (
            "prepare-index",
            "start-index",
            "import-index",
            "reconcile-index",
        ):
            with (
                self.subTest(command=command),
                patch("search.operations.config.load_config", return_value=self.config),
                patch.object(
                    lifecycle,
                    command.replace("-", "_"),
                    return_value={"status": "completed", "active_changed": False},
                ) as action,
                patch("builtins.print"),
            ):
                self.assertEqual(
                    0, cli.main(["--config", "/unused/product.json", command])
                )
            action.assert_called_once_with(self.config)

    def fake_stage_backend(self, *, vector_status="vectors-complete"):
        """Fake-only orchestration; these tests do not prove Docker/GPU health."""
        from search.catalog.bundle import CatalogBundle
        from search.operations.catalog import backend, runtime, vectors

        self.stage_calls = []
        self.vector_status = vector_status
        self.vector_passes = 1
        self.stage_revision = REVISION
        self.stage_drift = None
        self.stage_failure = None
        patch.object(
            lifecycle, "revision", side_effect=lambda *_: self.stage_revision
        ).start()

        def step(name):
            self.stage_calls.append(name)
            # The fake lower-level operation observes the same held product
            # lock: one-click staging must not unlock between its steps.
            with self.assertRaisesRegex(ValueError, "busy"):
                with locked_state(self.config):
                    self.fail("the product lock was released between steps")
            if self.stage_failure == name:
                raise ValueError("private-error-do-not-log")
            if self.stage_drift == name:
                self.stage_revision = "1" * 40

        def prepare(
            config,
            *,
            repo_root,
            bundle_path,
            manifest_sha256,
            revision,
            runner,
            catalog_revision=None,
        ):
            step("prepare")
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

        def stage_images(config, plan, *, repo_root, runner):
            step("images")
            return {"status": "images_staged", "services_started": False}

        def start(config, plan, *, repo_root, runner):
            step("start")
            return {"status": "running"}

        def admin(config, plan, *, repo_root, operation, runner):
            self.assertEqual("import", operation)
            step("import")
            return {"status": "completed"}

        def build(config, plan, *, repo_root, runner, max_passes, wall_seconds):
            step("vectors")
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
                "passes_this_invocation": self.vector_passes,
                "done_count": len(bundle.documents) if complete else 0,
                "pending_count": 0 if complete else len(bundle.documents),
                "runtime_verified": True,
                "host_cleanup_verified": True,
                "active_changed": False,
            }

        self.stage_prepare = patch.object(
            backend, "prepare", side_effect=prepare
        ).start()
        self.stage_start = patch.object(runtime, "start", side_effect=start).start()
        self.stage_images = patch.object(
            runtime, "stage_images", side_effect=stage_images
        ).start()
        self.stage_admin = patch.object(runtime, "admin", side_effect=admin).start()
        self.stage_vectors = patch.object(vectors, "build", side_effect=build).start()

    def retain_stage_catalog(self):
        """Keep the sealed origin plan; only Git and host execution are fake."""
        from search.operations.catalog import backend

        with locked_state(self.config) as store:
            origin = store.read("index-candidate.json")
        prepare = self.stage_prepare.side_effect

        def retained_prepare(*args, **kwargs):
            candidate = prepare(*args, **kwargs)
            if all(
                candidate[key] == origin[key]
                for key in ("bundle_path", "manifest_sha256", "document_count")
            ):
                return dict(origin)
            return candidate

        self.stage_prepare.side_effect = retained_prepare
        self.stage_revision = "c" * 40
        self.vector_passes = 0

        def git_runner(argv, *, timeout):
            self.assertEqual("git", argv[0])
            if "ls-tree" in argv:
                self.assertEqual(REVISION, argv[-1])
                paths = (
                    *backend.STAGED_FILES,
                    *backend.BUILD_FILES,
                    *backend.DATA_FILES,
                )
                return subprocess.CompletedProcess(argv, 0, "\0".join(paths) + "\0", "")
            self.assertIn("show", argv)
            revision, relative = argv[-1].split(":", 1)
            self.assertIn(revision, {REVISION, self.stage_revision})
            self.assertIn(
                relative,
                (*backend.STAGED_FILES, *backend.BUILD_FILES, *backend.DATA_FILES),
            )
            return subprocess.CompletedProcess(
                argv, 0, (lifecycle.ROOT / relative).read_text(), ""
            )

        return git_runner

    def stage(self, **options):
        fetch = None if options.get("snapshot") is not None else self.fetch
        return lifecycle.stage_catalog(self.config, fetch=fetch, **options)

    def stage_hint_paths(self):
        """Only fake marker locations; actual runtime authority stays in start."""
        with locked_state(self.config) as store:
            root = Path(store.read("index-candidate.json")["work_root"])
        return {
            name: root / name
            for name in (
                "runtime.json",
                "image-stage-started.json",
                "image-stage-complete.json",
            )
        }

    def test_stage_catalog_uses_single_lock_and_existing_granular_receipt_schema(self):
        self.fake_stage_backend()
        with patch.object(lifecycle, "locked_state", wraps=locked_state) as lock:
            result = self.stage(max_passes=2, wall_seconds=4800)
        lock.assert_called_once_with(self.config, create=True)
        self.assertEqual(
            ["prepare", "images", "start", "import", "vectors"], self.stage_calls
        )
        self.assertEqual("stage-catalog", result["operation"])
        self.assertEqual("vectors-complete", result["status"])
        self.assertIs(result["staging_only"], True)
        self.assertIs(result["active_changed"], False)
        self.assertIs(result["runtime_verified"], True)
        self.assertIs(result["import_skipped"], False)
        self.assertIs(result["image_staging_skipped"], False)
        self.assertEqual(
            [
                {"operation": name, "status": status}
                for name, status in (
                    ("sync", "staged"),
                    ("prepare-index", "staged"),
                    ("build-catalog-images", "staged"),
                    ("start-index", "staged"),
                    ("import-index", "staged"),
                    ("build-vectors", "vectors-complete"),
                )
            ],
            result["steps"],
        )
        receipts = lifecycle.status(self.config)["operations"]
        self.assertEqual(6, len(receipts))
        self.assertEqual(
            {
                "sync",
                "prepare-index",
                "build-catalog-images",
                "start-index",
                "import-index",
                "build-vectors",
            },
            {receipt["operation"] for receipt in receipts},
        )
        for receipt in receipts:
            self.assertEqual(
                {
                    "schema_version",
                    "operation",
                    "source_revision",
                    "status",
                    "active_changed",
                    "bundle_id",
                    "manifest_sha256",
                },
                set(receipt),
            )
            self.assertEqual(REVISION, receipt["source_revision"])
            self.assertEqual("staged", receipt["status"])
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_stage_clean_checkpoint_resumes_without_reimport_and_still_rechecks_vectors(
        self,
    ):
        self.fake_stage_backend(vector_status="checkpoint")
        first = self.stage()
        self.assertEqual("checkpoint", first["status"])
        self.assertIs(first["runtime_verified"], False)
        self.vector_status = "vectors-complete"
        second = self.stage()
        self.assertEqual("vectors-complete", second["status"])
        self.assertIs(second["import_skipped"], True)
        self.assertEqual(1, self.stage_admin.call_count)
        self.assertEqual(2, self.stage_start.call_count)
        self.assertEqual(2, self.stage_vectors.call_count)
        self.assertEqual("skipped", second["steps"][4]["status"])
        self.assertEqual(2, self.stage_images.call_count)

    def test_cross_commit_stage_keeps_origin_receipts_and_rechecks_host_gates(self):
        self.fake_stage_backend()
        first = self.stage()
        self.assertIs(first["catalog_reused"], False)
        origin_receipts = {
            path: path.read_bytes() for path in self.config.state_root.glob("op-*.json")
        }
        runner = self.retain_stage_catalog()
        second = self.stage(runner=runner, catalog_revision=REVISION)
        self.assertEqual(
            REVISION, self.stage_prepare.call_args.kwargs["catalog_revision"]
        )
        self.assertEqual(first["bundle_id"], second["bundle_id"])
        self.assertEqual(REVISION, second["revision"])
        self.assertEqual(REVISION, second["catalog_revision"])
        self.assertEqual(self.stage_revision, second["source_revision"])
        self.assertIs(second["catalog_reused"], True)
        self.assertIs(second["import_skipped"], True)
        self.assertEqual(1, self.stage_admin.call_count)
        self.assertEqual(2, self.stage_start.call_count)
        self.assertEqual(2, self.stage_vectors.call_count)
        self.assertEqual(
            origin_receipts, {path: path.read_bytes() for path in origin_receipts}
        )
        with locked_state(self.config) as store:
            self.assertEqual(REVISION, store.read("index-candidate.json")["revision"])
            self.assertEqual(
                self.stage_revision, store.read("candidate.json")["source_revision"]
            )
        receipts = [
            json.loads(path.read_bytes())
            for path in self.config.state_root.glob("op-*.json")
            if path not in origin_receipts
        ]
        self.assertEqual(5, len(receipts))
        self.assertEqual(
            {
                "sync",
                "prepare-index",
                "build-catalog-images",
                "start-index",
                "build-vectors",
            },
            {row["operation"] for row in receipts},
        )
        self.assertTrue(
            all(row["source_revision"] == self.stage_revision for row in receipts)
        )
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_cross_commit_stage_accepts_current_source_granular_import_receipt(self):
        self.fake_stage_backend()
        self.stage()
        origin_imports = [
            path
            for path in self.config.state_root.glob("op-*.json")
            if json.loads(path.read_bytes())["operation"] == "import-index"
        ]
        runner = self.retain_stage_catalog()
        lifecycle.import_index(self.config, runner=runner)
        for path in origin_imports:
            path.unlink()
        current_imports = {
            path: path.read_bytes()
            for path in self.config.state_root.glob("op-*.json")
            if json.loads(path.read_bytes())["operation"] == "import-index"
        }
        self.assertEqual(1, len(current_imports))
        result = self.stage(runner=runner)
        self.assertIs(result["import_skipped"], True)
        self.assertEqual(2, self.stage_admin.call_count)
        self.assertEqual(
            current_imports, {path: path.read_bytes() for path in current_imports}
        )

    def test_cross_commit_metadata_change_cannot_reuse_import_receipt(self):
        self.fake_stage_backend()
        first = self.stage()
        runner = self.retain_stage_catalog()
        self.assertIs(self.stage(runner=runner)["import_skipped"], True)
        self.rows[0]["title"] = "Changed sealed public metadata"
        self.vector_passes = 1
        changed = self.stage(runner=runner)
        self.assertNotEqual(first["bundle_id"], changed["bundle_id"])
        self.assertIs(changed["catalog_reused"], False)
        self.assertIs(changed["import_skipped"], False)
        self.assertEqual(self.stage_revision, changed["catalog_revision"])
        self.assertEqual(2, self.stage_admin.call_count)
        self.assertEqual(3, self.stage_start.call_count)
        self.assertEqual(3, self.stage_vectors.call_count)

    def test_cross_commit_cache_cannot_bypass_runtime_start_failure(self):
        self.fake_stage_backend()
        self.stage()
        runner = self.retain_stage_catalog()
        self.stage_failure = "start"
        with self.assertRaisesRegex(ValueError, "private-error"):
            self.stage(runner=runner)
        self.assertEqual(1, self.stage_admin.call_count)
        self.assertEqual(1, self.stage_vectors.call_count)
        self.assertTrue(
            any(
                row["operation"] == "start-index"
                and row["source_revision"] == self.stage_revision
                and row["status"] == "failed"
                for row in lifecycle.status(self.config)["operations"]
            )
        )

    def test_cross_commit_cache_cannot_bypass_vector_completion_failure(self):
        self.fake_stage_backend()
        self.stage()
        runner = self.retain_stage_catalog()
        self.stage_failure = "vectors"
        with self.assertRaisesRegex(ValueError, "private-error"):
            self.stage(runner=runner)
        self.assertEqual(1, self.stage_admin.call_count)
        self.assertEqual(2, self.stage_start.call_count)
        self.assertEqual(2, self.stage_vectors.call_count)
        self.assertTrue(
            any(
                row["operation"] == "build-vectors"
                and row["source_revision"] == self.stage_revision
                and row["status"] == "failed"
                for row in lifecycle.status(self.config)["operations"]
            )
        )
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_stage_image_failure_stops_before_start_and_preserves_failed_step(self):
        self.fake_stage_backend()
        self.stage_failure = "images"
        with self.assertRaisesRegex(ValueError, "private-error"):
            self.stage()
        self.assertEqual(["prepare", "images"], self.stage_calls)
        self.stage_start.assert_not_called()
        self.stage_admin.assert_not_called()
        self.stage_vectors.assert_not_called()
        self.assertTrue(
            any(
                row["operation"] == "build-catalog-images" and row["status"] == "failed"
                for row in lifecycle.status(self.config)["operations"]
            )
        )

    def test_stage_completed_image_records_are_reverified_before_runtime_reuse(self):
        self.fake_stage_backend()
        self.stage()
        for path in self.stage_hint_paths().values():
            path.touch(mode=0o600)
        self.stage_failure = "images"
        with self.assertRaisesRegex(ValueError, "private-error"):
            self.stage()
        self.assertEqual(2, self.stage_images.call_count)
        self.assertEqual(1, self.stage_start.call_count)
        self.assertEqual(1, self.stage_admin.call_count)
        self.assertEqual(1, self.stage_vectors.call_count)

    def test_stage_legacy_runtime_only_skips_image_stage_and_still_calls_start(self):
        self.fake_stage_backend()
        self.sync()
        lifecycle.prepare_index(self.config)
        paths = self.stage_hint_paths()
        paths["runtime.json"].touch(mode=0o600)
        self.stage_calls.clear()
        result = self.stage()
        self.assertEqual(["prepare", "start", "import", "vectors"], self.stage_calls)
        self.stage_images.assert_not_called()
        self.stage_start.assert_called_once()
        self.assertIs(result["image_staging_skipped"], True)
        self.assertEqual(
            {"operation": "build-catalog-images", "status": "skipped_existing_runtime"},
            result["steps"][2],
        )
        self.assertFalse(paths["image-stage-started.json"].exists())
        self.assertFalse(paths["image-stage-complete.json"].exists())

    def test_stage_legacy_runtime_hint_cannot_bypass_failed_runtime_verification(self):
        self.fake_stage_backend()
        self.sync()
        lifecycle.prepare_index(self.config)
        paths = self.stage_hint_paths()
        paths["runtime.json"].touch(mode=0o600)
        self.stage_failure = "start"
        with self.assertRaisesRegex(ValueError, "private-error"):
            self.stage()
        self.stage_images.assert_not_called()
        self.stage_start.assert_called_once()
        self.stage_admin.assert_not_called()
        self.stage_vectors.assert_not_called()
        self.assertFalse(paths["image-stage-started.json"].exists())
        self.assertFalse(paths["image-stage-complete.json"].exists())

    def test_stage_partial_image_marker_does_not_take_legacy_runtime_skip(self):
        self.fake_stage_backend()
        self.sync()
        lifecycle.prepare_index(self.config)
        paths = self.stage_hint_paths()
        paths["runtime.json"].touch(mode=0o600)
        paths["image-stage-started.json"].touch(mode=0o600)
        self.stage_failure = "images"
        with self.assertRaisesRegex(ValueError, "private-error"):
            self.stage()
        self.stage_images.assert_called_once()
        self.stage_start.assert_not_called()
        self.stage_admin.assert_not_called()
        self.stage_vectors.assert_not_called()

    def test_stage_nonregular_or_linked_markers_fail_before_build_or_start(self):
        self.fake_stage_backend()
        self.sync()
        lifecycle.prepare_index(self.config)
        paths = self.stage_hint_paths()
        for name, kind in (
            ("runtime.json", "symlink"),
            ("runtime.json", "dangling"),
            ("runtime.json", "fifo"),
            ("image-stage-started.json", "dangling"),
            ("image-stage-complete.json", "symlink"),
        ):
            path = paths[name]
            if kind == "fifo":
                os.mkfifo(path, mode=0o600)
            else:
                target = self.root / ("missing" if kind == "dangling" else "regular")
                if kind == "symlink":
                    target.touch(mode=0o600)
                path.symlink_to(target)
            with (
                self.subTest(name=name, kind=kind),
                self.assertRaisesRegex(ValueError, "catalog_hint_invalid"),
            ):
                self.stage()
            path.unlink()
        self.stage_images.assert_not_called()
        self.stage_start.assert_not_called()
        self.stage_admin.assert_not_called()
        self.stage_vectors.assert_not_called()

    def test_stage_same_source_complete_noop_does_not_skip_actual_runtime_or_journal_gate(
        self,
    ):
        self.fake_stage_backend()
        self.stage()
        self.stage_failure = "vectors"
        with self.assertRaisesRegex(ValueError, "private-error"):
            self.stage()
        self.assertEqual(1, self.stage_admin.call_count)
        self.assertEqual(2, self.stage_start.call_count)
        self.assertEqual(2, self.stage_vectors.call_count)
        receipts = lifecycle.status(self.config)["operations"]
        self.assertTrue(
            any(
                receipt["operation"] == "build-vectors"
                and receipt["status"] == "failed"
                for receipt in receipts
            )
        )
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_stage_missing_successful_import_receipt_does_not_blindly_skip_import(self):
        self.fake_stage_backend()
        self.sync()
        with locked_state(self.config) as store:
            candidate = lifecycle._candidate(store)
            with operation(store, "prepare-index", REVISION) as outcome:
                outcome.update(lifecycle._outcome(candidate))
        result = self.stage()
        self.assertIs(result["import_skipped"], False)
        self.stage_admin.assert_called_once()

    def test_stage_new_metadata_requires_new_import(self):
        self.fake_stage_backend()
        old = self.stage()
        self.rows[0]["title"] = "Different public metadata"
        new = self.stage()
        self.assertNotEqual(old["bundle_id"], new["bundle_id"])
        self.assertIs(new["import_skipped"], False)
        self.assertEqual(2, self.stage_admin.call_count)

    def test_stage_uncertain_import_stops_no_retry_reconcile_or_vector_work(self):
        self.fake_stage_backend()
        self.stage_failure = "import"
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "private-error"):
                self.stage()
        self.assertEqual(
            ["import", "import"],
            [call.kwargs["operation"] for call in self.stage_admin.call_args_list],
        )
        self.stage_vectors.assert_not_called()
        self.assertFalse((self.config.state_root / "active.json").exists())
        receipts = lifecycle.status(self.config)["operations"]
        self.assertEqual(
            2,
            sum(
                receipt["operation"] == "import-index" and receipt["status"] == "failed"
                for receipt in receipts
            ),
        )
        self.assertNotIn(
            "private-error",
            "".join(
                path.read_text() for path in self.config.state_root.glob("op-*.json")
            ),
        )

    def test_stage_failure_stops_later_steps_and_never_records_activation(self):
        self.fake_stage_backend()
        self.stage_failure = "start"
        with self.assertRaises(ValueError):
            self.stage()
        self.stage_admin.assert_not_called()
        self.stage_vectors.assert_not_called()
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_stage_sha_drift_during_prepare_refuses_next_step_and_plan_publication(
        self,
    ):
        self.fake_stage_backend()
        self.stage_drift = "prepare"
        with self.assertRaisesRegex(ValueError, "invocation_source_changed"):
            self.stage()
        self.stage_start.assert_not_called()
        self.stage_admin.assert_not_called()
        self.stage_vectors.assert_not_called()
        self.assertFalse((self.config.state_root / "index-candidate.json").exists())

    def test_stage_sha_drift_during_sync_does_not_publish_candidate_or_prepare(self):
        self.fake_stage_backend()
        original_fetch = self.fetch

        def changed_source_fetch(*args):
            result = original_fetch(*args)
            self.stage_revision = "1" * 40
            return result

        with self.assertRaisesRegex(ValueError, "invocation_source_changed"):
            lifecycle.stage_catalog(self.config, fetch=changed_source_fetch)
        self.stage_prepare.assert_not_called()
        self.assertFalse((self.config.state_root / "candidate.json").exists())

    def test_stage_started_operation_crash_receipt_blocks_before_any_step(self):
        self.fake_stage_backend()
        with locked_state(self.config, create=True) as store:
            store.write(
                "op-" + "1" * 32 + ".json",
                {
                    "schema_version": 1,
                    "operation": "build-vectors",
                    "source_revision": REVISION,
                    "status": "started",
                    "active_changed": False,
                },
            )
        with self.assertRaisesRegex(ValueError, "recovery_required"):
            self.stage()
        self.assertEqual(0, self.fetch_calls)
        self.stage_prepare.assert_not_called()
        self.stage_start.assert_not_called()
        self.stage_admin.assert_not_called()
        self.stage_vectors.assert_not_called()

    def test_stage_invalid_budget_refuses_before_state_or_network_mutation(self):
        self.fake_stage_backend()
        for options in (
            {"max_passes": True},
            {"max_passes": 0},
            {"max_passes": 41},
            {"wall_seconds": True},
            {"wall_seconds": 2399},
            {"wall_seconds": 36001},
        ):
            with (
                self.subTest(options=options),
                self.assertRaisesRegex(ValueError, "budget_invalid"),
            ):
                self.stage(**options)
        self.assertFalse(self.config.state_root.exists())
        self.assertEqual(0, self.fetch_calls)
        self.stage_prepare.assert_not_called()

    def test_stage_offline_snapshot_and_force_pass_through_existing_sync(self):
        self.fake_stage_backend()
        snapshot = self.root / "metadata.jsonl"
        snapshot.write_text(json.dumps(dataset()) + "\n")
        result = self.stage(snapshot=snapshot, force=True)
        self.assertEqual("vectors-complete", result["status"])
        self.assertEqual(0, self.fetch_calls)

    def test_stage_rejects_unverified_complete_result_before_successful_receipt(self):
        self.fake_stage_backend()
        build = self.stage_vectors.side_effect
        self.stage_vectors.side_effect = lambda *args, **kwargs: {
            **build(*args, **kwargs),
            "host_cleanup_verified": False,
        }
        with self.assertRaisesRegex(ValueError, "outcome_unverified"):
            self.stage()
        receipts = lifecycle.status(self.config)["operations"]
        vector_receipts = [
            receipt for receipt in receipts if receipt["operation"] == "build-vectors"
        ]
        self.assertEqual(1, len(vector_receipts))
        self.assertEqual("failed", vector_receipts[0]["status"])

    def test_stage_receipt_retention_refuses_later_step_without_trimming(self):
        self.fake_stage_backend()
        with patch("search.operations.state.MAX_OPERATIONS", 4):
            with self.assertRaisesRegex(ValueError, "retention_limit"):
                self.stage()
        self.stage_vectors.assert_not_called()
        self.assertEqual(4, len(lifecycle.status(self.config)["operations"]))

    def test_cli_prepare_and_stage_forward_only_explicit_catalog_origin(self):
        for command in ("prepare-index", "stage-catalog"):
            with (
                self.subTest(command=command),
                patch("search.operations.config.load_config", return_value=self.config),
                patch.object(
                    lifecycle,
                    command.replace("-", "_"),
                    return_value={"status": "vectors-complete"},
                ) as action,
                patch("builtins.print"),
            ):
                self.assertEqual(
                    0,
                    cli.main(
                        [
                            "--config",
                            "/unused/config.json",
                            command,
                            "--catalog-revision",
                            REVISION,
                        ]
                    ),
                )
            action.assert_called_once()
            self.assertEqual((self.config,), action.call_args.args)
            self.assertEqual(REVISION, action.call_args.kwargs["catalog_revision"])

    def test_cli_stage_catalog_dispatch_budget_and_checkpoint_or_complete_exit(self):
        for status, exit_code in (
            ("checkpoint", 2),
            ("vectors-complete", 0),
            ("blocked", 1),
        ):
            with (
                self.subTest(status=status),
                patch("search.operations.config.load_config", return_value=self.config),
                patch.object(
                    lifecycle,
                    "stage_catalog",
                    return_value={"status": status, "staging_only": True},
                ) as stage,
                patch("builtins.print"),
            ):
                self.assertEqual(
                    exit_code,
                    cli.main(
                        [
                            "--config",
                            "/unused/config.json",
                            "stage-catalog",
                            "--snapshot",
                            "/private/metadata.jsonl",
                            "--force",
                            "--max-passes",
                            "2",
                            "--wall-seconds",
                            "4800",
                        ]
                    ),
                )
            stage.assert_called_once_with(
                self.config,
                snapshot=Path("/private/metadata.jsonl"),
                force=True,
                max_passes=2,
                wall_seconds=4800,
            )


if __name__ == "__main__":
    unittest.main()
