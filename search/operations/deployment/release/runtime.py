"""Pure proof for the private, three-service CPU release before routing.

The caller authenticates the rendered/sealed Compose, immutable images, source
receipts and catalog lease, holds the operation locks and collects current
unfiltered Docker snapshots. These dictionaries cannot authenticate themselves.
This proof does no IO and grants no gateway, query, GPU or HTTP readiness claim.
The website network must contain precisely these three services; attaching a
gateway is a different future phase and deliberately fails this profile.
"""

from __future__ import annotations

import re

from search.operations.catalog import client_lease, policy
from search.operations.deployment import process_identity, source_bundle
from search.operations.shared import private_io


SERVICES = frozenset({"web", "conversation-api", "filtered-embedding"})
WEB_COMMAND = (
    "python",
    "-m",
    "uvicorn",
    "search.web.app:create_app",
    "--factory",
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
    "--workers",
    "1",
    "--no-access-log",
    "--no-proxy-headers",
    "--limit-concurrency",
    "32",
    "--timeout-keep-alive",
    "5",
)
RESOURCE_PROFILES = {
    "web": (1, 512 * 1024**2, 64),
    "filtered-embedding": (8, 8 * 1024**3, 96),
}
EMPTY = (None, [])


class ReleaseRuntimeError(ValueError):
    """Fixed diagnostic codes, without raw Docker evidence or private values."""


def _integer(value, expected):
    return type(value) is int and value == expected


def _environment(value):
    try:
        return source_bundle.parse_image_environment(value)
    except source_bundle.SourceBundleError:
        raise ReleaseRuntimeError("release_runtime_environment_invalid") from None


def _closed(value, fields, code):
    if type(value) is not dict or set(value) != fields:
        raise ReleaseRuntimeError(code)
    return value


def _labels(lease, role, source):
    return {
        "org.opencontainers.image.revision": lease["revision"],
        "io.depositar.ai.source.revision": lease["revision"],
        "io.depositar.ai.source.service": role,
        "io.depositar.ai.source.sha256": source,
    }


