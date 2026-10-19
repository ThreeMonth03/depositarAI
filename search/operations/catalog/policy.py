"""Pure actual Docker resource and identity proofs; no runtime mutations."""

from __future__ import annotations

import os
from pathlib import Path
import re

IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
SERVICES = frozenset({"fess-search", "opensearch-search", "ollama-embedding"})
LABEL_PREFIX = "io.depositar.ai.catalog."


class RuntimeError(ValueError):
    """An isolated catalog invariant failed."""


def catalog_bundle_target(bundle_id):
    """Preserve the verified content-addressed basename inside a helper."""
    if type(bundle_id) is not str or re.fullmatch(r"[0-9a-f]{64}", bundle_id) is None:
        raise RuntimeError("catalog_admin_bundle_id_invalid")
    return "/catalog/sha256-" + bundle_id


def _tmpfs(value):
    result = {}
    if type(value) is list:
        for entry in value:
            target, options = entry.split(":", 1)
            if target in result:
                raise RuntimeError("catalog_tmpfs_duplicate")
            result[target] = frozenset(options.split(","))
    elif type(value) is dict:
        result = {
            name: frozenset(options.split(",")) for name, options in value.items()
        }
    else:
        raise RuntimeError("catalog_tmpfs_invalid")
    return result


def _private_namespaces(host):
    return all(
        host.get(name, "") in allowed
        for name, allowed in {
            "Runtime": {"runc"},
            "PidMode": {"", "default"},
            "IpcMode": {"", "private"},
            "UTSMode": {"", "default"},
            "UsernsMode": {"", "default"},
        }.items()
    )


def _memory_bytes(value):
    """Normalize canonical byte counts, not units or permissive numeric input."""
    if type(value) is str and re.fullmatch(r"[1-9][0-9]{0,18}", value) is not None:
        value = int(value)
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise RuntimeError("catalog_runtime_memory_bytes_invalid")
    return value


def _volume_name(compose, mount):
    definition = compose.get("volumes", {}).get(mount.get("source"), {})
    name = definition.get("name")
    if (
        mount.get("source") != "catalog-index"
        or mount.get("target") != "/usr/share/opensearch/data"
        or definition != {"name": compose.get("name", "") + "-index", "external": True}
        or definition.get("external") is not True
        or type(name) is not str
    ):
        raise RuntimeError("catalog_runtime_volume_profile_changed")
    return name


