"""Closed, pure Docker proof for the private three-service inference plane.

Health is a process/runtime proof, not an Ollama generation warm-up or quality
claim. The baseline has no release client; its separately named leased profile
requires the catalog's sealed API/image contract and exact unfiltered peers.
"""

from __future__ import annotations

import hashlib
import re

from search.operations.catalog import client_lease, policy
from search.operations.deployment.gpu_ownership import GPUContainerProof
from search.operations.shared import private_io


SERVICES = frozenset({"scope-model", "answer-model", "reranker"})
ROLES = {"scope-model": "scope", "answer-model": "planner", "reranker": "reranker"}
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class InferenceRuntimeError(ValueError):
    """Fixed diagnostics; no Docker Env, logs, model bytes or credentials."""


def _environment(value):
    if type(value) is not list or len(value) > 256:
        raise InferenceRuntimeError("inference_runtime_environment_invalid")
    result = {}
    for entry in value:
        if type(entry) is not str or "=" not in entry:
            raise InferenceRuntimeError("inference_runtime_environment_invalid")
        key, content = entry.split("=", 1)
        if not key or key in result:
            raise InferenceRuntimeError("inference_runtime_environment_invalid")
        result[key] = content
    return result


def _equal(actual, expected, code):
    if private_io.canonical(actual) != private_io.canonical(expected):
        raise InferenceRuntimeError(code)


def _resources(row, service):
    host, state, config = row["HostConfig"], row["State"], row["Config"]
    if (
        config.get("User") != service["user"]
        or config.get("Tty") is not False
        or config.get("OpenStdin") is not False
        or config.get("StdinOnce") is not False
        or host.get("ReadonlyRootfs") is not True
        or host.get("Privileged") is not False
        or host.get("Init") is not True
        or host.get("AutoRemove") is not False
        or host.get("CapDrop") != ["ALL"]
        or host.get("CapAdd")
        or host.get("Devices")
        or host.get("DeviceCgroupRules")
        or host.get("VolumesFrom")
        or host.get("Annotations")
        or any(
            host.get(key) not in (None, [], "")
            for key in ("Links", "ExtraHosts", "Dns", "DnsOptions", "DnsSearch")
        )
        or host.get("CgroupnsMode", "") not in {"", "private"}
        or host.get("SecurityOpt") != ["no-new-privileges:true"]
        or host.get("Runtime") != "nvidia"
        or any(
            host.get(name, "") not in allowed
            for name, allowed in {
                "PidMode": {"", "default"},
                "IpcMode": {"", "private"},
                "UTSMode": {"", "default"},
                "UsernsMode": {"", "default"},
            }.items()
        )
        or type(host.get("NanoCpus")) is not int
        or host["NanoCpus"] != int(service["cpus"] * 10**9)
        or type(host.get("Memory")) is not int
        or host["Memory"] != service["mem_limit"]
        or type(host.get("MemorySwap")) is not int
        or host["MemorySwap"] != service["memswap_limit"]
        or type(host.get("PidsLimit")) is not int
        or host["PidsLimit"] != service["pids_limit"]
        or host.get("RestartPolicy")
        != {"Name": service["restart"], "MaximumRetryCount": 0}
        or host.get("LogConfig")
        != {
            "Type": service["logging"]["driver"],
            "Config": service["logging"]["options"],
        }
        or policy._tmpfs(host.get("Tmpfs") or {}) != policy._tmpfs(service["tmpfs"])
        or policy._ulimits(host.get("Ulimits") or [])
        or state.get("Running") is not True
        or state.get("Paused") is not False
        or state.get("Restarting") is not False
        or state.get("Dead") is not False
        or state.get("OOMKilled") is not False
        or state.get("Health", {}).get("Status") != "healthy"
        or type(state.get("Pid")) is not int
        or state["Pid"] <= 0
        or type(row.get("RestartCount")) is not int
        or row["RestartCount"] != 0
        or type(state.get("StartedAt")) is not str
        or not state["StartedAt"]
    ):
        raise InferenceRuntimeError("inference_runtime_resource_or_health_changed")
    health, expected = config.get("Healthcheck") or {}, service["healthcheck"]
    if (
        health.get("Test") != expected["test"]
        or type(health.get("Retries")) is not int
        or health["Retries"] != expected["retries"]
        or any(
            type(health.get(actual, 0)) is not int
            or health.get(actual, 0) != policy._duration(expected.get(setting, "0s"))
            for actual, setting in (
                ("Interval", "interval"),
                ("Timeout", "timeout"),
                ("StartPeriod", "start_period"),
                ("StartInterval", "start_interval"),
            )
        )
    ):
        raise InferenceRuntimeError("inference_runtime_healthcheck_changed")
    ports, bindings = (
        row["NetworkSettings"].get("Ports") or {},
        host.get("PortBindings") or {},
    )
    if (
        type(ports) is not dict
        or type(bindings) is not dict
        or any(ports.values())
        or any(bindings.values())
    ):
        raise InferenceRuntimeError("inference_runtime_publishes_port")
    requests = host.get("DeviceRequests")
    ids = service["deploy"]["resources"]["reservations"]["devices"][0]["device_ids"]
    if (
        type(requests) is not list
        or len(requests) != 1
        or type(requests[0]) is not dict
        or set(requests[0])
        != {"Driver", "Count", "DeviceIDs", "Capabilities", "Options"}
        or requests[0]["Options"] not in ({}, None)
    ):
        raise InferenceRuntimeError("inference_runtime_device_requests_changed")
    _equal(
        {key: value for key, value in requests[0].items() if key != "Options"},
        {"Driver": "nvidia", "Count": 0, "DeviceIDs": ids, "Capabilities": [["gpu"]]},
        "inference_runtime_device_requests_changed",
    )


