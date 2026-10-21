"""Native runtime proof failures remain closed at the CLI and receipt boundary."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from search.operations import cli, lifecycle
from search.operations.catalog import policy
from search.operations.deployment import composition, inference, inference_runtime
from search.operations.deployment.runtime_docker import InferenceError
from search.operations.state import locked_state, operation_receipts
from tests.unit.deployment.test_composition import REVISION, product
from tests.unit.deployment.test_inference_runtime import images, snapshot


PRIVATE = "private-runtime-snapshot-must-never-be-printed"
COMMANDS = (
    "up-private",
    "start-inference",
    "start-release",
    "prepare-gateway",
    "activate-gateway",
)
OWNERS = (
    (
        policy.RuntimeError,
        (
            "catalog_runtime_not_healthy",
            "catalog_requires_exactly_three_services",
            "catalog_runtime_receipt_changed",
        ),
    ),
    (
        inference_runtime.InferenceRuntimeError,
        (
            "inference_runtime_resource_or_health_changed",
            "inference_runtime_network_peers_changed",
            "inference_runtime_snapshot_invalid",
        ),
    ),
    (
        InferenceError,
        (
            "inference_gpu_ownership_blocked",
            "inference_partial_state_recovery_required",
            "inference_completed_runtime_changed",
            "inference_start_identity_changed",
        ),
    ),
)


class RuntimePolicyDiagnosticTests(unittest.TestCase):
    def invoke(self, arguments, *, error=None):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            if error is None:
                code = cli.main(arguments)
            else:
                with patch.object(cli, "_runtime_report", side_effect=error):
                    code = cli.main(arguments)
        self.assertEqual(1, code)
        self.assertEqual("", output.getvalue())
        self.assertNotIn(PRIVATE, errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        return json.loads(errors.getvalue())

    def failure(self, error, command="start-inference", *, expected=None):
        report = self.invoke(["--config", "/unused/product.json", command], error=error)
        wanted = {
            "schema_version": 1,
            "status": "failed",
            "code": "product_operation_failed",
            "error_type": type(error).__name__,
            "active_changed": False,
        }
        if expected is not None:
            wanted["error_code"] = expected
        self.assertEqual(wanted, report)

    def test_fixed_native_codes_cross_the_cli_for_each_private_command(self):
        for owner, codes in OWNERS:
            for code in codes:
                for command in COMMANDS:
                    with self.subTest(owner=owner.__name__, code=code, command=command):
                        self.failure(owner(code), command, expected=code)

    def test_native_gpu_rejection_exposes_only_the_fixed_owner_diagnostic(self):
        with patch.object(
            inference.gpu_ownership,
            "run_gpu_ownership",
            return_value={
                "schema_version": 1,
                "status": "blocked",
                "code": "gpu_ownership_host_access_mount",
                "private_snapshot": PRIVATE,
            },
        ) as preflight:
            try:
                inference._gpu(product(), (), None)
            except InferenceError as error:
                self.failure(error, expected="inference_gpu_ownership_blocked")
            else:
                self.fail("blocked ownership must stop inference")
        preflight.assert_called_once()

    def test_catalog_diagnostics_keep_existing_command_scope(self):
        for code in OWNERS[0][1]:
            for command in ("doctor", "status", "start-index", "reconcile-index"):
                with self.subTest(code=code, command=command):
                    self.failure(policy.RuntimeError(code), command, expected=code)

    def test_inference_diagnostics_do_not_cross_other_commands(self):
        for owner, codes in OWNERS[1:]:
            for code in codes:
                for command in (
                    "doctor",
                    "status",
                    "start-index",
                    "reconcile-index",
                    "build-vectors",
                    "stage-catalog",
                    "prepare-query",
                ):
                    with self.subTest(owner=owner.__name__, code=code, command=command):
                        self.failure(owner(code), command)

    def test_unknown_derived_forged_and_nonliteral_arguments_are_not_diagnostics(self):
        class SecretText(str):
            def __str__(self):
                raise AssertionError("must not convert a string subclass")

        for owner, codes in OWNERS:
            derived = type("Derived", (owner,), {"__module__": owner.__module__})
            forged = type(
                owner.__name__, (ValueError,), {"__module__": owner.__module__}
            )
            for error in (
                owner(PRIVATE),
                owner(codes[0], PRIVATE),
                owner(),
                owner(SecretText(codes[0])),
                owner({PRIVATE: codes[0]}),
                derived(codes[0]),
                forged(codes[0]),
            ):
                with self.subTest(
                    owner=owner.__name__, error_type=type(error).__name__
                ):
                    self.failure(error)

    def test_native_exception_stringification_is_never_used(self):
        for owner, codes in OWNERS:
            with (
                self.subTest(owner=owner.__name__),
                patch.object(
                    owner, "__str__", side_effect=AssertionError("no conversion")
                ),
            ):
                self.failure(owner(codes[0]), expected=codes[0])

    def test_a_known_code_from_another_owner_is_not_authority(self):
        for owner, _codes in OWNERS:
            for other, codes in OWNERS:
                if owner is not other:
                    with self.subTest(owner=owner.__name__, code=codes[0]):
                        self.failure(owner(codes[0]))

    def test_causes_and_contexts_do_not_grant_diagnostic_authority(self):
        for owner, codes in OWNERS:
            for attribute in ("__cause__", "__context__"):
                wrapper = ValueError(PRIVATE)
                setattr(wrapper, attribute, owner(codes[0]))
                with self.subTest(owner=owner.__name__, attribute=attribute):
                    self.failure(wrapper)

    def test_stopped_native_snapshot_fails_inside_real_locked_lifecycle_operation(self):
        with tempfile.TemporaryDirectory(
            prefix="depositar-runtime-proof-"
        ) as temporary:
            root = Path(temporary)
            config = replace(product(), state_root=root / "product")
            compose = composition._inference(
                config, REVISION, 1000, 1000, inference_source_sha256="6" * 64
            )
            image_rows = images()
            for name, image in image_rows.items():
                compose["services"][name]["image"] = image["Id"]
            containers, networks = snapshot(compose, image_rows)
            inference_runtime.validate_runtime(
                compose, image_rows, containers, networks
            )
            containers[0]["State"].update(Running=False, Pid=0)
            containers[0]["Config"]["Env"] = [
                "PRIVATE_SENTINEL=" + PRIVATE
                if entry.startswith("PRIVATE_SENTINEL=")
                else entry
                for entry in containers[0]["Config"]["Env"]
            ]
            with locked_state(config, create=True):
                pass

            def stopped(value, store, revision, **options):
                self.assertIs(value, config)
                self.assertEqual(REVISION, revision)
                self.assertEqual(cli.ROOT, options["repo_root"])
                with self.assertRaisesRegex(ValueError, "^product_operation_busy$"):
                    with locked_state(config):
                        self.fail("runtime owner executed without its product lock")
                return inference_runtime.validate_runtime(
                    compose, image_rows, containers, networks
                )

            with (
                patch("search.operations.config.load_config", return_value=config),
                patch.object(lifecycle, "revision", return_value=REVISION),
                patch.object(
                    inference, "start_private_inference", side_effect=stopped
                ) as owner,
                patch.object(
                    subprocess, "Popen", side_effect=AssertionError("no processes")
                ),
            ):
                report = self.invoke(
                    ["--config", str(root / "private.json"), "start-inference"]
                )
            owner.assert_called_once()
            self.assertEqual(
                "inference_runtime_resource_or_health_changed", report["error_code"]
            )
            self.assertIs(report["active_changed"], False)
            self.assertNotIn(str(root), json.dumps(report))
            with locked_state(config) as store:
                receipts = operation_receipts(store)
            self.assertEqual(1, len(receipts))
            receipt = receipts[0]
            self.assertEqual("start-inference", receipt["operation"])
            self.assertEqual(REVISION, receipt["source_revision"])
            self.assertEqual("failed", receipt["status"])
            self.assertEqual("InferenceRuntimeError", receipt["error_type"])
            self.assertIs(receipt["active_changed"], False)
            self.assertNotIn(PRIVATE, json.dumps(receipt))
            self.assertFalse((config.state_root / "active.json").exists())
            self.assertFalse((config.state_root / "inference-candidate.json").exists())
