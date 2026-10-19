"""Exercise the shared runtime client without starting a Docker process."""

from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from search.operations.deployment import image_build, inference, runtime_docker


ROOT = Path(__file__).resolve().parents[3]


class RuntimeDockerTests(unittest.TestCase):
    def test_runtime_client_owns_error_and_build_client_stays_separate(self):
        self.assertIs(inference.InferenceError, runtime_docker.InferenceError)
        self.assertEqual("InferenceError", runtime_docker.InferenceError.__name__)
        self.assertIsNot(runtime_docker.Docker, image_build._Docker)

    def test_fresh_import_does_not_load_controllers_or_model_dependencies(self):
        script = """
import subprocess
import sys
sys.path.insert(0, sys.argv[1])
def forbidden_process(*args, **kwargs):
    raise AssertionError("runtime client import started a process")
subprocess.Popen = forbidden_process
from search.operations.deployment import runtime_docker
for name in (
    "search.operations.deployment.inference",
    "search.operations.deployment.release",
    "search.operations.deployment.gateway",
    "search.operations.deployment.gateway.activation",
    "search.operations.deployment.inference_runtime",
    "search.operations.deployment.gpu_ownership",
    "scripts.prepare_search_models",
    "torch", "fastapi", "django", "rest_framework", "adrf", "pydantic",
):
    assert not any(
        loaded == name or loaded.startswith(name + ".") for loaded in sys.modules
    ), name
assert runtime_docker.MAX_COMMANDS == 64
"""
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script, str(ROOT)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_default_runner_and_clock_keep_legacy_module_patch_lookup(self):
        result = subprocess.CompletedProcess([], 0, "ok", "")
        with (
            mock.patch.object(inference.time, "monotonic", side_effect=[10, 20, 30]),
            mock.patch.object(
                inference.image_build, "_run", return_value=result
            ) as run,
        ):
            client = runtime_docker.Docker(Path("/private-client"), None)
            self.assertEqual(1810, client.deadline)
            self.assertEqual(b"ok", client.call(["info"]))
        run.assert_called_once_with(
            [
                "docker",
                "--host",
                "unix:///var/run/docker.sock",
                "--config",
                "/private-client",
                "info",
            ],
            timeout=30,
            environment={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
        )

    def test_combined_streams_are_charged_to_the_shared_cumulative_limit(self):
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess([], 0, b"ok", b"xx")
        )
        client = runtime_docker.Docker(Path("/private-client"), runner)
        with (
            mock.patch.object(image_build, "MAX_OUTPUT_BYTES", 4),
            mock.patch.object(image_build, "MAX_TOTAL_OUTPUT_BYTES", 6),
        ):
            self.assertEqual(b"ok", client.call(["info"]))
            with self.assertRaisesRegex(
                runtime_docker.InferenceError, "^inference_output_limit$"
            ):
                client.call(["info"])
        self.assertEqual(8, client.total_bytes)
        self.assertEqual(2, runner.call_count)

    def test_inspect_preserves_shared_parser_and_runtime_shape_error(self):
        runner = mock.Mock()
        client = runtime_docker.Docker(Path("/private-client"), runner)
        for raw, expected, code in (
            (
                b'{"Id":"sha256:first","Id":"sha256:second"}',
                image_build.ImageBuildError,
                "image_build_json_invalid",
            ),
            (
                b"[]",
                runtime_docker.InferenceError,
                "inference_image_inspection_invalid",
            ),
        ):
            with self.subTest(raw=raw):
                runner.return_value = subprocess.CompletedProcess([], 0, raw, b"")
                with self.assertRaisesRegex(expected, "^" + code + "$"):
                    client.inspect("private-reference")
                self.assertEqual(
                    ["image", "inspect", "--format", "{{json .}}", "private-reference"],
                    runner.call_args.args[0][5:],
                )
