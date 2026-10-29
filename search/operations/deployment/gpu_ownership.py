"""Read-only, fail-closed GPU ownership snapshot for one Linux Docker host.

This is a preflight, not a reservation or a security boundary against a hostile
root/operator, Docker daemon, OCI hook, or concurrent workload change. The narrow
profile accepts exactly the four configured physical GPUs, explicit full-UUID
NVIDIA DeviceRequests, and optional exact caller-sealed container/image/role
proofs. It rejects count/index/all/CDI/MIG/legacy-only requests, custom runtimes,
host namespaces, privileged/device grants, and ambiguous mounts.

The caller must obtain GPUContainerProof from trusted sealed runtime evidence;
constructing a dataclass is not proof of trust. Optional CPUContainerProof values
allow only an independently authenticated TLS pair's fixed capabilities and
read-only TLS mounts; they never authorize GPU access or compute PIDs.
No names or labels grant access.
All observed compute PIDs must belong to an allowed container's host-PID Docker
top inventory and its configured UUIDs, even when such containers already exist.
Inspection deliberately omits ordinary environment values, labels, command
lines and health logs. No secrets or model contents are opened, no container is
created/stopped, and no port or daemon setting is changed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import PurePosixPath
import re
import selectors
import signal
import subprocess
import time

from search.operations.config import GPUConfig, GPU_UUID, ProductConfig
from search.serialization import strict_json


CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
TLS_CPU_ROLES = frozenset({"private-api-tls-inner", "private-api-tls-outer"})
TLS_OUTER_CAPS = frozenset({"SETUID", "SETGID", "DAC_OVERRIDE", "CHOWN"})
MAX_CONTAINERS = 128
MAX_GPU_ROWS = 32
MAX_PIDS = 4096
MAX_ENTRIES = 256
MAX_BIND_SOURCES = 128
MAX_LOCAL_VOLUMES = 128
VOLUME_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")
MAX_OUTPUT_BYTES = 256 * 1024
MAX_TOTAL_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_CALLS = 2 * MAX_CONTAINERS + MAX_BIND_SOURCES + 2 * MAX_LOCAL_VOLUMES + 14
TOTAL_BUDGET_SECONDS = 120
COMMAND_TIMEOUT_SECONDS = 15
ERROR_CODES = frozenset(
    "gpu_ownership_" + suffix
    for suffix in (
        "command_count_limit",
        "command_failed",
        "command_limits_invalid",
        "command_output_limit",
        "command_timeout",
        "compute_duplicate",
        "compute_invalid",
        "compute_limit",
        "compute_role_mismatch",
        "config_invalid",
        "config_overlap",
        "cpu_identity_changed",
        "cpu_profile_invalid",
        "cpu_proof_invalid",
        "container_device_access",
        "container_duplicate",
        "container_host_access",
        "container_identity_invalid",
        "container_inventory_invalid",
        "container_limit",
        "container_not_running",
        "container_shape_invalid",
        "deadline_exceeded",
        "device_scope_duplicate",
        "device_scope_unknown",
        "host_access_mount",
        "host_profile_unsupported",
        "input_limit",
        "inventory_duplicate",
        "inventory_invalid",
        "inventory_scope_changed",
        "json_invalid",
        "legacy_gpu_access",
        "main_pid_missing",
        "mount_duplicate",
        "mount_evidence_invalid",
        "mount_invalid",
        "mount_inventory_mismatch",
        "mount_path_invalid",
        "mount_propagation_unknown",
        "mount_source_alias",
        "mount_source_limit",
        "mount_source_special",
        "mount_source_unverified",
        "mount_type_unknown",
        "nvidia_environment_invalid",
        "pid_evidence_missing",
        "pid_inventory_duplicate",
        "pid_inventory_invalid",
        "pid_owner_ambiguous",
        "preflight_failed",
        "proof_invalid",
        "sealed_container_missing",
        "sealed_cpu_container_missing",
        "sealed_identity_changed",
        "snapshot_changed",
        "unknown_compute_pid",
        "unsealed_gpu_container",
        "uuid_invalid",
        "volume_evidence_invalid",
        "volume_limit",
        "volume_metadata_invalid",
    )
)
DOCKER_PREFIX = (
    "docker",
    "--host",
    "unix:///var/run/docker.sock",
    "--config",
    "/proc/self/fd",
)
NVIDIA_KEYS = (
    "NVIDIA_VISIBLE_DEVICES",
    "NVIDIA_MIG_CONFIG_DEVICES",
    "NVIDIA_MIG_MONITOR_DEVICES",
    "NVIDIA_IMEX_CHANNELS",
)
BIND_METADATA_PROGRAM = """import pathlib, stat, sys
try:
    path = pathlib.Path(sys.argv[1])
    if str(path.resolve(strict=True)) != sys.argv[1]:
        code = 'alias'
    else:
        mode = path.lstat().st_mode
        code = 'ok' if stat.S_ISREG(mode) or stat.S_ISDIR(mode) else 'special'
except (OSError, RuntimeError):
    code = 'unverified'
