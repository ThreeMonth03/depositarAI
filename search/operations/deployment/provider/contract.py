"""Closed provider records; saved dictionaries never prove a live runtime."""

import hashlib
from pathlib import PurePosixPath
import re

from search.operations.shared import private_io


MAX_BYTES = 1024 * 1024
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
GPU_ROLES = {"scope-model", "answer-model", "reranker"}
CPU_ROLES = {"conversation-api", "web", "filtered-embedding"}
PROVIDER_FIELDS = {
    "schema_version",
    "kind",
    "source",
    "configuration",
    "source_plan_sha256",
    "inference_identity",
    "inference_complete_sha256",
    "model_manifest_sha256",
    "model_file_identity_sha256",
    "compute_sha256",
    "policy",
    "adopted_by_revision",
    "bootstrap_sha256",
    "namespace_sha256",
}
BACKEND_FIELDS = {
    "schema_version",
    "kind",
    "source",
    "configuration",
    "catalog_source",
    "build_state_root",
    "compose",
    "lease",
    "runtime",
    "image_proof",
    "image_authority_sha256",
    "catalog_binding",
    "owned_paths",
    "provider_sha256",
    "namespace_sha256",
}
ROLES_FIELDS = {
    "schema_version",
    "epoch",
    "provider_sha256",
    "active_backend_sha256",
    "rollback_backend_sha256",
    "tls_plan_ref",
    "pending_operation_sha256",
}
ERROR_CODES = frozenset(
    "shared_backend_provider_" + code
    for code in (
        "config_invalid",
        "record_invalid",
        "source_changed",
        "authority_changed",
        "not_adopted",
        "already_adopted",
        "busy",
        "namespace_unsafe",
        "recovery_required",
        "outcome_unconfirmed",
        "cas_conflict",
        "intent_invalid",
        "backend_protected",
        "bootstrap_invalid",
        "bootstrap_changed",
        "bootstrap_candidate",
        "runtime_changed",
        "model_changed",
        "deadline",
        "failed",
    )
)


class ProviderError(ValueError):
    """A fixed provider diagnostic without source paths or raw output."""


def require(value, code="record_invalid"):
    if not value:
        raise ProviderError("shared_backend_provider_" + code)


def digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def closed(value, fields):
    require(type(value) is dict and set(value) == fields)


def hexadecimal(value, pattern=HEX64):
    require(type(value) is str and pattern.fullmatch(value) is not None)


def path(value):
    require(type(value) is str and value.isascii())
    require(not value.startswith("//"))
    require(not any(ord(char) < 32 for char in value))
    parsed = PurePosixPath(value)
    require(parsed.is_absolute() and str(parsed) == value)
    require(value != "/" and ".." not in parsed.parts)
    return parsed


def reference(value):
    closed(value, {"path", "sha256"})
    path(value["path"])
    hexadecimal(value["sha256"])


def source(value):
    closed(value, {"root", "revision"})
    path(value["root"])
    hexadecimal(value["revision"], HEX40)


def _version(value, kind=None):
    require(type(value["schema_version"]) is int)
    require(value["schema_version"] == 1)
    if kind is not None:
        require(value["kind"] == kind)


def _maps(value, names):
    for name in names:
        require(type(value[name]) is dict and bool(value[name]))


def provider(value):
    closed(value, PROVIDER_FIELDS)
    _version(value, "model-provider")
    source(value["source"])
    reference(value["configuration"])
    for key in PROVIDER_FIELDS:
        if key.endswith("_sha256"):
            hexadecimal(value[key])
    hexadecimal(value["adopted_by_revision"], HEX40)
    _maps(value, ("inference_identity", "policy"))
    require(value["inference_identity"].get("schema_version") == 2)
    require(set(value["inference_identity"].get("image_ids", {})) == GPU_ROLES)
    request_policy(value["policy"])
    bounded(value)
    return value


def request_policy(value):
    closed(
        value, {"schema_version", "status", "request_policy_sha256", "policy"}
    )
    _version(value)
    require(value["status"] == "compatible")
    require(type(value["policy"]) is dict and bool(value["policy"]))
    require(value["request_policy_sha256"] == digest(value["policy"]))


def backend(value):
    closed(value, BACKEND_FIELDS)
    _version(value, "backend")
    source(value["source"])
    source(value["catalog_source"])
    reference(value["configuration"])
    path(value["build_state_root"])
    for key in (
        "provider_sha256",
        "image_authority_sha256",
        "namespace_sha256",
    ):
        hexadecimal(value[key])
    _maps(
        value,
        ("compose", "lease", "runtime", "image_proof", "catalog_binding"),
    )
    require(set(value["compose"].get("services", {})) == CPU_ROLES)
    require(set(value["runtime"].get("container_ids", {})) == CPU_ROLES)
    paths(value["owned_paths"])
    bounded(value)
    return value


def roles(value):
    closed(value, ROLES_FIELDS)
    _version(value)
    require(type(value["epoch"]) is int and 1 <= value["epoch"] < 2**63)
    for key in ("provider_sha256", "active_backend_sha256"):
        hexadecimal(value[key])
    for key in ("rollback_backend_sha256", "pending_operation_sha256"):
        if value[key] is not None:
            hexadecimal(value[key])
    require(value["active_backend_sha256"] != value["rollback_backend_sha256"])
    reference(value["tls_plan_ref"])
    return value


def bounded(value):
    require(len(private_io.canonical(value)) <= MAX_BYTES)


def paths(value):
    require(type(value) is list and len(value) <= 32)
    for item in value:
        path(item)
    require(len(set(value)) == len(value))


def resources(value):
    closed(value, {"container_ids", "network_ids", "owned_paths"})
    for key in ("container_ids", "network_ids"):
        require(type(value[key]) is list and len(value[key]) <= 16)
        for identifier in value[key]:
            hexadecimal(identifier)
    paths(value["owned_paths"])


def _intersects(left, right):
    return left == right or left in right.parents or right in left.parents


def require_disposable(role_record, proposed, protected):
    """Reject resources intersecting authenticated active/rollback closures."""
    roles(role_record)
    require(
        role_record["pending_operation_sha256"] is None, "recovery_required"
    )
    resources(proposed)
    for record in protected:
        resources(record)
        _check_resources(proposed, record)


def _check_resources(proposed, protected):
    for key in ("container_ids", "network_ids"):
        require(
            not set(proposed[key]) & set(protected[key]), "backend_protected"
        )
    for target in proposed["owned_paths"]:
        require(
            not any(
                _intersects(path(target), path(saved))
                for saved in protected["owned_paths"]
            ),
            "backend_protected",
        )
