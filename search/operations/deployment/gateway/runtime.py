"""Pure routing profile for one gateway attached to an unchanged CPU release.

The host authenticates source/image authority, the rendered and pinned plans,
completed release proof and exact created gateway/network IDs before calling.
It supplies full unfiltered network inspections and holds the operation locks.
This module does no IO, routes nothing and grants no activation or HTTP claim.
Catalog, inference and ranking proofs remain separate, unchanged obligations.
"""

from __future__ import annotations

from pathlib import Path
import re

from search.operations.catalog import client_lease, policy
from search.operations.deployment import composition, process_identity
from search.operations.deployment.release import runtime as release_runtime
from search.operations.deployment import source_bundle
from search.operations.shared import private_io

ROLE = "web-nginx"
COMMAND = ["-c", "/etc/nginx/routes/active.conf", "-g", "daemon off;"]
RESOURCE_PROFILE = (0.5, 64 * 1024**2, 32)


class GatewayRuntimeError(ValueError):
    """Fixed diagnostics; never raw Docker output or operator material."""


def _require(condition, code):
    if not condition:
        raise GatewayRuntimeError("gateway_runtime_" + code)


def _same(left, right):
    return private_io.canonical(left) == private_io.canonical(right)


def _plan(gateway, release, lease, image_proof):
    _require(
        type(gateway) is dict
        and set(gateway) == {"name", "services", "networks", "x-depositar-plan"},
        "plan_invalid",
    )
    _require(
        gateway["name"] == composition.GATEWAY_PROJECT
        and type(gateway["services"]) is dict
        and set(gateway["services"]) == {ROLE},
        "plan_invalid",
    )
    _require(
        type(image_proof) is dict
        and set(image_proof) == client_lease.IMAGE_FIELDS
        and type(image_proof["schema_version"]) is int
        and image_proof["schema_version"] == 1
        and image_proof["revision"] == lease["revision"]
        and image_proof["service"] == ROLE
        and type(image_proof["source_sha256"]) is str
        and source_bundle.DIGEST.fullmatch(image_proof["source_sha256"])
        and type(image_proof["image_id"]) is str
        and policy.IMAGE_ID.fullmatch(image_proof["image_id"])
        and type(image_proof["image_environment_sha256"]) is str
        and source_bundle.DIGEST.fullmatch(image_proof["image_environment_sha256"]),
        "image_proof_invalid",
    )
    website = release["networks"]["website"]["name"]
    ingress = composition.GATEWAY_PROJECT + "_ingress"
    _require(
        _same(
            gateway["networks"],
            {
                "route": {"external": True, "name": website},
                "ingress": {"name": ingress, "internal": False, "attachable": False},
            },
        ),
        "network_plan_invalid",
    )
    api = release["services"]["conversation-api"]
    uid, gid = (int(value) for value in api["user"].split(":"))
    api_key = next(
        value for value in api["volumes"] if value["target"] == "/run/secrets/api_key"
    )
    route_root = Path(api_key["source"]).parent.parent / "gateway/routes"
    labels = release_runtime._labels(lease, ROLE, image_proof["source_sha256"])
    expected = {
        **composition._runtime(uid, gid, cpus=0.5, memory=64 * 1024**2, pids=32),
        "image": image_proof["image_id"],
        "labels": labels,
        "environment": {"NVIDIA_VISIBLE_DEVICES": "void"},
        "command": COMMAND,
        "networks": ["route", "ingress"],
        "ports": [
            {
                "target": 8080,
                "published": "18083",
                "host_ip": "127.0.0.1",
                "protocol": "tcp",
            }
        ],
        "volumes": [composition._mount(route_root, "/etc/nginx/routes")],
        "healthcheck": composition._health(
            ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"]
        ),
    }
    _require(_same(gateway["services"][ROLE], expected), "service_plan_invalid")
    alias = release["name"] + "-web"
    _require(
        _same(
            gateway["x-depositar-plan"],
            composition._plan(
                lease["revision"],
                singleton=True,
                route_publication_required=True,
                immutable_built_images_required=True,
                approved_upstream=alias + ":8000",
                route_text=composition._route_text(alias),
            ),
        ),
        "route_plan_invalid",
    )
    return expected, website, ingress