def validate_core_containers(plan, compose, images, containers):
    """Validate exactly three catalog services without making a peer claim.

    This is only a pure container-profile proof. Its caller independently
    authenticates the prepared Compose/image receipts and actual image startup
    commands. The baseline runtime and separately sealed client-lease runtime
    each validate their own unfiltered actual network after this shared check.
    """
    if type(containers) is not list or len(containers) != 3:
        raise RuntimeError("catalog_requires_exactly_three_services")
    by_service = {}
    expected_network = plan["project"] + "_search_backend"
    for row in containers:
        if type(row) is not dict:
            raise RuntimeError("catalog_runtime_identity_changed")
        config, host, state = (
            row.get("Config", {}),
            row.get("HostConfig", {}),
            row.get("State", {}),
        )
        labels = config.get("Labels") or {}
        name = labels.get("com.docker.compose.service")
        if (
            name not in SERVICES
            or name in by_service
            or labels.get("com.docker.compose.project") != plan["project"]
            or labels.get("com.docker.compose.oneoff") != "False"
            or CONTAINER_ID.fullmatch(row.get("Id", "")) is None
            or row.get("Image") != images[name]["image_id"]
        ):
            raise RuntimeError("catalog_runtime_identity_changed")
        by_service[name] = row
        expected = compose["services"][name]
        if (
            config.get("User") != expected["user"]
            or host.get("ReadonlyRootfs") is not True
            or host.get("Privileged")
            or host.get("DeviceRequests")
            or host.get("Devices")
            or host.get("CapDrop") != ["ALL"]
            or host.get("CapAdd")
            or host.get("SecurityOpt") != ["no-new-privileges:true"]
            or host.get("AutoRemove")
            or host.get("VolumesFrom")
            or host.get("Init") is not True
            or not _private_namespaces(host)
            or host.get("NanoCpus") != int(float(expected["cpus"]) * 10**9)
            or _memory_bytes(host.get("Memory")) != _memory_bytes(expected["mem_limit"])
            or _memory_bytes(host.get("MemorySwap"))
            != _memory_bytes(expected["memswap_limit"])
            or host.get("PidsLimit") != expected["pids_limit"]
            or host.get("RestartPolicy", {}).get("Name") != expected["restart"]
            or host.get("LogConfig", {}).get("Type") != expected["logging"]["driver"]
            or host.get("LogConfig", {}).get("Config") != expected["logging"]["options"]
            or _tmpfs(host.get("Tmpfs") or {}) != _tmpfs(expected["tmpfs"])
            or _ulimits(host.get("Ulimits") or [])
            != _ulimits(expected.get("ulimits", {}))
        ):
            raise RuntimeError("catalog_runtime_resource_policy_changed")
        if (
            state.get("Running") is not True
            or state.get("Paused")
            or state.get("OOMKilled")
            or state.get("Health", {}).get("Status") != "healthy"
        ):
            raise RuntimeError("catalog_runtime_not_healthy")
        health = config.get("Healthcheck") or {}
        approved = expected["healthcheck"]
        if (
            health.get("Test") != approved["test"]
            or health.get("Retries") != approved["retries"]
            or any(
                health.get(actual, 0) != _duration(approved.get(setting, "0s"))
                for actual, setting in (
                    ("Interval", "interval"),
                    ("Timeout", "timeout"),
                    ("StartPeriod", "start_period"),
                    ("StartInterval", "start_interval"),
                )
            )
        ):
            raise RuntimeError("catalog_runtime_healthcheck_changed")
        if any(host.get("PortBindings", {}).values()) or any(
            row.get("NetworkSettings", {}).get("Ports", {}).values()
        ):
            raise RuntimeError("catalog_runtime_publishes_a_port")
        environment = {}
        for entry in config.get("Env") or []:
            key, value = entry.split("=", 1)
            if key in environment:
                raise RuntimeError("catalog_runtime_duplicate_environment")
            environment[key] = value
        if any(
            environment.get(key) != str(value)
            for key, value in expected["environment"].items()
        ):
            raise RuntimeError("catalog_runtime_environment_changed")
        mounts = [
            (
                mount.get("Type"),
                mount.get("Name")
                if mount.get("Type") == "volume"
                else mount.get("Source"),
                mount.get("Destination"),
                mount.get("RW"),
            )
            for mount in row.get("Mounts") or []
            if mount.get("Type") != "tmpfs"
        ]
        expected_mounts = [
            (
                mount["type"],
                _volume_name(compose, mount)
                if mount["type"] == "volume"
                else mount["source"],
                mount["target"],
                not mount.get("read_only", False),
            )
            for mount in expected["volumes"]
        ]
        expected_mounts.extend(
            (
                "bind",
                compose["secrets"][secret["source"]]["file"],
                "/run/secrets/" + secret["target"],
                False,
            )
            for secret in expected.get("secrets", [])
        )
        if len(mounts) != len(set(mounts)) or set(mounts) != set(expected_mounts):
            raise RuntimeError("catalog_runtime_mounts_changed")
        named_mounts = [
            mount for mount in row.get("Mounts") or [] if mount.get("Type") == "volume"
        ]
        if named_mounts:
            if (
                name != "opensearch-search"
                or len(named_mounts) != 1
                or named_mounts[0].get("Driver") != "local"
                or named_mounts[0].get("RW") is not True
                or type(named_mounts[0].get("Source")) is not str
                or not Path(named_mounts[0]["Source"]).is_absolute()
            ):
                raise RuntimeError("catalog_runtime_volume_driver_changed")
            host_volumes = [
                mount
                for mount in host.get("Mounts") or []
                if mount.get("Type") == "volume"
            ]
            if (
                len(host_volumes) != 1
                or host_volumes[0].get("Source") != named_mounts[0]["Name"]
                or host_volumes[0].get("Target") != "/usr/share/opensearch/data"
                or host_volumes[0].get("ReadOnly", False) is not False
                or host_volumes[0].get("VolumeOptions") != {"NoCopy": True}
                or host_volumes[0].get("VolumeOptions", {}).get("NoCopy") is not True
                or not set(host_volumes[0])
                <= {
                    "Type",
                    "Source",
                    "Target",
                    "ReadOnly",
                    "VolumeOptions",
                    "Consistency",
                }
                or host_volumes[0].get("Consistency", "") not in {"", "default"}
            ):
                raise RuntimeError("catalog_runtime_volume_options_changed")
    if set(by_service) != SERVICES:
        raise RuntimeError("catalog_runtime_service_inventory_changed")
    fess_id = by_service["fess-search"]["Id"]
    for name, row in by_service.items():
        mode = (
            "container:" + fess_id if name == "ollama-embedding" else expected_network
        )
        if row["HostConfig"].get("NetworkMode") != mode:
            raise RuntimeError("catalog_runtime_network_namespace_changed")
        attachments = set(row.get("NetworkSettings", {}).get("Networks") or {})
        if attachments != (set() if name == "ollama-embedding" else {expected_network}):
            raise RuntimeError("catalog_runtime_network_attachments_changed")
    return {name: row["Id"] for name, row in by_service.items()}


