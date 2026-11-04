"""GPU ownership fixtures fixtures and regressions."""

from __future__ import annotations
import copy
from dataclasses import asdict
import json
from pathlib import Path
from search.operations.config import GPUConfig, ProductConfig
from search.operations.deployment.gpu import contract as gpu_contract
from search.operations.deployment.gpu import snapshot as gpu_snapshot
from search.operations.deployment.gpu import collect as gpu_collect


UUIDS = tuple(
    f"GPU-00000000-0000-0000-0000-{index:012d}" for index in range(1, 5)
)


GPUS = GPUConfig(UUIDS[0], (UUIDS[1], UUIDS[2]), UUIDS[3])


CONFIG = ProductConfig(
    2,
    Path("/state"),
    Path("/api"),
    Path("/embedding"),
    Path("/reranker"),
    GPUS,
)


INVENTORY = "\n".join(f"{value}, {index}" for index, value in enumerate(UUIDS))


IMAGE = "sha256:" + "a" * 64


SCOPE_ID = "1" * 64


PLANNER_ID = "2" * 64


RERANKER_ID = "3" * 64


CPU_ID = "4" * 64


SCOPE = gpu_contract.GPUContainerProof(SCOPE_ID, IMAGE, "scope", (UUIDS[0],))


PLANNER = gpu_contract.GPUContainerProof(
    PLANNER_ID, IMAGE, "planner", UUIDS[1:3]
)


RERANKER = gpu_contract.GPUContainerProof(
    RERANKER_ID, IMAGE, "reranker", (UUIDS[3],)
)


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


def tls_container(role="inner", *, identifier=None):
    row = container(identifier or ("8" if role == "inner" else "9") * 64)
    row["NVIDIAEnv"] = ["NVIDIA_VISIBLE_DEVICES=void"]
    row["HostConfig"]["CapAdd"] = (
        sorted(gpu_contract.TLS_OUTER_CAPS | {"NET_BIND_SERVICE"})
        if role == "outer"
        else None
    )
    targets = [
        "/run/endpoint.conf",
        f"/run/tls/{role}.crt",
        f"/run/tls/{role}.key",
    ]
    if role == "outer":
        targets.append("/run/tls/ca.pem")
    row["Mounts"] = [
        {
            "Type": "bind",
            "Source": f"/private/tls/{role}-{index}",
            "Destination": target,
            "Propagation": "rprivate",
            "RW": False,
        }
        for index, target in enumerate(targets)
    ]
    row["HostConfig"]["Binds"] = [
        f"{m['Source']}:{m['Destination']}:ro,rprivate" for m in row["Mounts"]
    ]
    return row


def tls_proof(row, role="inner"):
    # Pure tests supply the independently verified facts required by the API.
    return gpu_contract.CPUContainerProof(
        row["Id"],
        row["Image"],
        "private-api-tls-" + role,
        gpu_snapshot.cpu_projection_sha256(row),
    )


def tls_sources(*rows):
    return frozenset(
        m["Source"]
        for row in rows
        for m in row["Mounts"]
        if m.get("Type") == "bind"
    )


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
            result = (
                INVENTORY
                if argv[1] == "--query-gpu=uuid,index"
                else self.compute
            )
        elif argv[0] == "python3":
            if argv[1:5] != ["-I", "-S", "-B", "-c"]:
                raise AssertionError("metadata probe must use isolated Python")
            result = "ok\n"
        else:
            if (
                tuple(argv[: len(gpu_collect.DOCKER_PREFIX)])
                != gpu_collect.DOCKER_PREFIX
            ):
                raise AssertionError("wrong local Docker command prefix")
            command = argv[len(gpu_collect.DOCKER_PREFIX)]
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
        return gpu_contract.CommandResult(0, result)


def local_volume_proof(name="catalog-index", root="/var/lib/docker"):
    return gpu_contract.LocalVolumeProof(
        name=name,
        driver="local",
        scope="local",
        options_empty=True,
        mountpoint=root + "/volumes/" + name + "/_data",
        created_at="2026-10-21T00:00:00Z",
        docker_root_dir=root,
    )


def volume_container(proof=None):
    proof = local_volume_proof() if proof is None else proof
    row = container()
    row["HostConfig"]["MountSpecsCount"] = 1
    row["Mounts"] = [
        {
            "Type": "volume",
            "Name": proof.name,
            "Driver": "local",
            "Source": proof.mountpoint,
            "Destination": "/usr/share/opensearch/data",
            "RW": True,
            "Propagation": "",
        }
    ]
    return row


class LocalVolumeRunner(FakeRunner):
    def __init__(self, containers=()):
        super().__init__(containers)
        self.root = "/var/lib/docker"
        self.volume_calls = 0
        self.info_calls = 0
        self.changed_metadata = None
        self.changed_root = None

    def __call__(self, argv, **limits):
        if (
            argv[0] == "docker"
            and argv[len(gpu_collect.DOCKER_PREFIX)] == "volume"
        ):
            self.calls.append((argv, limits["timeout"], limits["max_bytes"]))
            self.volume_calls += 1
            metadata = asdict(local_volume_proof(argv[-1], self.root))
            del metadata["docker_root_dir"]
            if self.changed_metadata is not None:
                self.changed_metadata(metadata, self.volume_calls)
            return gpu_contract.CommandResult(0, json.dumps(metadata))
        result = super().__call__(argv, **limits)
        if (
            argv[0] == "docker"
            and argv[len(gpu_collect.DOCKER_PREFIX)] == "info"
        ):
            self.info_calls += 1
            info = json.loads(result.stdout)
            info["DockerRootDir"] = (
                self.changed_root
                if self.info_calls > 1 and self.changed_root is not None
                else self.root
            )
            return gpu_contract.CommandResult(0, json.dumps(info))
        return result