def _gateway(row, service, image, proof, container_id):
    _require(
        type(row) is dict
        and all(
            type(row.get(key)) is dict
            for key in ("Config", "HostConfig", "State", "NetworkSettings")
        ),
        "container_invalid",
    )
    config, host = row["Config"], row["HostConfig"]
    labels = config.get("Labels")
    _require(
        row.get("Id") == container_id
        and row.get("Name") == "/" + composition.GATEWAY_PROJECT + "-web-nginx-1"
        and type(labels) is dict
        and all(
            labels.get(key) == value
            for key, value in {
                **service["labels"],
                "com.docker.compose.project": composition.GATEWAY_PROJECT,
                "com.docker.compose.service": ROLE,
                "com.docker.compose.oneoff": "False",
                "com.docker.compose.container-number": "1",
            }.items()
        ),
        "identity_changed",
    )
    _require(
        type(image) is dict
        and type(image.get("Config")) is dict
        and image.get("Id")
        == proof["image_id"]
        == row.get("Image")
        == config.get("Image"),
        "image_identity_changed",
    )
    built = image["Config"]
    _require(
        built.get("Entrypoint") == ["nginx"]
        and built.get("Cmd") == ["-g", "daemon off;"]
        and built.get("User") == "1000:1000"
        and all(
            built.get("Labels", {}).get(key) == value
            for key, value in service["labels"].items()
        ),
        "image_contract_changed",
    )
    _require(
        config.get("Cmd") == COMMAND
        and config.get("Entrypoint") == built.get("Entrypoint")
        and config.get("WorkingDir", "") == built.get("WorkingDir", "")
        and config.get("StopSignal", "") == built.get("StopSignal", ""),
        "command_changed",
    )
    environment = source_bundle.parse_image_environment(built.get("Env"))
    _require(
        client_lease.lease_digest(environment) == proof["image_environment_sha256"]
        and source_bundle.parse_image_environment(config.get("Env"))
        == environment | service["environment"],
        "environment_changed",
    )
    release_runtime._running(row)
    release_runtime._runtime_resources(row, service, RESOURCE_PROFILE)
    _require(
        type(row.get("Mounts")) is list
        and all(
            type(mount) is dict
            and (
                mount.get("RW") is True
                if mount.get("Type") == "tmpfs"
                else mount.get("RW") is False
            )
            for mount in row["Mounts"]
        ),
        "mount_request_changed",
    )
    release_runtime._mounts(row, service)
    _require(
        host.get("Binds") in (None, [])
        and type(host.get("Mounts")) is list
        and len(host["Mounts"]) == 1,
        "mount_request_changed",
    )
    mount = host["Mounts"][0]
    _require(
        type(mount) is dict
        and set(mount)
        <= {"Type", "Source", "Target", "ReadOnly", "Consistency", "BindOptions"}
        and all(
            mount.get(key) == value
            for key, value in {
                "Type": "bind",
                "Source": service["volumes"][0]["source"],
                "Target": "/etc/nginx/routes",
            }.items()
        )
        and mount.get("ReadOnly") is True
        and mount.get("Consistency", "") == ""
        and any(
            _same(mount.get("BindOptions", {}), options)
            for options in (
                {},
                {"CreateMountpoint": False},
                {"Propagation": "rprivate"},
                {"CreateMountpoint": False, "Propagation": "rprivate"},
            )
        ),
        "mount_request_changed",
    )
    binding = [{"HostIp": "127.0.0.1", "HostPort": "18083"}]
    _require(
        _same(host.get("PortBindings"), {"8080/tcp": binding}), "port_binding_changed"
    )
    exposed = built.get("ExposedPorts", {})
    _require(
        type(exposed) is dict
        and all(
            re.fullmatch(r"[1-9][0-9]{0,4}/tcp", key) and value == {}
            for key, value in exposed.items()
        ),
        "image_ports_invalid",
    )
    ports = {key: None for key in exposed} | {"8080/tcp": binding}
    _require(_same(row["NetworkSettings"].get("Ports"), ports), "port_binding_changed")


