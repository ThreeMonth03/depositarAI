"""Pure, closed permission proof for one private catalog API client.

The host must first authenticate private lease/compose/image receipts, verify
their ownership and immutable source bytes, and independently verify the core
runtime and projection/access proofs. These dictionaries are *not* authority
merely because someone constructed them. This module does no file, Docker or
network IO and cannot establish those host facts or authorize an HTTP caller.

The pure leased-runtime entry shares only core-container validation with
``policy.validate_runtime``; it does not relax that exact-two-peer baseline.
A separately sealed lease permits exactly one conversation-api peer;
the unfiltered actual catalog network must then contain the two sealed core IDs
and this one API ID. The caller must hold the operation lock and reverify facts
at the mutation boundary; the returned proof is an observation, not a lock.

The renderer emits image tags and plan-only metadata. The host release facade
authenticates source and completed receipts, pins image IDs/source labels and
seals the actual client identity before accepting this separate runtime phase.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
import re

from search.operations.catalog import policy
from search.operations.deployment import source_bundle
from search.operations.shared import private_io


SERVICE = "conversation-api"
LABEL_PREFIX = "io.depositar.ai.source."
CORES = frozenset({"fess-search", "opensearch-search"})
NETWORK_KEYS = frozenset({"website", "existing-inference", "catalog", "ranking"})
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
INDEX = re.compile(r"fess\.[0-9]{14,20}\Z")
CATALOG_FIELDS = frozenset(
    {
        "catalog_project",
        "catalog_network_id",
        "core_container_ids",
        "bundle_id",
        "manifest_sha256",
        "physical_index",
        "projection_sha256",
        "access_sha256",
    }
)
LEASE_FIELDS = CATALOG_FIELDS | {
    "lease_schema",
    "phase",
    "release_project",
    "release_compose_sha256",
    "revision",
    "source_sha256",
    "api_container_id",
    "api_image_id",
    "network_ids",
}
IMAGE_FIELDS = frozenset(
    {
        "schema_version",
        "revision",
        "service",
        "source_sha256",
        "image_id",
        "image_environment_sha256",
    }
)
API_FIELDS = frozenset(
    {
        "init",
        "restart",
        "runtime",
        "user",
        "read_only",
        "cap_drop",
        "security_opt",
        "cpus",
        "mem_limit",
        "memswap_limit",
        "pids_limit",
        "logging",
        "tmpfs",
        "image",
        "labels",
        "networks",
        "environment",
        "volumes",
        "healthcheck",
    }
)
EMPTY = (None, [])
# This closed service profile matches the canonical API Dockerfile. A source-image
# update that changes its startup must update this contract and its drift test.
API_COMMAND = (
    "python",
    "-m",
    "uvicorn",
    "search.api.conversation.app:create_app",
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
    "16",
)


class LeaseError(ValueError):
    """A fixed-code rejection, without paths, credentials or inspect output."""


def lease_digest(value):
    """SHA256 of canonical UTF-8 JSON plus LF, shared with private receipts.

    The digest binds bytes; it does not validate facts or authenticate a caller.
    """
    try:
        encoded = private_io.canonical(value)
        if len(encoded) > 2 * 1024**2:
            raise LeaseError("catalog_client_record_too_large")
        return hashlib.sha256(encoded).hexdigest()
    except LeaseError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise LeaseError("catalog_client_record_invalid") from None


def _source_labels(lease):
    return {
        "org.opencontainers.image.revision": lease["revision"],
        LABEL_PREFIX + "revision": lease["revision"],
        LABEL_PREFIX + "service": SERVICE,
        LABEL_PREFIX + "sha256": lease["source_sha256"],
    }


def _closed(value, fields, error):
    if type(value) is not dict or set(value) != fields:
        raise LeaseError(error)
    return value


def _catalog_record(value, fields, error):
    # Historical same-revision records remain byte-compatible. A reused
    # catalog additionally binds its original revision without relabelling it.
    if type(value) is dict and "catalog_revision" in value:
        fields = fields | {"catalog_revision"}
    return _closed(value, fields, error)


def _matches(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _number(value, expected):
    return type(value) is int and value == expected


def _path(value):
    if (
        type(value) is not str
        or len(value) > 4096
        or not value.startswith("/")
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        or str(PurePosixPath(value)) != value
        or ".." in PurePosixPath(value).parts
    ):
        raise LeaseError("catalog_client_mount_plan_invalid")
    return PurePosixPath(value)


def _identity(lease, catalog, compose, image):
    _catalog_record(lease, LEASE_FIELDS, "catalog_client_lease_schema_invalid")
    _catalog_record(catalog, CATALOG_FIELDS, "catalog_client_catalog_identity_invalid")
    _closed(image, IMAGE_FIELDS, "catalog_client_image_proof_invalid")
    if (
        not _number(lease["lease_schema"], 1)
        or lease["phase"] != "private_clients"
        or not _matches(lease["revision"], REVISION)
        or not _matches(lease["api_container_id"], policy.CONTAINER_ID)
        or not _matches(lease["api_image_id"], policy.IMAGE_ID)
        or any(
            not _matches(lease[key], DIGEST)
            for key in (
                "catalog_network_id",
                "bundle_id",
                "manifest_sha256",
                "projection_sha256",
                "access_sha256",
                "release_compose_sha256",
                "source_sha256",
            )
        )
        or not _matches(lease["physical_index"], INDEX)
    ):
        raise LeaseError("catalog_client_lease_identity_invalid")
    cores = _closed(
        lease["core_container_ids"], CORES, "catalog_client_core_identity_invalid"
    )
    if (
        any(not _matches(value, policy.CONTAINER_ID) for value in cores.values())
        or len(set(cores.values()) | {lease["api_container_id"]}) != 3
    ):
        raise LeaseError("catalog_client_core_identity_invalid")
    catalog_revision = lease.get("catalog_revision", lease["revision"])
    if not _matches(catalog_revision, REVISION):
        raise LeaseError("catalog_client_catalog_identity_invalid")
    if (
        any(
            lease_digest(lease[key]) != lease_digest(catalog[key])
            for key in CATALOG_FIELDS
        )
        or catalog.get("catalog_revision", lease["revision"]) != catalog_revision
    ):
        raise LeaseError("catalog_client_catalog_identity_changed")
    suffix = lease["bundle_id"][:12] + "-" + lease["revision"][:12]
    catalog_suffix = lease["bundle_id"][:12] + "-" + catalog_revision[:12]
    if (
        lease["catalog_project"] != "depositar-ai-catalog-" + catalog_suffix
        or lease["release_project"] != "depositar-ai-release-" + suffix
        or not _number(image["schema_version"], 1)
        or image["revision"] != lease["revision"]
        or image["service"] != SERVICE
        or image["source_sha256"] != lease["source_sha256"]
        or image["image_id"] != lease["api_image_id"]
        or not _matches(image["image_environment_sha256"], DIGEST)
    ):
        raise LeaseError("catalog_client_release_identity_changed")
    _closed(
        compose,
        {"name", "services", "networks", "x-depositar-plan"},
        "catalog_client_compose_invalid",
    )
    if (
        compose["name"] != lease["release_project"]
        or lease_digest(compose) != lease["release_compose_sha256"]
    ):
        raise LeaseError("catalog_client_composition_changed")
    _closed(
        compose["services"],
        {"web", SERVICE, "filtered-embedding"},
        "catalog_client_compose_invalid",
    )
    plan = compose["x-depositar-plan"]
    if (
        type(plan) is not dict
        or not _number(plan.get("schema_version"), 1)
        or plan.get("revision") != lease["revision"]
        or plan.get("catalog_revision", lease["revision"]) != catalog_revision
        or plan.get("bundle_id") != lease["bundle_id"]
        or plan.get("manifest_sha256") != lease["manifest_sha256"]
        or plan.get("catalog_client_lease_required") is not True
        or plan.get("query_provisioning_required") is not True
        or plan.get("immutable_built_images_required") is not True
        or plan.get("generated_answers") is not False
    ):
        raise LeaseError("catalog_client_compose_binding_changed")


def _network_plan(lease, compose):
    definitions = _closed(
        compose["networks"], NETWORK_KEYS, "catalog_client_network_plan_invalid"
    )
    names = {}
    for key, value in definitions.items():
        _closed(
            value,
            {"name", "internal", "attachable"}
            if key == "website"
            else {"name", "external"},
            "catalog_client_network_plan_invalid",
        )
        if (
            type(value["name"]) is not str
            or key == "website"
            and (value["internal"] is not True or value["attachable"] is not False)
            or key != "website"
            and value["external"] is not True
        ):
            raise LeaseError("catalog_client_network_plan_invalid")
        names[key] = value["name"]
    inference = re.fullmatch(
        r"(depositar-ai-inference-[0-9a-f]{12})_inference", names["existing-inference"]
    )
    if (
        names["website"] != lease["release_project"] + "_website"
        or names["catalog"] != lease["catalog_project"] + "_search_backend"
        or inference is None
        or names["ranking"] != inference[1] + "_ranking"
    ):
        raise LeaseError("catalog_client_network_plan_invalid")
    network_ids = _closed(
        lease["network_ids"], set(names.values()), "catalog_client_network_ids_invalid"
    )
    if (
        any(not _matches(value, policy.CONTAINER_ID) for value in network_ids.values())
        or len(set(network_ids.values())) != 4
        or network_ids[names["catalog"]] != lease["catalog_network_id"]
    ):
        raise LeaseError("catalog_client_network_ids_invalid")
    return network_ids


def _mount_plan(lease, expected):
    volumes = expected["volumes"]
    bundle_target = policy.catalog_bundle_target(lease["bundle_id"])
    targets = {
        bundle_target,
        "/run/secrets/api_key",
        "/run/secrets/index_query_password",
        "/run/index/binding.json",
        "/run/index/root-ca.pem",
    }
    if type(volumes) is not list or len(volumes) != 5:
        raise LeaseError("catalog_client_mount_plan_invalid")
    sources = {}
    for mount in volumes:
        _closed(
            mount,
            {"type", "source", "target", "read_only", "bind"},
            "catalog_client_mount_plan_invalid",
        )
        if (
            mount["type"] != "bind"
            or mount["read_only"] is not True
            or type(mount["bind"]) is not dict
            or set(mount["bind"]) != {"create_host_path"}
            or mount["bind"]["create_host_path"] is not False
            or type(mount["target"]) is not str
            or mount["target"] not in targets
            or mount["target"] in sources
        ):
            raise LeaseError("catalog_client_mount_plan_invalid")
        sources[mount["target"]] = _path(mount["source"])
    query_root = sources["/run/index/binding.json"].parent
    work = query_root.parent
    state = work.parent.parent
    approved = {
        bundle_target: state / "cache/bundles" / ("sha256-" + lease["bundle_id"]),
        "/run/secrets/api_key": state / "secrets/api_key",
        "/run/secrets/index_query_password": query_root / "index-query-password",
        "/run/index/binding.json": query_root / "binding.json",
        "/run/index/root-ca.pem": work / "data/opensearch/config/root-ca.pem",
    }
    if (
        query_root.name != "query-access"
        or work.parent.name != "catalogs"
        or work.name != lease["catalog_project"].removeprefix("depositar-ai-catalog-")
        or sources != approved
    ):
        raise LeaseError("catalog_client_mount_plan_invalid")
    return {("bind", str(source), target, False) for target, source in sources.items()}


def _service_plan(lease, compose):
    expected = _closed(
        compose["services"][SERVICE], API_FIELDS, "catalog_client_api_plan_invalid"
    )
    user = re.fullmatch(r"([1-9][0-9]{0,9}):([1-9][0-9]{0,9})", str(expected["user"]))
    if (
        type(expected["user"]) is not str
        or user is None
        or any(int(value) > 2**31 - 1 for value in user.groups())
        or expected["init"] is not True
        or expected["read_only"] is not True
        or expected["runtime"] != "runc"
        or expected["restart"] != "unless-stopped"
        or expected["cap_drop"] != ["ALL"]
        or expected["security_opt"] != ["no-new-privileges:true"]
        or not _number(expected["cpus"], 2)
        or policy._memory_bytes(expected["mem_limit"]) != 2 * 1024**3
        or policy._memory_bytes(expected["memswap_limit"]) != 2 * 1024**3
        or not _number(expected["pids_limit"], 96)
        or expected["logging"]
        != {"driver": "json-file", "options": {"max-size": "5m", "max-file": "3"}}
        or policy._tmpfs(expected["tmpfs"])
        != policy._tmpfs(["/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777"])
        or expected["image"] != lease["api_image_id"]
        or expected["labels"] != _source_labels(lease)
        or type(expected["networks"]) is not list
        or len(expected["networks"]) != 4
        or set(expected["networks"]) != NETWORK_KEYS
    ):
        raise LeaseError("catalog_client_api_plan_invalid")
    environment = {
        "SEARCH_API_MANIFEST_SHA256": lease["manifest_sha256"],
        "SEARCH_API_BUNDLE_PATH": policy.catalog_bundle_target(lease["bundle_id"]),
        "SEARCH_API_RERANK_ENABLED": "1",
        "SEARCH_API_RERANK_CANDIDATES": "20",
        "NVIDIA_VISIBLE_DEVICES": "void",
    }
    if expected["environment"] != environment:
        raise LeaseError("catalog_client_environment_plan_invalid")
    approved_health = _closed(
        expected["healthcheck"],
        {"test", "interval", "timeout", "retries", "start_period"},
        "catalog_client_health_plan_invalid",
    )
    if (
        approved_health["test"]
        != [
            "CMD",
            "python",
            "-c",
            "import urllib.request; urllib.request.urlopen("
            "'http://127.0.0.1:8000/healthz', timeout=2).close()",
        ]
        or approved_health["interval"] != "20s"
        or approved_health["timeout"] != "5s"
        or not _number(approved_health["retries"], 5)
        or approved_health["start_period"] != "0s"
    ):
        raise LeaseError("catalog_client_health_plan_invalid")
    return expected, _mount_plan(lease, expected)


def _actual(lease, expected, mounts, network_ids, image_environment, row):
    if type(row) is not dict:
        raise LeaseError("catalog_client_container_invalid")
    config, host, state, network = (
        row.get(key) for key in ("Config", "HostConfig", "State", "NetworkSettings")
    )
    if any(type(value) is not dict for value in (config, host, state, network)):
        raise LeaseError("catalog_client_container_invalid")
    labels = config.get("Labels")
    approved_labels = {
        "com.docker.compose.project": lease["release_project"],
        "com.docker.compose.service": SERVICE,
        "com.docker.compose.oneoff": "False",
        **_source_labels(lease),
    }
    if (
        row.get("Id") != lease["api_container_id"]
        or row.get("Image") != lease["api_image_id"]
        or config.get("Image") != lease["api_image_id"]
        or type(labels) is not dict
        or any(labels.get(key) != value for key, value in approved_labels.items())
    ):
        raise LeaseError("catalog_client_container_identity_changed")
    if (
        config.get("User") != expected["user"]
        or config.get("Cmd") != list(API_COMMAND)
        or config.get("Entrypoint") not in EMPTY
        or config.get("WorkingDir") != "/app"
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
        or not policy._private_namespaces(host)
        or host.get("CgroupnsMode", "") not in {"", "private"}
        or not _number(host.get("NanoCpus"), 2 * 10**9)
        or policy._memory_bytes(host.get("Memory")) != 2 * 1024**3
        or policy._memory_bytes(host.get("MemorySwap")) != 2 * 1024**3
        or not _number(host.get("PidsLimit"), 96)
        or host.get("RestartPolicy", {}).get("Name") != expected["restart"]
        or host.get("LogConfig")
        != {
            "Type": expected["logging"]["driver"],
            "Config": expected["logging"]["options"],
        }
        or policy._tmpfs(host.get("Tmpfs")) != policy._tmpfs(expected["tmpfs"])
        or policy._ulimits(host.get("Ulimits") or []) != {}
    ):
        raise LeaseError("catalog_client_resource_policy_changed")
    if (
        state.get("Running") is not True
        or state.get("Paused") is not False
        or state.get("OOMKilled") is not False
        or state.get("Health", {}).get("Status") != "healthy"
    ):
        raise LeaseError("catalog_client_not_healthy")
    health = config.get("Healthcheck")
    approved = expected["healthcheck"]
    if (
        type(health) is not dict
        or health.get("Test") != approved["test"]
        or not _number(health.get("Retries"), approved["retries"])
        or any(
            not _number(
                health.get(actual, 0), policy._duration(approved.get(setting, "0s"))
            )
            for actual, setting in (
                ("Interval", "interval"),
                ("Timeout", "timeout"),
                ("StartPeriod", "start_period"),
                ("StartInterval", "start_interval"),
            )
        )
    ):
        raise LeaseError("catalog_client_healthcheck_changed")
    for ports in (host.get("PortBindings"), network.get("Ports")):
        if type(ports) is not dict or any(
            value is not None for value in ports.values()
        ):
            raise LeaseError("catalog_client_publishes_a_port")
    try:
        env = source_bundle.parse_image_environment(config.get("Env"))
    except source_bundle.SourceBundleError:
        raise LeaseError("catalog_client_environment_changed") from None
    if env != image_environment | expected["environment"]:
        raise LeaseError("catalog_client_environment_changed")
    actual_mounts = row.get("Mounts")
    if type(actual_mounts) is not list:
        raise LeaseError("catalog_client_mounts_changed")
    binds, temporary = [], []
    for mount in actual_mounts:
        if type(mount) is not dict:
            raise LeaseError("catalog_client_mounts_changed")
        if mount.get("Type") == "tmpfs":
            temporary.append((mount.get("Destination"), mount.get("RW")))
        else:
            if mount.get("RW") is not False:
                raise LeaseError("catalog_client_mounts_changed")
            binds.append(
                tuple(mount.get(key) for key in ("Type", "Source", "Destination", "RW"))
            )
    if (
        len(binds) != 5
        or len(set(binds)) != 5
        or set(binds) != mounts
        or temporary not in ([], [("/tmp", True)])
    ):
        raise LeaseError("catalog_client_mounts_changed")
    attachments = network.get("Networks")
    if (
        host.get("NetworkMode") not in network_ids
        or type(attachments) is not dict
        or set(attachments) != set(network_ids)
        or any(
            type(value) is not dict or value.get("NetworkID") != network_ids[name]
            for name, value in attachments.items()
        )
    ):
        raise LeaseError("catalog_client_network_attachments_changed")


def validate_client_lease(
    lease,
    *,
    catalog_identity,
    release_compose,
    image_proof,
    image_environment,
    api_container,
    catalog_network,
):
    """Validate supplied sealed facts and actual inspect rows, never filter peers.

    ``catalog_identity`` is the closed CATALOG_FIELDS summary of independently
    host-verified runtime/projection/access receipts, plus ``catalog_revision``
    when a newer application reuses that original catalog. ``image_proof`` is the
    closed IMAGE_FIELDS host-verified source/image summary. Lease/image/compose
    source_sha256 is SHA256 of ``private_io.canonical(service_source_map)``:
    paths map to Git mode, size and SHA256, including Dockerfile, deny-all ignore
    and exact COPY inputs. Revision is Git's full 40-hex commit. Network IDs are
    full actual IDs, keyed by rendered names. The API service must be separately
    sealed with an immutable image ID and the required source labels; renderer
    tags alone do not pass this validation. Other release service capabilities
    and the source-map/build inspection remain independent caller obligations.
    ``image_environment`` must be the independently inspected same immutable
    image's Config.Env list, not the API container's Env. Its canonical parsed
    digest must match the independently verified image proof; the actual API
    environment must exactly match that map merged with the sealed Compose
    environment. No environment values are returned, including inherited ones.
    ``release_compose`` is the canonical renderer plan after host ID/label
    sealing, not Docker Compose's normalized configuration output.
    A caller that substitutes unverified dictionaries has not proved safety.

    Return only fixed identities and digests, never environment, mount paths,
    credentials, raw inspect output or arbitrary caller-supplied fields.
    """
    try:
        _identity(lease, catalog_identity, release_compose, image_proof)
        network_ids = _network_plan(lease, release_compose)
        expected, mounts = _service_plan(lease, release_compose)
        try:
            image_env = source_bundle.parse_image_environment(image_environment)
        except source_bundle.SourceBundleError:
            raise LeaseError("catalog_client_image_environment_invalid") from None
        if (
            lease_digest(image_env) != image_proof["image_environment_sha256"]
            or image_env.get("SOURCE_REVISION") != lease["revision"]
        ):
            raise LeaseError("catalog_client_image_environment_changed")
        _actual(lease, expected, mounts, network_ids, image_env, api_container)
        peers = set(lease["core_container_ids"].values()) | {lease["api_container_id"]}
        network = catalog_network
        if (
            type(network) is not dict
            or network.get("Id") != lease["catalog_network_id"]
            or network.get("Name") != lease["catalog_project"] + "_search_backend"
            or network.get("Internal") is not True
            or network.get("Attachable") is not False
            or type(network.get("Containers")) is not dict
            or set(network["Containers"]) != peers
            or network.get("Labels", {}).get("com.docker.compose.project")
            != lease["catalog_project"]
        ):
            raise LeaseError("catalog_client_network_peers_changed")
        return {
            "lease_schema": 1,
            "phase": "private_clients",
            "status": "verified",
            "lease_sha256": lease_digest(lease),
            "image_environment_sha256": image_proof["image_environment_sha256"],
            **(
                {"catalog_revision": lease["catalog_revision"]}
                if "catalog_revision" in lease
                else {}
            ),
            **{
                key: lease[key] for key in CATALOG_FIELDS if key != "core_container_ids"
            },
            "core_container_ids": dict(lease["core_container_ids"]),
            **{
                key: lease[key]
                for key in (
                    "release_project",
                    "release_compose_sha256",
                    "revision",
                    "source_sha256",
                    "api_container_id",
                    "api_image_id",
                )
            },
            "network_ids": dict(network_ids),
            "expected_peer_ids": sorted(peers),
        }
    except LeaseError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        RecursionError,
    ):
        raise LeaseError("catalog_client_evidence_invalid") from None


def validate_leased_runtime(
    core_plan,
    core_compose,
    core_images,
    core_containers,
    catalog_network,
    lease,
    *,
    catalog_identity,
    release_compose,
    image_proof,
    image_environment,
    api_container,
    core_image_inspections,
):
    """Prove the separate catalog-plus-one-client profile from supplied facts.

    This function is pure, not a host readiness/receipt facade. The host must
    independently authenticate the prepared source/Compose/image/query/lease
    records, hold product/catalog locks and collect actual same-snapshot Docker
    evidence. It must also verify projection/access evidence and all non-catalog
    networks. Supplied dictionaries cannot authenticate themselves.

    ``core_image_inspections`` contains actual inspections keyed by the three
    core roles, of the same immutable image IDs as ``core_images``. Startup Cmd
    and Entrypoint are checked here as in the baseline host execution proof.
    Docker may omit unset startup fields; exact ``.get`` comparison preserves
    the baseline's missing/null equivalence while keeping null distinct from [].
    The supplied catalog network is passed UNFILTERED to the existing lease
    checker: exactly two core peers plus this API are allowed, never an extra
    helper, former release or unknown container. No baseline/network object is
    synthesized to hide the client from ``policy.validate_runtime``.
    """
    try:
        try:
            core_ids = policy.validate_core_containers(
                core_plan, core_compose, core_images, core_containers
            )
        except policy.RuntimeError:
            raise LeaseError("catalog_client_core_runtime_changed") from None
        _closed(
            core_image_inspections,
            policy.SERVICES,
            "catalog_client_core_image_inspections_invalid",
        )
        for row in core_containers:
            role = row["Config"]["Labels"]["com.docker.compose.service"]
            inspection = core_image_inspections[role]
            if (
                type(inspection) is not dict
                or inspection.get("Id") != core_images[role]["image_id"]
                or type(inspection.get("Config")) is not dict
                or any(
                    row["Config"].get(name) != inspection["Config"].get(name)
                    for name in ("Cmd", "Entrypoint")
                )
            ):
                raise LeaseError("catalog_client_core_image_startup_changed")
        proof = validate_client_lease(
            lease,
            catalog_identity=catalog_identity,
            release_compose=release_compose,
            image_proof=image_proof,
            image_environment=image_environment,
            api_container=api_container,
            catalog_network=catalog_network,
        )
        if (
            catalog_network.get("Driver") != "bridge"
            or catalog_network.get("Scope") != "local"
            or catalog_network.get("Ingress") is not False
        ):
            raise LeaseError("catalog_client_network_driver_changed")
        network_name = lease["catalog_project"] + "_search_backend"
        for row in core_containers:
            role = row["Config"]["Labels"]["com.docker.compose.service"]
            if role not in CORES:
                continue  # The CPU embedding shares Fess's verified namespace.
            attachment = row["NetworkSettings"]["Networks"][network_name]
            if (
                type(attachment) is not dict
                or attachment.get("NetworkID") != lease["catalog_network_id"]
            ):
                raise LeaseError("catalog_client_core_network_identity_changed")
        if (
            type(core_plan) is not dict
            or not _number(core_plan.get("schema_version"), 1)
            or core_plan.get("project") != lease["catalog_project"]
            or core_plan.get("revision")
            != lease.get("catalog_revision", lease["revision"])
            or core_plan.get("manifest_sha256") != lease["manifest_sha256"]
            or {role: core_ids[role] for role in CORES} != lease["core_container_ids"]
            or len(set(core_ids.values()) | {lease["api_container_id"]}) != 4
        ):
            raise LeaseError("catalog_client_core_catalog_binding_changed")
        work = _path(core_plan["work_root"])
        data = _path(core_plan["data_root"])
        bundle = _path(core_plan["bundle_path"])
        state = work.parent.parent
        query_root = work / "query-access"
        approved = {
            (
                "bind",
                str(bundle),
                policy.catalog_bundle_target(lease["bundle_id"]),
                False,
            ),
            ("bind", str(state / "secrets/api_key"), "/run/secrets/api_key", False),
            (
                "bind",
                str(query_root / "index-query-password"),
                "/run/secrets/index_query_password",
                False,
            ),
            (
                "bind",
                str(query_root / "binding.json"),
                "/run/index/binding.json",
                False,
            ),
            (
                "bind",
                str(data / "opensearch/config/root-ca.pem"),
                "/run/index/root-ca.pem",
                False,
            ),
        }
        if (
            work
            != state
            / "catalogs"
            / lease["catalog_project"].removeprefix("depositar-ai-catalog-")
            or data != work / "data"
            or bundle != state / "cache/bundles" / ("sha256-" + lease["bundle_id"])
            or _mount_plan(lease, release_compose["services"][SERVICE]) != approved
        ):
            raise LeaseError("catalog_client_core_catalog_paths_changed")
        return {
            **proof,
            "core_runtime_verified": True,
            "catalog_container_ids": dict(core_ids),
        }
    except LeaseError:
        raise
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        RecursionError,
    ):
        raise LeaseError("catalog_client_leased_runtime_evidence_invalid") from None
