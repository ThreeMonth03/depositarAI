"""Freeze explicit private profile references for a single metadata snapshot."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import stat

from search.catalog.policy import MAX_DOCUMENTS
from search.catalog.resources import MAX_RESOURCES
from search.operations.documents import controller
from search.operations.metadata.source import strict_json
from search.operations.shared import private_io


MAX_SELECTED = MAX_DOCUMENTS * MAX_RESOURCES
MAX_SET_BYTES = 8 * 1024**2
ERROR_CODES = frozenset(
    {
        "document_profile_set_invalid",
        "document_profile_set_limit_exceeded",
        "document_profile_set_conflict",
    }
)
_FAILURE_CODE = re.compile(r"document_[a-z_]{1,80}\Z")
_FILENAME = re.compile(r"profile-set-([0-9a-f]{64})\.json\Z")


class ProfileSetError(ValueError):
    """A frozen set failed its private-file or explicit-reference contract."""


def validate(value):
    """Validate and copy a sorted set, rejecting duplicate resource identities."""
    if (
        type(value) is not dict
        or set(value)
        != {
            "schema_version",
            "catalog_manifest_sha256",
            "source_sha256",
            "profiles",
            "failures",
        }
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or any(
            type(value[key]) is not str
            or controller.SHA256.fullmatch(value[key]) is None
            for key in ("catalog_manifest_sha256", "source_sha256")
        )
        or type(value["profiles"]) is not list
        or type(value["failures"]) is not list
    ):
        raise ProfileSetError("document_profile_set_invalid")
    if not 1 <= len(value["profiles"]) + len(value["failures"]) <= MAX_SELECTED:
        raise ProfileSetError("document_profile_set_limit_exceeded")
    seen = set()
    for field, extra in (("profiles", "profile_sha256"), ("failures", "code")):
        previous = None
        for row in value[field]:
            if (
                type(row) is not dict
                or set(row) != {"dataset_id", "resource_id", extra}
                or any(
                    type(row[key]) is not str
                    or controller.UUID.fullmatch(row[key]) is None
                    for key in ("dataset_id", "resource_id")
                )
                or type(row[extra]) is not str
                or (
                    controller.SHA256 if extra == "profile_sha256" else _FAILURE_CODE
                ).fullmatch(row[extra])
                is None
            ):
                raise ProfileSetError("document_profile_set_invalid")
            identity = (row["dataset_id"], row["resource_id"])
            if (previous is not None and identity <= previous) or row[
                "resource_id"
            ] in seen:
                raise ProfileSetError("document_profile_set_invalid")
            previous = identity
            seen.add(row["resource_id"])
    return deepcopy(value)


def publish(parent_fd, catalog_manifest_sha256, source_sha256, profiles, failures):
    """Publish a content-addressed set once; identical publication is idempotent."""
    try:
        info = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ProfileSetError("document_profile_set_invalid")
        value = validate(
            {
                "schema_version": 1,
                "catalog_manifest_sha256": catalog_manifest_sha256,
                "source_sha256": source_sha256,
                "profiles": sorted(
                    profiles, key=lambda row: (row["dataset_id"], row["resource_id"])
                ),
                "failures": sorted(
                    failures, key=lambda row: (row["dataset_id"], row["resource_id"])
                ),
            }
        )
        raw = private_io.canonical(value)
        if len(raw) > MAX_SET_BYTES:
            raise ProfileSetError("document_profile_set_limit_exceeded")
        digest = hashlib.sha256(raw).hexdigest()
        name = "profile-set-" + digest + ".json"
        try:
            private_io.write_file(parent_fd, name, raw, max_bytes=MAX_SET_BYTES)
        except FileExistsError:
            if (
                private_io.read_file(
                    parent_fd, name, mode=0o600, max_bytes=MAX_SET_BYTES
                )
                != raw
            ):
                raise ProfileSetError("document_profile_set_conflict")
        return {"profile_set_file": name, "profile_set_sha256": digest}
    except ProfileSetError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise ProfileSetError("document_profile_set_invalid") from error


def read(path):
    """Read a canonical private set whose filename authenticates its exact bytes."""
    try:
        path = Path(path)
        parent = controller._external_root(path.parent)
        match = _FILENAME.fullmatch(path.name)
        if match is None:
            raise ProfileSetError("document_profile_set_invalid")
        descriptor = private_io.open_directory(parent)
        try:
            raw = private_io.read_file(
                descriptor, path.name, mode=0o600, max_bytes=MAX_SET_BYTES
            )
            private_io.verify_directory(descriptor, parent)
        finally:
            os.close(descriptor)
        value = validate(strict_json(raw))
        digest = hashlib.sha256(raw).hexdigest()
        if digest != match[1] or private_io.canonical(value) != raw:
            raise ProfileSetError("document_profile_set_invalid")
        return value, digest
    except ProfileSetError:
        raise
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise ProfileSetError("document_profile_set_invalid") from error