def _plan(compose, lease):
    """Close the other two renderer profiles; the lease checker closes API."""
    api = compose["services"]["conversation-api"]
    web = _closed(
        compose["services"]["web"],
        client_lease.API_FIELDS,
        "release_runtime_web_plan_invalid",
    )
    embedding = _closed(
        compose["services"]["filtered-embedding"],
        client_lease.API_FIELDS - {"labels"} | {"command"},
        "release_runtime_embedding_plan_invalid",
    )
    user = re.fullmatch(r"([1-9][0-9]{0,9}):([1-9][0-9]{0,9})", str(api["user"]))
    if user is None:
        raise ReleaseRuntimeError("release_runtime_user_plan_invalid")
    uid, gid = user.groups()
    common_tmpfs = "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777"
    for role, service in (("web", web), ("filtered-embedding", embedding)):
        cpus, memory, pids = RESOURCE_PROFILES[role]
        tmpfs = [common_tmpfs]
        if role == "filtered-embedding":
            tmpfs.append(
                f"/.ollama:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid={uid},gid={gid}"
            )
        if (
            service["user"] != api["user"]
            or service["init"] is not True
            or service["restart"] != "unless-stopped"
            or service["runtime"] != "runc"
            or service["read_only"] is not True
            or service["cap_drop"] != ["ALL"]
            or service["security_opt"] != ["no-new-privileges:true"]
            or not _integer(service["cpus"], cpus)
            or policy._memory_bytes(service["mem_limit"]) != memory
            or policy._memory_bytes(service["memswap_limit"]) != memory
            or not _integer(service["pids_limit"], pids)
            or service["logging"]
            != {"driver": "json-file", "options": {"max-size": "5m", "max-file": "3"}}
            or policy._tmpfs(service["tmpfs"]) != policy._tmpfs(tmpfs)
            or type(service["image"]) is not str
            or policy.IMAGE_ID.fullmatch(service["image"]) is None
        ):
            raise ReleaseRuntimeError("release_runtime_resource_plan_invalid")
        health = {
            "test": api["healthcheck"]["test"]
            if role == "web"
            else ["CMD", "ollama", "list"],
            "interval": "20s",
            "timeout": "5s",
            "retries": 5,
            "start_period": "0s",
        }
        if private_io.canonical(service["healthcheck"]) != private_io.canonical(health):
            raise ReleaseRuntimeError("release_runtime_health_plan_invalid")
    source = web["labels"].get("io.depositar.ai.source.sha256")
    if (
        type(source) is not str
        or source_bundle.DIGEST.fullmatch(source) is None
        or web["labels"] != _labels(lease, "web", source)
        or web["networks"] != {"website": {"aliases": [compose["name"] + "-web"]}}
        or web["environment"]
        != {
            "WEB_ORIGIN": "http://127.0.0.1:18083",
            "WEB_UPSTREAM": "http://conversation-api:8000",
            "NVIDIA_VISIBLE_DEVICES": "void",
        }
        or embedding["networks"] != ["website"]
        or embedding["command"] != ["serve"]
        or embedding["environment"]
        != {
            "OLLAMA_HOST": "0.0.0.0:11434",
            "OLLAMA_MODELS": "/models",
            "OLLAMA_NUM_PARALLEL": "1",
            "OLLAMA_MAX_LOADED_MODELS": "1",
            "OLLAMA_MAX_QUEUE": "1",
            "OLLAMA_NO_CLOUD": "1",
            "CUDA_VISIBLE_DEVICES": "-1",
            "NVIDIA_VISIBLE_DEVICES": "void",
        }
    ):
        raise ReleaseRuntimeError("release_runtime_service_plan_invalid")
    api_key = next(
        mount for mount in api["volumes"] if mount["target"] == "/run/secrets/api_key"
    )
    if web["volumes"] != [api_key]:
        raise ReleaseRuntimeError("release_runtime_mount_plan_invalid")
    volumes = embedding["volumes"]
    if type(volumes) is not list or len(volumes) != 1:
        raise ReleaseRuntimeError("release_runtime_mount_plan_invalid")
    mount = _closed(
        volumes[0],
        {"type", "source", "target", "read_only", "bind"},
        "release_runtime_mount_plan_invalid",
    )
    # Model-root provenance is a host receipt/configuration obligation. Here the
    # exact canonical path and readonly bind are closed and compared to Docker.
    client_lease._path(mount["source"])
    if (
        mount["type"] != "bind"
        or mount["target"] != "/models"
        or mount["read_only"] is not True
        or type(mount["bind"]) is not dict
        or set(mount["bind"]) != {"create_host_path"}
        or mount["bind"]["create_host_path"] is not False
    ):
        raise ReleaseRuntimeError("release_runtime_mount_plan_invalid")


def _running(row):
    """Bind healthy observations to one process start, including the API."""
    state, host, config = row["State"], row["HostConfig"], row["Config"]
    if (
        config.get("StdinOnce") is not False
        or not policy._private_namespaces(host)
        or state.get("Running") is not True
        or state.get("Paused") is not False
        or state.get("Restarting") is not False
        or state.get("Dead") is not False
        or state.get("OOMKilled") is not False
        or state.get("Health", {}).get("Status") != "healthy"
        or type(state.get("Pid")) is not int
        or state["Pid"] <= 0
        or not _integer(row.get("RestartCount"), 0)
        or type(state.get("StartedAt")) is not str
        or not state["StartedAt"]
    ):
        raise ReleaseRuntimeError("release_runtime_process_changed")


