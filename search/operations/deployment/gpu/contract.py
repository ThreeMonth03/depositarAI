"""Canonical GPU ownership records and limits.

Constructing a record does not establish trust.
"""

from __future__ import annotations
from dataclasses import dataclass
import re


CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")


IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


DIGEST = re.compile(r"[0-9a-f]{64}\Z")


TLS_CPU_ROLES = frozenset({"private-api-tls-inner", "private-api-tls-outer"})


TLS_OUTER_CAPS = frozenset({"SETUID", "SETGID", "DAC_OVERRIDE", "CHOWN"})


MAX_CONTAINERS = 128


MAX_GPU_ROWS = 32


MAX_PIDS = 4096


MAX_ENTRIES = 256


MAX_LOCAL_VOLUMES = 128


VOLUME_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}\Z")


MAX_OUTPUT_BYTES = 256 * 1024


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


NVIDIA_KEYS = (
    "NVIDIA_VISIBLE_DEVICES",
    "NVIDIA_MIG_CONFIG_DEVICES",
    "NVIDIA_MIG_MONITOR_DEVICES",
    "NVIDIA_IMEX_CHANNELS",
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

    The TLS producer authenticates its plan, material and healthy runtime
    first.
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
    daemon metadata twice; pure callers must supply equivalent trusted
    evidence.
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
