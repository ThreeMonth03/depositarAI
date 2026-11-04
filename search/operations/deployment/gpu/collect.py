"""Read-only, fail-closed GPU ownership snapshot for one Linux Docker host.

This is a preflight, not a reservation or a security boundary against a hostile
root/operator, Docker daemon, OCI hook, or concurrent workload change. The
narrow
profile accepts exactly the four configured physical GPUs, explicit full-UUID
NVIDIA DeviceRequests, and optional exact caller-sealed container/image/role
proofs. It rejects count/index/all/CDI/MIG/legacy-only requests, custom
runtimes,
host namespaces, privileged/device grants, and ambiguous mounts.

The caller must obtain GPUContainerProof from trusted sealed runtime evidence;
constructing a dataclass is not proof of trust. Optional CPUContainerProof
values
allow only an independently authenticated TLS pair's fixed capabilities and
read-only TLS mounts; they never authorize GPU access or compute PIDs.
No names or labels grant access.
All observed compute PIDs must belong to an allowed container's host-PID Docker
top inventory and its configured UUIDs, even when such containers already
exist.
Inspection deliberately omits ordinary environment values, labels, command
lines and health logs. No secrets or model contents are opened, no container is
created/stopped, and no port or daemon setting is changed.
"""

from __future__ import annotations
import math
import os
import selectors
import signal
import subprocess
import time
from search.operations.config import ProductConfig
from search.serialization import strict_json
from . import contract, snapshot


MAX_BIND_SOURCES = 128


MAX_TOTAL_OUTPUT_BYTES = 8 * 1024 * 1024


MAX_CALLS = (
    2 * contract.MAX_CONTAINERS
    + MAX_BIND_SOURCES
    + 2 * contract.MAX_LOCAL_VOLUMES
    + 14
)


TOTAL_BUDGET_SECONDS = 120


COMMAND_TIMEOUT_SECONDS = 15


DOCKER_PREFIX = (
    "docker",
    "--host",
    "unix:///var/run/docker.sock",
    "--config",
    "/proc/self/fd",
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
_ENV_CONDITION = "or " + " ".join(
    f'(eq $key "{key}")' for key in contract.NVIDIA_KEYS
)


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
    '"AnnotationsPresent":{{if index .HostConfig '
    '"Annotations"}}true{{else}}false{{end}},'
    '"Binds":{{json .HostConfig.Binds}},'
    '"MountSpecsCount":{{if index .HostConfig "Mounts"}}{{len (index '
    '.HostConfig "Mounts")}}{{else}}0{{end}},'
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
        or not 1 <= max_bytes <= contract.MAX_OUTPUT_BYTES
    ):
        raise contract.GPUOwnershipError(
            "gpu_ownership_command_limits_invalid"
        )
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
                raise contract.GPUOwnershipError(
                    "gpu_ownership_command_timeout"
                )
            for key, _mask in selector.select(remaining):
                chunk = os.read(
                    key.fileobj.fileno(), min(65536, max_bytes + 1)
                )
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise contract.GPUOwnershipError(
                        "gpu_ownership_command_output_limit"
                    )
                streams[key.data].extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise contract.GPUOwnershipError("gpu_ownership_command_timeout")
        process.wait(timeout=remaining)
        result = contract.CommandResult(
            process.returncode,
            streams["stdout"].decode("utf-8", errors="strict"),
            streams["stderr"].decode("utf-8", errors="strict"),
        )
        completed = True
        return result
    except contract.GPUOwnershipError:
        raise
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise contract.GPUOwnershipError(
            "gpu_ownership_command_failed"
        ) from None
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


def _bind_metadata(raw):
    code = snapshot._text(raw).strip()
    if code == "ok":
        return
    if code not in {"alias", "special", "unverified"}:
        code = "unverified"
    raise contract.GPUOwnershipError("gpu_ownership_mount_source_" + code)


def _bind_command(source):
    return ["python3", "-I", "-S", "-B", "-c", BIND_METADATA_PROGRAM, source]


def probe_bind_source(source, *, runner=None, timeout=COMMAND_TIMEOUT_SECONDS):
    """Read path metadata only: reject aliases and device/socket/FIFO leaves.

    No file contents are read. Trusted daemon/cgroup enforcement remains a
    prerequisite; this bounded subprocess leaf check is not a recursive audit.
    """
    snapshot._path(source)
    runner = bounded_runner if runner is None else runner
    result = runner(_bind_command(source), timeout=timeout, max_bytes=1024)
    if (
        type(result.returncode) is not int
        or result.returncode != 0
        or type(result.stdout) is not str
        or type(result.stderr) is not str
        or len(result.stdout.encode("utf-8"))
        + len(result.stderr.encode("utf-8"))
        > 1024
    ):
        raise contract.GPUOwnershipError(
            "gpu_ownership_mount_source_unverified"
        )
    _bind_metadata(result.stdout)


