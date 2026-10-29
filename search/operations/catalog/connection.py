"""Read sealed credentials and bind one catalog client to verified Docker peers."""

from __future__ import annotations

import ipaddress
import os
import re

from search.operations.catalog import execution, projection
from search.operations.catalog.policy import RuntimeError
from search.operations.shared import private_io


def _secret(fd, name):
    raw = private_io.read_file(fd, name, mode=0o600, max_bytes=65)
    if re.fullmatch(rb"[0-9a-f]{64}\n", raw) is None:
        raise RuntimeError("catalog_query_secret_invalid")
    return raw.decode("ascii").removesuffix("\n")


def materials(context):
    """Read bounded sealed admin material into memory; never publish it."""
    secret_root = context["data"] / "secrets"
    ca_root = context["data"] / "opensearch/config"
    secret_fd = private_io.open_directory(secret_root)
    ca_fd = None
    try:
        ca_fd = private_io.open_directory(ca_root)
        admin = _secret(secret_fd, "opensearch_admin_password")
        ca = private_io.read_file(ca_fd, "root-ca.pem", mode=0o400, max_bytes=64 * 1024)
        private_io.verify_directory(secret_fd, secret_root)
        private_io.verify_directory(ca_fd, ca_root)
        return admin, ca
    finally:
        if ca_fd is not None:
            os.close(ca_fd)
        os.close(secret_fd)


def peers(context, runtime, images):
    return snapshot_peers(
        context,
        runtime,
        execution.inspect_runtime(context, images, include_snapshot=True),
    )


def snapshot_peers(context, runtime, snapshot):
    """Select pinned addresses only from the host's verified Docker snapshot."""
    if type(snapshot) is not tuple or len(snapshot) != 3:
        raise RuntimeError("catalog_query_runtime_snapshot_invalid")
    proof, containers, network = snapshot
    if not projection.same_json(
        proof, {key: runtime[key] for key in ("container_ids", "network_id")}
    ):
        raise RuntimeError("catalog_query_runtime_changed")
    name = context["plan"]["project"] + "_search_backend"
    by_id = {row["Id"]: row for row in containers}
    peers = {}
    for role in ("fess-search", "opensearch-search"):
        container_id = proof["container_ids"][role]
        try:
            attachment = by_id[container_id]["NetworkSettings"]["Networks"][name]
            literal = attachment["IPAddress"]
            interface = ipaddress.IPv4Interface(
                network["Containers"][container_id]["IPv4Address"]
            )
            address = ipaddress.IPv4Address(literal)
            valid = (
                type(literal) is str
                and str(address) == literal
                and interface.ip == address
                and attachment["NetworkID"] == proof["network_id"]
                and any(
                    address in ipaddress.IPv4Network(cidr)
                    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
                )
            )
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise RuntimeError("catalog_query_peer_changed")
        peers[role] = literal
    if len(set(peers.values())) != 2:
        raise RuntimeError("catalog_query_peer_changed")
    return peers