print(code)
"""

# A projected inspect record: no ordinary Env, Labels, Cmd, health logs, or
# DeviceRequest Options values. Terminal nulls avoid Go template comma state.
_ENV_CONDITION = "or " + " ".join(f'(eq $key "{key}")' for key in NVIDIA_KEYS)
INSPECT_FORMAT = (
    '{"Id":{{json .Id}},"Image":{{json .Image}},'
    '"RestartCount":{{json .RestartCount}},"State":{'
    '"Running":{{json .State.Running}},"Paused":{{json .State.Paused}},'
    '"Restarting":{{json .State.Restarting}},"Dead":{{json .State.Dead}},'
    '"Pid":{{json .State.Pid}},"StartedAt":{{json .State.StartedAt}}},'
    '"HostConfig":{'
    '"Runtime":{{json .HostConfig.Runtime}},'
    '"PidMode":{{json .HostConfig.PidMode}},'
    '"IpcMode":{{json .HostConfig.IpcMode}},'
    '"Privileged":{{json .HostConfig.Privileged}},'
    '"Devices":{{json .HostConfig.Devices}},'
    '"DeviceCgroupRules":{{json .HostConfig.DeviceCgroupRules}},'
    '"CapAdd":{{json .HostConfig.CapAdd}},'
    '"VolumesFrom":{{json .HostConfig.VolumesFrom}},'
    '"AnnotationsPresent":{{if index .HostConfig "Annotations"}}true{{else}}false{{end}},'
    '"Binds":{{json .HostConfig.Binds}},'
    '"MountSpecsCount":{{if index .HostConfig "Mounts"}}{{len (index .HostConfig "Mounts")}}{{else}}0{{end}},'
    '"DeviceRequests":[{{range .HostConfig.DeviceRequests}}{'
    '"Driver":{{json .Driver}},"Count":{{json .Count}},'
    '"DeviceIDs":{{json .DeviceIDs}},"Capabilities":{{json .Capabilities}},'
    '"OptionsEmpty":{{if .Options}}false{{else}}true{{end}}},{{end}}null]},'
    '"Mounts":{{json .Mounts}},"NVIDIAEnv":['
    '{{range .Config.Env}}{{$key := index (split . "=") 0}}{{if '
    + _ENV_CONDITION
    + "}}{{json .}},{{end}}{{end}}null]}"
)
VOLUME_INSPECT_FORMAT = (
    '{"name":{{json .Name}},"driver":{{json .Driver}},'
    '"scope":{{json .Scope}},'
    '"options_empty":{{if .Options}}false{{else}}true{{end}},'
    '"mountpoint":{{json .Mountpoint}},"created_at":{{json .CreatedAt}}}'
)


class GPUOwnershipError(ValueError):
    """Fixed diagnostic only; never attach inspect output or private paths."""


@dataclass(frozen=True)
class GPUContainerProof:
    container_id: str
    image_id: str
    role: str
    gpu_uuids: tuple[str, ...]


@dataclass(frozen=True)
class CPUContainerProof:
    """An independently verified private-API TLS profile, never GPU authority.

    The TLS producer authenticates its plan, material and healthy runtime first.
    A caller-created value cannot establish those facts. The digest seals every
    field of the projected local-daemon inspection, including process identity.
    """

    container_id: str
    image_id: str
    role: str
    projection_sha256: str


@dataclass(frozen=True)
class LocalVolumeProof:
    """Metadata gathered from the trusted local daemon, never from labels.

    Constructing this value does not establish trust. The collector verifies
    daemon metadata twice; pure callers must supply equivalent trusted evidence.
    No volume contents or inaccessible Docker data directories are inspected.
    """

    name: str
    driver: str
    scope: str
    options_empty: bool
    mountpoint: str
    created_at: str
    docker_root_dir: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


def bounded_runner(argv, *, timeout, max_bytes):
    """Stream bounded local CLI output; terminate only the spawned CLI group.

    Injected runners must honor the same deadline/output contract. This default
    uses no shell, no stdin, a fixed tool PATH/locale, and no caller Docker
    context/host/config or NVIDIA variables. The explicit Linux local socket
    prevents inspecting a remote Docker host against local nvidia-smi PIDs.
    """
    if (
        type(timeout) not in {int, float}
        or not math.isfinite(timeout)
        or not 0 < timeout <= COMMAND_TIMEOUT_SECONDS
        or type(max_bytes) is not int
        or not 1 <= max_bytes <= MAX_OUTPUT_BYTES
    ):
        raise GPUOwnershipError("gpu_ownership_command_limits_invalid")
    process = None
    selector = selectors.DefaultSelector()
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    completed = False
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": os.defpath, "LC_ALL": "C", "LANG": "C"},
            start_new_session=True,
        )
        for name in streams:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GPUOwnershipError("gpu_ownership_command_timeout")
            for key, _mask in selector.select(remaining):
                chunk = os.read(key.fileobj.fileno(), min(65536, max_bytes + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise GPUOwnershipError("gpu_ownership_command_output_limit")
                streams[key.data].extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GPUOwnershipError("gpu_ownership_command_timeout")
        process.wait(timeout=remaining)
        result = CommandResult(
            process.returncode,
            streams["stdout"].decode("utf-8", errors="strict"),
            streams["stderr"].decode("utf-8", errors="strict"),
        )
        completed = True
        return result
    except GPUOwnershipError:
        raise
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise GPUOwnershipError("gpu_ownership_command_failed") from None
    finally:
        selector.close()
        if process is not None:
            if not completed:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            for name in streams:
                stream = getattr(process, name)
                if stream is not None:
                    stream.close()


def _uuid(value):
    if type(value) is not str or GPU_UUID.fullmatch(value) is None:
        raise GPUOwnershipError("gpu_ownership_uuid_invalid")
    return "GPU-" + value[4:].lower()


def _roles(gpus):
    if (
        type(gpus) is not GPUConfig
        or type(gpus.planner) is not tuple
        or len(gpus.planner) != 2
    ):
        raise GPUOwnershipError("gpu_ownership_config_invalid")
    roles = {
        "scope": (_uuid(gpus.scope),),
        "planner": tuple(_uuid(value) for value in gpus.planner),
        "reranker": (_uuid(gpus.reranker),),
    }
    if len({value for values in roles.values() for value in values}) != 4:
        raise GPUOwnershipError("gpu_ownership_config_overlap")
    return roles


def _proofs(allowed, roles):
    if type(allowed) is not tuple or len(allowed) > 3:
        raise GPUOwnershipError("gpu_ownership_proof_invalid")
    result, used_roles = {}, set()
    for proof in allowed:
        if (
            type(proof) is not GPUContainerProof
            or type(proof.container_id) is not str
            or CONTAINER_ID.fullmatch(proof.container_id) is None
            or type(proof.image_id) is not str
            or IMAGE_ID.fullmatch(proof.image_id) is None
            or type(proof.role) is not str
            or proof.role not in roles
            or type(proof.gpu_uuids) is not tuple
            or not 1 <= len(proof.gpu_uuids) <= 2
            or tuple(sorted(_uuid(value) for value in proof.gpu_uuids))
            != tuple(sorted(roles[proof.role]))
            or proof.container_id in result
            or proof.role in used_roles
        ):
            raise GPUOwnershipError("gpu_ownership_proof_invalid")
        result[proof.container_id] = proof
        used_roles.add(proof.role)
    return result


def _cpu_proofs(values, gpu_proofs):
    if type(values) is not tuple or len(values) > 2:
        raise GPUOwnershipError("gpu_ownership_cpu_proof_invalid")
    result, roles = {}, set()
    for proof in values:
        if (
            type(proof) is not CPUContainerProof
            or type(proof.container_id) is not str
            or CONTAINER_ID.fullmatch(proof.container_id) is None
            or type(proof.image_id) is not str
            or IMAGE_ID.fullmatch(proof.image_id) is None
            or type(proof.role) is not str
            or proof.role not in TLS_CPU_ROLES
            or type(proof.projection_sha256) is not str
            or DIGEST.fullmatch(proof.projection_sha256) is None
            or proof.container_id in result
            or proof.container_id in gpu_proofs
            or proof.role in roles
        ):
            raise GPUOwnershipError("gpu_ownership_cpu_proof_invalid")
        result[proof.container_id] = proof
        roles.add(proof.role)
    return result


def _projection(row):
    """Retain all projected fields; normalize only mount-map presentation."""
    if type(row) is not dict or type(row.get("Mounts")) is not list:
        raise GPUOwnershipError("gpu_ownership_container_shape_invalid")
    destinations = []
    for mount in row["Mounts"]:
        if type(mount) is not dict or type(mount.get("Destination")) is not str:
            raise GPUOwnershipError("gpu_ownership_mount_invalid")
        destinations.append(mount["Destination"])
    if len(set(destinations)) != len(destinations):
        raise GPUOwnershipError("gpu_ownership_mount_duplicate")
    return {
        **row,
        "Mounts": sorted(row["Mounts"], key=lambda mount: mount["Destination"]),
    }


def cpu_projection_sha256(row):
    """Hash a full GPU inspect projection; this does not authenticate its source."""
    try:
        raw = json.dumps(
            _projection(row),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(raw) > MAX_OUTPUT_BYTES:
            raise GPUOwnershipError("gpu_ownership_container_shape_invalid")
        return hashlib.sha256(raw).hexdigest()
    except GPUOwnershipError:
        raise
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise GPUOwnershipError("gpu_ownership_container_shape_invalid") from None


def _cpu_targets(proof):
    role = proof.role.removeprefix("private-api-tls-")
    return {"/run/endpoint.conf", f"/run/tls/{role}.crt", f"/run/tls/{role}.key"} | (
        {"/run/tls/ca.pem"} if role == "outer" else set()
    )


def _text(value):
    if type(value) is not str:
        raise GPUOwnershipError("gpu_ownership_input_limit")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise GPUOwnershipError("gpu_ownership_input_limit") from None
    if size > MAX_OUTPUT_BYTES:
        raise GPUOwnershipError("gpu_ownership_input_limit")
    return value


def _inventory(raw, roles):
    rows, indexes = set(), set()
    lines = _text(raw).splitlines()
    if not 1 <= len(lines) <= MAX_GPU_ROWS:
        raise GPUOwnershipError("gpu_ownership_inventory_invalid")
    for line in lines:
        fields = [value.strip() for value in line.split(",")]
        if len(fields) != 2 or re.fullmatch(r"0|[1-9][0-9]{0,2}", fields[1]) is None:
            raise GPUOwnershipError("gpu_ownership_inventory_invalid")
        identifier = _uuid(fields[0])
        if identifier in rows or fields[1] in indexes:
            raise GPUOwnershipError("gpu_ownership_inventory_duplicate")
        rows.add(identifier)
        indexes.add(fields[1])
    if rows != {value for values in roles.values() for value in values}:
        raise GPUOwnershipError("gpu_ownership_inventory_scope_changed")
    return rows


def _list(value, *, nullable=True):
    if value is None and nullable:
        return []
    if type(value) is not list or len(value) > MAX_ENTRIES:
        raise GPUOwnershipError("gpu_ownership_container_shape_invalid")
    return value


def _path(value, *, target=False, tls_targets=()):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 4096
        or any(character in value for character in "\x00\r\n:")
        or not value.startswith("/")
        or value.startswith("//")
        or str(PurePosixPath(value)) != value
        or ".." in PurePosixPath(value).parts
    ):
        raise GPUOwnershipError("gpu_ownership_mount_path_invalid")
    path = PurePosixPath(value)
    private_target = target and (
        value in tls_targets
        or path == PurePosixPath("/run/opensearch-ca/root-ca.pem")
        or any(
            path == root or root in path.parents
            for root in (PurePosixPath("/run/secrets"), PurePosixPath("/run/index"))
        )
    )
    if (
        path == PurePosixPath("/")
        or path.parts[1] in {"dev", "proc", "sys"}
        or path.parts[1] == "run"
        and not private_target
        or path == PurePosixPath("/var")
        or path == PurePosixPath("/var/run")
        or PurePosixPath("/var/run") in path.parents
        or path.name.endswith(("docker.sock", "containerd.sock"))
    ):
        raise GPUOwnershipError("gpu_ownership_host_access_mount")
    return value


def _bind_metadata(raw):
    code = _text(raw).strip()
    if code == "ok":
        return
    if code not in {"alias", "special", "unverified"}:
        code = "unverified"
    raise GPUOwnershipError("gpu_ownership_mount_source_" + code)


def _bind_command(source):
    return ["python3", "-I", "-S", "-B", "-c", BIND_METADATA_PROGRAM, source]


def probe_bind_source(source, *, runner=None, timeout=COMMAND_TIMEOUT_SECONDS):
    """Read path metadata only: reject aliases and device/socket/FIFO leaves.

    No file contents are read. Trusted daemon/cgroup enforcement remains a
    prerequisite; this bounded subprocess leaf check is not a recursive audit.
    """
    _path(source)
    runner = bounded_runner if runner is None else runner
    result = runner(_bind_command(source), timeout=timeout, max_bytes=1024)
    if (
        type(result.returncode) is not int
        or result.returncode != 0
        or type(result.stdout) is not str
        or type(result.stderr) is not str
        or len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8"))
        > 1024
    ):
        raise GPUOwnershipError("gpu_ownership_mount_source_unverified")
    _bind_metadata(result.stdout)


def _volume_proofs(values):
    if type(values) is not tuple:
        raise GPUOwnershipError("gpu_ownership_volume_evidence_invalid")
    if len(values) > MAX_LOCAL_VOLUMES:
        raise GPUOwnershipError("gpu_ownership_volume_limit")
    result = {}
    roots = set()
    for proof in values:
        if (
            type(proof) is not LocalVolumeProof
            or type(proof.name) is not str
            or VOLUME_NAME.fullmatch(proof.name) is None
            or type(proof.driver) is not str
            or proof.driver != "local"
            or type(proof.scope) is not str
            or proof.scope != "local"
            or proof.options_empty is not True
            or type(proof.created_at) is not str
            or re.fullmatch(r"[0-9T:.Z+-]{1,64}", proof.created_at) is None
        ):
            raise GPUOwnershipError("gpu_ownership_volume_evidence_invalid")
        root = _path(proof.docker_root_dir)
        point = _path(proof.mountpoint)
        if point != root + "/volumes/" + proof.name + "/_data" or proof.name in result:
            raise GPUOwnershipError("gpu_ownership_volume_evidence_invalid")
        roots.add(root)
        result[proof.name] = proof
    if len(roots) > 1:
        raise GPUOwnershipError("gpu_ownership_volume_evidence_invalid")
    return result


def _local_volume_names(containers):
    names = set()
    for row in containers:
        for mount in _list(row.get("Mounts"), nullable=False):
            if type(mount) is not dict:
                raise GPUOwnershipError("gpu_ownership_mount_invalid")
            if mount.get("Type") != "volume":
                continue
            name = mount.get("Name")
            if type(name) is not str or VOLUME_NAME.fullmatch(name) is None:
                raise GPUOwnershipError("gpu_ownership_mount_type_unknown")
            names.add(name)
            if len(names) > MAX_LOCAL_VOLUMES:
                raise GPUOwnershipError("gpu_ownership_volume_limit")
    return names


def _collect_local_volumes(containers, docker_root_dir, command):
    names = _local_volume_names(containers)
    if not names:
        return ()
    root = _path(docker_root_dir)
    # Only the daemon root's own path metadata is checked for aliases. Never
    # enter /volumes/<name>/_data or read index/model/credential contents.
    _bind_metadata(command(_bind_command(root)))
    proofs = []
    for name in sorted(names):
        metadata = _strict_json(
            command(
                [
                    *DOCKER_PREFIX,
                    "volume",
                    "inspect",
                    "--format",
                    VOLUME_INSPECT_FORMAT,
                    name,
                ]
            )
        )
        if (
            type(metadata) is not dict
            or set(metadata)
            != {"name", "driver", "scope", "options_empty", "mountpoint", "created_at"}
            or metadata.get("name") != name
        ):
            raise GPUOwnershipError("gpu_ownership_volume_metadata_invalid")
        proof = LocalVolumeProof(**metadata, docker_root_dir=root)
        try:
            _volume_proofs((proof,))
        except GPUOwnershipError:
            raise GPUOwnershipError("gpu_ownership_volume_metadata_invalid") from None
        proofs.append(proof)
    return tuple(proofs)


def _mounts(row, cpu_proof=None):
    host = row["HostConfig"]
    sources, targets = [], set()
    mounts = _list(row.get("Mounts"), nullable=False)
    tls_targets = _cpu_targets(cpu_proof) if cpu_proof is not None else set()
    bind_targets = [
        m.get("Destination")
        for m in mounts
        if type(m) is dict and m.get("Type") == "bind"
    ]
    if cpu_proof is not None and (
        any(type(target) is not str for target in bind_targets)
        or set(bind_targets) != tls_targets
        or any(
            type(m) is not dict
            or m.get("Type") not in {"bind", "tmpfs"}
            or m.get("Type") == "tmpfs"
            and m.get("Destination") != "/tmp"
            for m in mounts
        )
    ):
        raise GPUOwnershipError("gpu_ownership_cpu_profile_invalid")
    for mount in mounts:
        if type(mount) is not dict:
            raise GPUOwnershipError("gpu_ownership_mount_invalid")
        # A volume is structurally accepted here, but grants no authority until
        # _containers matches separate daemon-owned LocalVolumeProof evidence.
        if type(mount.get("Type")) is not str or mount["Type"] not in {
            "bind",
            "tmpfs",
            "volume",
        }:
            raise GPUOwnershipError("gpu_ownership_mount_type_unknown")
        if mount["Type"] == "volume" and (
            type(mount.get("Name")) is not str
            or VOLUME_NAME.fullmatch(mount["Name"]) is None
            or type(mount.get("Driver")) is not str
            or mount["Driver"] != "local"
            or type(mount.get("RW")) is not bool
        ):
            raise GPUOwnershipError("gpu_ownership_mount_type_unknown")
        target = _path(mount.get("Destination"), target=True, tls_targets=tls_targets)
        # These are container-local query/secret destinations, not host /run.
        # The independently verified source must still be an ordinary private
        # path, and the actual mount must not permit writes at these targets.
        if PurePosixPath(target).parts[1] == "run" and (
            mount["Type"] != "bind" or mount.get("RW") is not False
        ):
            raise GPUOwnershipError("gpu_ownership_host_access_mount")
        if target in targets:
            raise GPUOwnershipError("gpu_ownership_mount_duplicate")
        targets.add(target)
        if type(mount.get("Propagation", "")) is not str or mount.get(
            "Propagation", ""
        ) not in {"", "private", "rprivate"}:
            raise GPUOwnershipError("gpu_ownership_mount_propagation_unknown")
        if mount["Type"] == "bind":
            sources.append(_path(mount.get("Source")))
        elif mount["Type"] == "volume":
            _path(mount.get("Source"))
    binds = _list(host.get("Binds"))
    if len(binds) > len(mounts):
        raise GPUOwnershipError("gpu_ownership_mount_inventory_mismatch")
    for bind in binds:
        if type(bind) is not str or len(bind) > 8192:
            raise GPUOwnershipError("gpu_ownership_mount_invalid")
        pieces = bind.split(":")
        if len(pieces) not in {2, 3}:
            raise GPUOwnershipError("gpu_ownership_mount_invalid")
        source, target = (
            pieces[0],
            _path(pieces[1], target=True, tls_targets=tls_targets),
        )
        is_bind = source.startswith("/")
        if is_bind:
            _path(source)
        elif VOLUME_NAME.fullmatch(source) is None:
            raise GPUOwnershipError("gpu_ownership_mount_path_invalid")
        if len(pieces) == 3 and not set(pieces[2].split(",")) <= {
            "ro",
            "rw",
            "private",
            "rprivate",
            "z",
            "Z",
            "nocopy",
        }:
            raise GPUOwnershipError("gpu_ownership_mount_propagation_unknown")
        if not any(
            mount.get("Type") == ("bind" if is_bind else "volume")
            and mount.get("Source" if is_bind else "Name") == source
            and mount.get("Destination") == target
            for mount in mounts
        ):
            raise GPUOwnershipError("gpu_ownership_mount_inventory_mismatch")
    count = host.get("MountSpecsCount")
    if type(count) is not int or not 0 <= count <= len(mounts):
        raise GPUOwnershipError("gpu_ownership_mount_inventory_mismatch")
    return tuple(sources)


def _container(row, cpu_proof=None):
    if type(row) is not dict or set(row) != {
        "Id",
        "Image",
        "RestartCount",
        "State",
        "HostConfig",
        "Mounts",
        "NVIDIAEnv",
    }:
        raise GPUOwnershipError("gpu_ownership_container_shape_invalid")
    identifier, image = row.get("Id"), row.get("Image")
    if (
        type(identifier) is not str
        or CONTAINER_ID.fullmatch(identifier) is None
        or type(image) is not str
        or IMAGE_ID.fullmatch(image) is None
        or type(row.get("RestartCount")) is not int
        or not 0 <= row["RestartCount"] <= 2**31 - 1
    ):
        raise GPUOwnershipError("gpu_ownership_container_identity_invalid")
    if cpu_proof is not None:
        _cpu_proofs((cpu_proof,), {})
        if (
            identifier != cpu_proof.container_id
            or image != cpu_proof.image_id
            or cpu_projection_sha256(row) != cpu_proof.projection_sha256
        ):
            raise GPUOwnershipError("gpu_ownership_cpu_identity_changed")
    host, state = row.get("HostConfig"), row.get("State")
    if (
        type(host) is not dict
        or set(host)
        != {
            "Runtime",
            "PidMode",
            "IpcMode",
            "Privileged",
            "Devices",
            "DeviceCgroupRules",
            "CapAdd",
            "VolumesFrom",
            "AnnotationsPresent",
            "Binds",
            "MountSpecsCount",
            "DeviceRequests",
        }
        or type(state) is not dict
        or set(state) != {"Running", "Paused", "Restarting", "Dead", "Pid", "StartedAt"}
    ):
        raise GPUOwnershipError("gpu_ownership_container_shape_invalid")
    if (
        state.get("Running") is not True
        or any(
            state.get(name) is not False for name in ("Paused", "Restarting", "Dead")
        )
        or type(state.get("Pid")) is not int
        or not 1 <= state["Pid"] <= 2**31 - 1
        or type(state.get("StartedAt")) is not str
        or not re.fullmatch(r"[0-9T:.Z+-]{1,64}", state["StartedAt"])
    ):
        raise GPUOwnershipError("gpu_ownership_container_not_running")
    if (
        host.get("Privileged") is not False
        or host.get("AnnotationsPresent") is not False
        or any(
            type(host.get(key)) is not str for key in ("Runtime", "PidMode", "IpcMode")
        )
        or host.get("Runtime") not in {"runc", "nvidia"}
        or host.get("PidMode") not in {"", "default"}
        or host.get("IpcMode") not in {"", "private"}
    ):
        raise GPUOwnershipError("gpu_ownership_container_host_access")
    for key in ("Devices", "DeviceCgroupRules", "VolumesFrom"):
        if key not in host or _list(host[key]):
            raise GPUOwnershipError("gpu_ownership_container_device_access")
    capabilities = _list(host.get("CapAdd"))
    if cpu_proof is None:
        if "CapAdd" not in host or capabilities:
            raise GPUOwnershipError("gpu_ownership_container_device_access")
    else:
        if (
            host["Runtime"] != "runc"
            or any(type(value) is not str for value in capabilities)
            or len(set(capabilities)) != len(capabilities)
        ):
            raise GPUOwnershipError("gpu_ownership_cpu_profile_invalid")
        capabilities = {value.removeprefix("CAP_") for value in capabilities}
        approved = (
            (TLS_OUTER_CAPS, TLS_OUTER_CAPS | {"NET_BIND_SERVICE"})
            if cpu_proof.role == "private-api-tls-outer"
            else (frozenset(),)
        )
        if capabilities not in approved:
            raise GPUOwnershipError("gpu_ownership_cpu_profile_invalid")
    requests = _list(host.get("DeviceRequests"), nullable=False)
    if cpu_proof is not None and requests:
        raise GPUOwnershipError("gpu_ownership_cpu_profile_invalid")
    exposed = set()
    for request in requests:
        if (
            type(request) is not dict
            or set(request)
            != {"Driver", "Count", "DeviceIDs", "Capabilities", "OptionsEmpty"}
            or type(request.get("Driver")) is not str
            or request.get("Driver") not in {"", "nvidia"}
            or type(request.get("Count")) is not int
            or request["Count"] != 0
            or request.get("Capabilities") != [["gpu"]]
            or request.get("OptionsEmpty") is not True
        ):
            raise GPUOwnershipError("gpu_ownership_device_scope_unknown")
        identifiers = _list(request.get("DeviceIDs"), nullable=False)
        if not 1 <= len(identifiers) <= 4:
            raise GPUOwnershipError("gpu_ownership_device_scope_unknown")
        for value in identifiers:
            try:
                identifier_uuid = _uuid(value)
            except GPUOwnershipError:
                raise GPUOwnershipError("gpu_ownership_device_scope_unknown") from None
            if identifier_uuid in exposed:
                raise GPUOwnershipError("gpu_ownership_device_scope_duplicate")
            exposed.add(identifier_uuid)
    environment = {}
    for entry in _list(row.get("NVIDIAEnv"), nullable=False):
        if type(entry) is not str or len(entry) > 4096 or "=" not in entry:
            raise GPUOwnershipError("gpu_ownership_nvidia_environment_invalid")
        key, value = entry.split("=", 1)
        if key not in NVIDIA_KEYS or key in environment or "\x00" in value:
            raise GPUOwnershipError("gpu_ownership_nvidia_environment_invalid")
        environment[key] = value
    if any(environment.get(key, "") for key in NVIDIA_KEYS[1:]):
        raise GPUOwnershipError("gpu_ownership_legacy_gpu_access")
    visible = environment.get("NVIDIA_VISIBLE_DEVICES", "")
    if visible and visible not in {"none", "void"}:
        values = visible.split(",")
        if len(values) > 4:
            raise GPUOwnershipError("gpu_ownership_legacy_gpu_access")
        try:
            visible_set = {_uuid(value) for value in values}
        except GPUOwnershipError:
            raise GPUOwnershipError("gpu_ownership_legacy_gpu_access") from None
        if len(visible_set) != len(values) or not exposed or visible_set != exposed:
            raise GPUOwnershipError("gpu_ownership_legacy_gpu_access")
    elif exposed and visible in {"none", "void"}:
        raise GPUOwnershipError("gpu_ownership_legacy_gpu_access")
    if cpu_proof is not None and environment != {"NVIDIA_VISIBLE_DEVICES": "void"}:
        raise GPUOwnershipError("gpu_ownership_cpu_profile_invalid")
    return identifier, image, frozenset(exposed), _mounts(row, cpu_proof)


def _containers(
    containers,
    proofs,
    roles,
    verified_bind_sources,
    verified_local_volumes=(),
    cpu_proofs=None,
):
    if type(containers) is not list or len(containers) > MAX_CONTAINERS:
        raise GPUOwnershipError("gpu_ownership_container_limit")
    if type(verified_bind_sources) is not frozenset:
        raise GPUOwnershipError("gpu_ownership_mount_evidence_invalid")
    volumes = _volume_proofs(verified_local_volumes)
    cpu_proofs = {} if cpu_proofs is None else cpu_proofs
    observed_volumes = set()
    result = {}
    for row in containers:
        cpu = cpu_proofs.get(row.get("Id")) if type(row) is dict else None
        identifier, image, exposed, sources = _container(row, cpu)
        if identifier in result:
            raise GPUOwnershipError("gpu_ownership_container_duplicate")
        if not set(sources) <= verified_bind_sources:
            raise GPUOwnershipError("gpu_ownership_mount_source_unverified")
        for mount in row["Mounts"]:
            if mount["Type"] != "volume":
                continue
            name = mount["Name"]
            proof = volumes.get(name)
            if proof is None or mount["Source"] != proof.mountpoint:
                raise GPUOwnershipError("gpu_ownership_volume_evidence_invalid")
            observed_volumes.add(name)
        proof = proofs.get(identifier)
        if exposed:
            if proof is None:
                raise GPUOwnershipError("gpu_ownership_unsealed_gpu_container")
            if image != proof.image_id or exposed != frozenset(roles[proof.role]):
                raise GPUOwnershipError("gpu_ownership_sealed_identity_changed")
        elif proof is not None:
            raise GPUOwnershipError("gpu_ownership_sealed_identity_changed")
        result[identifier] = row
    if not set(proofs) <= set(result):
        raise GPUOwnershipError("gpu_ownership_sealed_container_missing")
    if not set(cpu_proofs) <= set(result):
        raise GPUOwnershipError("gpu_ownership_sealed_cpu_container_missing")
    if observed_volumes != set(volumes):
        raise GPUOwnershipError("gpu_ownership_volume_evidence_invalid")
    return result


def _pids(raw):
    lines = _text(raw).splitlines()
    if not lines or lines[0].strip() != "PID" or not 2 <= len(lines) <= MAX_PIDS + 1:
        raise GPUOwnershipError("gpu_ownership_pid_inventory_invalid")
    result = set()
    for line in lines[1:]:
        value = line.strip()
        if re.fullmatch(r"[1-9][0-9]{0,9}", value) is None or int(value) > 2**31 - 1:
            raise GPUOwnershipError("gpu_ownership_pid_inventory_invalid")
        if int(value) in result:
            raise GPUOwnershipError("gpu_ownership_pid_inventory_duplicate")
        result.add(int(value))
    return result


def validate_gpu_ownership(
    gpus: GPUConfig,
    *,
    inventory: str,
    containers: list[dict],
    compute_apps: str,
    container_pids: dict[str, str],
    allowed: tuple[GPUContainerProof, ...] = (),
    cpu_proofs: tuple[CPUContainerProof, ...] = (),
    verified_bind_sources: frozenset[str] = frozenset(),
    verified_local_volumes: tuple[LocalVolumeProof, ...] = (),
) -> dict:
    """Pure snapshot validation; mount evidence is supplied by host gather.

    Raises a fixed GPUOwnershipError code; never returns raw inspect/PID/env
    output. Successful evidence is intentionally not an exclusive GPU lease.
    """
    roles = _roles(gpus)
    available = _inventory(inventory, roles)
    proofs = _proofs(allowed, roles)
    cpu = _cpu_proofs(cpu_proofs, proofs)
    by_id = _containers(
        containers,
        proofs,
        roles,
        verified_bind_sources,
        verified_local_volumes,
        cpu_proofs=cpu,
    )
    if type(container_pids) is not dict or set(container_pids) != set(proofs):
        raise GPUOwnershipError("gpu_ownership_pid_evidence_missing")
    owners = {}
    for identifier, proof in proofs.items():
        pids = _pids(container_pids[identifier])
        if by_id[identifier]["State"]["Pid"] not in pids:
            raise GPUOwnershipError("gpu_ownership_main_pid_missing")
        for pid in pids:
            if pid in owners:
                raise GPUOwnershipError("gpu_ownership_pid_owner_ambiguous")
            owners[pid] = proof
    tasks = _text(compute_apps).splitlines()
    if len(tasks) > MAX_PIDS:
        raise GPUOwnershipError("gpu_ownership_compute_limit")
    seen = set()
    for line in tasks:
        fields = [value.strip() for value in line.split(",")]
        if len(fields) != 2 or re.fullmatch(r"[1-9][0-9]{0,9}", fields[1]) is None:
            raise GPUOwnershipError("gpu_ownership_compute_invalid")
        identifier, pid = _uuid(fields[0]), int(fields[1])
        if identifier not in available or pid > 2**31 - 1:
            raise GPUOwnershipError("gpu_ownership_compute_invalid")
        if (identifier, pid) in seen:
            raise GPUOwnershipError("gpu_ownership_compute_duplicate")
        seen.add((identifier, pid))
        proof = owners.get(pid)
        if proof is None:
            raise GPUOwnershipError("gpu_ownership_unknown_compute_pid")
        if identifier not in roles[proof.role]:
            raise GPUOwnershipError("gpu_ownership_compute_role_mismatch")
    return {
        "schema_version": 1,
        "status": "ok",
        "code": "gpu_ownership_verified",
        "running_container_count": len(by_id),
        "sealed_gpu_container_count": len(proofs),
        **({"sealed_cpu_container_count": len(cpu)} if cpu else {}),
        "compute_process_count": len({pid for _identifier, pid in seen}),
        "compute_gpu_pid_record_count": len(seen),
        "gpu_count": len(available),
        "snapshot_only": True,
        "exclusive_reservation": False,
        "runtime_modified": False,
    }


def _strict_json(raw):
    try:
        return strict_json(_text(raw).encode("utf-8"))
    except (ValueError, RecursionError, UnicodeError):
        raise GPUOwnershipError("gpu_ownership_json_invalid") from None


def _container_ids(raw):
    lines = _text(raw).splitlines()
    if len(lines) > MAX_CONTAINERS or any(
        CONTAINER_ID.fullmatch(line) is None for line in lines
    ):
        raise GPUOwnershipError("gpu_ownership_container_inventory_invalid")
    if len(set(lines)) != len(lines):
        raise GPUOwnershipError("gpu_ownership_container_duplicate")
    return tuple(sorted(lines))


def _inspect(command, identifier):
    row = _strict_json(
        command([*DOCKER_PREFIX, "inspect", "--format", INSPECT_FORMAT, identifier])
    )
    if type(row) is not dict or row.get("Id") != identifier:
        raise GPUOwnershipError("gpu_ownership_container_identity_invalid")
    host = row.get("HostConfig")
    if type(host) is not dict:
        raise GPUOwnershipError("gpu_ownership_container_shape_invalid")
    for parent, key in ((row, "NVIDIAEnv"), (host, "DeviceRequests")):
        entries = parent.get(key)
        if type(entries) is not list or not entries or entries[-1] is not None:
            raise GPUOwnershipError("gpu_ownership_container_shape_invalid")
        parent[key] = entries[:-1]
    return row


def _snapshot(row, cpu_proof=None):
    """Docker's mount map has no list-order contract; retain every field.

    Validate destinations and reject duplicates before ordering. This only
    removes presentation-order noise, not changed access, paths or peers.
    """
    _container(row, cpu_proof)
    return _projection(row)


def run_gpu_ownership(
    config: ProductConfig,
    *,
    allowed: tuple[GPUContainerProof, ...] = (),
    cpu_proofs: tuple[CPUContainerProof, ...] = (),
    runner=None,
    bind_source_probe=None,
    clock=time.monotonic,
) -> dict:
    """Gather a bounded local Docker/NVIDIA snapshot and return sanitized status.

    It rechecks running full IDs and projected inspect records before success.
    This catches observed restarts/config changes, but is not atomic and cannot
    prevent work starting just after the check. No automatic retries occur.
    """
    runner = bounded_runner if runner is None else runner
    started = None
    calls, output_bytes = 0, 0

    def command(argv):
        nonlocal calls, output_bytes
        now = clock()
        if (
            type(started) not in {int, float}
            or type(now) not in {int, float}
            or not math.isfinite(started)
            or not math.isfinite(now)
            or now < started
            or now - started >= TOTAL_BUDGET_SECONDS
        ):
            raise GPUOwnershipError("gpu_ownership_deadline_exceeded")
        calls += 1
        if calls > MAX_CALLS:
            raise GPUOwnershipError("gpu_ownership_command_count_limit")
        if output_bytes >= MAX_TOTAL_OUTPUT_BYTES:
            raise GPUOwnershipError("gpu_ownership_command_output_limit")
        result = runner(
            argv,
            timeout=min(
                COMMAND_TIMEOUT_SECONDS, TOTAL_BUDGET_SECONDS - (now - started)
            ),
            max_bytes=min(MAX_OUTPUT_BYTES, MAX_TOTAL_OUTPUT_BYTES - output_bytes),
        )
        if (
            type(result.returncode) is not int
            or type(result.stdout) is not str
            or type(result.stderr) is not str
        ):
            raise GPUOwnershipError("gpu_ownership_command_failed")
        size = len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8"))
        output_bytes += size
        if size > MAX_OUTPUT_BYTES or output_bytes > MAX_TOTAL_OUTPUT_BYTES:
            raise GPUOwnershipError("gpu_ownership_command_output_limit")
        if result.returncode != 0:
            raise GPUOwnershipError("gpu_ownership_command_failed")
        return result.stdout

    try:
        started = clock()
        if type(config) is not ProductConfig:
            raise GPUOwnershipError("gpu_ownership_config_invalid")
        roles = _roles(config.gpus)
        proofs = _proofs(allowed, roles)
        cpu = _cpu_proofs(cpu_proofs, proofs)
        info = _strict_json(
            command(
                [
                    *DOCKER_PREFIX,
                    "info",
                    "--format",
                    '{"OSType":{{json .OSType}},"DefaultRuntime":{{json .DefaultRuntime}},"DockerRootDir":{{json .DockerRootDir}}}',
                ]
            )
        )
        if (
            type(info) is not dict
            or info.get("OSType") != "linux"
            or type(info.get("DefaultRuntime")) is not str
            or info["DefaultRuntime"] not in {"runc", "nvidia"}
        ):
            raise GPUOwnershipError("gpu_ownership_host_profile_unsupported")
        inventory = command(
            ["nvidia-smi", "--query-gpu=uuid,index", "--format=csv,noheader,nounits"]
        )
        _inventory(inventory, roles)
        ids = _container_ids(command([*DOCKER_PREFIX, "ps", "--quiet", "--no-trunc"]))
        containers = [_inspect(command, identifier) for identifier in ids]
        sources = {
            source
            for row in containers
            for source in _container(row, cpu.get(row.get("Id")))[3]
        }
        if len(sources) > MAX_BIND_SOURCES:
            raise GPUOwnershipError("gpu_ownership_mount_source_limit")
        for source in sorted(sources):
            if bind_source_probe is None:
                _bind_metadata(command(_bind_command(source)))
            else:
                # An injected probe is trusted test/operator code and must
                # honor this timeout just like an injected command runner.
                remaining = started + TOTAL_BUDGET_SECONDS - clock()
                if not 0 < remaining <= TOTAL_BUDGET_SECONDS:
                    raise GPUOwnershipError("gpu_ownership_deadline_exceeded")
                bind_source_probe(
                    source, timeout=min(COMMAND_TIMEOUT_SECONDS, remaining)
                )
        volumes = _collect_local_volumes(containers, info.get("DockerRootDir"), command)
        _containers(
            containers, proofs, roles, frozenset(sources), volumes, cpu_proofs=cpu
        )
        pids = {
            identifier: command([*DOCKER_PREFIX, "top", identifier, "-eo", "pid"])
            for identifier in sorted(proofs)
        }
        compute_apps = command(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid",
                "--format=csv,noheader,nounits",
            ]
        )
        result = validate_gpu_ownership(
            config.gpus,
            inventory=inventory,
            containers=containers,
            compute_apps=compute_apps,
            container_pids=pids,
            allowed=allowed,
            cpu_proofs=cpu_proofs,
            verified_bind_sources=frozenset(sources),
            verified_local_volumes=volumes,
        )
        if ids != _container_ids(
            command([*DOCKER_PREFIX, "ps", "--quiet", "--no-trunc"])
        ):
            raise GPUOwnershipError("gpu_ownership_snapshot_changed")
        after = [_inspect(command, identifier) for identifier in ids]
        if [_snapshot(row, cpu.get(row.get("Id"))) for row in containers] != [
            _snapshot(row, cpu.get(row.get("Id"))) for row in after
        ]:
            raise GPUOwnershipError("gpu_ownership_snapshot_changed")
        if volumes:
            after_info = _strict_json(
                command(
                    [
                        *DOCKER_PREFIX,
                        "info",
                        "--format",
                        '{"OSType":{{json .OSType}},"DefaultRuntime":{{json .DefaultRuntime}},"DockerRootDir":{{json .DockerRootDir}}}',
                    ]
                )
            )
            if (
                after_info != info
                or _collect_local_volumes(after, info["DockerRootDir"], command)
                != volumes
            ):
                raise GPUOwnershipError("gpu_ownership_snapshot_changed")
        now = clock()
        if (
            not math.isfinite(now)
            or not started <= now < started + TOTAL_BUDGET_SECONDS
        ):
            raise GPUOwnershipError("gpu_ownership_deadline_exceeded")
        result["command_count"] = calls
        return result
    except GPUOwnershipError as error:
        code = str(error)
        if code not in ERROR_CODES:
            code = "gpu_ownership_preflight_failed"
    except Exception:
        # Injected tool/filesystem diagnostics may contain secrets. Never expose
        # exception text or result stdout/stderr in the public preflight report.
        code = "gpu_ownership_preflight_failed"
    return {
        "schema_version": 1,
        "status": "blocked",
        "code": code,
        "snapshot_only": True,
        "exclusive_reservation": False,
        "runtime_modified": False,
    }