def _runtime_resources(row, service, profile):
    host, config = row["HostConfig"], row["Config"]
    cpus, memory, pids = profile
    if (
        config.get("User") != service["user"]
        or config.get("Tty") is not False
        or config.get("OpenStdin") is not False
        or host.get("ReadonlyRootfs") is not True
        or host.get("Privileged") is not False
        or host.get("AutoRemove") is not False
        or host.get("Init") is not True
        or host.get("Annotations") not in (None, {})
        or any(
            host.get(key) not in EMPTY
            for key in (
                "DeviceRequests",
                "Devices",
                "DeviceCgroupRules",
                "CapAdd",
                "VolumesFrom",
                "Links",
                "ExtraHosts",
                "Dns",
                "DnsOptions",
                "DnsSearch",
            )
        )
        or host.get("CapDrop") != ["ALL"]
        or host.get("SecurityOpt") != ["no-new-privileges:true"]
        or host.get("CgroupnsMode", "") not in {"", "private"}
        or not _integer(host.get("NanoCpus"), cpus * 10**9)
        or policy._memory_bytes(host.get("Memory")) != memory
        or policy._memory_bytes(host.get("MemorySwap")) != memory
        or not _integer(host.get("PidsLimit"), pids)
        or host.get("RestartPolicy")
        != {"Name": service["restart"], "MaximumRetryCount": 0}
        or host.get("LogConfig")
        != {
            "Type": service["logging"]["driver"],
            "Config": service["logging"]["options"],
        }
        or policy._tmpfs(host.get("Tmpfs")) != policy._tmpfs(service["tmpfs"])
        or policy._ulimits(host.get("Ulimits") or []) != {}
    ):
        raise ReleaseRuntimeError("release_runtime_resource_policy_changed")
    health, approved = config.get("Healthcheck"), service["healthcheck"]
    if (
        type(health) is not dict
        or health.get("Disable", False) is not False
        or health.get("Test") != approved["test"]
        or not _integer(health.get("Retries"), approved["retries"])
        or any(
            not _integer(
                health.get(actual, 0), policy._duration(approved.get(key, "0s"))
            )
            for actual, key in (
                ("Interval", "interval"),
                ("Timeout", "timeout"),
                ("StartPeriod", "start_period"),
                ("StartInterval", "start_interval"),
            )
        )
    ):
        raise ReleaseRuntimeError("release_runtime_healthcheck_changed")


def _resources(row, service, role):
    _runtime_resources(row, service, RESOURCE_PROFILES[role])
    for value in (
        row["HostConfig"].get("PortBindings"),
        row["NetworkSettings"].get("Ports"),
    ):
        if type(value) is not dict or any(item is not None for item in value.values()):
            raise ReleaseRuntimeError("release_runtime_publishes_port")


def _mounts(row, service):
    mounts = row.get("Mounts")
    if type(mounts) is not list:
        raise ReleaseRuntimeError("release_runtime_mounts_changed")
    binds, temporary = [], []
    for mount in mounts:
        if type(mount) is not dict:
            raise ReleaseRuntimeError("release_runtime_mounts_changed")
        if mount.get("Type") == "tmpfs":
            if mount.get("RW") is not True:
                raise ReleaseRuntimeError("release_runtime_mounts_changed")
            temporary.append(mount.get("Destination"))
        else:
            if mount.get("Propagation") != "rprivate":
                raise ReleaseRuntimeError("release_runtime_mounts_changed")
            binds.append(
                tuple(mount.get(key) for key in ("Type", "Source", "Destination", "RW"))
            )
    expected = [
        (mount["type"], mount["source"], mount["target"], False)
        for mount in service["volumes"]
    ]
    if (
        len(binds) != len(expected)
        or set(binds) != set(expected)
        or len(temporary) != len(set(temporary))
        or not set(temporary) <= set(policy._tmpfs(service["tmpfs"]))
    ):
        raise ReleaseRuntimeError("release_runtime_mounts_changed")


def _image_and_command(role, service, image, row, revision):
    config, built = row["Config"], image["Config"]
    if role in {"web", "conversation-api"}:
        if (
            built.get("Cmd")
            != list(WEB_COMMAND if role == "web" else client_lease.API_COMMAND)
            or built.get("Entrypoint") not in EMPTY
            or built.get("WorkingDir") != "/app"
            or any(
                built.get("Labels", {}).get(key) != value
                for key, value in service["labels"].items()
            )
        ):
            raise ReleaseRuntimeError("release_runtime_image_contract_changed")
    elif built.get("Entrypoint") != ["/bin/ollama"] or built.get("Cmd") != ["serve"]:
        raise ReleaseRuntimeError("release_runtime_image_contract_changed")
    if (
        config.get("Cmd") != service.get("command", built.get("Cmd"))
        or config.get("Entrypoint") != built.get("Entrypoint")
        or config.get("WorkingDir", "") != built.get("WorkingDir", "")
        or config.get("StopSignal", "") != built.get("StopSignal", "")
    ):
        raise ReleaseRuntimeError("release_runtime_command_changed")
    environment = _environment(built.get("Env"))
    if role != "filtered-embedding" and environment.get("SOURCE_REVISION") != revision:
        raise ReleaseRuntimeError("release_runtime_image_revision_changed")
    if _environment(config.get("Env")) != environment | service["environment"]:
        raise ReleaseRuntimeError("release_runtime_environment_changed")


