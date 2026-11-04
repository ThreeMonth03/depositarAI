"""Pure ownership proof of authenticated, unfiltered host snapshot facts."""

from __future__ import annotations
import hashlib
import json
from pathlib import PurePosixPath
import re
from search.operations.config import GPUConfig, GPU_UUID
from . import contract


def _uuid(value):
    if type(value) is not str or GPU_UUID.fullmatch(value) is None:
        raise contract.GPUOwnershipError("gpu_ownership_uuid_invalid")
    return "GPU-" + value[4:].lower()


def _roles(gpus):
    if (
        type(gpus) is not GPUConfig
        or type(gpus.planner) is not tuple
        or len(gpus.planner) != 2
    ):
        raise contract.GPUOwnershipError("gpu_ownership_config_invalid")
    roles = {
        "scope": (_uuid(gpus.scope),),
        "planner": tuple(_uuid(value) for value in gpus.planner),
        "reranker": (_uuid(gpus.reranker),),
    }
    if len({value for values in roles.values() for value in values}) != 4:
        raise contract.GPUOwnershipError("gpu_ownership_config_overlap")
    return roles


def _proofs(allowed, roles):
    if type(allowed) is not tuple or len(allowed) > 3:
        raise contract.GPUOwnershipError("gpu_ownership_proof_invalid")
    result, used_roles = {}, set()
    for proof in allowed:
        if (
            type(proof) is not contract.GPUContainerProof
            or type(proof.container_id) is not str
            or contract.CONTAINER_ID.fullmatch(proof.container_id) is None
            or type(proof.image_id) is not str
            or contract.IMAGE_ID.fullmatch(proof.image_id) is None
            or type(proof.role) is not str
            or proof.role not in roles
            or type(proof.gpu_uuids) is not tuple
            or not 1 <= len(proof.gpu_uuids) <= 2
            or tuple(sorted(_uuid(value) for value in proof.gpu_uuids))
            != tuple(sorted(roles[proof.role]))
            or proof.container_id in result
            or proof.role in used_roles
        ):
            raise contract.GPUOwnershipError("gpu_ownership_proof_invalid")
        result[proof.container_id] = proof
        used_roles.add(proof.role)
    return result


def _cpu_proofs(values, gpu_proofs):
    if type(values) is not tuple or len(values) > 2:
        raise contract.GPUOwnershipError("gpu_ownership_cpu_proof_invalid")
    result, roles = {}, set()
    for proof in values:
        if (
            type(proof) is not contract.CPUContainerProof
            or type(proof.container_id) is not str
            or contract.CONTAINER_ID.fullmatch(proof.container_id) is None
            or type(proof.image_id) is not str
            or contract.IMAGE_ID.fullmatch(proof.image_id) is None
            or type(proof.role) is not str
            or proof.role not in contract.TLS_CPU_ROLES
            or type(proof.projection_sha256) is not str
            or contract.DIGEST.fullmatch(proof.projection_sha256) is None
            or proof.container_id in result
            or proof.container_id in gpu_proofs
            or proof.role in roles
        ):
            raise contract.GPUOwnershipError("gpu_ownership_cpu_proof_invalid")
        result[proof.container_id] = proof
        roles.add(proof.role)
    return result


def _projection(row):
    """Retain all projected fields; normalize only mount-map presentation."""
    if type(row) is not dict or type(row.get("Mounts")) is not list:
        raise contract.GPUOwnershipError(
            "gpu_ownership_container_shape_invalid"
        )
    destinations = []
    for mount in row["Mounts"]:
        if (
            type(mount) is not dict
            or type(mount.get("Destination")) is not str
        ):
            raise contract.GPUOwnershipError("gpu_ownership_mount_invalid")
        destinations.append(mount["Destination"])
    if len(set(destinations)) != len(destinations):
        raise contract.GPUOwnershipError("gpu_ownership_mount_duplicate")
    return {
        **row,
        "Mounts": sorted(
            row["Mounts"], key=lambda mount: mount["Destination"]
        ),
    }


