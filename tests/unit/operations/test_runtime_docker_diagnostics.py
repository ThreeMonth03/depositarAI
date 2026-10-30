"""Closed Docker diagnostics cross the real CLI/receipt boundary without Docker."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from search.operations import cli, lifecycle
from search.operations.config import GPUConfig, ProductConfig
from search.operations.deployment import inference, runtime_docker
from search.operations.deployment.images import build as image_build
from search.operations.state import (
    ActivationOutcomeError,
    locked_state,
    operation_receipts,
)


ROOT = Path(__file__).resolve().parents[3]
REVISION = "a" * 40
PRIVATE = "private-docker-output-credential-must-never-be-printed"
COMMANDS = {
    "up-private",
    "start-inference",
    "start-release",
    "prepare-gateway",
    "activate-gateway",
}
CODES = {
    "inference_command_budget_invalid",
    "inference_client_guard_invalid",
    "inference_command_budget",
    "inference_command_invalid",
    "inference_output_limit",
    "inference_command_timeout",
    "inference_command_failed",
    "inference_image_inspection_invalid",
    "inference_container_inventory_invalid",
    "inference_network_inventory_invalid",
}


class RuntimeDockerDiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-docker-diagnostic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case_number = 0

    def invoke(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = cli.main(arguments)
        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(PRIVATE, errors.getvalue())
        self.assertNotIn(str(self.root), errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        return json.loads(errors.getvalue())

    def assert_failure(self, report, *, error_type="InferenceError", code=None):
        expected = {
            "schema_version": 1,
            "status": "failed",
            "code": "product_operation_failed",
            "error_type": error_type,
            "active_changed": False,
        }
        if code is not None:
            expected["error_code"] = code
        self.assertEqual(report, expected)
        self.assertIs(report["active_changed"], False)

    def lifecycle_failure(self, action, expected):
        """Keep CLI dispatch, private state, locking and failed receipts real."""
        self.case_number += 1
        config = ProductConfig(
            2,
            self.root / f"product-{self.case_number}",
            self.root / "api-models",
            self.root / "embedding-models",
            self.root / "reranker-models",
            GPUConfig("GPU-a", ("GPU-b", "GPU-c"), "GPU-d"),
        )
        with locked_state(config, create=True):
            pass

        def fail(value, store, revision, **options):
            self.assertIs(value, config)
            self.assertEqual(revision, REVISION)
            self.assertEqual(options["repo_root"], cli.ROOT)
            with self.assertRaisesRegex(ValueError, "^product_operation_busy$"):
                with locked_state(config):
                    self.fail("native owner ran without its operation lock")
            action()
            self.fail("Docker fixture did not fail")

        with (
            patch("search.operations.config.load_config", return_value=config),
            patch.object(lifecycle, "revision", return_value=REVISION),
            patch.object(
                inference, "start_private_inference", side_effect=fail
            ) as owner,
            patch.object(
                subprocess, "Popen", side_effect=AssertionError("process started")
            ),
        ):
            report = self.invoke(
                ["--config", str(self.root / "private.json"), "start-inference"]
            )
        owner.assert_called_once()
        self.assert_failure(report, code=expected)
        with locked_state(config) as store:
            receipts = operation_receipts(store)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["operation"], "start-inference")
        self.assertEqual(receipts[0]["source_revision"], REVISION)
        self.assertEqual(receipts[0]["status"], "failed")
        self.assertEqual(receipts[0]["error_type"], "InferenceError")
        self.assertIs(receipts[0]["active_changed"], False)
        self.assertFalse((config.state_root / "active.json").exists())

    def client(self, *, outputs=None, **options):
        if outputs is None:
            outputs = [subprocess.CompletedProcess([], 0, b"", b"")]
        runner = Mock(side_effect=outputs)
        return runtime_docker.Docker(self.root / PRIVATE, runner, **options), runner

    def test_invalid_client_configuration_is_a_fixed_diagnostic(self):
        for options, expected in (
            ({"max_commands": True}, "inference_command_budget_invalid"),
            ({"max_commands": 0}, "inference_command_budget_invalid"),
            ({"verify_client": PRIVATE}, "inference_client_guard_invalid"),
        ):
            with self.subTest(options=options):
                runner = Mock(side_effect=AssertionError("runner must not execute"))
                self.lifecycle_failure(
                    lambda: runtime_docker.Docker(
                        self.root / PRIVATE, runner, **options
                    ),
                    expected,
                )
                runner.assert_not_called()

    def test_exhausted_command_count_does_not_retry_the_runner(self):
        client, runner = self.client(max_commands=1)

        def exhaust():
            self.assertEqual(client.call(["info"]), b"")
            client.call(["info"])

        self.lifecycle_failure(exhaust, "inference_command_budget")
        self.assertEqual(runner.call_count, 1)

    def test_expired_total_budget_stops_before_a_command(self):
        client, runner = self.client(deadline=0)
        with patch.object(runtime_docker.time, "monotonic", return_value=1):
            self.lifecycle_failure(
                lambda: client.call(["info"]), "inference_command_budget"
            )
        runner.assert_not_called()

    def test_command_finishing_after_deadline_has_a_fixed_timeout(self):
        client, runner = self.client(deadline=100)
        with patch.object(runtime_docker.time, "monotonic", side_effect=[0, 101]):
            self.lifecycle_failure(
                lambda: client.call(["info"]), "inference_command_timeout"
            )
        self.assertEqual(runner.call_count, 1)

    def test_raw_output_and_cumulative_output_limits_do_not_print_bytes(self):
        for cumulative in (False, True):
            with self.subTest(cumulative=cumulative):
                outputs = [subprocess.CompletedProcess([], 0, PRIVATE.encode(), b"")]
                if cumulative:
                    outputs *= 2
                client, runner = self.client(outputs=outputs)

                def overflow():
                    if cumulative:
                        self.assertEqual(client.call(["info"]), PRIVATE.encode())
                    client.call(["info"])

                with (
                    patch.object(
                        image_build,
                        "MAX_OUTPUT_BYTES",
                        len(PRIVATE) if cumulative else len(PRIVATE) - 1,
                    ),
                    patch.object(image_build, "MAX_TOTAL_OUTPUT_BYTES", len(PRIVATE)),
                ):
                    self.lifecycle_failure(overflow, "inference_output_limit")
                self.assertEqual(runner.call_count, 2 if cumulative else 1)

    def test_invalid_runner_result_does_not_print_its_stream_values(self):
        for result in (
            subprocess.CompletedProcess([], True, PRIVATE, PRIVATE),
            subprocess.CompletedProcess([], 0, [PRIVATE], b""),
            subprocess.CompletedProcess([], 0, b"", {PRIVATE: PRIVATE}),
        ):
            with self.subTest(result_type=type(result.stdout).__name__):
                client, runner = self.client(outputs=[result])
                self.lifecycle_failure(
                    lambda: client.call(["info"]), "inference_command_invalid"
                )
                self.assertEqual(runner.call_count, 1)

    def test_failed_commands_keep_process_details_private(self):
        for result in (
            subprocess.CompletedProcess([], 1, PRIVATE, PRIVATE),
            OSError(PRIVATE),
            subprocess.TimeoutExpired(PRIVATE, 30, output=PRIVATE, stderr=PRIVATE),
        ):
            with self.subTest(result_type=type(result).__name__):
                client, runner = self.client(outputs=[result])
                self.lifecycle_failure(
                    lambda: client.call(["info"]),
                    "inference_command_timeout"
                    if isinstance(result, subprocess.TimeoutExpired)
                    else "inference_command_failed",
                )
                self.assertEqual(runner.call_count, 1)

    def test_non_object_image_report_has_a_fixed_diagnostic(self):
        client, runner = self.client(
            outputs=[subprocess.CompletedProcess([], 0, b"[]", PRIVATE)]
        )
        self.lifecycle_failure(
            lambda: client.inspect(PRIVATE), "inference_image_inspection_invalid"
        )
        self.assertEqual(runner.call_count, 1)

    def test_container_inventory_fails_before_querying_networks(self):
        client, runner = self.client(
            outputs=[subprocess.CompletedProcess([], 0, PRIVATE, b"")]
        )
        self.lifecycle_failure(
            lambda: runtime_docker.project_inventory(client, PRIVATE),
            "inference_container_inventory_invalid",
        )
        self.assertEqual(runner.call_count, 1)

    def test_network_inventory_has_a_fixed_diagnostic(self):
        client, runner = self.client(
            outputs=[
                subprocess.CompletedProcess([], 0, b"", PRIVATE),
                subprocess.CompletedProcess([], 0, b'{"Name":123}', PRIVATE),
            ]
        )
        self.lifecycle_failure(
            lambda: runtime_docker.project_inventory(client, PRIVATE),
            "inference_network_inventory_invalid",
        )
        self.assertEqual(runner.call_count, 2)

    def test_fixed_codes_are_scoped_to_private_runtime_commands(self):
        for command in COMMANDS:
            for code in CODES:
                with self.subTest(command=command, code=code):
                    with patch.object(
                        cli,
                        "_runtime_report",
                        side_effect=runtime_docker.InferenceError(code),
                    ):
                        report = self.invoke(
                            ["--config", "/unused/product.json", command]
                        )
                    self.assert_failure(report, code=code)

    def test_other_runtime_commands_cannot_gain_private_runtime_diagnostics(self):
        for command in (
            "doctor",
            "status",
            "sync",
            "stage-catalog",
            "build-vectors",
            "build-images",
            "build-catalog-images",
            "prepare-secrets",
            "prepare-query",
            "prepare-index",
            "start-index",
            "import-index",
            "reconcile-index",
            "package-product",
        ):
            with self.subTest(command=command):
                options = (
                    ["--destination", "/unused/package", "--scanner", "/unused/scanner"]
                    if command == "package-product"
                    else []
                )
                with patch.object(
                    cli,
                    "_runtime_report",
                    side_effect=runtime_docker.InferenceError(
                        "inference_command_failed"
                    ),
                ):
                    report = self.invoke(
                        ["--config", "/unused/product.json", command, *options]
                    )
                self.assert_failure(report)

    def test_public_and_source_commands_do_not_gain_private_runtime_diagnostics(self):
        from search.operations.deployment import product_restore
        from search.operations.deployment.public_entry import host, updates

        for command, owner, action, options in (
            (
                "restore-product",
                product_restore,
                "restore_product",
                [
                    "--package",
                    "/unused/package",
                    "--expected-revision",
                    REVISION,
                    "--expected-manifest-sha256",
                    "b" * 64,
                    "--destination",
                    "/unused/restore",
                ],
            ),
            (
                "prepare-public-entry",
                host,
                "prepare_public_entry",
                [
                    "--entry-config",
                    "/unused/entry.json",
                    "--release-compose",
                    "/unused/release.json",
                    "--web-image",
                    "fixture",
                    "--web-nginx-image",
                    "fixture",
                ],
            ),
            (
                "up-public-entry",
                host,
                "up_public_entry",
                [
                    "--entry-config",
                    "/unused/entry.json",
                    "--release-compose",
                    "/unused/release.json",
                    "--web-image",
                    "fixture",
                    "--web-nginx-image",
                    "fixture",
                ],
            ),
            (
                "update-public-entry",
                updates,
                "update_public_entry",
                [
                    "--entry-config",
                    "/unused/entry.json",
                    "--release-compose",
                    "/unused/release.json",
                    "--web-image",
                    "fixture",
                    "--web-nginx-image",
                    "fixture",
                ],
            ),
            (
                "rollback-public-entry",
                updates,
                "rollback_public_entry",
                ["--entry-config", "/unused/entry.json"],
            ),
        ):
            with self.subTest(command=command):
                with patch.object(
                    owner,
                    action,
                    side_effect=runtime_docker.InferenceError(
                        "inference_command_failed"
                    ),
                ):
                    report = self.invoke([command, *options])
                self.assert_failure(report)

    def test_foreign_or_private_error_values_cannot_forge_a_diagnostic(self):
        reads = []

        class PrivateValue:
            def __str__(self):
                reads.append("str")
                raise AssertionError(PRIVATE)

        class PrivateString(str):
            def __eq__(self, other):
                reads.append("eq")
                raise AssertionError(PRIVATE)

            def __hash__(self):
                reads.append("hash")
                raise AssertionError(PRIVATE)

        forged = type(
            "InferenceError",
            (ValueError,),
            {"__module__": "search.operations.deployment.runtime_docker"},
        )
        derived = type(
            "Derived",
            (runtime_docker.InferenceError,),
            {"__module__": "search.operations.deployment.runtime_docker"},
        )
        foreign = type("Foreign", (ValueError,), {})
        foreign.__module__ = PrivateString(
            "search.operations.deployment.runtime_docker"
        )
        errors = (
            forged("inference_command_failed"),
            derived("inference_command_failed"),
            foreign("inference_command_failed"),
            runtime_docker.InferenceError(PRIVATE),
            runtime_docker.InferenceError("inference_command_failed", PRIVATE),
            runtime_docker.InferenceError(),
            runtime_docker.InferenceError(PrivateString("inference_command_failed")),
            runtime_docker.InferenceError(PrivateValue()),
        )
        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                with patch.object(cli, "_runtime_report", side_effect=error):
                    report = self.invoke(
                        ["--config", "/unused/product.json", "start-inference"]
                    )
                self.assert_failure(report, error_type=type(error).__name__)
        self.assertEqual(reads, [])

    def test_wrapped_errors_do_not_recover_a_diagnostic_from_private_context(self):
        for attribute in ("__cause__", "__context__"):
            with self.subTest(attribute=attribute):
                error = ValueError(PRIVATE)
                setattr(
                    error,
                    attribute,
                    runtime_docker.InferenceError("inference_command_failed"),
                )
                with patch.object(cli, "_runtime_report", side_effect=error):
                    report = self.invoke(
                        ["--config", "/unused/product.json", "start-inference"]
                    )
                self.assert_failure(report, error_type="ValueError")

    def test_activation_recovery_preserves_true_or_unknown_outcome_with_runtime_cause(
        self,
    ):
        for changed in (True, None):
            for command in ("activate-gateway", "up-private"):
                with self.subTest(changed=changed, command=command):
                    error = ActivationOutcomeError(active_changed=changed)
                    error.__cause__ = runtime_docker.InferenceError(
                        "inference_command_failed"
                    )
                    with patch.object(cli, "_runtime_report", side_effect=error):
                        report = self.invoke(
                            ["--config", "/unused/product.json", command]
                        )
                    self.assertEqual(report["schema_version"], 1)
                    self.assertEqual(report["status"], "recovery_required")
                    self.assertEqual(report["code"], "product_operation_failed")
                    self.assertEqual(
                        report["error_code"], "product_activation_outcome_unconfirmed"
                    )
                    self.assertIs(report["active_changed"], changed)
                    self.assertIs(report["activation_performed"], changed)
                    self.assertIs(report["runtime_verified"], False)
                    self.assertIs(report["confers_runtime_authority"], False)

    def test_cli_failure_does_not_import_runtime_controllers_or_frameworks(self):
        program = """
import contextlib
import importlib.abc
import io
import json
import sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
blocked = (
    'search.operations.deployment.inference',
    'search.operations.deployment.release',
    'search.operations.deployment.gateway',
    'search.operations.deployment.gpu_ownership',
    'torch', 'django', 'rest_framework', 'adrf', 'pydantic',
)
class BlockImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('unnecessary runtime dependency')
sys.meta_path.insert(0, BlockImports())
from search.operations import cli
from search.operations.deployment import runtime_docker
def forbidden_process(*args, **kwargs):
    raise AssertionError('CLI diagnostic started a process')
output, errors = io.StringIO(), io.StringIO()
with patch('subprocess.Popen', forbidden_process), patch.object(
    cli, '_runtime_report', side_effect=runtime_docker.InferenceError('inference_command_timeout')
), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
    code = cli.main(['--config', '/unused/product.json', 'start-inference'])
assert code == 1 and not output.getvalue()
report = json.loads(errors.getvalue())
assert report['error_code'] == 'inference_command_timeout'
assert not any(name in sys.modules for name in blocked)
print(json.dumps(report))
"""
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", program, str(ROOT)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assert_failure(json.loads(result.stdout), code="inference_command_timeout")
