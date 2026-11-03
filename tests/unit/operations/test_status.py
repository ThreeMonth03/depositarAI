"""Local status never substitutes retained activation evidence for live
health."""

from __future__ import annotations

from dataclasses import replace
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from search.catalog.errors import BundleError
from search.operations import lifecycle, local_status, state
from search.operations.shared import private_io
from tests.unit.deployment.test_composition import product


REVISION = "a" * 40
BUNDLE = "b" * 64
MANIFEST = "c" * 64


def digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


class LocalStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="depositar-local-status-"
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = replace(product(), state_root=self.root / "product")
        self.addCleanup(mock.patch.stopall)
        mock.patch(
            "subprocess.run", side_effect=AssertionError("no process IO")
        ).start()

    def initialize(self):
        with state.locked_state(self.config, create=True):
            pass

    def receipt(self, name="sync", status="staged", *, bundle=False):
        self.initialize()
        with state.locked_state(self.config) as store:
            number = len(state.operation_receipts(store))
            value = {
                "schema_version": 1,
                "operation": name,
                "source_revision": REVISION,
                "status": status,
                "active_changed": name == "activate-gateway"
                and status == "staged",
            }
            if bundle:
                value.update(bundle_id=BUNDLE, manifest_sha256=MANIFEST)
            store.write(f"op-{number:032x}.json", value)

    def write(self, path, value):
        path.write_bytes(private_io.canonical(value))
        path.chmod(0o600)

    def activation(self, *, receipt_status="staged"):
        self.initialize()
        gateway = self.config.state_root / "gateway"
        gateway.mkdir(mode=0o700)
        activation = gateway / "activation"
        activation.mkdir(mode=0o700)
        identity = {
            "schema_version": 1,
            "revision": REVISION,
            "project": "depositar-ai-gateway",
            "release_project": "depositar-ai-release-"
            + BUNDLE[:12]
            + "-"
            + REVISION[:12],
            "bundle_id": BUNDLE,
            "manifest_sha256": MANIFEST,
        }
        prepared = {
            key: identity[key]
            for key in (
                "schema_version",
                "revision",
                "project",
                "release_project",
            )
        }
        prepared.update(
            status="gateway_prepared", source_manifest_sha256="d" * 64
        )
        self.write(gateway / "prepared.json", prepared)
        complete = {
            **identity,
            "source_manifest_sha256": "d" * 64,
            "prepared_sha256": digest(prepared),
            "native_compose_sha256": "e" * 64,
            "bootstrap_sha256": "f" * 64,
            "route_sha256": "1" * 64,
            "private_release_proof_sha256": "2" * 64,
            "probe_run_id": "3" * 32,
            "active_changed": False,
            "confers_runtime_authority": True,
            "status": "gateway_active",
            "proof": {"retained": "private local proof"},
            "http_acceptance": {"retained": "private local HTTP result"},
            "runtime_verified": True,
            "gateway_verified": True,
            "loopback_verified": True,
            "two_turn_search_verified": True,
            "session_reset_verified": True,
            "ports_published": True,
            "private_release_acceptance_verified": True,
            "search_quality_verified": False,
            "activation_performed": True,
        }
        self.write(activation / "complete.json", complete)
        pointer = {
            **identity,
            "status": "gateway_active",
            "activation_complete_sha256": digest(complete),
            "active_changed": True,
        }
        self.write(self.config.state_root / "active.json", pointer)
        if receipt_status is not None:
            self.receipt("activate-gateway", receipt_status, bundle=True)
        return pointer, complete

    def status(self):
        result = lifecycle.status(self.config)
        self.assertIs(result["runtime_verified"], False)
        return result

    def test_known_activation_record_faults_become_only_recovery_reasons(self):
        self.initialize()
        self.write(self.config.state_root / "active.json", {})
        for error_type in (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            OverflowError,
        ):
            error = error_type("PRIVATE_SENTINEL")
            with state.locked_state(self.config) as store:
                with (
                    self.subTest(error_type=error_type.__name__),
                    mock.patch.object(
                        local_status, "_recorded_activation", side_effect=error
                    ),
                ):
                    result = local_status.classify_local_state(store, [])
            self.assertEqual(
                {
                    "status": "recovery_required",
                    "active_release": None,
                    "recovery_reasons": ["activation_record_invalid"],
                },
                result,
            )
            self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))

    def test_unexpected_activation_faults_and_interrupts_propagate(self):
        self.initialize()
        self.write(self.config.state_root / "active.json", {})
        for error in (RuntimeError("synthetic"), KeyboardInterrupt()):
            with state.locked_state(self.config) as store:
                with (
                    self.subTest(error_type=type(error).__name__),
                    mock.patch.object(
                        local_status, "_recorded_activation", side_effect=error
                    ),
                    self.assertRaises(type(error)) as failure,
                ):
                    local_status.classify_local_state(store, [])
                self.assertIs(error, failure.exception)

    def test_matching_operation_fault_is_outside_activation_record_catch(
        self,
    ):
        self.activation(receipt_status=None)
        with state.locked_state(self.config) as store:
            with (
                mock.patch.object(
                    local_status,
                    "_recorded_activation",
                    wraps=local_status._recorded_activation,
                ) as read_activation,
                self.assertRaises(KeyError) as failure,
            ):
                local_status.classify_local_state(
                    store, [{"status": "staged"}]
                )
            read_activation.assert_called_once_with(store)
            self.assertEqual(("operation",), failure.exception.args)

    def test_entry_or_artifact_fault_does_not_read_activation(self):
        self.initialize()
        (self.config.state_root / "gateway").mkdir(mode=0o700)
        for name in ("listdir", "stat"):
            error = PermissionError("synthetic metadata failure")
            with state.locked_state(self.config) as store:
                with (
                    self.subTest(boundary=name),
                    mock.patch.object(
                        local_status.os, name, side_effect=error
                    ),
                    mock.patch.object(
                        local_status, "_recorded_activation"
                    ) as read_activation,
                    mock.patch.object(
                        private_io, "open_child_directory"
                    ) as open_child,
                    self.assertRaises(PermissionError) as failure,
                ):
                    local_status.classify_local_state(store, [])
                self.assertIs(error, failure.exception)
                read_activation.assert_not_called()
                open_child.assert_not_called()

    def test_gateway_intent_fd_closes_on_success_and_observation_faults(self):
        self.initialize()
        gateway = self.config.state_root / "gateway"
        gateway.mkdir(mode=0o700)
        (gateway / "activation").mkdir(mode=0o700)
        for fault in (None, "listdir", "verify"):
            with state.locked_state(self.config) as store:
                opened = []
                native_open = private_io.open_child_directory
                native_listdir = os.listdir
                native_verify = private_io.verify_directory
                error = PermissionError("synthetic gateway failure")

                def open_child(parent, name):
                    descriptor = native_open(parent, name)
                    opened.append(descriptor)
                    return descriptor

                def list_directory(descriptor):
                    if descriptor in opened and fault == "listdir":
                        raise error
                    return native_listdir(descriptor)

                def verify_directory(descriptor, path, **options):
                    if descriptor in opened and fault == "verify":
                        raise error
                    return native_verify(descriptor, path, **options)

                with (
                    self.subTest(fault=fault),
                    mock.patch.object(
                        private_io, "open_child_directory", open_child
                    ),
                    mock.patch.object(os, "listdir", list_directory),
                    mock.patch.object(
                        private_io, "verify_directory", verify_directory
                    ),
                ):
                    if fault is None:
                        result = local_status.classify_local_state(store, [])
                        self.assertEqual(
                            ["activation_pointer_missing"],
                            result["recovery_reasons"],
                        )
                    else:
                        with self.assertRaises(PermissionError) as failure:
                            local_status.classify_local_state(store, [])
                        self.assertIs(error, failure.exception)
                self.assertEqual(1, len(opened))
                with self.assertRaises(OSError) as closed:
                    os.fstat(opened[0])
                self.assertEqual(errno.EBADF, closed.exception.errno)

    def test_gateway_open_fault_does_not_close_an_unowned_fd(self):
        self.initialize()
        (self.config.state_root / "gateway").mkdir(mode=0o700)
        error = PermissionError("synthetic gateway open failure")
        with state.locked_state(self.config) as store:
            held = os.fstat(store.fd)
            with (
                mock.patch.object(
                    private_io, "open_child_directory", side_effect=error
                ),
                mock.patch.object(os, "close", wraps=os.close) as close,
                self.assertRaises(PermissionError) as failure,
            ):
                local_status.classify_local_state(store, [])
            self.assertIs(error, failure.exception)
            close.assert_not_called()
            self.assertEqual(held.st_ino, os.fstat(store.fd).st_ino)

    def test_invalid_artifact_skips_intent_but_not_active_pointer(self):
        self.initialize()
        gateway = self.config.state_root / "gateway"
        gateway.mkdir(mode=0o700)
        artifact = self.config.state_root / "image-candidate.json"
        artifact.write_text("PRIVATE_SENTINEL")
        artifact.chmod(0o644)
        with state.locked_state(self.config) as store:
            with mock.patch.object(
                private_io, "open_child_directory"
            ) as open_child:
                result = local_status.classify_local_state(store, [])
            open_child.assert_not_called()
        self.assertEqual(["artifact_invalid"], result["recovery_reasons"])
        gateway.rmdir()
        pointer, _ = self.activation()
        with state.locked_state(self.config) as store:
            with mock.patch.object(
                local_status,
                "_recorded_activation",
                wraps=local_status._recorded_activation,
            ) as read_activation:
                result = local_status.classify_local_state(
                    store, state.operation_receipts(store)
                )
            read_activation.assert_called_once_with(store)
        self.assertEqual(pointer, result["active_release"])
        self.assertEqual(["artifact_invalid"], result["recovery_reasons"])

    def test_single_names_snapshot_and_metadata_precede_activation(self):
        self.activation()
        self.write(self.config.state_root / "image-candidate.json", {})
        with state.locked_state(self.config) as store:
            operations = state.operation_receipts(store)
            events = []
            native_listdir, native_stat = os.listdir, os.stat
            native_activation = local_status._recorded_activation

            def list_directory(descriptor):
                events.append(("listdir", descriptor == store.fd))
                return native_listdir(descriptor)

            def observe_stat(path, *args, **options):
                if options.get("dir_fd") == store.fd:
                    events.append(("stat", path))
                return native_stat(path, *args, **options)

            def activation(actual_store):
                events.append(("activation", None))
                return native_activation(actual_store)

            with (
                mock.patch.object(os, "listdir", list_directory),
                mock.patch.object(os, "stat", observe_stat),
                mock.patch.object(
                    local_status, "_recorded_activation", activation
                ),
            ):
                result = local_status.classify_local_state(store, operations)
        self.assertEqual("active_recorded", result["status"])
        self.assertEqual(1, events.count(("listdir", True)))
        start = events.index(("activation", None))
        self.assertEqual(
            {"gateway", "image-candidate.json"},
            {path for kind, path in events[:start] if kind == "stat"},
        )

    def test_activation_history_short_circuits_gateway_inspection(self):
        self.receipt("activate-gateway", bundle=True)
        (self.config.state_root / "gateway").mkdir(mode=0o700)
        with state.locked_state(self.config) as store:
            operations = state.operation_receipts(store)
            with mock.patch.object(
                private_io, "open_child_directory"
            ) as open_child:
                result = local_status.classify_local_state(store, operations)
            open_child.assert_not_called()
        self.assertEqual(
            ["activation_pointer_missing"], result["recovery_reasons"]
        )

    def test_absent_and_empty_initialized_state_do_not_invent_activation(self):
        result = self.status()
        self.assertEqual("not_initialized", result["status"])
        self.assertFalse(self.config.state_root.exists())
        self.initialize()
        result = self.status()
        self.assertEqual("staged", result["status"])
        self.assertIsNone(result["active_release"])
        self.assertEqual([], result["operations"])

    def test_catalog_staging_and_clean_checkpoint_receipts_remain_staged(self):
        for name in (
            "sync",
            "prepare-index",
            "build-catalog-images",
            "start-index",
            "import-index",
            "reconcile-index",
            "build-vectors",
        ):
            self.receipt(name)
        result = self.status()
        self.assertEqual("staged", result["status"])
        self.assertIsNone(result["active_release"])
        self.assertEqual([], result["recovery_reasons"])

    def test_failed_and_interrupted_operations_require_recovery(self):
        for name, status in (("sync", "failed"), ("prepare-index", "started")):
            self.receipt(name, status)
        result = self.status()
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual(
            ["operation_failed", "operation_incomplete"],
            result["recovery_reasons"],
        )

    def test_successful_app_preparation_is_distinct_from_catalog_only(self):
        for name in (
            "build-images",
            "prepare-secrets",
            "start-inference",
            "prepare-query",
            "start-release",
            "prepare-gateway",
            "package-product",
        ):
            with self.subTest(operation=name):
                self.receipt(name)
                result = self.status()
                self.assertEqual("partially_prepared", result["status"])
                self.assertIsNone(result["active_release"])

    def test_artifact_without_operation_is_partial_and_secrets_unread(
        self,
    ):
        self.initialize()
        secrets = self.config.state_root / "secrets"
        secrets.mkdir(mode=0o700)
        (secrets / "api_key").write_text("PRIVATE_SENTINEL")
        result = self.status()
        self.assertEqual("partially_prepared", result["status"])
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))

    def test_active_pointer_reports_only_bound_local_identity(self):
        pointer, _ = self.activation()
        before = {
            path: (
                path.stat().st_ino,
                path.stat().st_mtime_ns,
                path.read_bytes(),
            )
            for path in self.config.state_root.rglob("*")
            if path.is_file()
        }
        result = self.status()
        self.assertEqual("active_recorded", result["status"])
        self.assertEqual(pointer, result["active_release"])
        self.assertEqual([], result["recovery_reasons"])
        self.assertNotIn("private local", json.dumps(result))
        self.assertEqual(
            before,
            {
                path: (
                    path.stat().st_ino,
                    path.stat().st_mtime_ns,
                    path.read_bytes(),
                )
                for path in self.config.state_root.rglob("*")
                if path.is_file()
            },
        )

    def test_active_record_survives_failed_or_missing_completion(
        self,
    ):
        pointer, _ = self.activation(receipt_status="failed")
        result = self.status()
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual(pointer, result["active_release"])
        self.assertEqual(
            ["activation_operation_unconfirmed", "operation_failed"],
            result["recovery_reasons"],
        )

    def test_pointer_with_interrupted_operation_is_visible_but_uncertain(
        self,
    ):
        pointer, _ = self.activation(receipt_status="started")
        result = self.status()
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual(pointer, result["active_release"])
        self.assertEqual(
            ["activation_operation_unconfirmed", "operation_incomplete"],
            result["recovery_reasons"],
        )

    def test_active_pointer_without_receipt_is_not_confirmed(self):
        pointer, _ = self.activation(receipt_status=None)
        result = self.status()
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual(pointer, result["active_release"])
        self.assertEqual(
            ["activation_operation_unconfirmed"], result["recovery_reasons"]
        )

    def test_active_identity_is_independent_of_a_newer_metadata_candidate(
        self,
    ):
        pointer, _ = self.activation()
        row = {
            "id": "00000000-0000-4000-8000-000000000001",
            "name": "new-candidate",
            "title": "New public metadata",
            "private": False,
            "state": "active",
            "type": "dataset",
            "resources": [],
        }
        with mock.patch.object(lifecycle, "revision", return_value="e" * 40):
            lifecycle.sync_metadata(
                self.config,
                fetch=lambda start, rows, deadline: {
                    "count": 1,
                    "results": [row] if start == 0 else [],
                },
            )
        result = self.status()
        self.assertEqual("active_recorded", result["status"])
        self.assertEqual(pointer, result["active_release"])
        self.assertEqual("e" * 40, result["candidate"]["source_revision"])
        self.assertNotEqual(BUNDLE, result["candidate"]["bundle_id"])

    def test_operation_receipt_must_match_the_active_recorded_identity(self):
        self.activation()
        path = next(self.config.state_root.glob("op-*.json"))
        receipt = json.loads(path.read_bytes())
        for change in (
            {"source_revision": "e" * 40},
            {"bundle_id": "e" * 64},
            {"manifest_sha256": "e" * 64},
        ):
            with self.subTest(change=change):
                self.write(path, {**receipt, **change})
                result = self.status()
                self.assertEqual("recovery_required", result["status"])
                self.assertEqual(
                    ["activation_operation_unconfirmed"],
                    result["recovery_reasons"],
                )
                self.assertIsNotNone(result["active_release"])

    def test_successful_activation_history_without_pointer_requires_recovery(
        self,
    ):
        self.receipt("activate-gateway", bundle=True)
        result = self.status()
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual(
            ["activation_pointer_missing"], result["recovery_reasons"]
        )

    def test_activation_intent_without_pointer_requires_recovery(self):
        self.initialize()
        gateway = self.config.state_root / "gateway"
        gateway.mkdir(mode=0o700)
        (gateway / "activation").mkdir(mode=0o700)
        result = self.status()
        self.assertEqual("recovery_required", result["status"])
        self.assertEqual(
            ["activation_pointer_missing"], result["recovery_reasons"]
        )

    def test_pointer_schema_and_identity_fail_closed(self):
        pointer, _ = self.activation()
        path = self.config.state_root / "active.json"
        for change in (
            {"schema_version": True},
            {"active_changed": 1},
            {"revision": "../PRIVATE_SENTINEL"},
            {"bundle_id": "invalid"},
            {"project": "other"},
            {"release_project": "other"},
            {"extra": "/private/secret"},
        ):
            with self.subTest(change=change):
                self.write(path, {**pointer, **change})
                result = self.status()
                self.assertEqual("recovery_required", result["status"])
                self.assertIsNone(result["active_release"])
                self.assertEqual(
                    ["activation_record_invalid"], result["recovery_reasons"]
                )
                self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))

    def test_malformed_pointer_types_and_json_fail_closed(self):
        self.initialize()
        path = self.config.state_root / "active.json"
        for raw in (
            b"null",
            b"[]",
            b"1",
            b"{broken",
            b'{"status":1,"status":2}',
        ):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                path.chmod(0o600)
                result = self.status()
                self.assertEqual("recovery_required", result["status"])
                self.assertIsNone(result["active_release"])

    def test_missing_or_mismatched_complete_never_confirms_pointer(self):
        pointer, complete = self.activation()
        complete_path = (
            self.config.state_root / "gateway/activation/complete.json"
        )
        complete_path.unlink()
        self.assertEqual("recovery_required", self.status()["status"])
        self.write(complete_path, {**complete, "manifest_sha256": "9" * 64})
        self.assertEqual("recovery_required", self.status()["status"])
        pointer["activation_complete_sha256"] = digest(
            {**complete, "manifest_sha256": "9" * 64}
        )
        self.write(self.config.state_root / "active.json", pointer)
        self.assertEqual("recovery_required", self.status()["status"])

    def test_invalid_historical_flags_are_not_accepted_by_hash_alone(
        self,
    ):
        pointer, complete = self.activation()
        for change in (
            {"runtime_verified": False},
            {"gateway_verified": 1},
            {"schema_version": True},
            {"status": "started"},
            {"prepared_sha256": "9" * 64},
            {"proof": []},
            {"extra": "PRIVATE_SENTINEL"},
        ):
            with self.subTest(change=change):
                altered = {**complete, **change}
                pointer["activation_complete_sha256"] = digest(altered)
                self.write(self.config.state_root / "active.json", pointer)
                self.write(
                    self.config.state_root
                    / "gateway/activation/complete.json",
                    altered,
                )
                self.assertIsNone(self.status()["active_release"])

    def test_changed_preparation_invalidates_local_activation_binding(self):
        self.activation()
        self.write(
            self.config.state_root / "gateway/prepared.json", {"changed": True}
        )
        self.assertIsNone(self.status()["active_release"])

    def test_pointer_special_files_and_links_are_not_read_or_treated_absent(
        self,
    ):
        self.initialize()
        pointer = self.config.state_root / "active.json"
        target = self.root / "PRIVATE_SENTINEL"
        target.write_text("PRIVATE_SENTINEL")
        for kind in (
            "symlink",
            "broken_symlink",
            "fifo",
            "directory",
            "hardlink",
        ):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    pointer.symlink_to(target)
                elif kind == "broken_symlink":
                    pointer.symlink_to(self.root / "absent")
                elif kind == "fifo":
                    os.mkfifo(pointer, 0o600)
                elif kind == "directory":
                    pointer.mkdir(mode=0o700)
                else:
                    os.link(target, pointer)
                    pointer.chmod(0o600)
                result = self.status()
                self.assertEqual("recovery_required", result["status"])
                self.assertIsNone(result["active_release"])
                self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))
                if pointer.is_dir() and not pointer.is_symlink():
                    pointer.rmdir()
                else:
                    pointer.unlink()

    def test_application_artifact_links_and_wrong_modes_require_recovery(self):
        self.initialize()
        path = self.config.state_root / "gateway"
        path.symlink_to(self.root / "missing")
        self.assertEqual(
            ["artifact_invalid"], self.status()["recovery_reasons"]
        )
        path.unlink()
        path.mkdir(mode=0o755)
        self.assertEqual(
            ["artifact_invalid"], self.status()["recovery_reasons"]
        )

    def test_activation_evidence_child_symlink_is_rejected(self):
        self.activation()
        complete = self.config.state_root / "gateway/activation/complete.json"
        target = self.root / "PRIVATE_SENTINEL"
        complete.rename(target)
        complete.symlink_to(target)
        self.assertEqual(
            ["activation_record_invalid"], self.status()["recovery_reasons"]
        )

    def test_missing_candidate_bundle_is_not_mistaken_for_absent_candidate(
        self,
    ):
        self.initialize()
        with state.locked_state(self.config) as store:
            store.write(
                "candidate.json",
                {
                    "schema_version": 1,
                    "source_revision": REVISION,
                    "bundle_path": str(
                        store.root / "cache/bundles" / ("sha256-" + BUNDLE)
                    ),
                    "bundle_id": BUNDLE,
                    "manifest_sha256": MANIFEST,
                    "document_count": 1,
                    "receipt_path": "unused",
                },
            )
        with self.assertRaises(BundleError):
            self.status()

    def test_broken_state_root_symlink_is_not_not_initialized(self):
        self.config.state_root.symlink_to(self.root / "missing")
        with self.assertRaises((OSError, ValueError)):
            self.status()

    def test_status_keeps_the_existing_configuration_and_lock_barriers(self):
        self.initialize()
        with state.locked_state(self.config):
            with self.assertRaisesRegex(ValueError, "busy"):
                self.status()
        with self.assertRaisesRegex(ValueError, "configuration_mismatch"):
            lifecycle.status(
                replace(self.config, api_model_root=self.root / "other")
            )


if __name__ == "__main__":
    unittest.main()