def cpu_projection_sha256(row):
    """Hash a full GPU inspect projection; this does not authenticate its
    source.
    """
    try:
        raw = json.dumps(
            _projection(row),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(raw) > contract.MAX_OUTPUT_BYTES:
            raise contract.GPUOwnershipError(
                "gpu_ownership_container_shape_invalid"
            )
        return hashlib.sha256(raw).hexdigest()
    except contract.GPUOwnershipError:
        raise
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise contract.GPUOwnershipError(
            "gpu_ownership_container_shape_invalid"
        ) from None


def _cpu_targets(proof):
    role = proof.role.removeprefix("private-api-tls-")
    return {
        "/run/endpoint.conf",
        f"/run/tls/{role}.crt",
        f"/run/tls/{role}.key",
    } | ({"/run/tls/ca.pem"} if role == "outer" else set())


def _text(value):
    if type(value) is not str:
        raise contract.GPUOwnershipError("gpu_ownership_input_limit")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise contract.GPUOwnershipError("gpu_ownership_input_limit") from None
    if size > contract.MAX_OUTPUT_BYTES:
        raise contract.GPUOwnershipError("gpu_ownership_input_limit")
    return value


def _inventory(raw, roles):
    rows, indexes = set(), set()
    lines = _text(raw).splitlines()
    if not 1 <= len(lines) <= contract.MAX_GPU_ROWS:
        raise contract.GPUOwnershipError("gpu_ownership_inventory_invalid")
    for line in lines:
        fields = [value.strip() for value in line.split(",")]
        if (
            len(fields) != 2
            or re.fullmatch(r"0|[1-9][0-9]{0,2}", fields[1]) is None
        ):
            raise contract.GPUOwnershipError("gpu_ownership_inventory_invalid")
        identifier = _uuid(fields[0])
        if identifier in rows or fields[1] in indexes:
            raise contract.GPUOwnershipError(
                "gpu_ownership_inventory_duplicate"
            )
        rows.add(identifier)
        indexes.add(fields[1])
    if rows != {value for values in roles.values() for value in values}:
        raise contract.GPUOwnershipError(
            "gpu_ownership_inventory_scope_changed"
        )
    return rows


def _list(value, *, nullable=True):
    if value is None and nullable:
        return []
    if type(value) is not list or len(value) > contract.MAX_ENTRIES:
        raise contract.GPUOwnershipError(
            "gpu_ownership_container_shape_invalid"
        )
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
        raise contract.GPUOwnershipError("gpu_ownership_mount_path_invalid")
    path = PurePosixPath(value)
    private_target = target and (
        value in tls_targets
        or path == PurePosixPath("/run/opensearch-ca/root-ca.pem")
        or any(
            path == root or root in path.parents
            for root in (
                PurePosixPath("/run/secrets"),
                PurePosixPath("/run/index"),
            )
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
        raise contract.GPUOwnershipError("gpu_ownership_host_access_mount")
    return value


def _volume_proofs(values):
    if type(values) is not tuple:
        raise contract.GPUOwnershipError(
            "gpu_ownership_volume_evidence_invalid"
        )
    if len(values) > contract.MAX_LOCAL_VOLUMES:
        raise contract.GPUOwnershipError("gpu_ownership_volume_limit")
    result = {}
    roots = set()
    for proof in values:
        if (
            type(proof) is not contract.LocalVolumeProof
            or type(proof.name) is not str
            or contract.VOLUME_NAME.fullmatch(proof.name) is None
            or type(proof.driver) is not str
            or proof.driver != "local"
            or type(proof.scope) is not str
            or proof.scope != "local"
            or proof.options_empty is not True
            or type(proof.created_at) is not str
            or re.fullmatch(r"[0-9T:.Z+-]{1,64}", proof.created_at) is None
        ):
            raise contract.GPUOwnershipError(
                "gpu_ownership_volume_evidence_invalid"
            )
        root = _path(proof.docker_root_dir)
        point = _path(proof.mountpoint)
        if (
            point != root + "/volumes/" + proof.name + "/_data"
            or proof.name in result
        ):
            raise contract.GPUOwnershipError(
                "gpu_ownership_volume_evidence_invalid"
            )
        roots.add(root)
        result[proof.name] = proof
    if len(roots) > 1:
        raise contract.GPUOwnershipError(
            "gpu_ownership_volume_evidence_invalid"
        )
    return result


def _local_volume_names(containers):
    names = set()
    for row in containers:
        for mount in _list(row.get("Mounts"), nullable=False):
            if type(mount) is not dict:
                raise contract.GPUOwnershipError("gpu_ownership_mount_invalid")
            if mount.get("Type") != "volume":
                continue
            name = mount.get("Name")
            if (
                type(name) is not str
                or contract.VOLUME_NAME.fullmatch(name) is None
            ):
                raise contract.GPUOwnershipError(
                    "gpu_ownership_mount_type_unknown"
                )
            names.add(name)
            if len(names) > contract.MAX_LOCAL_VOLUMES:
                raise contract.GPUOwnershipError("gpu_ownership_volume_limit")
    return names


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
        raise contract.GPUOwnershipError("gpu_ownership_cpu_profile_invalid")
    for mount in mounts:
        if type(mount) is not dict:
            raise contract.GPUOwnershipError("gpu_ownership_mount_invalid")
        # A volume is structurally accepted here, but grants no authority until
        # _containers matches separate daemon-owned LocalVolumeProof evidence.
        if type(mount.get("Type")) is not str or mount["Type"] not in {
            "bind",
            "tmpfs",
            "volume",
        }:
            raise contract.GPUOwnershipError(
                "gpu_ownership_mount_type_unknown"
            )
        if mount["Type"] == "volume" and (
            type(mount.get("Name")) is not str
            or contract.VOLUME_NAME.fullmatch(mount["Name"]) is None
            or type(mount.get("Driver")) is not str
            or mount["Driver"] != "local"
            or type(mount.get("RW")) is not bool
        ):
            raise contract.GPUOwnershipError(
                "gpu_ownership_mount_type_unknown"
            )
        target = _path(
            mount.get("Destination"), target=True, tls_targets=tls_targets
        )
        # These are container-local query/secret destinations, not host /run.
        # The independently verified source must still be an ordinary private
        # path, and the actual mount must not permit writes at these targets.
        if PurePosixPath(target).parts[1] == "run" and (
            mount["Type"] != "bind" or mount.get("RW") is not False
        ):
            raise contract.GPUOwnershipError("gpu_ownership_host_access_mount")
        if target in targets:
            raise contract.GPUOwnershipError("gpu_ownership_mount_duplicate")
        targets.add(target)
        if type(mount.get("Propagation", "")) is not str or mount.get(
            "Propagation", ""
        ) not in {"", "private", "rprivate"}:
            raise contract.GPUOwnershipError(
                "gpu_ownership_mount_propagation_unknown"
            )
        if mount["Type"] == "bind":
            sources.append(_path(mount.get("Source")))
        elif mount["Type"] == "volume":
            _path(mount.get("Source"))
    binds = _list(host.get("Binds"))
    if len(binds) > len(mounts):
        raise contract.GPUOwnershipError(
            "gpu_ownership_mount_inventory_mismatch"
        )
    for bind in binds:
        if type(bind) is not str or len(bind) > 8192:
            raise contract.GPUOwnershipError("gpu_ownership_mount_invalid")
        pieces = bind.split(":")
        if len(pieces) not in {2, 3}:
            raise contract.GPUOwnershipError("gpu_ownership_mount_invalid")
        source, target = (
            pieces[0],
            _path(pieces[1], target=True, tls_targets=tls_targets),
        )
        is_bind = source.startswith("/")
        if is_bind:
            _path(source)
        elif contract.VOLUME_NAME.fullmatch(source) is None:
            raise contract.GPUOwnershipError(
                "gpu_ownership_mount_path_invalid"
            )
        if len(pieces) == 3 and not set(pieces[2].split(",")) <= {
            "ro",
            "rw",
            "private",
            "rprivate",
            "z",
            "Z",
            "nocopy",
        }:
            raise contract.GPUOwnershipError(
                "gpu_ownership_mount_propagation_unknown"
            )
        if not any(
            mount.get("Type") == ("bind" if is_bind else "volume")
            and mount.get("Source" if is_bind else "Name") == source
            and mount.get("Destination") == target
            for mount in mounts
        ):
            raise contract.GPUOwnershipError(
                "gpu_ownership_mount_inventory_mismatch"
            )
    count = host.get("MountSpecsCount")
    if type(count) is not int or not 0 <= count <= len(mounts):
        raise contract.GPUOwnershipError(
            "gpu_ownership_mount_inventory_mismatch"
        )
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
        raise contract.GPUOwnershipError(
            "gpu_ownership_container_shape_invalid"
        )
    identifier, image = row.get("Id"), row.get("Image")
    if (
        type(identifier) is not str
        or contract.CONTAINER_ID.fullmatch(identifier) is None
        or type(image) is not str
        or contract.IMAGE_ID.fullmatch(image) is None
        or type(row.get("RestartCount")) is not int
        or not 0 <= row["RestartCount"] <= 2**31 - 1
    ):
        raise contract.GPUOwnershipError(
            "gpu_ownership_container_identity_invalid"
        )
    if cpu_proof is not None:
        _cpu_proofs((cpu_proof,), {})
        if (
            identifier != cpu_proof.container_id
            or image != cpu_proof.image_id
            or cpu_projection_sha256(row) != cpu_proof.projection_sha256
        ):
            raise contract.GPUOwnershipError(
                "gpu_ownership_cpu_identity_changed"
            )
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
        or set(state)
        != {"Running", "Paused", "Restarting", "Dead", "Pid", "StartedAt"}
    ):
        raise contract.GPUOwnershipError(
            "gpu_ownership_container_shape_invalid"
        )
    if (
        state.get("Running") is not True
        or any(
            state.get(name) is not False
            for name in ("Paused", "Restarting", "Dead")
        )
        or type(state.get("Pid")) is not int
        or not 1 <= state["Pid"] <= 2**31 - 1
        or type(state.get("StartedAt")) is not str
        or not re.fullmatch(r"[0-9T:.Z+-]{1,64}", state["StartedAt"])
    ):
        raise contract.GPUOwnershipError("gpu_ownership_container_not_running")
    if (
        host.get("Privileged") is not False
        or host.get("AnnotationsPresent") is not False
        or any(
            type(host.get(key)) is not str
            for key in ("Runtime", "PidMode", "IpcMode")
        )
        or host.get("Runtime") not in {"runc", "nvidia"}
        or host.get("PidMode") not in {"", "default"}
        or host.get("IpcMode") not in {"", "private"}
    ):
        raise contract.GPUOwnershipError("gpu_ownership_container_host_access")
    for key in ("Devices", "DeviceCgroupRules", "VolumesFrom"):
        if key not in host or _list(host[key]):
            raise contract.GPUOwnershipError(
                "gpu_ownership_container_device_access"
            )
    capabilities = _list(host.get("CapAdd"))
    if cpu_proof is None:
        if "CapAdd" not in host or capabilities:
            raise contract.GPUOwnershipError(
                "gpu_ownership_container_device_access"
            )
    else:
        if (
            host["Runtime"] != "runc"
            or any(type(value) is not str for value in capabilities)
            or len(set(capabilities)) != len(capabilities)
        ):
            raise contract.GPUOwnershipError(
                "gpu_ownership_cpu_profile_invalid"
            )
        capabilities = {value.removeprefix("CAP_") for value in capabilities}
        approved = (
            (
                contract.TLS_OUTER_CAPS,
                contract.TLS_OUTER_CAPS | {"NET_BIND_SERVICE"},
            )
            if cpu_proof.role == "private-api-tls-outer"
            else (frozenset(),)
        )
        if capabilities not in approved:
            raise contract.GPUOwnershipError(
                "gpu_ownership_cpu_profile_invalid"
            )
    requests = _list(host.get("DeviceRequests"), nullable=False)
    if cpu_proof is not None and requests:
        raise contract.GPUOwnershipError("gpu_ownership_cpu_profile_invalid")
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
            raise contract.GPUOwnershipError(
                "gpu_ownership_device_scope_unknown"
            )
        identifiers = _list(request.get("DeviceIDs"), nullable=False)
        if not 1 <= len(identifiers) <= 4:
            raise contract.GPUOwnershipError(
                "gpu_ownership_device_scope_unknown"
            )
        for value in identifiers:
            try:
                identifier_uuid = _uuid(value)
            except contract.GPUOwnershipError:
                raise contract.GPUOwnershipError(
                    "gpu_ownership_device_scope_unknown"
                ) from None
            if identifier_uuid in exposed:
                raise contract.GPUOwnershipError(
                    "gpu_ownership_device_scope_duplicate"
                )
            exposed.add(identifier_uuid)
    environment = {}
    for entry in _list(row.get("NVIDIAEnv"), nullable=False):
        if type(entry) is not str or len(entry) > 4096 or "=" not in entry:
            raise contract.GPUOwnershipError(
                "gpu_ownership_nvidia_environment_invalid"
            )
        key, value = entry.split("=", 1)
        if (
            key not in contract.NVIDIA_KEYS
            or key in environment
            or "\x00" in value
        ):
            raise contract.GPUOwnershipError(
                "gpu_ownership_nvidia_environment_invalid"
            )
        environment[key] = value
    if any(environment.get(key, "") for key in contract.NVIDIA_KEYS[1:]):
        raise contract.GPUOwnershipError("gpu_ownership_legacy_gpu_access")
    visible = environment.get("NVIDIA_VISIBLE_DEVICES", "")
    if visible and visible not in {"none", "void"}:
        values = visible.split(",")
        if len(values) > 4:
            raise contract.GPUOwnershipError("gpu_ownership_legacy_gpu_access")
        try:
            visible_set = {_uuid(value) for value in values}
        except contract.GPUOwnershipError:
            raise contract.GPUOwnershipError(
                "gpu_ownership_legacy_gpu_access"
            ) from None
        if (
            len(visible_set) != len(values)
            or not exposed
            or visible_set != exposed
        ):
            raise contract.GPUOwnershipError("gpu_ownership_legacy_gpu_access")
    elif exposed and visible in {"none", "void"}:
        raise contract.GPUOwnershipError("gpu_ownership_legacy_gpu_access")
    if cpu_proof is not None and environment != {
        "NVIDIA_VISIBLE_DEVICES": "void"
    }:
        raise contract.GPUOwnershipError("gpu_ownership_cpu_profile_invalid")
    return identifier, image, frozenset(exposed), _mounts(row, cpu_proof)


def _containers(
    containers,
    proofs,
    roles,
    verified_bind_sources,
    verified_local_volumes=(),
    cpu_proofs=None,
):
    if (
        type(containers) is not list
        or len(containers) > contract.MAX_CONTAINERS
    ):
        raise contract.GPUOwnershipError("gpu_ownership_container_limit")
    if type(verified_bind_sources) is not frozenset:
        raise contract.GPUOwnershipError(
            "gpu_ownership_mount_evidence_invalid"
        )
    volumes = _volume_proofs(verified_local_volumes)
    cpu_proofs = {} if cpu_proofs is None else cpu_proofs
    observed_volumes = set()
    result = {}
    for row in containers:
        cpu = cpu_proofs.get(row.get("Id")) if type(row) is dict else None
        identifier, image, exposed, sources = _container(row, cpu)
        if identifier in result:
            raise contract.GPUOwnershipError(
                "gpu_ownership_container_duplicate"
            )
        if not set(sources) <= verified_bind_sources:
            raise contract.GPUOwnershipError(
                "gpu_ownership_mount_source_unverified"
            )
        for mount in row["Mounts"]:
            if mount["Type"] != "volume":
                continue
            name = mount["Name"]
            proof = volumes.get(name)
            if proof is None or mount["Source"] != proof.mountpoint:
                raise contract.GPUOwnershipError(
                    "gpu_ownership_volume_evidence_invalid"
                )
            observed_volumes.add(name)
        proof = proofs.get(identifier)
        if exposed:
            if proof is None:
                raise contract.GPUOwnershipError(
                    "gpu_ownership_unsealed_gpu_container"
                )
            if image != proof.image_id or exposed != frozenset(
                roles[proof.role]
            ):
                raise contract.GPUOwnershipError(
                    "gpu_ownership_sealed_identity_changed"
                )
        elif proof is not None:
            raise contract.GPUOwnershipError(
                "gpu_ownership_sealed_identity_changed"
            )
        result[identifier] = row
    if not set(proofs) <= set(result):
        raise contract.GPUOwnershipError(
            "gpu_ownership_sealed_container_missing"
        )
    if not set(cpu_proofs) <= set(result):
        raise contract.GPUOwnershipError(
            "gpu_ownership_sealed_cpu_container_missing"
        )
    if observed_volumes != set(volumes):
        raise contract.GPUOwnershipError(
            "gpu_ownership_volume_evidence_invalid"
        )
    return result


def _pids(raw):
    lines = _text(raw).splitlines()
    if (
        not lines
        or lines[0].strip() != "PID"
        or not 2 <= len(lines) <= contract.MAX_PIDS + 1
    ):
        raise contract.GPUOwnershipError("gpu_ownership_pid_inventory_invalid")
    result = set()
    for line in lines[1:]:
        value = line.strip()
        if (
            re.fullmatch(r"[1-9][0-9]{0,9}", value) is None
            or int(value) > 2**31 - 1
        ):
            raise contract.GPUOwnershipError(
                "gpu_ownership_pid_inventory_invalid"
            )
        if int(value) in result:
            raise contract.GPUOwnershipError(
                "gpu_ownership_pid_inventory_duplicate"
            )
        result.add(int(value))
    return result


def validate_gpu_ownership(
    gpus: GPUConfig,
    *,
    inventory: str,
    containers: list[dict],
    compute_apps: str,
    container_pids: dict[str, str],
    allowed: tuple[contract.GPUContainerProof, ...] = (),
    cpu_proofs: tuple[contract.CPUContainerProof, ...] = (),
    verified_bind_sources: frozenset[str] = frozenset(),
    verified_local_volumes: tuple[contract.LocalVolumeProof, ...] = (),
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
        raise contract.GPUOwnershipError("gpu_ownership_pid_evidence_missing")
    owners = {}
    for identifier, proof in proofs.items():
        pids = _pids(container_pids[identifier])
        if by_id[identifier]["State"]["Pid"] not in pids:
            raise contract.GPUOwnershipError("gpu_ownership_main_pid_missing")
        for pid in pids:
            if pid in owners:
                raise contract.GPUOwnershipError(
                    "gpu_ownership_pid_owner_ambiguous"
                )
            owners[pid] = proof
    tasks = _text(compute_apps).splitlines()
    if len(tasks) > contract.MAX_PIDS:
        raise contract.GPUOwnershipError("gpu_ownership_compute_limit")
    seen = set()
    for line in tasks:
        fields = [value.strip() for value in line.split(",")]
        if (
            len(fields) != 2
            or re.fullmatch(r"[1-9][0-9]{0,9}", fields[1]) is None
        ):
            raise contract.GPUOwnershipError("gpu_ownership_compute_invalid")
        identifier, pid = _uuid(fields[0]), int(fields[1])
        if identifier not in available or pid > 2**31 - 1:
            raise contract.GPUOwnershipError("gpu_ownership_compute_invalid")
        if (identifier, pid) in seen:
            raise contract.GPUOwnershipError("gpu_ownership_compute_duplicate")
        seen.add((identifier, pid))
        proof = owners.get(pid)
        if proof is None:
            raise contract.GPUOwnershipError(
                "gpu_ownership_unknown_compute_pid"
            )
        if identifier not in roles[proof.role]:
            raise contract.GPUOwnershipError(
                "gpu_ownership_compute_role_mismatch"
            )
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


def _snapshot(row, cpu_proof=None):
    """Docker's mount map has no list-order contract; retain every field.

    Validate destinations and reject duplicates before ordering. This only
    removes presentation-order noise, not changed access, paths or peers.
    """
    _container(row, cpu_proof)
    return _projection(row)