def _validate_containers(
    compose,
    images,
    containers,
    lease,
    *,
    catalog_identity,
    image_proof,
    image_environment,
    catalog_network,
    validated_clients=None,
):
    """The unchanged CPU capability profile, without a website peer claim."""
    if (
        type(images) is not dict
        or set(images) != SERVICES
        or type(containers) is not list
        or len(containers) != 3
    ):
        raise ReleaseRuntimeError("release_runtime_inventory_invalid")
    by_role, seen = {}, set()
    for row in containers:
        if type(row) is not dict or any(
            type(row.get(key)) is not dict
            for key in ("Config", "HostConfig", "State", "NetworkSettings")
        ):
            raise ReleaseRuntimeError("release_runtime_container_invalid")
        labels = row["Config"].get("Labels")
        if type(labels) is not dict:
            raise ReleaseRuntimeError("release_runtime_identity_changed")
        role = labels.get("com.docker.compose.service")
        if (
            role not in SERVICES
            or role in by_role
            or type(row.get("Id")) is not str
            or policy.CONTAINER_ID.fullmatch(row["Id"]) is None
            or row["Id"] in seen
            or labels.get("com.docker.compose.project") != compose["name"]
            or labels.get("com.docker.compose.oneoff") != "False"
        ):
            raise ReleaseRuntimeError("release_runtime_identity_changed")
        by_role[role] = row
        seen.add(row["Id"])
    try:
        member = {
            "lease": lease,
            "catalog_identity": catalog_identity,
            "release_compose": compose,
            "image_proof": image_proof,
            "image_environment": image_environment,
            "api_container": by_role["conversation-api"],
            "catalog_network": catalog_network,
        }
        if validated_clients is None:
            client = client_lease.validate_client_lease(**member)
            core_ids = set(client["core_container_ids"].values())
        else:
            matches = [
                proof for evidence, proof in validated_clients if evidence == member
            ]
            if len(matches) != 1:
                raise ReleaseRuntimeError("release_runtime_shared_client_changed")
            client = matches[0]
            core_ids = set().union(
                *(set(p["core_container_ids"].values()) for _, p in validated_clients)
            )
            other_apis = {p["api_container_id"] for _, p in validated_clients} - {
                client["api_container_id"]
            }
            if seen & other_apis:
                raise ReleaseRuntimeError("release_runtime_identity_collision")
    except client_lease.LeaseError:
        raise ReleaseRuntimeError("release_runtime_client_lease_invalid") from None
    if seen & core_ids:
        raise ReleaseRuntimeError("release_runtime_identity_collision")
    _plan(compose, lease)
    website = compose["networks"]["website"]["name"]
    network_ids = client["network_ids"]
    for role, row in by_role.items():
        service, image = compose["services"][role], images[role]
        if (
            type(image) is not dict
            or type(image.get("Config")) is not dict
            or image.get("Id") != service["image"]
            or row.get("Image") != image["Id"]
            or row["Config"].get("Image") != image["Id"]
            or any(
                row["Config"]["Labels"].get(key) != value
                for key, value in service.get("labels", {}).items()
            )
        ):
            raise ReleaseRuntimeError("release_runtime_image_identity_changed")
        _running(row)
        _image_and_command(role, service, image, row, lease["revision"])
        if role == "conversation-api":
            if _environment(image["Config"]["Env"]) != _environment(image_environment):
                raise ReleaseRuntimeError("release_runtime_api_image_env_changed")
            continue
        _resources(row, service, role)
        _mounts(row, service)
        attachments = row["NetworkSettings"].get("Networks")
        if (
            row["HostConfig"].get("NetworkMode") != website
            or type(attachments) is not dict
            or set(attachments) != {website}
            or type(attachments[website]) is not dict
            or attachments[website].get("NetworkID") != network_ids[website]
        ):
            raise ReleaseRuntimeError("release_runtime_network_attachment_changed")
        if role == "web":
            aliases = attachments[website].get("Aliases")
            if type(aliases) is not list or compose["name"] + "-web" not in aliases:
                raise ReleaseRuntimeError("release_runtime_web_alias_changed")
    return by_role, client


