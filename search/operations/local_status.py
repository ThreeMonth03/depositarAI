"""Interpret retained local activation evidence without observing services."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import os
import re
import stat

from search.operations.deployment.composition import GATEWAY_PROJECT
from search.operations.shared import private_io
from search.operations.state import MAX_BYTES
from search.serialization import strict_json


_CATALOG_OPERATIONS = frozenset(
    {
        "sync",
        "prepare-index",
        "build-catalog-images",
        "start-index",
        "import-index",
        "reconcile-index",
        "build-vectors",
    }
)
_APP_DIRECTORIES = {"image-builds", "secrets", "inference", "releases", "gateway"}
_APP_RECORDS = {
    "image-candidate.json",
    "api-key-identity.json",
    "inference-candidate.json",
    "release-candidate.json",
    "query-candidate.json",
}
_IDENTITY = {
    "schema_version",
    "revision",
    "project",
    "release_project",
    "bundle_id",
    "manifest_sha256",
}
_DIGESTS = {
    "source_manifest_sha256",
    "prepared_sha256",
    "native_compose_sha256",
    "bootstrap_sha256",
    "route_sha256",
    "private_release_proof_sha256",
}
_COMPLETED_FLAGS = {
    "active_changed": False,
    "confers_runtime_authority": True,
    "runtime_verified": True,
    "gateway_verified": True,
    "loopback_verified": True,
    "two_turn_search_verified": True,
    "session_reset_verified": True,
    "ports_published": True,
    "private_release_acceptance_verified": True,
    "search_quality_verified": False,
    "activation_performed": True,
}


def _require(condition):
    if not condition:
        raise ValueError("product_local_activation_invalid")


def _digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _record(descriptor, name):
    return strict_json(
        private_io.read_file(descriptor, name, mode=0o600, max_bytes=MAX_BYTES)
    )


def _recorded_activation(store):
    """Bind the fixed pointer to its completed local record, never runtime."""
    pointer = store.read("active.json")
    _require(
        type(pointer) is dict
        and set(pointer)
        == _IDENTITY | {"status", "activation_complete_sha256", "active_changed"}
        and type(pointer["schema_version"]) is int
        and pointer["schema_version"] == 1
        and pointer["status"] == "gateway_active"
        and pointer["active_changed"] is True
        and type(pointer["revision"]) is str
        and re.fullmatch(r"[0-9a-f]{40}", pointer["revision"]) is not None
        and pointer["project"] == GATEWAY_PROJECT
        and all(
            _digest(pointer[key])
            for key in ("bundle_id", "manifest_sha256", "activation_complete_sha256")
        )
    )
    _require(
        pointer["release_project"]
        == "depositar-ai-release-"
        + pointer["bundle_id"][:12]
        + "-"
        + pointer["revision"][:12]
    )
    with ExitStack() as stack:
        gateway = private_io.open_child_directory(store.fd, "gateway")
        stack.callback(os.close, gateway)
        activation = private_io.open_child_directory(gateway, "activation")
        stack.callback(os.close, activation)
        complete = _record(activation, "complete.json")
        _require(
            type(complete) is dict
            and set(complete)
            == _IDENTITY
            | _DIGESTS
            | set(_COMPLETED_FLAGS)
            | {"probe_run_id", "status", "proof", "http_acceptance"}
            and hashlib.sha256(private_io.canonical(complete)).hexdigest()
            == pointer["activation_complete_sha256"]
            and all(complete[key] == pointer[key] for key in _IDENTITY)
            and type(complete["schema_version"]) is int
            and complete["status"] == "gateway_active"
            and all(complete[key] is value for key, value in _COMPLETED_FLAGS.items())
            and all(_digest(complete[key]) for key in _DIGESTS)
            and type(complete["probe_run_id"]) is str
            and re.fullmatch(r"[0-9a-f]{32}", complete["probe_run_id"]) is not None
            and type(complete["proof"]) is dict
            and type(complete["http_acceptance"]) is dict
        )
        prepared = _record(gateway, "prepared.json")
        _require(
            type(prepared) is dict
            and hashlib.sha256(private_io.canonical(prepared)).hexdigest()
            == complete["prepared_sha256"]
            and all(
                prepared.get(key) == pointer[key]
                for key in ("revision", "project", "release_project")
            )
            and type(prepared.get("schema_version")) is int
            and prepared["schema_version"] == 1
            and prepared.get("status") == "gateway_prepared"
            and prepared.get("source_manifest_sha256")
            == complete["source_manifest_sha256"]
        )
        private_io.verify_directory(gateway, store.root / "gateway")
        private_io.verify_directory(activation, store.root / "gateway/activation")
        _require(store.read("active.json") == pointer)
    return pointer


def classify_local_state(store, operations):
    """Return a local summary; failed or ambiguous evidence requires recovery."""
    names = set(os.listdir(store.fd))
    reasons = set()
    active = None
    for status, reason in (
        ("started", "operation_incomplete"),
        ("failed", "operation_failed"),
    ):
        if any(item["status"] == status for item in operations):
            reasons.add(reason)
    for name in names & (_APP_DIRECTORIES | _APP_RECORDS):
        info = os.stat(name, dir_fd=store.fd, follow_symlinks=False)
        directory = name in _APP_DIRECTORIES
        if (
            info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (
                stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
            )
            or (not directory and info.st_nlink != 1)
        ):
            reasons.add("artifact_invalid")
    if "active.json" in names:
        try:
            active = _recorded_activation(store)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            OverflowError,
        ):
            reasons.add("activation_record_invalid")
        else:
            if not any(
                item["operation"] == "activate-gateway"
                and item["status"] == "staged"
                and item["active_changed"] is True
                and item["source_revision"] == active["revision"]
                and item.get("bundle_id") == active["bundle_id"]
                and item.get("manifest_sha256") == active["manifest_sha256"]
                for item in operations
            ):
                reasons.add("activation_operation_unconfirmed")
    elif any(item["operation"] == "activate-gateway" for item in operations):
        reasons.add("activation_pointer_missing")
    elif "gateway" in names and "artifact_invalid" not in reasons:
        gateway = private_io.open_child_directory(store.fd, "gateway")
        try:
            if "activation" in os.listdir(gateway):
                reasons.add("activation_pointer_missing")
            private_io.verify_directory(gateway, store.root / "gateway")
        finally:
            os.close(gateway)
    if reasons:
        status = "recovery_required"
    elif active is not None:
        status = "active_recorded"
    elif names & (_APP_DIRECTORIES | _APP_RECORDS) or any(
        item["operation"] not in _CATALOG_OPERATIONS for item in operations
    ):
        status = "partially_prepared"
    else:
        status = "staged"
    return {
        "status": status,
        "active_release": active,
        "recovery_reasons": sorted(reasons),
    }