def validate_runtime(plan, compose, images, containers, network):
    """Validate the baseline runtime with exactly its two actual network peers.

    This contract deliberately does not accept client leases or a network from
    which peers have been filtered. The separately named leased-runtime proof
    validates the extended three-peer profile without weakening this baseline.
    """
    container_ids = validate_core_containers(plan, compose, images, containers)
    expected_network = plan["project"] + "_search_backend"
    peers = {container_ids[name] for name in ("fess-search", "opensearch-search")}
    if (
        network.get("Name") != expected_network
        or network.get("Internal") is not True
        or network.get("Attachable") is not False
        or set(network.get("Containers") or {}) != peers
        or network.get("Labels", {}).get("com.docker.compose.project")
        != plan["project"]
        or CONTAINER_ID.fullmatch(network.get("Id", "")) is None
    ):
        raise RuntimeError("catalog_runtime_network_peers_changed")
    return {
        "container_ids": container_ids,
        "network_id": network["Id"],
    }


def _ulimits(value):
    if type(value) is dict:
        return {name: (entry["soft"], entry["hard"]) for name, entry in value.items()}
    if type(value) is list:
        result = {}
        for entry in value:
            name = entry["Name"]
            if name in result:
                raise RuntimeError("catalog_runtime_duplicate_ulimit")
            result[name] = (entry["Soft"], entry["Hard"])
        return result
    raise RuntimeError("catalog_runtime_ulimits_invalid")


def _duration(value):
    if (
        type(value) is not str
        or len(value) > 126
        or re.fullmatch(r"(?:(?:0|[1-9][0-9]{0,18})(?:ns|us|ms|s|m|h)){1,6}", value)
        is None
    ):
        raise RuntimeError("catalog_healthcheck_duration_invalid")
    scales = {
        "ns": 1,
        "us": 1000,
        "ms": 10**6,
        "s": 10**9,
        "m": 60 * 10**9,
        "h": 3600 * 10**9,
    }
    total, previous_scale = 0, 3600 * 10**9 + 1
    for number, unit in re.findall(r"([0-9]+)(ns|us|ms|s|m|h)", value):
        scale = scales[unit]
        if scale >= previous_scale:
            raise RuntimeError("catalog_healthcheck_duration_invalid")
        total += int(number) * scale
        if total > 2**63 - 1:
            raise RuntimeError("catalog_healthcheck_duration_invalid")
        previous_scale = scale
    return total


def admin_arguments(context, identity, *, journal=None):
    """One closed command/mount contract shared by create and inspect."""
    command = [
        "--bundle",
        catalog_bundle_target(context["bundle"].bundle_id),
        "--manifest-sha256",
        context["plan"]["manifest_sha256"],
        identity["operation"],
        "--deadline-seconds",
    ]
    if identity["operation"] in {"import", "reconcile"}:
        if journal is not None:
            raise RuntimeError("catalog_admin_unexpected_journal")
        return [*command, "600" if identity["operation"] == "import" else "180"]
    if (
        identity["operation"] != "vector-pass"
        or re.fullmatch(r"[0-9a-f]{32}", identity.get("run_id", "")) is None
        or re.fullmatch(r"[0-9a-f]{64}", identity.get("recipe_sha256", "")) is None
        or journal
        != context["root"] / "vector-operations" / identity["run_id"] / "events"
        or any(character in str(journal) for character in (",", "\n", "\r"))
    ):
        raise RuntimeError("catalog_vector_journal_identity_invalid")
    result = [
        *command,
        "1200",
        "--journal-dir",
        "/vector-journal",
        "--run-id",
        identity["run_id"],
        "--recipe-sha256",
        identity["recipe_sha256"],
        "--poll-seconds",
        "1",
    ]
    expected_state = identity.get("expected_before_state_sha256")
    if expected_state is not None:
        if (
            type(expected_state) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected_state) is None
        ):
            raise RuntimeError("catalog_vector_checkpoint_invalid")
        result.extend(["--expected-before-state-sha256", expected_state])
    return result