def _mounts(row, service):
    actual = []
    if type(row.get("Mounts")) is not list:
        raise InferenceRuntimeError("inference_runtime_mounts_changed")
    for mount in row["Mounts"]:
        if type(mount) is not dict:
            raise InferenceRuntimeError("inference_runtime_mounts_changed")
        if mount.get("Type") == "tmpfs":
            if mount.get("Destination") not in policy._tmpfs(service["tmpfs"]):
                raise InferenceRuntimeError("inference_runtime_mounts_changed")
            continue
        if mount.get("Propagation") != "rprivate":
            raise InferenceRuntimeError("inference_runtime_mounts_changed")
        actual.append(
            (
                mount.get("Type"),
                mount.get("Source"),
                mount.get("Destination"),
                mount.get("RW"),
            )
        )
    expected = [
        (item["type"], item["source"], item["target"], False)
        for item in service["volumes"]
    ]
    if len(actual) != len(expected) or set(actual) != set(expected):
        raise InferenceRuntimeError("inference_runtime_mounts_changed")


def validate_runtime(compose, images, containers, networks):
    """Prove exact running IDs, image Env/commands, resources, devices and peers.

    The caller authenticates ``compose`` and ``images`` independently from clean
    source/configuration and immutable image inspection. A caller dictionary is
    not itself attestation. Nothing here trusts a service name as GPU authority.
    """
    return _validate_runtime(compose, images, containers, networks)


