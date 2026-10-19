"""Offline native-profile/receipt tests; never run Docker, HTTP or a helper."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from search.operations.deployment.gateway import probe_runtime as probe
from search.operations.deployment import runtime_docker, source_bundle
from search.operations.shared import private_io
from tests.unit.deployment.release import test_probe as private
from tests.unit.deployment.test_source_bundle import fixture_sources, fixture_tree


RUN = "d" * 32
IMAGE_ID = "sha256:" + "e" * 64
CONTAINER_ID = "f" * 64
PRIVATE = "secret-http-body-env-token-canary"


def materials():
    sources = fixture_sources()
    plan = source_bundle.plan_source_bundle(
        private.REVISION, fixture_tree(sources), sources
    )
    image = {
        "Id": IMAGE_ID,
        "Config": {
            "Env": [
                "SOURCE_REVISION=" + private.REVISION,
                "PYTHONDONTWRITEBYTECODE=1",
                "PYTHONUNBUFFERED=1",
                "PATH=/usr/bin",
            ],
            "Labels": plan["services"]["web"]["required_labels"],
            "User": "1000:1000",
            "WorkingDir": "/app",
            "Entrypoint": None,
            "Cmd": ["uvicorn", "search.web.app:create_app", "--factory"],
            "ExposedPorts": {"8000/tcp": {}},
        },
    }
    proof = source_bundle.validate_built_image(plan, "web", IMAGE_ID, image)
    return plan, image, proof


def native_row(image, *, exited):
    command = [
        "-B",
        "-c",
        probe.SCRIPT,
        private.REVISION,
        private.BUNDLE,
        private.MANIFEST,
    ]
    return {
        "Id": CONTAINER_ID,
        "Name": "/depositar-ai-gateway-probe-" + RUN,
        "Image": IMAGE_ID,
        "Path": "python",
        "Args": command,
        "Config": {
            **image["Config"],
            "Image": IMAGE_ID,
            "User": "1000:1000",
            "Entrypoint": ["python"],
            "Cmd": command,
            "Env": image["Config"]["Env"]
            + ["NVIDIA_VISIBLE_DEVICES=void", "CUDA_VISIBLE_DEVICES="],
            "Labels": {
                **image["Config"]["Labels"],
                probe.LABEL + "run": RUN,
                probe.LABEL + "revision": private.REVISION,
            },
            "OpenStdin": False,
            "Tty": False,
            "Volumes": None,
            "Healthcheck": {"Test": ["NONE"]},
        },
        "HostConfig": {
            "NetworkMode": "host",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges:true"],
            "Init": True,
            "AutoRemove": False,
            "Runtime": "runc",
            "PidMode": "",
            "IpcMode": "private",
            "UTSMode": "",
            "UsernsMode": "",
            "CgroupnsMode": "private",
            "NanoCpus": 10**9,
            "Memory": 1024**3,
            "MemorySwap": 1024**3,
            "PidsLimit": 64,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "LogConfig": {"Type": "none", "Config": {}},
            "Ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 1024}],
            "Tmpfs": {"/tmp": probe.TMPFS.split(":", 1)[1]},
            "PortBindings": {},
            "Binds": None,
            "Mounts": None,
            "Devices": [],
            "DeviceRequests": [],
        },
        "NetworkSettings": {
            "Networks": {"host": {"NetworkID": "1" * 64 if exited else ""}},
            "Ports": {},
        },
        "Mounts": [],
        "State": {
            "Status": "exited" if exited else "created",
            "Running": False,
            "Paused": False,
            "OOMKilled": False,
            "ExitCode": 0,
        },
    }


class FakeDocker:
    def __init__(self, image):
        self.image = copy.deepcopy(image)
        self.created = native_row(image, exited=False)
        self.exited = native_row(image, exited=True)
        self.output = json.dumps(private.web_ready()).encode()
        self.calls, self.filters, self.inspect_count = [], {}, 0
        self.start_error, self.rm_error = None, None
        self.final_image = None
        self.callback = None

    def call(self, arguments, *, timeout):
        self.calls.append((copy.deepcopy(arguments), timeout))
        if self.callback:
            self.callback(arguments)
        if arguments[:2] == ["image", "inspect"]:
            image = (
                self.final_image
                if self.final_image is not None and len(self.calls) > 1
                else self.image
            )
            return json.dumps(image).encode()
        if arguments[0] == "ps":
            value = arguments[arguments.index("--filter") + 1]
            responses = self.filters.get(value, [])
            return responses.pop(0) if responses else b""
        if arguments[0] == "create":
            return (CONTAINER_ID + "\n").encode()
        if arguments[0] == "inspect":
            self.inspect_count += 1
            return json.dumps(
                self.created if self.inspect_count == 1 else self.exited
            ).encode()
        if arguments[0] == "start":
            if self.start_error:
                raise self.start_error
            return self.output
        if arguments[0] == "rm":
            if self.rm_error:
                raise self.rm_error
            return (CONTAINER_ID + "\n").encode()
        raise AssertionError("unexpected offline command")


class NativeGatewayProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        os.chmod(self.directory, 0o700)
        self.fd = private_io.open_directory(self.directory)
        self.addCleanup(os.close, self.fd)
        self.plan, self.image, self.proof = materials()
        self.docker = FakeDocker(self.image)

    def invoke(self, **overrides):
        values = {
            "source_plan": self.plan,
            "image_inspection": self.image,
            "image_proof": self.proof,
            "revision": private.REVISION,
            "bundle_id": private.BUNDLE,
            "manifest_sha256": private.MANIFEST,
            "directory_fd": self.fd,
            "directory_path": self.directory,
            "run_id": RUN,
        }
        values.update(overrides)
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                return probe.verify_gateway_from_image(self.docker, **values)
            finally:
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(errors.getvalue(), "")
                for name in os.listdir(self.fd):
                    if name.endswith(".json"):
                        raw = private_io.read_file(
                            self.fd, name, mode=0o600, max_bytes=16384
                        )
                        self.assertNotIn(PRIVATE.encode(), raw)
                        self.assertNotIn(b"http://", raw)
                        self.assertNotIn(probe.SCRIPT.encode(), raw)

    def failed(self, **overrides):
        with self.assertRaises(probe.GatewayProbeRuntimeError) as caught:
            self.invoke(**overrides)
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.assertFalse((self.directory / "complete.json").exists())

    def commands(self):
        return [arguments[0] for arguments, _ in self.docker.calls]

    def test_native_success_has_twelve_calls_fixed_profile_and_no_authority(self):
        result = self.invoke()
        self.assertEqual(result["website"], private.web_ready())
        self.assertTrue(result["gateway_http_verified"])
        self.assertEqual(
            result["probe_runtime"],
            {
                "container_id": CONTAINER_ID,
                "run_id": RUN,
                "helper_removed_verified": True,
                "host_network_verified": True,
            },
        )
        for flag in (
            "model_warmup_verified",
            "readiness_verified",
            "confers_runtime_authority",
            "active_changed",
            "search_quality_verified",
        ):
            self.assertIs(result[flag], False)
        self.assertEqual(len(self.docker.calls), 12)
        self.assertEqual(
            self.commands(),
            [
                "image",
                "ps",
                "ps",
                "create",
                "inspect",
                "start",
                "inspect",
                "rm",
                "ps",
                "ps",
                "ps",
                "image",
            ],
        )
        command = self.docker.calls[3][0]
        for flag, value in (
            ("--network", "host"),
            ("--pull", "never"),
            ("--entrypoint", "python"),
            ("--user", "1000:1000"),
            ("--runtime", "runc"),
            ("--memory", "1g"),
            ("--memory-swap", "1g"),
            ("--cpus", "1"),
            ("--pids-limit", "64"),
            ("--log-driver", "none"),
            ("--restart", "no"),
            ("--tmpfs", probe.TMPFS),
        ):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertIn("--no-healthcheck", command)
        self.assertNotIn("--mount", command)
        self.assertNotIn("--gpus", command)
        self.assertEqual(
            command[command.index(IMAGE_ID) :],
            [
                IMAGE_ID,
                "-B",
                "-c",
                probe.SCRIPT,
                private.REVISION,
                private.BUNDLE,
                private.MANIFEST,
            ],
        )
        self.assertEqual(self.docker.calls[5][1], 285)
        self.assertTrue(all(0 < timeout <= 285 for _, timeout in self.docker.calls))
        self.assertEqual(
            set(os.listdir(self.fd)), {"intent.json", "container.json", "complete.json"}
        )
        self.assertEqual(
            json.loads((self.directory / "complete.json").read_bytes())["report"],
            result,
        )

    def test_same_public_identity_cannot_reenter_completed_or_partial_directory(self):
        private_io.write_json(
            self.fd, "intent.json", {"status": "unknown"}, max_bytes=1024
        )
        self.failed()
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(set(os.listdir(self.fd)), {"intent.json"})

    def test_invalid_public_identity_or_source_proof_fails_before_daemon_or_write(self):
        for name, value in (
            ("run_id", "unsafe\n"),
            ("revision", "x" * 40),
            ("bundle_id", "x" * 64),
            ("manifest_sha256", True),
            ("image_proof", self.proof | {"extra": PRIVATE}),
            ("image_proof", self.proof | {"revision": "1" * 40}),
        ):
            with self.subTest(name=name, value=type(value).__name__):
                self.failed(**{name: value})
                self.assertEqual(self.docker.calls, [])
                self.assertEqual(os.listdir(self.fd), [])

    def test_existing_name_or_run_label_prevents_create(self):
        for value in (
            "name=depositar-ai-gateway-probe-" + RUN,
            "label=" + probe.LABEL + "run=" + RUN,
        ):
            with self.subTest(filter=value):
                docker = FakeDocker(self.image)
                docker.filters[value] = [(CONTAINER_ID + "\n").encode()]
                self.docker = docker
                self.failed()
                self.assertNotIn("create", self.commands())
                for name in os.listdir(self.fd):
                    os.unlink(name, dir_fd=self.fd)

    def test_native_profile_drift_never_starts_or_removes_created_helper(self):
        cases = [
            ("Id", "1" * 64),
            ("Image", "sha256:" + "1" * 64),
            ("Name", "/foreign"),
            ("Config.User", "0:0"),
            ("Config.Cmd", []),
            ("Config.Entrypoint", ["sh"]),
            ("Config.Healthcheck", {"Test": ["CMD", "curl", PRIVATE]}),
            (
                "Config.Env",
                self.image["Config"]["Env"] + ["NVIDIA_VISIBLE_DEVICES=all"],
            ),
            ("Config.Volumes", {"/private": {}}),
            ("Config.Tty", True),
            ("HostConfig.NetworkMode", "bridge"),
            ("HostConfig.ReadonlyRootfs", False),
            ("HostConfig.Privileged", True),
            ("HostConfig.Init", False),
            ("HostConfig.Runtime", "nvidia"),
            ("HostConfig.PidMode", "host"),
            ("HostConfig.IpcMode", "host"),
            ("HostConfig.CgroupnsMode", "host"),
            ("HostConfig.DeviceRequests", [{"Count": -1}]),
            ("HostConfig.CapAdd", ["SYS_ADMIN"]),
            ("HostConfig.SecurityOpt", []),
            ("HostConfig.Binds", ["/private:/private"]),
            ("HostConfig.Mounts", [{"Type": "bind"}]),
            ("HostConfig.VolumesFrom", ["foreign"]),
            ("HostConfig.PortBindings", {"8080/tcp": [{"HostPort": "18083"}]}),
            ("HostConfig.Memory", 2 * 1024**3),
            ("HostConfig.MemorySwap", -1),
            ("HostConfig.NanoCpus", 0),
            ("HostConfig.PidsLimit", 0),
            ("HostConfig.Ulimits", [{"Name": "nofile", "Soft": 65536, "Hard": 65536}]),
            ("HostConfig.LogConfig", {"Type": "json-file", "Config": {}}),
            ("HostConfig.RestartPolicy", {"Name": "always", "MaximumRetryCount": 0}),
            ("HostConfig.Tmpfs", {"/tmp": "rw"}),
            ("NetworkSettings.Networks", {"host": {}, "catalog": {}}),
            ("NetworkSettings.Ports", {"8000/tcp": [{"HostPort": "8000"}]}),
            ("Mounts", [{"Type": "bind", "Destination": "/secret", "RW": False}]),
            ("State.Status", "running"),
            ("State.Running", True),
            ("State.OOMKilled", True),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                self.docker = FakeDocker(self.image)
                target = self.docker.created
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                self.failed()
                self.assertNotIn("start", self.commands())
                self.assertNotIn("rm", self.commands())
                for name in os.listdir(self.fd):
                    os.unlink(name, dir_fd=self.fd)

    def test_timeout_running_or_unknown_exit_is_quarantined_without_kill_or_rm(self):
        self.docker.start_error = subprocess.TimeoutExpired(PRIVATE, 285)
        self.docker.exited["State"].update(Status="running", Running=True)
        self.failed()
        self.assertEqual(self.commands().count("start"), 1)
        self.assertNotIn("rm", self.commands())
        self.assertNotIn("kill", self.commands())
        self.assertEqual(
            set(os.listdir(self.fd)), {"intent.json", "container.json", "failure.json"}
        )

    def test_failed_start_with_confirmed_exact_exited_helper_is_removed_but_fails(self):
        self.docker.start_error = ValueError(PRIVATE)
        self.failed()
        self.assertEqual(self.commands().count("rm"), 1)
        self.assertTrue(
            json.loads((self.directory / "failure.json").read_bytes())[
                "helper_removed_verified"
            ]
        )

    def test_nonzero_native_exit_does_not_accept_even_ready_output(self):
        self.docker.exited["State"]["ExitCode"] = 1
        self.failed()
        self.assertEqual(self.commands().count("rm"), 1)

    def test_exited_profile_identity_or_capability_drift_prevents_removal(self):
        for field, value in (
            ("Image", "sha256:" + "1" * 64),
            ("Id", "1" * 64),
            ("HostConfig.NetworkMode", "bridge"),
            ("Config.Cmd", ["sh", "-c", PRIVATE]),
            ("State.ExitCode", False),
        ):
            with self.subTest(field=field):
                self.docker = FakeDocker(self.image)
                target = self.docker.exited
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                self.failed()
                self.assertEqual(self.commands().count("start"), 1)
                self.assertNotIn("rm", self.commands())
                for name in os.listdir(self.fd):
                    os.unlink(name, dir_fd=self.fd)

    def test_material_inputs_are_not_mutated(self):
        before = copy.deepcopy((self.plan, self.image, self.proof))
        self.invoke()
        self.assertEqual((self.plan, self.image, self.proof), before)

    def test_first_native_image_drift_is_rejected_before_create(self):
        self.docker.image["Config"]["Env"].append("UNEXPECTED=" + PRIVATE)
        self.failed()
        self.assertEqual(self.commands(), ["image"])

    def test_ambiguous_create_output_never_selects_or_removes_a_container(self):
        original = self.docker.call

        def call(arguments, *, timeout):
            result = original(arguments, timeout=timeout)
            return (
                result + CONTAINER_ID.encode() if arguments[0] == "create" else result
            )

        self.docker.call = call
        self.failed()
        self.assertNotIn("start", self.commands())
        self.assertNotIn("rm", self.commands())
        self.assertFalse((self.directory / "container.json").exists())

    def test_closed_output_contract_failures_clean_up_only_exact_exited_helper(self):
        outputs = [
            PRIVATE.encode(),
            b"{}",
            b'{"schema_version":1,"schema_version":1}',
            b'{"elapsed_ms":NaN}',
            b"x" * 4097,
            json.dumps(private.web_ready() | {"revision": "1" * 40}).encode(),
            json.dumps(
                private.web_ready() | {"session_reset_verified": False}
            ).encode(),
            json.dumps(private.web_ready() | {"extra": PRIVATE}).encode(),
        ]
        for output in outputs:
            with self.subTest(length=len(output)):
                self.docker = FakeDocker(self.image)
                self.docker.output = output
                self.failed()
                self.assertEqual(self.commands().count("rm"), 1)
                for name in os.listdir(self.fd):
                    os.unlink(name, dir_fd=self.fd)

    def test_rm_failure_or_fresh_presence_never_publishes_success(self):
        self.docker.rm_error = ValueError(PRIVATE)
        self.failed()
        self.assertEqual(self.commands().count("rm"), 1)
        self.assertFalse(
            json.loads((self.directory / "failure.json").read_bytes())[
                "helper_removed_verified"
            ]
        )

    def test_all_three_fresh_absence_views_are_required(self):
        filters = [
            "id=" + CONTAINER_ID,
            "name=depositar-ai-gateway-probe-" + RUN,
            "label=" + probe.LABEL + "run=" + RUN,
        ]
        for value in filters:
            with self.subTest(filter=value):
                self.docker = FakeDocker(self.image)
                self.docker.filters[value] = (
                    [b""] if not value.startswith("id=") else []
                ) + [CONTAINER_ID.encode()]
                self.failed()
                self.assertEqual(self.commands().count("rm"), 1)
                for name in os.listdir(self.fd):
                    os.unlink(name, dir_fd=self.fd)

    def test_image_config_and_source_proof_are_freshly_checked_at_both_boundaries(self):
        self.docker.final_image = copy.deepcopy(self.image)
        self.docker.final_image["Config"]["Cmd"] = ["sh", "-c", PRIVATE]
        self.failed()
        self.assertEqual(self.commands().count("rm"), 1)

    def test_receipt_edit_before_start_stops_without_mutating_created_helper(self):
        def edit(arguments):
            if arguments[0] == "inspect":
                (self.directory / "intent.json").write_bytes(b"{}\n")

        self.docker.callback = edit
        self.failed()
        self.assertNotIn("start", self.commands())
        self.assertNotIn("rm", self.commands())

    def test_replaced_retained_directory_cannot_receive_complete(self):
        moved = self.directory.with_name(self.directory.name + "-old")
        self.addCleanup(
            lambda: moved.rmdir()
            if moved.exists() and not list(moved.iterdir())
            else None
        )

        def replace(arguments):
            if arguments[0] == "start":
                self.directory.rename(moved)
                self.directory.mkdir(mode=0o700)

        self.docker.callback = replace
        self.failed()
        self.assertNotIn("rm", self.commands())
        for name in os.listdir(self.fd):
            os.unlink(name, dir_fd=self.fd)

    def test_overall_deadline_is_not_extended_for_cleanup(self):
        clock = [0.0]

        def advance(arguments):
            if arguments[0] == "start":
                clock[0] = probe.TOTAL_SECONDS + 1

        self.docker.callback = advance
        with mock.patch.object(probe.time, "monotonic", side_effect=lambda: clock[0]):
            self.failed()
        self.assertNotIn("rm", self.commands())

    def test_final_complete_receipt_cas_deadline_cannot_return_ready(self):
        clock = [0.0]
        original = private_io.read_file

        def read(parent_fd, name, **kwargs):
            result = original(parent_fd, name, **kwargs)
            if name == "complete.json":
                clock[0] = probe.TOTAL_SECONDS + 1
            return result

        with (
            mock.patch.object(probe.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(private_io, "read_file", side_effect=read),
            self.assertRaisesRegex(
                probe.GatewayProbeRuntimeError, "^gateway_probe_runtime_failed$"
            ),
        ):
            self.invoke()
        self.assertEqual(len(self.docker.calls), 12)
        self.assertEqual(self.commands().count("rm"), 1)
        self.assertEqual(
            set(os.listdir(self.fd)),
            {"intent.json", "container.json", "complete.json", "failure.json"},
        )
        failure = json.loads((self.directory / "failure.json").read_bytes())
        self.assertTrue(failure["helper_removed_verified"])

    def test_uses_real_bounded_docker_client_without_host_wire_model_import(self):
        fake = self.docker

        def runner(arguments, *, timeout, environment):
            self.assertEqual(
                arguments[:5],
                [
                    "docker",
                    "--host",
                    "unix:///var/run/docker.sock",
                    "--config",
                    "/private/client",
                ],
            )
            self.assertEqual(
                environment,
                {
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                },
            )
            return subprocess.CompletedProcess(
                arguments, 0, fake.call(arguments[5:], timeout=timeout), b""
            )

        self.docker = runtime_docker.Docker("/private/client", runner, max_commands=12)
        with mock.patch.dict(sys.modules, {"pydantic": None}):
            result = self.invoke()
        self.assertTrue(result["gateway_http_verified"])
        self.assertEqual(self.docker.calls, 12)


class FixedImageScriptTests(unittest.TestCase):
    def test_shared_script_preserves_strict_two_turn_refinement_reset_without_ops_import(
        self,
    ):
        contract = private.WebControlFlowTests()
        contract.setUp()
        contract.token, contract.replacement = "T" * 43, "R" * 43
        fixture = private.ScriptFixture(contract.responses())
        output = io.StringIO()
        with (
            mock.patch.dict(sys.modules, contract.modules),
            mock.patch("search.transport.http.UrllibTransport", return_value=fixture),
            mock.patch(
                "sys.argv",
                ["probe", private.REVISION, private.BUNDLE, private.MANIFEST],
            ),
            mock.patch.object(probe.time, "monotonic", return_value=1.0),
            redirect_stdout(output),
        ):
            exec(probe.SCRIPT, {})
        result = private.probe._web(
            private.probe._json(output.getvalue().encode()),
            private.REVISION,
            private.BUNDLE,
            private.MANIFEST,
        )
        self.assertEqual(result["code"], "ready")
        self.assertEqual(len(fixture.calls), 10)
        self.assertNotIn("search.operations", probe.SCRIPT)
        self.assertIn(private.probe._WEB_ACCEPTANCE_SCRIPT, probe.SCRIPT)
        self.assertTrue(probe.SCRIPT.startswith(private.probe._COMMON_SCRIPT))
        for request, timeout, maximum in fixture.calls:
            self.assertTrue(request.url.startswith("http://127.0.0.1:18083/"))
            self.assertEqual(dict(request.headers)["Origin"], "http://127.0.0.1:18083")
            self.assertLessEqual(timeout, 130)
            self.assertEqual(maximum, 256 * 1024)


if __name__ == "__main__":
    unittest.main()