def _collect_local_volumes(containers, docker_root_dir, command):
    names = snapshot._local_volume_names(containers)
    if not names:
        return ()
    root = snapshot._path(docker_root_dir)
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
            != {
                "name",
                "driver",
                "scope",
                "options_empty",
                "mountpoint",
                "created_at",
            }
            or metadata.get("name") != name
        ):
            raise contract.GPUOwnershipError(
                "gpu_ownership_volume_metadata_invalid"
            )
        proof = contract.LocalVolumeProof(**metadata, docker_root_dir=root)
        try:
            snapshot._volume_proofs((proof,))
        except contract.GPUOwnershipError:
            raise contract.GPUOwnershipError(
                "gpu_ownership_volume_metadata_invalid"
            ) from None
        proofs.append(proof)
    return tuple(proofs)


def _strict_json(raw):
    try:
        return strict_json(snapshot._text(raw).encode("utf-8"))
    except (ValueError, RecursionError, UnicodeError):
        raise contract.GPUOwnershipError(
            "gpu_ownership_json_invalid"
        ) from None


def _container_ids(raw):
    lines = snapshot._text(raw).splitlines()
    if len(lines) > contract.MAX_CONTAINERS or any(
        contract.CONTAINER_ID.fullmatch(line) is None for line in lines
    ):
        raise contract.GPUOwnershipError(
            "gpu_ownership_container_inventory_invalid"
        )
    if len(set(lines)) != len(lines):
        raise contract.GPUOwnershipError("gpu_ownership_container_duplicate")
    return tuple(sorted(lines))


def _inspect(command, identifier):
    row = _strict_json(
        command(
            [*DOCKER_PREFIX, "inspect", "--format", INSPECT_FORMAT, identifier]
        )
    )
    if type(row) is not dict or row.get("Id") != identifier:
        raise contract.GPUOwnershipError(
            "gpu_ownership_container_identity_invalid"
        )
    host = row.get("HostConfig")
    if type(host) is not dict:
        raise contract.GPUOwnershipError(
            "gpu_ownership_container_shape_invalid"
        )
    for parent, key in ((row, "NVIDIAEnv"), (host, "DeviceRequests")):
        entries = parent.get(key)
        if type(entries) is not list or not entries or entries[-1] is not None:
            raise contract.GPUOwnershipError(
                "gpu_ownership_container_shape_invalid"
            )
        parent[key] = entries[:-1]
    return row