def _validate_runtime(compose, images, containers, networks, *, client=None):
    """Share GPU policy without manufacturing a baseline network snapshot."""
    try:
        if (
            type(compose) is not dict
            or set(compose.get("services", {})) != SERVICES
            or set(compose.get("networks", {})) != {"inference", "ranking"}
            or type(images) is not dict
            or set(images) != SERVICES
            or type(containers) is not list
            or len(containers) != 3
            or type(networks) is not list
            or len(networks) != 2
        ):
            raise InferenceRuntimeError("inference_runtime_inventory_invalid")
        plan = compose.get("x-depositar-plan", {})
        if (
            type(plan) is not dict
            or type(plan.get("schema_version")) is not int
            or plan.get("schema_version") != 2
            or type(plan.get("inference_source_sha256")) is not str
            or DIGEST.fullmatch(plan["inference_source_sha256"]) is None
            or type(plan.get("recipe_sha256")) is not str
            or DIGEST.fullmatch(plan["recipe_sha256"]) is None
            or compose["name"] != "depositar-ai-inference-" + plan["recipe_sha256"][:12]
            or (images["reranker"]["Config"].get("Labels") or {}).get(
                "io.depositar.ai.source.sha256"
            )
            != plan["inference_source_sha256"]
        ):
            raise InferenceRuntimeError("inference_runtime_source_changed")
        by_service, seen = {}, set()
        for row in containers:
            if type(row) is not dict or any(
                type(row.get(key)) is not dict
                for key in ("Config", "HostConfig", "State", "NetworkSettings")
            ):
                raise InferenceRuntimeError("inference_runtime_container_invalid")
            labels = row["Config"].get("Labels") or {}
            name = labels.get("com.docker.compose.service")
            if (
                name not in SERVICES
                or name in by_service
                or labels.get("com.docker.compose.project") != compose["name"]
                or labels.get("com.docker.compose.oneoff") != "False"
                or type(row.get("Id")) is not str
                or policy.CONTAINER_ID.fullmatch(row["Id"]) is None
                or row["Id"] in seen
                or row.get("Image") != images[name]["Id"]
                or row["Config"].get("Image") != images[name]["Id"]
                or any(
                    labels.get(key) != value
                    for key, value in (
                        images[name]["Config"].get("Labels") or {}
                    ).items()
                )
            ):
                raise InferenceRuntimeError("inference_runtime_identity_changed")
            seen.add(row["Id"])
            by_service[name] = row
            service, image = compose["services"][name], images[name]["Config"]
            _resources(row, service)
            _mounts(row, service)
            environment = _environment(image.get("Env") or [])
            environment.update(
                {key: str(value) for key, value in service["environment"].items()}
            )
            _equal(
                _environment(row["Config"].get("Env") or []),
                environment,
                "inference_runtime_environment_changed",
            )
            if (
                row["Config"].get("Cmd") != service.get("command", image.get("Cmd"))
                or row["Config"].get("Entrypoint") != image.get("Entrypoint")
                or row["Config"].get("WorkingDir", "") != image.get("WorkingDir", "")
                or row["Config"].get("StopSignal", "") != image.get("StopSignal", "")
            ):
                raise InferenceRuntimeError("inference_runtime_command_changed")
            network = compose["networks"][service["networks"][0]]["name"]
            if row["HostConfig"].get("NetworkMode") != network or set(
                row["NetworkSettings"].get("Networks") or {}
            ) != {network}:
                raise InferenceRuntimeError(
                    "inference_runtime_network_attachment_changed"
                )
        if client is not None and seen & set(client["expected_peer_ids"]):
            raise InferenceRuntimeError("inference_client_container_identity_collision")
        by_network = {}
        for row in networks:
            if type(row) is not dict or row.get("Name") in by_network:
                raise InferenceRuntimeError(
                    "inference_runtime_network_inventory_changed"
                )
            by_network[row.get("Name")] = row
        expected_names = {entry["name"] for entry in compose["networks"].values()}
        if set(by_network) != expected_names:
            raise InferenceRuntimeError("inference_runtime_network_inventory_changed")
        network_ids = {}
        for key, expected in compose["networks"].items():
            row = by_network[expected["name"]]
            peers = {
                entry["Id"]
                for name, entry in by_service.items()
                if key in compose["services"][name]["networks"]
            }
            if client is not None:
                peers.add(client["api_container_id"])
                if row.get("Id") != client["network_ids"][expected["name"]]:
                    raise InferenceRuntimeError("inference_client_network_id_changed")
                if type(row.get("Containers")) is not dict:
                    raise InferenceRuntimeError(
                        "inference_runtime_network_peers_changed"
                    )
            if (
                type(row.get("Id")) is not str
                or policy.CONTAINER_ID.fullmatch(row["Id"]) is None
                or row.get("Internal") is not True
                or row.get("Attachable") is not False
                or row.get("Ingress") is not False
                or row.get("Driver") != "bridge"
                or row.get("Scope") != "local"
                or row.get("Labels", {}).get("com.docker.compose.project")
                != compose["name"]
                or row.get("Labels", {}).get("com.docker.compose.network") != key
                or set(row.get("Containers") or {}) != peers
            ):
                raise InferenceRuntimeError("inference_runtime_network_peers_changed")
            network_ids[key] = row["Id"]
            for name, container in by_service.items():
                if container["Id"] in peers:
                    attachment = container["NetworkSettings"]["Networks"][
                        expected["name"]
                    ]
                    if attachment.get("NetworkID") != row["Id"]:
                        raise InferenceRuntimeError(
                            "inference_runtime_network_id_changed"
                        )
        if len(set(network_ids.values())) != 2:
            raise InferenceRuntimeError("inference_runtime_network_id_changed")
        return {
            "container_ids": {
                name: row["Id"] for name, row in sorted(by_service.items())
            },
            "network_ids": network_ids,
            "start_identity_sha256": hashlib.sha256(
                private_io.canonical(
                    {
                        name: {
                            "started_at": row["State"]["StartedAt"],
                            "pid": row["State"]["Pid"],
                        }
                        for name, row in sorted(by_service.items())
                    }
                )
            ).hexdigest(),
        }
    except InferenceRuntimeError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError):
        raise InferenceRuntimeError("inference_runtime_snapshot_invalid") from None


