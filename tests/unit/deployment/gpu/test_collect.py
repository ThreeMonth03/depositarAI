"""GPU ownership collect fixtures and regressions."""

from __future__ import annotations
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from search.operations.deployment.gpu import contract as gpu_contract
from search.operations.deployment.gpu import collect as gpu_collect
from tests.unit.deployment.gpu.fixtures import (
    CONFIG,
    CPU_ID,
    FakeRunner,
    SCOPE,
    SCOPE_ID,
    bind,
    container,
    scope_container,
)


class GPUHostGatherTests(unittest.TestCase):
    def run_preflight(self, runner, **changes):
        return gpu_collect.run_gpu_ownership(
            CONFIG, runner=runner, clock=lambda: 0, **changes
        )

    def test_read_only_local_gather_cpu_success(self):
        runner = FakeRunner([container()])
        result = self.run_preflight(runner)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["command_count"], 7)
        self.assertEqual(runner.ps_count, 2)
        for argv, timeout, cap in runner.calls:
            self.assertLessEqual(timeout, gpu_collect.COMMAND_TIMEOUT_SECONDS)
            self.assertLessEqual(cap, gpu_contract.MAX_OUTPUT_BYTES)
            self.assertGreater(cap, 0)
            self.assertNotIn("run", argv)
            self.assertNotIn("stop", argv)
            self.assertNotIn("exec", argv)

    def test_allowed_idle_container_still_runs_top_and_compute_query(self):
        runner = FakeRunner([scope_container()], pids={SCOPE_ID: "PID\n101"})
        self.assertEqual(
            self.run_preflight(runner, allowed=(SCOPE,))["status"], "ok"
        )
        self.assertTrue(
            any("top" in argv for argv, _timeout, _cap in runner.calls)
        )
        self.assertTrue(
            any(
                "--query-compute-apps=gpu_uuid,pid" in argv
                for argv, _timeout, _cap in runner.calls
            )
        )

    def test_no_names_labels_env_commands_or_health_logs_in_projection(self):
        for excluded in (
            ".Name",
            ".Config.Labels",
            "json .Config.Env",
            ".Config.Cmd",
            ".State.Health",
            "json .Options",
        ):
            self.assertNotIn(excluded, gpu_collect.INSPECT_FORMAT)
        self.assertIn("NVIDIA_VISIBLE_DEVICES", gpu_collect.INSPECT_FORMAT)
        self.assertIn(
            'index .HostConfig "Annotations"', gpu_collect.INSPECT_FORMAT
        )
        self.assertIn('index .HostConfig "Mounts"', gpu_collect.INSPECT_FORMAT)

    def test_new_running_container_rejects_snapshot(self):
        runner = FakeRunner([container()])
        runner.new_ids = (CPU_ID, "5" * 64)
        self.assertEqual(
            self.run_preflight(runner)["code"],
            "gpu_ownership_snapshot_changed",
        )

    def test_restart_or_resource_mutation_rejects_snapshot(self):
        for change in (
            lambda row: row.update(RestartCount=1),
            lambda row: row["State"].update(Pid=999),
            lambda row: row["HostConfig"].update(Runtime="nvidia"),
        ):
            runner = FakeRunner([container()])
            runner.changed_inspect = change
            self.assertEqual(
                self.run_preflight(runner)["code"],
                "gpu_ownership_snapshot_changed",
            )

    def test_mount_presentation_order_does_not_change_the_snapshot(self):
        row = bind(container(), "/data/source", "/data")
        extra = bind(container(), "/private/query", "/run/secrets/api_key")
        row["Mounts"].extend(extra["Mounts"])
        row["HostConfig"]["Binds"].extend(extra["HostConfig"]["Binds"])
        runner = FakeRunner([row])
        runner.changed_inspect = lambda value: value["Mounts"].reverse()
        self.assertEqual("ok", self.run_preflight(runner)["status"])

    def test_mount_content_changes_remain_rejected_after_order_normalization(
        self,
    ):
        for change in (
            lambda row: row["Mounts"][0].update(RW=True),
            lambda row: row["Mounts"][0].update(Source="/different/source"),
            lambda row: row["Mounts"][0].update(
                Destination="/different/target"
            ),
        ):
            with self.subTest(change=change):
                runner = FakeRunner(
                    [bind(container(), "/data/source", "/data")]
                )

                # Keep the independently represented source/target pair aligned
                # when changing it, so snapshot comparison itself must reject
                # it.
                def changed(row):
                    change(row)
                    mount = row["Mounts"][0]
                    row["HostConfig"]["Binds"] = [
                        mount["Source"]
                        + ":"
                        + mount["Destination"]
                        + ":ro,rprivate"
                    ]

                runner.changed_inspect = changed
                self.assertEqual(
                    "gpu_ownership_snapshot_changed",
                    self.run_preflight(runner)["code"],
                )

    def test_command_error_does_not_leak_output_or_exception(self):
        runner = FakeRunner()
        runner.result_override = gpu_contract.CommandResult(
            1, "SECRET_INSPECT", "SECRET_STDERR"
        )
        result = self.run_preflight(runner)
        self.assertEqual(result["code"], "gpu_ownership_command_failed")
        self.assertNotIn("SECRET", json.dumps(result))

        def unknown_code(_argv, **_kwargs):
            raise gpu_contract.GPUOwnershipError(
                "gpu_ownership_private_secret_value"
            )

        result = self.run_preflight(unknown_code)
        self.assertEqual(result["code"], "gpu_ownership_preflight_failed")
        self.assertNotIn("private_secret", json.dumps(result))

        def fail(_argv, **_kwargs):
            raise RuntimeError("SECRET_EXCEPTION")

        result = self.run_preflight(fail)
        self.assertEqual(result["code"], "gpu_ownership_preflight_failed")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_command_output_combined_bytes_bounded(self):
        runner = FakeRunner()
        runner.result_override = gpu_contract.CommandResult(
            0, "x" * gpu_contract.MAX_OUTPUT_BYTES, "x"
        )
        self.assertEqual(
            self.run_preflight(runner)["code"],
            "gpu_ownership_command_output_limit",
        )

    def test_total_output_and_command_count_limits_are_enforced(self):
        with mock.patch.object(gpu_collect, "MAX_TOTAL_OUTPUT_BYTES", 16):
            self.assertEqual(
                self.run_preflight(FakeRunner())["code"],
                "gpu_ownership_command_output_limit",
            )
        with mock.patch.object(gpu_collect, "MAX_CALLS", 1):
            self.assertEqual(
                self.run_preflight(FakeRunner())["code"],
                "gpu_ownership_command_count_limit",
            )

    def test_default_bind_metadata_probe_is_bounded_read_only_command(self):
        runner = FakeRunner([bind(container(), "/catalog")])
        result = self.run_preflight(runner)
        self.assertEqual(result["status"], "ok")
        probes = [call for call in runner.calls if call[0][0] == "python3"]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0][0][-1], "/catalog")
        self.assertIn("path.lstat()", probes[0][0][-2])
        self.assertNotIn("read_text", probes[0][0][-2])

    def test_nonfinite_clock_or_total_deadline_rejected(self):
        for tick in (float("nan"), float("inf")):
            result = gpu_collect.run_gpu_ownership(
                CONFIG, runner=FakeRunner(), clock=lambda: tick
            )
            self.assertEqual(result["code"], "gpu_ownership_deadline_exceeded")
        values = iter((0, 121))
        result = gpu_collect.run_gpu_ownership(
            CONFIG, runner=FakeRunner(), clock=lambda: next(values)
        )
        self.assertEqual(result["code"], "gpu_ownership_deadline_exceeded")

    def test_clock_exception_is_sanitized(self):
        def fail():
            raise RuntimeError("SECRET_CLOCK")

        result = gpu_collect.run_gpu_ownership(
            CONFIG, runner=FakeRunner(), clock=fail
        )
        self.assertEqual(result["code"], "gpu_ownership_preflight_failed")

    def test_remote_or_unknown_runtime_profile_rejected(self):
        runner = FakeRunner()
        runner.default_runtime = "unknown"
        self.assertEqual(
            self.run_preflight(runner)["code"],
            "gpu_ownership_host_profile_unsupported",
        )
        self.assertEqual(
            gpu_collect.DOCKER_PREFIX[2], "unix:///var/run/docker.sock"
        )

    def test_strict_json_decode_failures_are_sanitized(self):
        for value in (
            '{"OSType":"linux","OSType":"other"}',
            '{"OSType":NaN}',
            '{"OSType":Infinity}',
            '{"OSType":-Infinity}',
        ):
            runner = FakeRunner()
            runner.result_override = gpu_contract.CommandResult(0, value)
            self.assertEqual(
                self.run_preflight(runner)["code"],
                "gpu_ownership_json_invalid",
            )
        with mock.patch.object(
            gpu_collect, "strict_json", side_effect=RecursionError
        ):
            self.assertEqual(
                self.run_preflight(FakeRunner())["code"],
                "gpu_ownership_json_invalid",
            )

    def test_non_utf8_command_text_remains_a_sanitized_preflight_failure(self):
        runner = FakeRunner()
        runner.result_override = gpu_contract.CommandResult(
            0, "\ud800private-input"
        )
        report = self.run_preflight(runner)
        self.assertEqual("gpu_ownership_preflight_failed", report["code"])
        self.assertNotIn("private-input", json.dumps(report))

    def test_actual_mount_probe_reads_metadata_only(self):
        with tempfile.TemporaryDirectory(
            prefix="depositar-gpu-source."
        ) as directory:
            source = Path(directory)
            file = source / "private"
            file.write_text("SECRET_DO_NOT_READ")
            gpu_collect.probe_bind_source(str(file))
            gpu_collect.probe_bind_source(str(source))
            link = source / "alias"
            link.symlink_to(file)
            with self.assertRaisesRegex(
                gpu_contract.GPUOwnershipError,
                "^gpu_ownership_mount_source_alias$",
            ):
                gpu_collect.probe_bind_source(str(link))
            with self.assertRaisesRegex(
                gpu_contract.GPUOwnershipError,
                "^gpu_ownership_mount_source_unverified$",
            ):
                gpu_collect.probe_bind_source(str(source / "missing"))

    def test_gather_probes_each_bind_and_sanitizes_failures(self):
        runner = FakeRunner([bind(container(), "/catalog")])
        probed = []
        result = self.run_preflight(
            runner,
            bind_source_probe=lambda source, **_limits: probed.append(source),
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(probed, ["/catalog"])

        def fail(_source, **_limits):
            raise OSError("SECRET_PATH")

        result = self.run_preflight(
            FakeRunner([bind(container(), "/catalog")]), bind_source_probe=fail
        )
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_real_bounded_runner_output_and_timeout(self):
        result = gpu_collect.bounded_runner(
            [sys.executable, "-I", "-S", "-c", "print('ok')"],
            timeout=2,
            max_bytes=128,
        )
        self.assertEqual(result.stdout, "ok\n")
        with self.assertRaisesRegex(
            gpu_contract.GPUOwnershipError,
            "^gpu_ownership_command_output_limit$",
        ):
            gpu_collect.bounded_runner(
                [sys.executable, "-I", "-S", "-c", "print('x'*1024)"],
                timeout=2,
                max_bytes=128,
            )
        with self.assertRaisesRegex(
            gpu_contract.GPUOwnershipError, "^gpu_ownership_command_timeout$"
        ):
            gpu_collect.bounded_runner(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    "import time;time.sleep(1)",
                ],
                timeout=0.02,
                max_bytes=128,
            )

    def test_failed_stream_preserves_all_cleanup_and_order(self):
        events = []
        stdout, stderr = mock.Mock(), mock.Mock()
        stdout.fileno.return_value = 11
        stderr.fileno.return_value = 12
        stdout.close.side_effect = lambda: events.append("stdout.close")
        stderr.close.side_effect = lambda: events.append("stderr.close")
        process = mock.Mock(pid=123, stdout=stdout, stderr=stderr)
        process.wait.side_effect = lambda **_limits: events.append("wait")
        selector = mock.Mock()
        selector.get_map.return_value = {11: stdout, 12: stderr}
        selector.select.return_value = [
            (mock.Mock(fileobj=stdout, data="stdout"), 1)
        ]
        selector.close.side_effect = lambda: events.append("selector.close")

        with (
            mock.patch.object(
                gpu_collect.subprocess, "Popen", return_value=process
            ),
            mock.patch.object(
                gpu_collect.selectors,
                "DefaultSelector",
                return_value=selector,
            ),
            mock.patch.object(gpu_collect.os, "set_blocking"),
            mock.patch.object(
                gpu_collect.os, "read", side_effect=OSError("SECRET_READ")
            ),
            mock.patch.object(gpu_collect.time, "monotonic", return_value=0),
            mock.patch.object(
                gpu_collect.os,
                "killpg",
                side_effect=lambda *_args: events.append("killpg"),
            ) as killpg,
        ):
            with self.assertRaisesRegex(
                gpu_contract.GPUOwnershipError,
                "^gpu_ownership_command_failed$",
            ):
                gpu_collect.bounded_runner(
                    ["fixture-command"], timeout=1, max_bytes=128
                )

        self.assertEqual(
            [
                "selector.close",
                "killpg",
                "wait",
                "stdout.close",
                "stderr.close",
            ],
            events,
        )
        killpg.assert_called_once_with(123, gpu_collect.signal.SIGKILL)
        process.wait.assert_called_once_with(timeout=2)
        stdout.close.assert_called_once_with()
        stderr.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
