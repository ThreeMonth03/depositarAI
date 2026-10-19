"""Strict GPU snapshot proofs, host gather bounds, and no runtime mutations."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from search.operations.config import GPUConfig, ProductConfig
from search.operations.deployment import gpu_ownership as ownership


UUIDS = tuple(f"GPU-00000000-0000-0000-0000-{index:012d}" for index in range(1, 5))
GPUS = GPUConfig(UUIDS[0], (UUIDS[1], UUIDS[2]), UUIDS[3])
CONFIG = ProductConfig(
    1, Path("/state"), Path("/api"), Path("/embedding"), Path("/reranker"), GPUS
)
INVENTORY = "\n".join(f"{value}, {index}" for index, value in enumerate(UUIDS))
IMAGE = "sha256:" + "a" * 64
SCOPE_ID = "1" * 64
PLANNER_ID = "2" * 64
RERANKER_ID = "3" * 64
CPU_ID = "4" * 64
SCOPE = ownership.GPUContainerProof(SCOPE_ID, IMAGE, "scope", (UUIDS[0],))
PLANNER = ownership.GPUContainerProof(PLANNER_ID, IMAGE, "planner", UUIDS[1:3])
RERANKER = ownership.GPUContainerProof(RERANKER_ID, IMAGE, "reranker", (UUIDS[3],))


def container(identifier=CPU_ID, *, pid=100, gpu_uuids=()):
    return {
        "Id": identifier,
        "Image": IMAGE,
        "RestartCount": 0,
        "State": {
            "Running": True,
            "Paused": False,
            "Restarting": False,
            "Dead": False,
            "Pid": pid,
            "StartedAt": "2026-10-08T00:00:00.000000000Z",
        },
        "HostConfig": {
            "Runtime": "runc",
            "PidMode": "",
            "IpcMode": "private",
            "Privileged": False,
            "Devices": None,
            "DeviceCgroupRules": None,
            "CapAdd": None,
            "VolumesFrom": None,
            "AnnotationsPresent": False,
            "Binds": None,
            "MountSpecsCount": 0,
            "DeviceRequests": []
            if not gpu_uuids
            else [
                {
                    "Driver": "nvidia",
                    "Count": 0,
                    "DeviceIDs": list(gpu_uuids),
                    "Capabilities": [["gpu"]],
                    "OptionsEmpty": True,
                }
            ],
        },
        "Mounts": [],
        "NVIDIAEnv": [],
    }


def scope_container():
    return container(SCOPE_ID, pid=101, gpu_uuids=SCOPE.gpu_uuids)


def bind(row, source, target="/data"):
    row["HostConfig"]["Binds"] = [f"{source}:{target}:ro,rprivate"]
    row["Mounts"] = [
        {
            "Type": "bind",
            "Source": source,
            "Destination": target,
            "Propagation": "rprivate",
            "RW": False,
        }
    ]
    return row


class FakeRunner:
    def __init__(self, containers=(), *, compute="", pids=None):
        self.containers = {row["Id"]: copy.deepcopy(row) for row in containers}
        self.compute = compute
        self.pids = {} if pids is None else pids
        self.calls = []
        self.ps_count = 0
        self.inspect_count = {}
        self.new_ids = None
        self.changed_inspect = None
        self.result_override = None
        self.default_runtime = "runc"

    def __call__(self, argv, *, timeout, max_bytes):
        self.calls.append((argv, timeout, max_bytes))
        if self.result_override is not None:
            return self.result_override
        if argv[0] == "nvidia-smi":
            result = INVENTORY if argv[1] == "--query-gpu=uuid,index" else self.compute
        elif argv[0] == "python3":
            if argv[1:5] != ["-I", "-S", "-B", "-c"]:
                raise AssertionError("metadata probe must use isolated Python")
            result = "ok\n"
        else:
            if tuple(argv[: len(ownership.DOCKER_PREFIX)]) != ownership.DOCKER_PREFIX:
                raise AssertionError("wrong local Docker command prefix")
            command = argv[len(ownership.DOCKER_PREFIX)]
            if command == "info":
                result = json.dumps(
                    {"OSType": "linux", "DefaultRuntime": self.default_runtime}
                )
            elif command == "ps":
                self.ps_count += 1
                ids = (
                    self.containers
                    if self.ps_count == 1 or self.new_ids is None
                    else self.new_ids
                )
                result = "\n".join(sorted(ids))
            elif command == "inspect":
                identifier = argv[-1]
                self.inspect_count[identifier] = (
                    self.inspect_count.get(identifier, 0) + 1
                )
                row = copy.deepcopy(self.containers[identifier])
                if (
                    self.inspect_count[identifier] > 1
                    and self.changed_inspect is not None
                ):
                    self.changed_inspect(row)
                row["HostConfig"]["DeviceRequests"].append(None)
                row["NVIDIAEnv"].append(None)
                result = json.dumps(row)
            elif command == "top":
                if argv[-2:] != ["-eo", "pid"]:
                    raise AssertionError("top must omit secret command lines")
                result = self.pids[argv[-3]]
            else:
                raise AssertionError("mutating or unsupported Docker command")
        return ownership.CommandResult(0, result)


class GPUOwnershipTests(unittest.TestCase):
    def validate(self, **changes):
        values = {
            "inventory": INVENTORY,
            "containers": [],
            "compute_apps": "",
            "container_pids": {},
            "allowed": (),
        }
        values.update(changes)
        return ownership.validate_gpu_ownership(GPUS, **values)

    def blocked(self, code, **changes):
        with self.assertRaisesRegex(ownership.GPUOwnershipError, "^" + code + "$"):
            self.validate(**changes)

    def test_four_unclaimed_gpus_and_cpu_container_pass(self):
        result = self.validate(containers=[container()])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["gpu_count"], 4)
        self.assertFalse(result["exclusive_reservation"])
        self.assertFalse(result["runtime_modified"])

    def test_idle_gpu_claim_is_blocked_without_compute_activity(self):
        self.blocked(
            "gpu_ownership_unsealed_gpu_container", containers=[scope_container()]
        )

    def test_existing_exact_sealed_idle_gpu_container_passes(self):
        result = self.validate(
            containers=[scope_container()],
            allowed=(SCOPE,),
            container_pids={SCOPE_ID: "PID\n101\n"},
        )
        self.assertEqual(result["sealed_gpu_container_count"], 1)
        self.assertEqual(result["compute_process_count"], 0)

    def test_all_roles_and_multigpu_pid_are_attributed(self):
        rows = [
            scope_container(),
            container(PLANNER_ID, pid=102, gpu_uuids=PLANNER.gpu_uuids),
            container(RERANKER_ID, pid=103, gpu_uuids=RERANKER.gpu_uuids),
        ]
        result = self.validate(
            containers=rows,
            allowed=(SCOPE, PLANNER, RERANKER),
            container_pids={
                SCOPE_ID: "PID\n101\n201",
                PLANNER_ID: "PID\n102\n202",
                RERANKER_ID: "PID\n103\n203",
            },
            compute_apps=f"{UUIDS[0]},201\n{UUIDS[1]},202\n{UUIDS[2]},202\n{UUIDS[3]},203",
        )
        self.assertEqual(result["compute_process_count"], 3)
        self.assertEqual(result["compute_gpu_pid_record_count"], 4)

    def test_unknown_host_pid_still_blocked_with_allowed_container(self):
        self.blocked(
            "gpu_ownership_unknown_compute_pid",
            containers=[scope_container()],
            allowed=(SCOPE,),
            container_pids={SCOPE_ID: "PID\n101"},
            compute_apps=f"{UUIDS[0]},999",
        )

    def test_host_compute_without_container_is_blocked(self):
        self.blocked(
            "gpu_ownership_unknown_compute_pid", compute_apps=f"{UUIDS[0]},100"
        )

    def test_sealed_container_cannot_compute_on_other_role(self):
        self.blocked(
            "gpu_ownership_compute_role_mismatch",
            containers=[scope_container()],
            allowed=(SCOPE,),
            container_pids={SCOPE_ID: "PID\n101"},
            compute_apps=f"{UUIDS[3]},101",
        )

    def test_other_idle_gpu_container_is_not_allowed_by_one_proof(self):
        self.blocked(
            "gpu_ownership_unsealed_gpu_container",
            containers=[
                scope_container(),
                container(PLANNER_ID, pid=102, gpu_uuids=PLANNER.gpu_uuids),
            ],
            allowed=(SCOPE,),
            container_pids={SCOPE_ID: "PID\n101"},
        )

    def test_replaced_container_id_or_image_is_rejected(self):
        for field, value, code in (
            ("Id", "5" * 64, "gpu_ownership_unsealed_gpu_container"),
            ("Image", "sha256:" + "b" * 64, "gpu_ownership_sealed_identity_changed"),
        ):
            row = scope_container()
            row[field] = value
            with self.subTest(field=field):
                self.blocked(
                    code,
                    containers=[row],
                    allowed=(SCOPE,),
                    container_pids={SCOPE_ID: "PID\n101"},
                )

    def test_missing_sealed_container_is_rejected(self):
        self.blocked("gpu_ownership_sealed_container_missing", allowed=(SCOPE,))

    def test_proof_requires_full_ids_exact_role_gpus_and_no_duplicates(self):
        invalid = (
            ownership.GPUContainerProof("scope-model", IMAGE, "scope", (UUIDS[0],)),
            ownership.GPUContainerProof(SCOPE_ID, "image:latest", "scope", (UUIDS[0],)),
            ownership.GPUContainerProof(SCOPE_ID, IMAGE, "any", (UUIDS[0],)),
            ownership.GPUContainerProof(SCOPE_ID, IMAGE, "scope", (UUIDS[3],)),
            ownership.GPUContainerProof(SCOPE_ID, IMAGE, "planner", (UUIDS[1],)),
        )
        for proof in invalid:
            with self.subTest(proof=proof):
                self.blocked("gpu_ownership_proof_invalid", allowed=(proof,))
        self.blocked("gpu_ownership_proof_invalid", allowed=(SCOPE, SCOPE))
        self.blocked("gpu_ownership_proof_invalid", allowed=[SCOPE])

    def test_config_overlap_rejected(self):
        with self.assertRaisesRegex(
            ownership.GPUOwnershipError, "^gpu_ownership_config_overlap$"
        ):
            ownership.validate_gpu_ownership(
                GPUConfig(UUIDS[0], UUIDS[1:3], UUIDS[0]),
                inventory=INVENTORY,
                containers=[],
                compute_apps="",
                container_pids={},
            )

    def test_inventory_missing_extra_duplicate_numeric_and_mig_rejected(self):
        invalid = (
            INVENTORY.rsplit("\n", 1)[0],
            INVENTORY + "\nGPU-99999999-0000-0000-0000-000000000005,4",
            INVENTORY + "\n" + INVENTORY.splitlines()[0],
            INVENTORY.replace(", 3", ", 2"),
            INVENTORY.replace(UUIDS[0], "0"),
            INVENTORY.replace(UUIDS[0], "MIG-" + UUIDS[0]),
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ownership.GPUOwnershipError):
                    self.validate(inventory=raw)

    def test_count_all_numeric_partial_mig_unknown_requests_fail_closed(self):
        cases = (
            {"Count": -1, "DeviceIDs": []},
            {"Count": 2, "DeviceIDs": []},
            {"DeviceIDs": ["all"]},
            {"DeviceIDs": ["0"]},
            {"DeviceIDs": [UUIDS[0][:16]]},
            {"DeviceIDs": ["MIG-" + UUIDS[0]]},
            {"Driver": "other"},
            {"OptionsEmpty": False},
            {"Capabilities": [["unknown"]]},
            {"Count": True},
        )
        for change in cases:
            row = scope_container()
            row["HostConfig"]["DeviceRequests"][0].update(change)
            with self.subTest(change=change):
                with self.assertRaises(ownership.GPUOwnershipError):
                    self.validate(containers=[row])

    def test_duplicate_uuid_request_is_rejected(self):
        row = scope_container()
        row["HostConfig"]["DeviceRequests"][0]["DeviceIDs"].append(UUIDS[0])
        self.blocked("gpu_ownership_device_scope_duplicate", containers=[row])

    def test_empty_request_or_unknown_fields_are_rejected(self):
        for change in ({"DeviceIDs": []}, {"Other": False}):
            row = scope_container()
            row["HostConfig"]["DeviceRequests"][0].update(change)
            self.blocked("gpu_ownership_device_scope_unknown", containers=[row])

    def test_legacy_visible_devices_cannot_grant_gpu(self):
        for value in ("all", "0", UUIDS[0], "nvidia.com/gpu=all"):
            row = container()
            row["HostConfig"]["Runtime"] = "nvidia"
            row["NVIDIAEnv"] = ["NVIDIA_VISIBLE_DEVICES=" + value]
            with self.subTest(value=value):
                with self.assertRaises(ownership.GPUOwnershipError):
                    self.validate(containers=[row])

    def test_explicit_visible_devices_must_match_device_request(self):
        row = scope_container()
        row["NVIDIAEnv"] = ["NVIDIA_VISIBLE_DEVICES=" + UUIDS[0]]
        self.assertEqual(
            self.validate(
                containers=[row],
                allowed=(SCOPE,),
                container_pids={SCOPE_ID: "PID\n101"},
            )["status"],
            "ok",
        )
        for value in (UUIDS[3], "none", "void", UUIDS[0] + "," + UUIDS[0]):
            row["NVIDIAEnv"] = ["NVIDIA_VISIBLE_DEVICES=" + value]
            with self.subTest(value=value):
                self.blocked("gpu_ownership_legacy_gpu_access", containers=[row])

    def test_nvidia_runtime_no_gpu_visible_is_cpu_only(self):
        for value in ("", "none", "void"):
            row = container()
            row["HostConfig"]["Runtime"] = "nvidia"
            row["NVIDIAEnv"] = ["NVIDIA_VISIBLE_DEVICES=" + value]
            with self.subTest(value=value):
                self.assertEqual(self.validate(containers=[row])["status"], "ok")

    def test_management_devices_imex_and_duplicate_env_rejected(self):
        for key in ownership.NVIDIA_KEYS[1:]:
            row = container()
            row["NVIDIAEnv"] = [key + "=all"]
            self.blocked("gpu_ownership_legacy_gpu_access", containers=[row])
        row = container()
        row["NVIDIAEnv"] = ["NVIDIA_VISIBLE_DEVICES=void"] * 2
        self.blocked("gpu_ownership_nvidia_environment_invalid", containers=[row])

    def test_host_namespace_privileged_custom_runtime_annotations_rejected(self):
        for field, value in (
            ("Privileged", True),
            ("Privileged", None),
            ("PidMode", "host"),
            ("PidMode", "container:" + CPU_ID),
            ("IpcMode", "host"),
            ("Runtime", "custom"),
            ("Runtime", ""),
            ("AnnotationsPresent", True),
        ):
            row = container()
            row["HostConfig"][field] = value
            with self.subTest(field=field, value=value):
                self.blocked("gpu_ownership_container_host_access", containers=[row])

    def test_direct_host_devices_rules_caps_and_inherited_volumes_rejected(self):
        for field, value in (
            ("Devices", [{"PathOnHost": "/dev/nvidia0"}]),
            ("DeviceCgroupRules", ["c 195:* rwm"]),
            ("CapAdd", ["SYS_ADMIN"]),
            ("VolumesFrom", ["other"]),
        ):
            row = container()
            row["HostConfig"][field] = value
            with self.subTest(field=field):
                self.blocked("gpu_ownership_container_device_access", containers=[row])

    def test_mount_source_and_destination_host_access_rejected(self):
        for path in (
            "/",
            "/dev",
            "/dev/nvidia0",
            "/proc/1/root",
            "/sys",
            "/run",
            "/var/run/docker.sock",
            "/var",
        ):
            for source, target in ((path, "/data"), ("/data", path)):
                row = bind(container(), source, target)
                with self.subTest(source=source, target=target):
                    self.blocked("gpu_ownership_host_access_mount", containers=[row])

    def test_mount_path_alias_propagation_inherited_driver_volume_rejected(self):
        for source in (
            "/data/../dev",
            "/data//x",
            "/data/./x",
            "relative",
            "/data\x00x",
        ):
            with self.subTest(source=source):
                self.blocked(
                    "gpu_ownership_mount_path_invalid",
                    containers=[bind(container(), source)],
                )
        row = bind(container(), "/data")
        row["Mounts"][0]["Propagation"] = "rshared"
        self.blocked("gpu_ownership_mount_propagation_unknown", containers=[row])
        row = bind(container(), "/data")
        row["Mounts"][0]["Type"] = "volume"
        self.blocked("gpu_ownership_mount_type_unknown", containers=[row])

    def test_private_readonly_query_destinations_are_not_host_run_mounts(self):
        for target in (
            "/run/secrets",
            "/run/secrets/api_key",
            "/run/index/binding.json",
            "/run/index/root-ca.pem",
            "/run/opensearch-ca/root-ca.pem",
        ):
            with self.subTest(target=target):
                row = bind(container(), "/private/query-file", target)
                self.assertEqual(
                    "ok",
                    self.validate(
                        containers=[row],
                        verified_bind_sources=frozenset({"/private/query-file"}),
                    )["status"],
                )
                row["Mounts"][0]["RW"] = True
                self.blocked("gpu_ownership_host_access_mount", containers=[row])

    def test_query_destination_exception_never_allows_host_run_sources_or_sockets(
        self,
    ):
        for source, target in (
            ("/run/secrets", "/run/secrets"),
            ("/run/index/binding.json", "/run/index/binding.json"),
            ("/private", "/run/other"),
            ("/private", "/run/opensearch-ca"),
            ("/run/opensearch-ca/root-ca.pem", "/run/opensearch-ca/root-ca.pem"),
            ("/private", "/run/secrets/docker.sock"),
            ("/private", "/run/index/containerd.sock"),
        ):
            with self.subTest(source=source, target=target):
                self.blocked(
                    "gpu_ownership_host_access_mount",
                    containers=[bind(container(), source, target)],
                )

    def test_mount_requires_separate_source_evidence(self):
        row = bind(container(), "/catalog")
        self.blocked("gpu_ownership_mount_source_unverified", containers=[row])
        self.assertEqual(
            self.validate(
                containers=[row], verified_bind_sources=frozenset({"/catalog"})
            )["status"],
            "ok",
        )

    def test_host_bind_and_actual_mount_must_agree(self):
        row = bind(container(), "/catalog")
        row["HostConfig"]["Binds"] = ["/other:/data:ro"]
        self.blocked("gpu_ownership_mount_inventory_mismatch", containers=[row])

    def test_container_count_duplicate_shape_state_fail_closed(self):
        self.blocked(
            "gpu_ownership_container_limit",
            containers=[container()] * (ownership.MAX_CONTAINERS + 1),
        )
        self.blocked(
            "gpu_ownership_container_duplicate", containers=[container(), container()]
        )
        for field, value in (
            ("Paused", True),
            ("Running", False),
            ("Pid", 0),
            ("Dead", None),
        ):
            row = container()
            row["State"][field] = value
            with self.subTest(field=field):
                self.blocked("gpu_ownership_container_not_running", containers=[row])
        row = container()
        row["Config"] = {"Env": ["PRIVATE_SECRET=do-not-expose"]}
        self.blocked("gpu_ownership_container_shape_invalid", containers=[row])

    def test_pid_evidence_exact_main_pid_duplicate_unknown_and_malformed(self):
        common = {"containers": [scope_container()], "allowed": (SCOPE,)}
        self.blocked("gpu_ownership_pid_evidence_missing", **common)
        self.blocked(
            "gpu_ownership_main_pid_missing",
            **common,
            container_pids={SCOPE_ID: "PID\n999"},
        )
        for raw in (
            "PID\n0",
            "PID\n101 command secret",
            "PID\n2147483648",
            "pid\n101",
            "PID",
            "PID\n",
        ):
            with self.subTest(raw=raw):
                self.blocked(
                    "gpu_ownership_pid_inventory_invalid",
                    **common,
                    container_pids={SCOPE_ID: raw},
                )
        self.blocked(
            "gpu_ownership_pid_inventory_duplicate",
            **common,
            container_pids={SCOPE_ID: "PID\n101\n101"},
        )

    def test_same_pid_cannot_belong_to_two_allowed_containers(self):
        self.blocked(
            "gpu_ownership_pid_owner_ambiguous",
            containers=[
                scope_container(),
                container(RERANKER_ID, pid=101, gpu_uuids=RERANKER.gpu_uuids),
            ],
            allowed=(SCOPE, RERANKER),
            container_pids={SCOPE_ID: "PID\n101", RERANKER_ID: "PID\n101"},
        )

    def test_compute_invalid_duplicate_and_no_process_text_rejected(self):
        for raw in (
            "No running processes found",
            "[N/A],123",
            f"{UUIDS[0]},[N/A]",
            f"{UUIDS[0]},0",
            f"{UUIDS[0]},2147483648",
            f"{UUIDS[0]},101,secret",
            "\n",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ownership.GPUOwnershipError):
                    self.validate(compute_apps=raw)
        self.blocked(
            "gpu_ownership_compute_duplicate",
            containers=[scope_container()],
            allowed=(SCOPE,),
            container_pids={SCOPE_ID: "PID\n101"},
            compute_apps=f"{UUIDS[0]},101\n{UUIDS[0]},101",
        )

    def test_invalid_unhashable_fields_always_use_fixed_errors(self):
        for key in ("Runtime", "PidMode", "IpcMode"):
            row = container()
            row["HostConfig"][key] = []
            self.blocked("gpu_ownership_container_host_access", containers=[row])
        row = scope_container()
        row["HostConfig"]["DeviceRequests"][0]["Driver"] = {}
        self.blocked("gpu_ownership_device_scope_unknown", containers=[row])
        for key in ("Type", "Propagation"):
            row = bind(container(), "/catalog")
            row["Mounts"][0][key] = []
            with self.assertRaises(ownership.GPUOwnershipError):
                self.validate(containers=[row])
        self.blocked(
            "gpu_ownership_mount_path_invalid", containers=[bind(container(), "//")]
        )


class GPUHostGatherTests(unittest.TestCase):
    def run_preflight(self, runner, **changes):
        return ownership.run_gpu_ownership(
            CONFIG, runner=runner, clock=lambda: 0, **changes
        )

    def test_read_only_local_gather_cpu_success(self):
        runner = FakeRunner([container()])
        result = self.run_preflight(runner)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["command_count"], 7)
        self.assertEqual(runner.ps_count, 2)
        for argv, timeout, cap in runner.calls:
            self.assertLessEqual(timeout, ownership.COMMAND_TIMEOUT_SECONDS)
            self.assertLessEqual(cap, ownership.MAX_OUTPUT_BYTES)
            self.assertGreater(cap, 0)
            self.assertNotIn("run", argv)
            self.assertNotIn("stop", argv)
            self.assertNotIn("exec", argv)

    def test_allowed_idle_container_still_runs_top_and_compute_query(self):
        runner = FakeRunner([scope_container()], pids={SCOPE_ID: "PID\n101"})
        self.assertEqual(self.run_preflight(runner, allowed=(SCOPE,))["status"], "ok")
        self.assertTrue(any("top" in argv for argv, _timeout, _cap in runner.calls))
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
            self.assertNotIn(excluded, ownership.INSPECT_FORMAT)
        self.assertIn("NVIDIA_VISIBLE_DEVICES", ownership.INSPECT_FORMAT)
        self.assertIn('index .HostConfig "Annotations"', ownership.INSPECT_FORMAT)
        self.assertIn('index .HostConfig "Mounts"', ownership.INSPECT_FORMAT)

    def test_new_running_container_rejects_snapshot(self):
        runner = FakeRunner([container()])
        runner.new_ids = (CPU_ID, "5" * 64)
        self.assertEqual(
            self.run_preflight(runner)["code"], "gpu_ownership_snapshot_changed"
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
                self.run_preflight(runner)["code"], "gpu_ownership_snapshot_changed"
            )

    def test_mount_presentation_order_does_not_change_the_snapshot(self):
        row = bind(container(), "/data/source", "/data")
        extra = bind(container(), "/private/query", "/run/secrets/api_key")
        row["Mounts"].extend(extra["Mounts"])
        row["HostConfig"]["Binds"].extend(extra["HostConfig"]["Binds"])
        runner = FakeRunner([row])
        runner.changed_inspect = lambda value: value["Mounts"].reverse()
        self.assertEqual("ok", self.run_preflight(runner)["status"])

    def test_mount_content_changes_remain_rejected_after_order_normalization(self):
        for change in (
            lambda row: row["Mounts"][0].update(RW=True),
            lambda row: row["Mounts"][0].update(Source="/different/source"),
            lambda row: row["Mounts"][0].update(Destination="/different/target"),
        ):
            with self.subTest(change=change):
                runner = FakeRunner([bind(container(), "/data/source", "/data")])

                # Keep the independently represented source/target pair aligned
                # when changing it, so snapshot comparison itself must reject it.
                def changed(row):
                    change(row)
                    mount = row["Mounts"][0]
                    row["HostConfig"]["Binds"] = [
                        mount["Source"] + ":" + mount["Destination"] + ":ro,rprivate"
                    ]

                runner.changed_inspect = changed
                self.assertEqual(
                    "gpu_ownership_snapshot_changed", self.run_preflight(runner)["code"]
                )

    def test_command_error_does_not_leak_output_or_exception(self):
        runner = FakeRunner()
        runner.result_override = ownership.CommandResult(
            1, "SECRET_INSPECT", "SECRET_STDERR"
        )
        result = self.run_preflight(runner)
        self.assertEqual(result["code"], "gpu_ownership_command_failed")
        self.assertNotIn("SECRET", json.dumps(result))

        def unknown_code(_argv, **_kwargs):
            raise ownership.GPUOwnershipError("gpu_ownership_private_secret_value")

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
        runner.result_override = ownership.CommandResult(
            0, "x" * ownership.MAX_OUTPUT_BYTES, "x"
        )
        self.assertEqual(
            self.run_preflight(runner)["code"], "gpu_ownership_command_output_limit"
        )

    def test_total_output_and_command_count_limits_are_enforced(self):
        with mock.patch.object(ownership, "MAX_TOTAL_OUTPUT_BYTES", 16):
            self.assertEqual(
                self.run_preflight(FakeRunner())["code"],
                "gpu_ownership_command_output_limit",
            )
        with mock.patch.object(ownership, "MAX_CALLS", 1):
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
            result = ownership.run_gpu_ownership(
                CONFIG, runner=FakeRunner(), clock=lambda: tick
            )
            self.assertEqual(result["code"], "gpu_ownership_deadline_exceeded")
        values = iter((0, 121))
        result = ownership.run_gpu_ownership(
            CONFIG, runner=FakeRunner(), clock=lambda: next(values)
        )
        self.assertEqual(result["code"], "gpu_ownership_deadline_exceeded")

    def test_clock_exception_is_sanitized(self):
        def fail():
            raise RuntimeError("SECRET_CLOCK")

        result = ownership.run_gpu_ownership(CONFIG, runner=FakeRunner(), clock=fail)
        self.assertEqual(result["code"], "gpu_ownership_preflight_failed")

    def test_remote_or_unknown_runtime_profile_rejected(self):
        runner = FakeRunner()
        runner.default_runtime = "unknown"
        self.assertEqual(
            self.run_preflight(runner)["code"], "gpu_ownership_host_profile_unsupported"
        )
        self.assertEqual(ownership.DOCKER_PREFIX[2], "unix:///var/run/docker.sock")

    def test_strict_json_decode_failures_are_sanitized(self):
        for value in (
            '{"OSType":"linux","OSType":"other"}',
            '{"OSType":NaN}',
            '{"OSType":Infinity}',
            '{"OSType":-Infinity}',
        ):
            runner = FakeRunner()
            runner.result_override = ownership.CommandResult(0, value)
            self.assertEqual(
                self.run_preflight(runner)["code"], "gpu_ownership_json_invalid"
            )
        with mock.patch.object(ownership, "strict_json", side_effect=RecursionError):
            self.assertEqual(
                self.run_preflight(FakeRunner())["code"], "gpu_ownership_json_invalid"
            )

    def test_non_utf8_command_text_remains_a_sanitized_preflight_failure(self):
        runner = FakeRunner()
        runner.result_override = ownership.CommandResult(0, "\ud800private-input")
        report = self.run_preflight(runner)
        self.assertEqual("gpu_ownership_preflight_failed", report["code"])
        self.assertNotIn("private-input", json.dumps(report))

    def test_actual_mount_probe_reads_metadata_only(self):
        with tempfile.TemporaryDirectory(prefix="depositar-gpu-source.") as directory:
            source = Path(directory)
            file = source / "private"
            file.write_text("SECRET_DO_NOT_READ")
            ownership.probe_bind_source(str(file))
            ownership.probe_bind_source(str(source))
            link = source / "alias"
            link.symlink_to(file)
            with self.assertRaisesRegex(
                ownership.GPUOwnershipError, "^gpu_ownership_mount_source_alias$"
            ):
                ownership.probe_bind_source(str(link))
            with self.assertRaisesRegex(
                ownership.GPUOwnershipError, "^gpu_ownership_mount_source_unverified$"
            ):
                ownership.probe_bind_source(str(source / "missing"))

    def test_gather_probes_each_bind_and_sanitizes_failures(self):
        runner = FakeRunner([bind(container(), "/catalog")])
        probed = []
        result = self.run_preflight(
            runner, bind_source_probe=lambda source, **_limits: probed.append(source)
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
        result = ownership.bounded_runner(
            [sys.executable, "-I", "-S", "-c", "print('ok')"], timeout=2, max_bytes=128
        )
        self.assertEqual(result.stdout, "ok\n")
        with self.assertRaisesRegex(
            ownership.GPUOwnershipError, "^gpu_ownership_command_output_limit$"
        ):
            ownership.bounded_runner(
                [sys.executable, "-I", "-S", "-c", "print('x'*1024)"],
                timeout=2,
                max_bytes=128,
            )
        with self.assertRaisesRegex(
            ownership.GPUOwnershipError, "^gpu_ownership_command_timeout$"
        ):
            ownership.bounded_runner(
                [sys.executable, "-I", "-S", "-c", "import time;time.sleep(1)"],
                timeout=0.02,
                max_bytes=128,
            )


class GPUCompositionContractTests(unittest.TestCase):
    """Allocation handoff only; no real Docker normalization or runtime proof."""

    def rendered(self):
        from search.operations.deployment.composition import (
            QueryBindingPaths,
            render_composition,
        )
        from tests.unit.deployment.test_composition import REVISION, catalog, product

        config = product()
        plan = catalog(config)
        services = render_composition(
            config,
            plan,
            QueryBindingPaths.for_catalog(config, plan),
            REVISION,
            uid=1000,
            gid=1000,
            inference_source_sha256="6" * 64,
        )["inference"]["services"]
        rows, proofs, pids = [], [], {}
        for index, (name, role) in enumerate(
            (
                ("scope-model", "scope"),
                ("answer-model", "planner"),
                ("reranker", "reranker"),
            ),
            start=1,
        ):
            service = services[name]
            devices = service["deploy"]["resources"]["reservations"]["devices"]
            self.assertEqual(len(devices), 1)
            identifiers = tuple(devices[0]["device_ids"])
            self.assertEqual(devices[0]["capabilities"], ["gpu"])
            identifier, pid = str(index) * 64, 100 + index
            row = container(identifier, pid=pid, gpu_uuids=identifiers)
            row["HostConfig"]["Runtime"] = service["runtime"]
            row["NVIDIAEnv"] = [
                "NVIDIA_VISIBLE_DEVICES="
                + service["environment"]["NVIDIA_VISIBLE_DEVICES"]
            ]
            self.assertEqual(
                service["environment"]["NVIDIA_DRIVER_CAPABILITIES"], "compute,utility"
            )
            rows.append(row)
            proofs.append(
                ownership.GPUContainerProof(identifier, IMAGE, role, identifiers)
            )
            pids[identifier] = f"PID\n{pid}"
        inventory = "\n".join(
            f"{value},{index}" for index, value in enumerate(config.gpus.all_uuids())
        )
        return config, {
            "inventory": inventory,
            "containers": rows,
            "allowed": tuple(proofs),
            "container_pids": pids,
            "compute_apps": "",
        }

    def test_renderer_explicit_role_uuids_satisfy_ownership_profile(self):
        config, evidence = self.rendered()
        self.assertEqual(
            ownership.validate_gpu_ownership(config.gpus, **evidence)["status"], "ok"
        )

    def test_base_image_all_cannot_override_explicit_renderer_allocation(self):
        config, evidence = self.rendered()
        evidence["containers"][0]["NVIDIAEnv"] = ["NVIDIA_VISIBLE_DEVICES=all"]
        with self.assertRaisesRegex(
            ownership.GPUOwnershipError, "^gpu_ownership_legacy_gpu_access$"
        ):
            ownership.validate_gpu_ownership(config.gpus, **evidence)


if __name__ == "__main__":
    unittest.main()