def _proof(by_role, client):
    return {
        "container_ids": {role: row["Id"] for role, row in sorted(by_role.items())},
        "network_ids": dict(client["network_ids"]),
        "start_identity_sha256": process_identity.start_identity_sha256(by_role),
        "lease_sha256": client["lease_sha256"],
    }


def validate_runtime(
    compose,
    images,
    containers,
    networks,
    lease,
    *,
    catalog_identity,
    image_proof,
    image_environment,
    catalog_network,
    shared_clients=None,
):
    """Prove exact CPU release capabilities and pre-gateway website peers.

    ``images`` holds actual Docker image inspections keyed by all three roles.
    The host independently authenticates source/build/registry authority before
    supplying them; matching inspect dictionaries alone is not such authority.
    The API reuses the unchanged catalog ``validate_client_lease`` contract.
    ``networks`` contains the full unfiltered website inspection, optionally the
    other lease networks. Only website peers are proved here; the caller must
    independently verify catalog core, inference and ranking peers without
    filtering. ``catalog_network`` is separately passed unfiltered to the lease
    checker. No gateway is allowed in this pre-activation phase.

    ``shared_clients`` explicitly opts into one or two fully authenticated
    CLIENT_FIELDS evidence records. Validate the entire registered set, then
    require an exact match for this release's lease, plan, image, environment,
    actual API and catalog snapshot. Bare proofs and API ID allowlists do not
    authorize sharing; the default remains the single-client contract.

    Shared private helpers from catalog policy normalize memory, duration,
    tmpfs and namespaces. The API field/path helpers intentionally bind this
    profile to the same renderer contract. No inputs are changed and only fixed
    IDs/digests are returned, never Env, mount paths or arbitrary caller fields.
    """
    try:
        if (
            type(images) is not dict
            or set(images) != SERVICES
            or type(containers) is not list
            or len(containers) != 3
            or type(networks) is not list
            or not 1 <= len(networks) <= 4
        ):
            raise ReleaseRuntimeError("release_runtime_inventory_invalid")
        validated_clients = None
        if shared_clients is not None:
            try:
                proofs = client_lease.validate_clients(shared_clients)
            except client_lease.LeaseError:
                raise ReleaseRuntimeError(
                    "release_runtime_client_lease_invalid"
                ) from None
            validated_clients = list(zip(shared_clients, proofs))
        by_role, client = _validate_containers(
            compose,
            images,
            containers,
            lease,
            catalog_identity=catalog_identity,
            image_proof=image_proof,
            image_environment=image_environment,
            catalog_network=catalog_network,
            validated_clients=validated_clients,
        )
        seen = {row["Id"] for row in by_role.values()}
        website = compose["networks"]["website"]["name"]
        network_ids = client["network_ids"]
        by_network = {}
        for row in networks:
            if (
                type(row) is not dict
                or row.get("Name") not in network_ids
                or row["Name"] in by_network
                or row.get("Id") != network_ids[row["Name"]]
            ):
                raise ReleaseRuntimeError("release_runtime_network_inventory_changed")
            by_network[row["Name"]] = row
        if website not in by_network:
            raise ReleaseRuntimeError("release_runtime_network_inventory_changed")
        network = by_network[website]
        if (
            network.get("Internal") is not True
            or network.get("Attachable") is not False
            or network.get("Ingress") is not False
            or network.get("Driver") != "bridge"
            or network.get("Scope") != "local"
            or network.get("Labels", {}).get("com.docker.compose.project")
            != compose["name"]
            or network.get("Labels", {}).get("com.docker.compose.network") != "website"
            or type(network.get("Containers")) is not dict
            or set(network["Containers"]) != seen
        ):
            raise ReleaseRuntimeError("release_runtime_website_peers_changed")
        return _proof(by_role, client)
    except ReleaseRuntimeError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        RecursionError,
    ):
        raise ReleaseRuntimeError("release_runtime_snapshot_invalid") from None
