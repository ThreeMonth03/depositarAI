"""Exercise the shared runtime client without starting a Docker process."""

from pathlib import Path
import json
import subprocess
import sys
import unittest
from unittest import mock

from search.operations.deployment import image_build, inference, runtime_docker


ROOT = Path(__file__).resolve().parents[3]


class RuntimeDockerTests(unittest.TestCase):
    def test_explicit_platform_inspection_preserves_default_call_shape(self):
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess([], 0, b'{"Id":"fixture"}', b"")
        )
        client = runtime_docker.Docker("/private-client", runner)
        client.inspect("fixture-tag", platform="linux/amd64")
        self.assertEqual(
            [
                "image",
                "inspect",
                "--platform",
                "linux/amd64",
                "--format",
                "{{json .}}",
                "fixture-tag",
            ],
            runner.call_args.args[0][5:],
        )
        for value in ("", "linux/arm64", "linux/amd64 --remote", True):
            with (
                self.subTest(value=value),
                self.assertRaises(runtime_docker.InferenceError),
            ):
                client.inspect("fixture-tag", platform=value)
        self.assertEqual(runner.call_count, 1)

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
client = runtime_docker.Docker(
    "/private-client",
    lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
)
assert runtime_docker.project_inventory(client, "private-project") == ([], set())
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

    def test_default_runner_and_clock_use_the_shared_client_owners(self):
        result = subprocess.CompletedProcess([], 0, "ok", "")
        with (
            mock.patch.object(
                runtime_docker.time, "monotonic", side_effect=[10, 20, 30]
            ),
            mock.patch.object(image_build, "_run", return_value=result) as run,
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


class ProjectInventoryTests(unittest.TestCase):
    def client(self, containers, networks, *, max_commands=64):
        runner = mock.Mock(
            side_effect=[
                subprocess.CompletedProcess([], 0, output, b"")
                for output in (containers, networks)
            ]
        )
        return (
            runtime_docker.Docker("/private-client", runner, max_commands=max_commands),
            runner,
        )

    def test_inventory_uses_only_filtered_read_commands_and_shared_budget(self):
        ids = [letter * 64 for letter in "abc"]
        names = ["private-project_first", "private-project_second"]
        networks = b"\n".join(json.dumps({"Name": name}).encode() for name in names)
        client, runner = self.client(("\n".join(ids) + "\n").encode(), networks)
        self.assertEqual(
            runtime_docker.project_inventory(client, "private-project"),
            (ids, set(names)),
        )
        self.assertEqual(client.calls, 2)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(
            [call.args[0][5:] for call in runner.call_args_list],
            [
                [
                    "ps",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    "label=com.docker.compose.project=private-project",
                ],
                [
                    "network",
                    "ls",
                    "--no-trunc",
                    "--format",
                    "{{json .}}",
                    "--filter",
                    "name=private-project_",
                ],
            ],
        )

    def test_invalid_container_inventory_stops_before_network_query(self):
        cases = [
            b"\xff",
            b"short\n",
            b"A" * 64,
            b" " + b"a" * 64,
            (b"a" * 64 + b"\n") * 2,
            "\n".join(letter * 64 for letter in "abcd").encode(),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                client, runner = self.client(raw, b"")
                with self.assertRaisesRegex(
                    runtime_docker.InferenceError,
                    "^inference_container_inventory_invalid$",
                ):
                    runtime_docker.project_inventory(client, "private-project")
                self.assertEqual(runner.call_count, 1)

    def test_invalid_network_inventory_has_a_closed_diagnostic(self):
        cases = [
            [None],
            [[]],
            [{}],
            [{"Name": 1}],
            [{"Name": []}],
            [{"Name": {}}],
            [{"Name": "same"}, {"Name": "same"}],
            [{"Name": name} for name in ("first", "second", "third")],
        ]
        for values in cases:
            with self.subTest(values=values):
                raw = b"\n".join(json.dumps(value).encode() for value in values)
                client, runner = self.client(b"", raw)
                with self.assertRaisesRegex(
                    runtime_docker.InferenceError,
                    "^inference_network_inventory_invalid$",
                ):
                    runtime_docker.project_inventory(client, "private-project")
                self.assertEqual(runner.call_count, 2)

    def test_duplicate_json_keys_still_use_the_strict_shared_parser(self):
        client, _ = self.client(b"", b'{"Name":"first","Name":"second"}')
        with self.assertRaisesRegex(
            image_build.ImageBuildError, "^image_build_json_invalid$"
        ):
            runtime_docker.project_inventory(client, "private-project")

    def test_exhausted_budget_prevents_network_process_creation(self):
        client, runner = self.client(b"", b"", max_commands=1)
        with self.assertRaisesRegex(
            runtime_docker.InferenceError, "^inference_command_budget$"
        ):
            runtime_docker.project_inventory(client, "private-project")
        self.assertEqual(client.calls, 2)
        self.assertEqual(runner.call_count, 1)
