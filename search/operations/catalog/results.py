"""Bounded catalog-admin output parsing and import/reconcile report validation."""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from search.serialization import strict_json


MAX_BYTES = 4 * 1024 * 1024


class ResultError(ValueError):
    pass


def _exact_keys(value: Mapping[str, Any], expected: set[str], where: str) -> None:
    if set(value) != expected:
        raise ResultError(f"{where} fields differ from the approved schema")


def _integer(value: Any, minimum: int = 0, maximum: int = 1_000_000_000) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _finite(value: Any, minimum: float = 0.0) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(converted) and converted >= minimum


def parse_record(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes:
        raise ResultError("catalog-admin output must be bytes")
    if len(raw) > MAX_BYTES or not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise ResultError("catalog-admin output is not one bounded JSON record")
    try:
        value = strict_json(raw)
    except (UnicodeError, ValueError, RecursionError):
        raise ResultError("catalog-admin output is not strict JSON") from None
    if not isinstance(value, dict):
        raise ResultError("catalog-admin output root is not an object")
    return value


def validate_import(
    value: Mapping[str, Any],
    bundle_id: str,
    manifest: str,
    *,
    operation: Optional[str] = None,
) -> None:
    _exact_keys(
        value,
        {
            "schema_version",
            "status",
            "bundle_id",
            "manifest_sha256",
            "document_count",
            "batch_count",
            "elapsed_seconds",
            "inventory_verified",
        },
        "import report",
    )
    if (
        not _integer(value.get("schema_version"), 1, 1)
        or value.get("status")
        not in {
            "imported",
            "already-active-no-op",
            "reconciled-active",
        }
        or value.get("bundle_id") != bundle_id
        or value.get("manifest_sha256") != manifest
        or not _integer(value.get("document_count"), 1, 10_000)
        or not _integer(value.get("batch_count"), 0, 10_000)
        or not _finite(value.get("elapsed_seconds"))
        or value.get("inventory_verified") is not True
    ):
        raise ResultError("catalog import report values are invalid")
    if (
        value["status"] in {"already-active-no-op", "reconciled-active"}
        and value["batch_count"] != 0
    ):
        raise ResultError("a non-writing catalog report cannot contain batches")
    if operation == "import" and value["status"] not in {
        "imported",
        "already-active-no-op",
    }:
        raise ResultError(
            "catalog import report does not match the requested operation"
        )
    if operation == "reconcile" and value["status"] != "reconciled-active":
        raise ResultError(
            "catalog import report does not match the requested operation"
        )
    if operation not in (None, "import", "reconcile"):
        raise ResultError("catalog import report operation is invalid")
