"""Real activation receipt failures retain the published pointer; no Docker/GPU."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import errno
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.operations import cli, lifecycle
from search.operations.config import GPUConfig, ProductConfig
from search.operations.deployment.gateway import activation
from search.operations.shared import private_io
from search.operations.state import StateStore, locked_state, operation_receipts


REVISION = "a" * 40
PRIVATE = "private-storage-detail-must-not-be-printed"


class ActivationOutcomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-activation-outcome-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        revision = patch.object(lifecycle, "revision", return_value=REVISION)
        revision.start()
        self.addCleanup(revision.stop)
        self.case_number = 0

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
        dataset = {
            "id": "00000000-0000-4000-8000-000000000001",
            "name": "test-dataset",
            "title": "Public metadata",
            "private": False,
            "state": "active",
            "type": "dataset",
            "resources": [],
        }
        report = lifecycle.sync_metadata(
            self.config,
            fetch=lambda start, rows, deadline: {
                "count": 1,
                "results": [dataset][start : start + rows],
            },
        )
        self.plan = {
            "schema_version": 1,
            "revision": REVISION,
            "bundle_path": report["bundle_path"],
            "manifest_sha256": report["manifest_sha256"],
            "document_count": report["document_count"],
        }
        with locked_state(self.config) as store:
            store.write("index-candidate.json", self.plan)

    def activate(self, config, store, revision, plan, **options):
        self.assertIs(config, self.config)
        self.assertEqual(revision, REVISION)
        self.assertEqual(plan, self.plan)
        with self.assertRaisesRegex(ValueError, "product_operation_busy"):
            with locked_state(config):
                self.fail("activation released the operation lock")
        store.write("active.json", {"revision": revision})
        return {
            "status": "gateway_active",
            "active_changed": True,
            "activation_performed": True,
            "confers_runtime_authority": True,
        }

    def run_cli(self, **patches):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch("search.operations.config.load_config", return_value=self.config),
            patch.object(activation, "activate_gateway", **patches) as owner,
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            code = cli.main(
                ["--config", str(self.root / "private.json"), "activate-gateway"]
            )
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        raw = errors.getvalue()
        self.assertNotIn(PRIVATE, raw)
        self.assertNotIn(str(self.root), raw)
        self.assertNotIn("Traceback", raw)
        return json.loads(raw), owner

    def activation_receipt(self):
        with locked_state(self.config) as store:
            return next(
                row
                for row in operation_receipts(store)
                if row["operation"] == "activate-gateway"
            )

    def assert_unconfirmed(self, report, changed):
        self.assertEqual(report["status"], "recovery_required")
        self.assertEqual(report["code"], "product_operation_failed")
        self.assertEqual(report["error_code"], "product_activation_outcome_unconfirmed")
        self.assertIs(report["active_changed"], changed)
        self.assertIs(report["activation_performed"], changed)
        self.assertIs(report["confers_runtime_authority"], False)
        self.assertIs(report["runtime_verified"], False)

    def test_receipt_failure_after_publication_does_not_claim_no_active_change(self):
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

                    with patch.object(StateStore, "write", new=write):
                        report, owner = self.run_cli(side_effect=self.activate)
                    owner.assert_called_once()
                    self.assert_unconfirmed(report, True)
                    self.assertEqual(writes, ["started", "staged"])
                    with locked_state(self.config) as store:
                        self.assertEqual(
                            store.read("active.json"), {"revision": REVISION}
                        )
                    receipt = self.activation_receipt()
                    self.assertEqual(
                        receipt["status"], "staged" if committed else "started"
                    )
                    self.assertIs(receipt["active_changed"], committed)

    def test_pointer_write_io_failure_reports_unknown_change_without_read_retry(self):
        original = private_io.write_json
        read = StateStore.read
        for error_number in (errno.ENOSPC, errno.EROFS):
            for published in (False, True):
                with self.subTest(error_number=error_number, published=published):
                    self.new_case()
                    writes = []

                    def write(parent_fd, name, value, **options):
                        if name == "active.json":
                            writes.append(name)
                            if published:
                                original(parent_fd, name, value, **options)
                            raise OSError(error_number, PRIVATE, str(self.root))
                        return original(parent_fd, name, value, **options)

                    def no_pointer_read(store, name):
                        if name == "active.json":
                            self.fail(
                                "IO failure cannot be resolved by rereading the pointer"
                            )
                        return read(store, name)

                    with (
                        patch.object(private_io, "write_json", new=write),
                        patch.object(StateStore, "read", new=no_pointer_read),
                    ):
                        report, owner = self.run_cli(side_effect=self.activate)
                    owner.assert_called_once()
                    self.assertEqual(writes, ["active.json"])
                    self.assert_unconfirmed(report, None)
                    self.assertIs(
                        (self.config.state_root / "active.json").exists(), published
                    )
                    receipt = self.activation_receipt()
                    self.assertEqual(receipt["status"], "started")
                    self.assertIs(receipt["active_changed"], False)

    def test_owner_error_after_successful_pointer_write_preserves_active_change(self):
        for error_number in (errno.ENOSPC, errno.EROFS):
            with self.subTest(error_number=error_number):
                self.new_case()

                def activate(*args, **options):
                    self.activate(*args, **options)
                    raise OSError(error_number, PRIVATE, str(self.root))

                report, owner = self.run_cli(side_effect=activate)
                owner.assert_called_once()
                self.assert_unconfirmed(report, True)
                with locked_state(self.config) as store:
                    self.assertEqual(store.read("active.json"), {"revision": REVISION})
                receipt = self.activation_receipt()
                self.assertEqual(receipt["status"], "started")
                self.assertIs(receipt["active_changed"], False)

    def test_invalid_owner_report_cannot_hide_a_successful_pointer_write(self):
        self.new_case()

        def activate(*args, **options):
            self.activate(*args, **options)
            return None

        report, owner = self.run_cli(side_effect=activate)
        owner.assert_called_once()
        self.assert_unconfirmed(report, True)
        with locked_state(self.config) as store:
            self.assertEqual(store.read("active.json"), {"revision": REVISION})
        receipt = self.activation_receipt()
        self.assertEqual(receipt["status"], "started")
        self.assertIs(receipt["active_changed"], False)

    def test_failure_before_activation_preserves_no_active_change(self):
        for error_number in (errno.ENOSPC, errno.EROFS):
            with self.subTest(error_number=error_number):
                self.new_case()
                report, owner = self.run_cli(
                    side_effect=OSError(error_number, PRIVATE, str(self.root))
                )
                owner.assert_called_once()
                self.assertIs(report["active_changed"], False)
                self.assertFalse((self.config.state_root / "active.json").exists())
                receipt = self.activation_receipt()
                self.assertEqual(receipt["status"], "failed")
                self.assertIs(receipt["active_changed"], False)

    def test_started_receipt_failure_cannot_invoke_activation(self):
        original = StateStore.write
        for error_number in (errno.ENOSPC, errno.EROFS):
            with self.subTest(error_number=error_number):
                self.new_case()

                def write(store, name, value, *, replace=False):
                    if (
                        name.startswith("op-")
                        and value.get("operation") == "activate-gateway"
                    ):
                        raise OSError(error_number, PRIVATE, str(self.root))
                    return original(store, name, value, replace=replace)

                with patch.object(StateStore, "write", new=write):
                    report, owner = self.run_cli(side_effect=self.activate)
                owner.assert_not_called()
                self.assertIs(report["active_changed"], False)
                self.assertFalse((self.config.state_root / "active.json").exists())
                with locked_state(self.config) as store:
                    self.assertEqual(
                        [row["operation"] for row in operation_receipts(store)],
                        ["sync"],
                    )


if __name__ == "__main__":
    unittest.main()