def validate_admin(
    context, image, identity, row, fess_id, secret, *, stopped, journal=None
):
    """Prove the exact helper's capabilities before it reads a secret or exits."""
    host, config, state = (
        row.get("HostConfig", {}),
        row.get("Config", {}),
        row.get("State", {}),
    )
    labels = config.get("Labels") or {}
    environment = {}
    for entry in config.get("Env") or []:
        key, value = entry.split("=", 1)
        if key in environment:
            raise RuntimeError("catalog_admin_duplicate_environment")
        environment[key] = value
    bundle_target = catalog_bundle_target(context["bundle"].bundle_id)
    expected_cmd = admin_arguments(context, identity, journal=journal)
    expected_mounts = {
        ("bind", str(Path(context["plan"]["bundle_path"])), bundle_target, False),
        ("bind", str(secret), "/run/secrets/fess_initial_admin_password", False),
    }
    if journal is not None:
        expected_mounts.add(("bind", str(journal), "/vector-journal", True))
    mounts = [
        (
            mount.get("Type"),
            mount.get("Source"),
            mount.get("Destination"),
            mount.get("RW"),
        )
        for mount in row.get("Mounts") or []
        if mount.get("Type") != "tmpfs"
    ]
    if (
        type(row.get("Id")) is not str
        or CONTAINER_ID.fullmatch(row["Id"]) is None
        or row.get("Image") != image["image_id"]
        or config.get("User") != f"{os.geteuid()}:{os.getegid()}"
        or config.get("WorkingDir") != "/opt/depositar-search"
        or config.get("Cmd") != expected_cmd
        or config.get("Entrypoint")
        != ["python3", "-E", "-s", "-B", "-u", "-m", "search.catalog_admin"]
        or labels.get(LABEL_PREFIX + "admin-project") != context["plan"]["project"]
        or labels.get(LABEL_PREFIX + "admin-run") != identity["run_id"]
        or labels.get(LABEL_PREFIX + "admin-operation") != identity["operation"]
        or labels.get("com.docker.compose.project")
        or environment.get("NVIDIA_VISIBLE_DEVICES") != "void"
        or environment.get("CUDA_VISIBLE_DEVICES") != ""
        or host.get("NetworkMode") != "container:" + fess_id
        or row.get("NetworkSettings", {}).get("Networks")
        or any((host.get("PortBindings") or {}).values())
        or any((row.get("NetworkSettings", {}).get("Ports") or {}).values())
        or host.get("ReadonlyRootfs") is not True
        or host.get("Privileged")
        or host.get("CapDrop") != ["ALL"]
        or host.get("CapAdd")
        or host.get("SecurityOpt") != ["no-new-privileges:true"]
        or host.get("Init") is not True
        or host.get("AutoRemove")
        or host.get("VolumesFrom")
        or host.get("DeviceRequests")
        or host.get("Devices")
        or not _private_namespaces(host)
        or host.get("NanoCpus") != 10**9
        or host.get("Memory") != 1024**3
        or host.get("MemorySwap") != 1024**3
        or host.get("PidsLimit") != 64
        or host.get("RestartPolicy", {}).get("Name") != "no"
        or host.get("LogConfig", {}).get("Type") != "none"
        or _ulimits(host.get("Ulimits") or []) != {"nofile": (1024, 1024)}
        or _tmpfs(host.get("Tmpfs") or {})
        != {
            "/tmp": frozenset(
                f"rw,noexec,nosuid,nodev,size=64m,uid={os.geteuid()},gid={os.getegid()},mode=700".split(
                    ","
                )
            )
        }
        or len(mounts) != len(set(mounts))
        or set(mounts) != expected_mounts
        or state.get("Paused")
        or state.get("OOMKilled")
        or state.get("Status")
        not in ({"created", "exited"} if stopped else {"created"})
        or state.get("Running") is not False
    ):
        raise RuntimeError("catalog_admin_capabilities_changed")
    return row["Id"]
