"""Query-only OpenSearch access bound to one private, physical catalog index.

Only the operator transport holds administrator credentials. The API receives
the distinct query secret and compatible binding, never this operator module.
A secret without its completed binding is evidence of an interrupted provision;
automatic retries must not rotate it or overwrite an existing remote identity.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import secrets

from search.serialization import strict_json
from search.catalog.bundle import CatalogBundle
from search.catalog.policy import MAX_DOCUMENTS
from search.operations.catalog import projection
from search.operations.shared import private_io
from search.planning import index_filters as filters


USER = "depositar_filtered"
SECRET_NAME = "index-query-password"
BINDING_NAME = "binding.json"
MAX_BINDING_BYTES = 4096
KINDS = ("roles", "internalusers", "rolesmapping")
SECURITY_PATH = "/_plugins/_security/api/"


class AccessError(ValueError):
    """Fixed diagnostics, without secrets, administrator bodies or remote errors."""


def role_definition(index):
    projection.require_index(index)
    return {
        "cluster_permissions": [],
        "index_permissions": [
            {
                "index_patterns": [index],
                "allowed_actions": ["indices:data/read/search*"],
            }
        ],
        "tenant_permissions": [],
    }


def _binding(bundle, index):
    projection.require_index(index)
    if (
        not isinstance(bundle, CatalogBundle)
        or not 1 <= len(bundle.documents) <= MAX_DOCUMENTS
        or any(
            type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (bundle.bundle_id, bundle.manifest_sha256)
        )
    ):
        raise AccessError("catalog_access_bundle_invalid")
    return {
        "index": index,
        "manifest_sha256": bundle.manifest_sha256,
        "bundle_id": bundle.bundle_id,
        "projection_schema": filters.VERSION,
    }


def _request(client, method, path, payload=None, **kwargs):
    try:
        return projection.request(client, method, path, payload, **kwargs)
    except projection.ProjectionError:
        raise AccessError("catalog_access_transport_failed") from None


def _local(fd, root, expected, *, require_complete):
    private_io.verify_directory(fd, root)
    values = {}
    for name, limit in ((SECRET_NAME, 65), (BINDING_NAME, MAX_BINDING_BYTES)):
        try:
            values[name] = private_io.read_file(fd, name, mode=0o600, max_bytes=limit)
        except FileNotFoundError:
            pass
    if not values and not require_complete:
        return None
    if set(values) != {SECRET_NAME, BINDING_NAME}:
        raise AccessError("catalog_access_recovery_required")
    if re.fullmatch(rb"[0-9a-f]{64}\n", values[SECRET_NAME]) is None:
        raise AccessError("catalog_access_secret_invalid")
    try:
        binding = strict_json(values[BINDING_NAME])
    except (ValueError, UnicodeError, RecursionError):
        raise AccessError("catalog_access_binding_invalid") from None
    if not projection.same_json(binding, expected):
        raise AccessError("catalog_access_binding_changed")
    private_io.verify_directory(fd, root)
    return values[SECRET_NAME].decode("ascii").removesuffix("\n")


def _remote(client):
    result = {}
    for kind in KINDS:
        status, body = _request(client, "GET", SECURITY_PATH + kind + "/" + USER)
        if status == 404:
            result[kind] = None
        elif status == 200 and type(body) is dict and set(body) == {USER}:
            value = body[USER]
            if type(value) is not dict:
                raise AccessError("catalog_access_identity_invalid")
            result[kind] = value
        else:
            raise AccessError("catalog_access_identity_invalid")
    return result


def _flags(value):
    return all(
        value.get(key, False) is False for key in ("reserved", "hidden", "static")
    )


def _verify_remote(values, index):
    role, user, mapping = (values[kind] for kind in KINDS)
    if any(type(value) is not dict or not _flags(value) for value in values.values()):
        raise AccessError("catalog_access_permissions_changed")
    expected_role = role_definition(index)
    permissions = role.get("index_permissions")
    if (
        role.get("cluster_permissions") != []
        or role.get("tenant_permissions") != []
        or type(permissions) is not list
        or len(permissions) != 1
        or type(permissions[0]) is not dict
    ):
        raise AccessError("catalog_access_permissions_changed")
    item = permissions[0]
    if (
        not projection.same_json(item.get("index_patterns"), [index])
        or not projection.same_json(
            item.get("allowed_actions"),
            expected_role["index_permissions"][0]["allowed_actions"],
        )
        or item.get("dls", "") != ""
        or item.get("fls", []) != []
        or item.get("masked_fields", []) != []
        or set(item)
        - {"index_patterns", "allowed_actions", "dls", "fls", "masked_fields"}
        or user.get("backend_roles") != []
        or user.get("opendistro_security_roles", []) != []
        or user.get("attributes", {}) != {}
        or mapping.get("users") != [USER]
        or mapping.get("backend_roles") != []
        or mapping.get("hosts") != []
        or mapping.get("and_backend_roles", []) != []
    ):
        raise AccessError("catalog_access_permissions_changed")
    role_allowed = {
        "cluster_permissions",
        "index_permissions",
        "tenant_permissions",
        "reserved",
        "hidden",
        "static",
        "description",
    }
    user_allowed = {
        "hash",
        "password",
        "backend_roles",
        "opendistro_security_roles",
        "attributes",
        "reserved",
        "hidden",
        "static",
        "description",
    }
    mapping_allowed = {
        "users",
        "backend_roles",
        "hosts",
        "and_backend_roles",
        "reserved",
        "hidden",
        "static",
        "description",
    }
    if (
        set(role) - role_allowed
        or set(user) - user_allowed
        or set(mapping) - mapping_allowed
    ):
        raise AccessError("catalog_access_permissions_changed")


def _probes(client, bundle, index, password):
    wrong = "0" * 64 if password != "0" * 64 else "1" * 64
    checks = (
        (
            "search_allowed",
            "POST",
            "/" + index + "/_search",
            {"size": 0, "track_total_hits": True, "query": {"match_all": {}}},
            password,
            200,
        ),
        ("wrong_key", "POST", "/" + index + "/_search", {"size": 0}, wrong, 401),
        (
            "write_denied",
            "PUT",
            "/" + index + "/_doc/authorization-probe",
            {"authorization_probe": True},
            password,
            403,
        ),
        (
            "config_denied",
            "POST",
            "/fess_user.user/_search",
            {"size": 0},
            password,
            403,
        ),
        (
            "security_denied",
            "GET",
            SECURITY_PATH + "internalusers",
            None,
            password,
            403,
        ),
        (
            "mapping_write_denied",
            "PUT",
            "/" + index + "/_mapping",
            {"properties": {}},
            password,
            403,
        ),
    )
    evidence = []
    for name, method, path, payload, credential, expected in checks:
        status, body = _request(
            client, method, path, payload, username=USER, password=credential
        )
        if status != expected:
            raise AccessError("catalog_access_privilege_probe_failed")
        if name == "search_allowed":
            try:
                projection.require_complete_shards(body)
            except (projection.ProjectionError, AttributeError):
                raise AccessError("catalog_access_search_incomplete") from None
            hits = body.get("hits")
            if (
                body.get("timed_out") is not False
                or body.get("terminated_early", False) is not False
                or type(hits) is not dict
                or not projection.same_json(
                    hits.get("total"),
                    {"value": len(bundle.documents), "relation": "eq"},
                )
                or hits.get("hits") != []
            ):
                raise AccessError("catalog_access_search_incomplete")
        evidence.append(
            {"name": name, "http": status, "expected": expected, "passed": True}
        )
    return evidence


def _proof(bundle, index, evidence, *, created):
    return {
        "schema_version": 1,
        "status": "ready",
        "created": created,
        "binding": _binding(bundle, index),
        "role": role_definition(index),
        "document_count": len(bundle.documents),
        "probes": evidence,
        "admin_mounted_in_api": False,
    }


def _verify(client, bundle, index, root, fd, password, *, created):
    _verify_remote(_remote(client), index)
    evidence = _probes(client, bundle, index, password)
    private_io.verify_directory(fd, root)
    current = private_io.read_file(fd, SECRET_NAME, mode=0o600, max_bytes=65)
    if current != (password + "\n").encode("ascii"):
        raise AccessError("catalog_access_secret_changed")
    if (
        not created
        and _local(fd, root, _binding(bundle, index), require_complete=True) != password
    ):
        raise AccessError("catalog_access_secret_changed")
    _verify_remote(_remote(client), index)
    return _proof(bundle, index, evidence, created=created)


def verify_access(client, bundle, index, private_root):
    """Verify a completed binding and permissions without rotating or provisioning."""
    expected = _binding(bundle, index)
    root = Path(private_root)
    fd = private_io.open_directory(root)
    try:
        password = _local(fd, root, expected, require_complete=True)
        return _verify(client, bundle, index, root, fd, password, created=False)
    finally:
        os.close(fd)


def provision_access(client, bundle, index, private_root):
    """Create one query identity, or verify an exact completed no-op rerun.

    The caller holds the product/catalog lock and journal. If interrupted after
    secret publication, the next invocation requires explicit operator recovery.
    Binding publication happens only after remote readback and denial probes.
    """
    expected = _binding(bundle, index)
    root = Path(private_root)
    fd = private_io.open_directory(root)
    try:
        password = _local(fd, root, expected, require_complete=False)
        if password is not None:
            return _verify(client, bundle, index, root, fd, password, created=False)
        if any(value is not None for value in _remote(client).values()):
            raise AccessError("catalog_access_identity_recovery_required")
        password = secrets.token_hex(32)
        if re.fullmatch(r"[0-9a-f]{64}", password) is None:
            raise AccessError("catalog_access_secret_invalid")
        private_io.verify_directory(fd, root)
        private_io.write_file(
            fd, SECRET_NAME, (password + "\n").encode("ascii"), max_bytes=65
        )
        for kind, body in (
            ("roles", role_definition(index)),
            (
                "internalusers",
                {"password": password, "backend_roles": [], "attributes": {}},
            ),
            ("rolesmapping", {"users": [USER], "backend_roles": [], "hosts": []}),
        ):
            private_io.verify_directory(fd, root)
            status, _value = _request(
                client, "PUT", SECURITY_PATH + kind + "/" + USER, body
            )
            if status not in {200, 201}:
                raise AccessError("catalog_access_provision_failed")
        proof = _verify(client, bundle, index, root, fd, password, created=True)
        private_io.verify_directory(fd, root)
        private_io.write_json(fd, BINDING_NAME, expected, max_bytes=MAX_BINDING_BYTES)
        # Readback also checks secret inode/mode/link-count and final directory identity.
        if _local(fd, root, expected, require_complete=True) != password:
            raise AccessError("catalog_access_secret_changed")
        return proof
    finally:
        os.close(fd)