def run_gpu_ownership(
    config: ProductConfig,
    *,
    allowed: tuple[contract.GPUContainerProof, ...] = (),
    cpu_proofs: tuple[contract.CPUContainerProof, ...] = (),
    runner=None,
    bind_source_probe=None,
    clock=time.monotonic,
) -> dict:
    """Gather a bounded local Docker/NVIDIA snapshot and return sanitized
    status.

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
            raise contract.GPUOwnershipError("gpu_ownership_deadline_exceeded")
        calls += 1
        if calls > MAX_CALLS:
            raise contract.GPUOwnershipError(
                "gpu_ownership_command_count_limit"
            )
        if output_bytes >= MAX_TOTAL_OUTPUT_BYTES:
            raise contract.GPUOwnershipError(
                "gpu_ownership_command_output_limit"
            )
        result = runner(
            argv,
            timeout=min(
                COMMAND_TIMEOUT_SECONDS, TOTAL_BUDGET_SECONDS - (now - started)
            ),
            max_bytes=min(
                contract.MAX_OUTPUT_BYTES,
                MAX_TOTAL_OUTPUT_BYTES - output_bytes,
            ),
        )
        if (
            type(result.returncode) is not int
            or type(result.stdout) is not str
            or type(result.stderr) is not str
        ):
            raise contract.GPUOwnershipError("gpu_ownership_command_failed")
        size = len(result.stdout.encode("utf-8")) + len(
            result.stderr.encode("utf-8")
        )
        output_bytes += size
        if (
            size > contract.MAX_OUTPUT_BYTES
            or output_bytes > MAX_TOTAL_OUTPUT_BYTES
        ):
            raise contract.GPUOwnershipError(
                "gpu_ownership_command_output_limit"
            )
        if result.returncode != 0:
            raise contract.GPUOwnershipError("gpu_ownership_command_failed")
        return result.stdout

    try:
        started = clock()
        if type(config) is not ProductConfig:
            raise contract.GPUOwnershipError("gpu_ownership_config_invalid")
        roles = snapshot._roles(config.gpus)
        proofs = snapshot._proofs(allowed, roles)
        cpu = snapshot._cpu_proofs(cpu_proofs, proofs)
        info = _strict_json(
            command(
                [
                    *DOCKER_PREFIX,
                    "info",
                    "--format",
                    '{"OSType":{{json .OSType}},"DefaultRuntime":{{json '
                    '.DefaultRuntime}},"DockerRootDir":{{json '
                    ".DockerRootDir}}}",
                ]
            )
        )
        if (
            type(info) is not dict
            or info.get("OSType") != "linux"
            or type(info.get("DefaultRuntime")) is not str
            or info["DefaultRuntime"] not in {"runc", "nvidia"}
        ):
            raise contract.GPUOwnershipError(
                "gpu_ownership_host_profile_unsupported"
            )
        inventory = command(
            [
                "nvidia-smi",
                "--query-gpu=uuid,index",
                "--format=csv,noheader,nounits",
            ]
        )
        snapshot._inventory(inventory, roles)
        ids = _container_ids(
            command([*DOCKER_PREFIX, "ps", "--quiet", "--no-trunc"])
        )
        containers = [_inspect(command, identifier) for identifier in ids]
        sources = {
            source
            for row in containers
            for source in snapshot._container(row, cpu.get(row.get("Id")))[3]
        }
        if len(sources) > MAX_BIND_SOURCES:
            raise contract.GPUOwnershipError(
                "gpu_ownership_mount_source_limit"
            )
        for source in sorted(sources):
            if bind_source_probe is None:
                _bind_metadata(command(_bind_command(source)))
            else:
                # An injected probe is trusted test/operator code and must
                # honor this timeout just like an injected command runner.
                remaining = started + TOTAL_BUDGET_SECONDS - clock()
                if not 0 < remaining <= TOTAL_BUDGET_SECONDS:
                    raise contract.GPUOwnershipError(
                        "gpu_ownership_deadline_exceeded"
                    )
                bind_source_probe(
                    source, timeout=min(COMMAND_TIMEOUT_SECONDS, remaining)
                )
        volumes = _collect_local_volumes(
            containers, info.get("DockerRootDir"), command
        )
        snapshot._containers(
            containers,
            proofs,
            roles,
            frozenset(sources),
            volumes,
            cpu_proofs=cpu,
        )
        pids = {
            identifier: command(
                [*DOCKER_PREFIX, "top", identifier, "-eo", "pid"]
            )
            for identifier in sorted(proofs)
        }
        compute_apps = command(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid",
                "--format=csv,noheader,nounits",
            ]
        )
        result = snapshot.validate_gpu_ownership(
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
            raise contract.GPUOwnershipError("gpu_ownership_snapshot_changed")
        after = [_inspect(command, identifier) for identifier in ids]
        if [
            snapshot._snapshot(row, cpu.get(row.get("Id")))
            for row in containers
        ] != [
            snapshot._snapshot(row, cpu.get(row.get("Id"))) for row in after
        ]:
            raise contract.GPUOwnershipError("gpu_ownership_snapshot_changed")
        if volumes:
            after_info = _strict_json(
                command(
                    [
                        *DOCKER_PREFIX,
                        "info",
                        "--format",
                        '{"OSType":{{json .OSType}},"DefaultRuntime":{{jso'
                        'n .DefaultRuntime}},"DockerRootDir":{{json '
                        ".DockerRootDir}}}",
                    ]
                )
            )
            if (
                after_info != info
                or _collect_local_volumes(
                    after, info["DockerRootDir"], command
                )
                != volumes
            ):
                raise contract.GPUOwnershipError(
                    "gpu_ownership_snapshot_changed"
                )
        now = clock()
        if (
            not math.isfinite(now)
            or not started <= now < started + TOTAL_BUDGET_SECONDS
        ):
            raise contract.GPUOwnershipError("gpu_ownership_deadline_exceeded")
        result["command_count"] = calls
        return result
    except contract.GPUOwnershipError as error:
        code = str(error)
        if code not in contract.ERROR_CODES:
            code = "gpu_ownership_preflight_failed"
    except Exception:
        # Injected tool/filesystem diagnostics may contain secrets. Never
        # expose
        # exception text or result stdout/stderr in the public preflight
        # report.
        code = "gpu_ownership_preflight_failed"
    return {
        "schema_version": 1,
        "status": "blocked",
        "code": code,
        "snapshot_only": True,
        "exclusive_reservation": False,
        "runtime_modified": False,
    }