def validate_leased_runtime(
    compose,
    images,
    containers,
    networks,
    lease,
    *,
    catalog_identity,
    release_compose,
    image_proof,
    image_environment,
    api_container,
    catalog_network,
):
    """Prove three GPU roles plus exactly one separately sealed CPU API client.

    The host authenticates source/configuration, completed image/query/lease
    receipts and actual immutable image inspections before supplying these facts.
    It retains product/catalog locks, separately validates catalog core runtime
    and every other release service/network, and collects one current Docker
    snapshot. These dictionaries do not authenticate themselves. GPU ownership,
    warmup, HTTP readiness and route activation remain separate host obligations.

    The API's full catalog lease is checked here, including image/source/Env,
    resources, mounts and all four network IDs. The two inference networks and
    the catalog network must be passed UNFILTERED. An ID allowlist or a fabricated
    baseline snapshot cannot substitute for that API proof. Inputs are not
    modified; output contains only identities/digests, never Env or mount paths.
    """
    try:
        try:
            client = client_lease.validate_client_lease(
                lease,
                catalog_identity=catalog_identity,
                release_compose=release_compose,
                image_proof=image_proof,
                image_environment=image_environment,
                api_container=api_container,
                catalog_network=catalog_network,
            )
        except client_lease.LeaseError:
            raise InferenceRuntimeError("inference_client_lease_invalid") from None
        project = release_compose["networks"]["existing-inference"][
            "name"
        ].removesuffix("_inference")
        if compose.get("name") != project or any(
            compose["networks"][key]["name"] != project + "_" + key
            or compose["networks"][key]["name"]
            != release_compose["networks"][release_key]["name"]
            for key, release_key in (
                ("inference", "existing-inference"),
                ("ranking", "ranking"),
            )
        ):
            raise InferenceRuntimeError("inference_client_source_or_network_changed")
        proof = _validate_runtime(compose, images, containers, networks, client=client)
        return {
            **proof,
            "client_lease_sha256": client["lease_sha256"],
            "api_container_id": client["api_container_id"],
            "api_image_id": client["api_image_id"],
        }
    except InferenceRuntimeError:
        raise
    except (
        KeyError,
        TypeError,
        AttributeError,
        ValueError,
        OverflowError,
        RecursionError,
    ):
        raise InferenceRuntimeError("inference_client_snapshot_invalid") from None


def ownership_proofs(compose, proof, images):
    """Construct GPU allowance only after this exact runtime proof was checked."""
    return tuple(
        GPUContainerProof(
            proof["container_ids"][name],
            images[name]["Id"],
            ROLES[name],
            tuple(
                compose["services"][name]["deploy"]["resources"]["reservations"][
                    "devices"
                ][0]["device_ids"]
            ),
        )
        for name in sorted(SERVICES)
    )