def validate_runtime(
    compose,
    images,
    containers,
    networks,
    lease,
    *,
    gateway_compose,
    gateway_image,
    gateway_image_proof,
    gateway_container_id,
    ingress_network_id,
    release_proof,
    catalog_identity,
    image_proof,
    image_environment,
    catalog_network,
):
    """Verify exact four website peers and a gateway-only ingress, without IO.

    ``release_proof`` is the previously authenticated pre-routing release proof.
    Gateway/container/network IDs must come from the host's sealed creation
    evidence. CPU capabilities are checked again against their existing lease;
    the entire website and ingress peer sets are then checked without filtering.
    The caller separately proves unfiltered catalog/GPU networks and route bytes.
    """
    try:
        _require(
            type(containers) is list
            and len(containers) == 4
            and type(networks) is list
            and len(networks) == 2
            and type(gateway_container_id) is str
            and policy.CONTAINER_ID.fullmatch(gateway_container_id)
            and type(ingress_network_id) is str
            and policy.CONTAINER_ID.fullmatch(ingress_network_id),
            "inventory_invalid",
        )
        gateways = [
            row
            for row in containers
            if type(row) is dict and row.get("Id") == gateway_container_id
        ]
        _require(len(gateways) == 1, "identity_changed")
        cpu = [row for row in containers if row is not gateways[0]]
        by_role, client = release_runtime._validate_containers(
            compose,
            images,
            cpu,
            lease,
            catalog_identity=catalog_identity,
            image_proof=image_proof,
            image_environment=image_environment,
            catalog_network=catalog_network,
        )
        current_release = release_runtime._proof(by_role, client)
        _require(_same(current_release, release_proof), "release_identity_changed")
        _require(
            gateway_container_id
            not in set(current_release["container_ids"].values())
            | set(client["core_container_ids"].values())
            and ingress_network_id not in set(client["network_ids"].values()),
            "identity_collision",
        )
        service, website, ingress = _plan(
            gateway_compose, compose, lease, gateway_image_proof
        )
        gateway = gateways[0]
        _gateway(
            gateway, service, gateway_image, gateway_image_proof, gateway_container_id
        )
        expected_ids = {
            website: client["network_ids"][website],
            ingress: ingress_network_id,
        }
        by_network = {}
        for network in networks:
            _require(
                type(network) is dict
                and network.get("Name") in expected_ids
                and network["Name"] not in by_network
                and network.get("Id") == expected_ids[network["Name"]],
                "network_identity_changed",
            )
            by_network[network["Name"]] = network
        _require(set(by_network) == set(expected_ids), "network_identity_changed")
        all_ids = set(current_release["container_ids"].values()) | {
            gateway_container_id
        }
        for name, network in by_network.items():
            routing = name == website
            _require(
                network.get("Driver") == "bridge"
                and network.get("Scope") == "local"
                and network.get("Internal") is routing
                and network.get("Attachable") is False
                and network.get("Ingress") is False
                and network.get("EnableIPv6") is False
                and type(network.get("Labels")) is dict
                and network["Labels"].get("com.docker.compose.project")
                == (compose["name"] if routing else composition.GATEWAY_PROJECT)
                and network["Labels"].get("com.docker.compose.network")
                == ("website" if routing else "ingress")
                and type(network.get("Containers")) is dict
                and set(network["Containers"])
                == (all_ids if routing else {gateway_container_id}),
                "network_peers_changed",
            )
        attachments = gateway["NetworkSettings"].get("Networks")
        _require(
            gateway["HostConfig"].get("NetworkMode") in expected_ids
            and type(attachments) is dict
            and set(attachments) == set(expected_ids),
            "network_attachment_changed",
        )
        for name, attachment in attachments.items():
            _require(
                type(attachment) is dict
                and attachment.get("NetworkID") == expected_ids[name]
                and type(attachment.get("EndpointID")) is str
                and policy.CONTAINER_ID.fullmatch(attachment["EndpointID"])
                and by_network[name]["Containers"][gateway_container_id].get(
                    "EndpointID"
                )
                == attachment["EndpointID"],
                "network_attachment_changed",
            )
        all_roles = by_role | {ROLE: gateway}
        return {
            "container_ids": {
                role: row["Id"] for role, row in sorted(all_roles.items())
            },
            "network_ids": dict(client["network_ids"]) | {ingress: ingress_network_id},
            "start_identity_sha256": process_identity.start_identity_sha256(all_roles),
            "release_proof_sha256": client_lease.lease_digest(release_proof),
            "gateway_compose_sha256": client_lease.lease_digest(gateway_compose),
            "gateway_image_proof_sha256": client_lease.lease_digest(
                gateway_image_proof
            ),
        }
    except GatewayRuntimeError:
        raise
    except (
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        RecursionError,
    ):
        raise GatewayRuntimeError("gateway_runtime_snapshot_invalid") from None
