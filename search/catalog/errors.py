"""Credential-free catalog errors and bounded diagnostic records."""

from __future__ import annotations

from typing import (
    Optional,
)
import urllib.error
import urllib.parse
import urllib.request


class CatalogError(RuntimeError):
    """Base class whose messages deliberately contain no remote response data."""


class ConfigurationError(CatalogError):
    pass


class TransportError(CatalogError):
    pass


class CatalogTimeout(TransportError):
    pass


class ProtocolError(CatalogError):
    pass


class FessBusy(ProtocolError):
    """Validated HTTP 429; retain only a safe delta-seconds retry hint."""

    def __init__(self, retry_after_seconds: Optional[int]) -> None:
        super().__init__("Fess returned HTTP 429")
        self.retry_after_seconds = retry_after_seconds


class BundleError(CatalogError):
    pass


class AuthenticationError(CatalogError):
    pass


class InventoryConflict(CatalogError):
    pass


class InventoryUnstable(ProtocolError):
    """A structurally valid paged inventory changed between page requests."""


class ImportFailed(CatalogError):
    """An import failure where server-side commit state may be unknown."""

    possibly_polluted = True


class TokenCleanupError(CatalogError):
    def __init__(self, message: str, *, possibly_polluted: bool = False) -> None:
        super().__init__(message)
        self.possibly_polluted = bool(possibly_polluted)


class VectorBuildError(CatalogError):
    """Vector failure is never permission to enable a generation path."""

    generation_enabled = False

    def __init__(self, message: str, *, possibly_polluted: bool = False) -> None:
        super().__init__(message)
        self.possibly_polluted = bool(possibly_polluted)


class UnsafeQuery(CatalogError):
    pass


_DIAGNOSTIC_STAGES = frozenset(
    {
        "operation",
        "bulk_dispatched",
        "bulk_after_prior_batch",
        "bulk_response",
        "post_import_inventory",
        "token_cleanup",
        "token_recovery",
        "token_journal",
        "logout",
    }
)


_HTTP_FAILURE_CAUSES = {
    401: "http_401",
    403: "http_403",
    429: "http_429",
    500: "http_500",
    502: "http_502",
    503: "http_503",
}


_DIAGNOSTIC_TYPES = {
    ConfigurationError: ("configuration", {"configuration"}),
    BundleError: ("bundle_invalid", {"bundle_invalid"}),
    TransportError: ("transport", {"transport", "request_timeout"}),
    CatalogTimeout: ("operation_deadline", {"operation_deadline"}),
    ProtocolError: (
        "response_invalid",
        {
            "response_invalid",
            "http_error",
            "api_rejected",
            *_HTTP_FAILURE_CAUSES.values(),
        },
    ),
    FessBusy: ("http_429", {"http_429"}),
    AuthenticationError: ("authentication", {"authentication"}),
    InventoryConflict: ("inventory_conflict", {"inventory_conflict"}),
    InventoryUnstable: ("inventory_unstable", {"inventory_unstable"}),
}


_IMPORT_CAUSES = {"unknown", "bulk_response_partial", "inventory_mismatch"}.union(
    *(causes for _default, causes in _DIAGNOSTIC_TYPES.values())
)


_DIAGNOSTIC_TYPES.update(
    {
        ImportFailed: ("unknown", _IMPORT_CAUSES),
        TokenCleanupError: (
            "cleanup_unverified",
            _IMPORT_CAUSES | {"cleanup_unverified"},
        ),
    }
)


_DIAGNOSTIC_NAMES = {
    kind.__name__: policy for kind, policy in _DIAGNOSTIC_TYPES.items()
}


_IMPORT_STAGES = {
    "bulk_dispatched": _IMPORT_CAUSES
    - {"unknown", "bulk_response_partial", "inventory_mismatch"},
    "bulk_after_prior_batch": _IMPORT_CAUSES
    - {"unknown", "bulk_response_partial", "inventory_mismatch"},
    "bulk_response": {"api_rejected", "bulk_response_partial"},
    "post_import_inventory": _IMPORT_CAUSES - {"unknown", "bulk_response_partial"},
}


def _diagnosed(error, *, stage="operation", cause=None):
    policy = next(
        (policy for kind, policy in _DIAGNOSTIC_TYPES.items() if type(error) is kind),
        None,
    )
    if (
        policy is not None
        and type(stage) is str
        and stage in _DIAGNOSTIC_STAGES
        and (cause is None or type(cause) is str and cause in policy[1])
    ):
        error.diagnostic_stage = stage
        error.diagnostic_cause = cause
    return error


def _failure_detail(error, *, stage=None):
    policy = next(
        (policy for kind, policy in _DIAGNOSTIC_TYPES.items() if type(error) is kind),
        None,
    )
    if policy is None:
        return None
    selected_stage = (
        error.__dict__.get("diagnostic_stage", "operation") if stage is None else stage
    )
    cause = error.__dict__.get("diagnostic_cause")
    cause = policy[0] if cause is None else cause
    if (
        type(selected_stage) is not str
        or selected_stage not in _DIAGNOSTIC_STAGES
        or type(cause) is not str
        or cause not in policy[1]
        or cause == "unknown"
    ):
        return None
    return {"error_type": type(error).__name__, "stage": selected_stage, "cause": cause}


def _failure_cause(error):
    detail = _failure_detail(error)
    return detail["cause"] if detail is not None else "unknown"


def _valid_failure_detail(value):
    if (
        type(value) is not dict
        or any(type(key) is not str for key in value)
        or set(value) != {"error_type", "stage", "cause"}
    ):
        return False
    if any(type(value[key]) is not str for key in value):
        return False
    policy = _DIAGNOSTIC_NAMES.get(value["error_type"])
    return (
        policy is not None
        and value["stage"] in _DIAGNOSTIC_STAGES
        and value["cause"] in policy[1]
        and value["cause"] != "unknown"
        and (
            value["error_type"] != "ImportFailed"
            or value["cause"] in _IMPORT_STAGES.get(value["stage"], ())
        )
    )


def valid_failure_record(value):
    """Validate a decoded, bounded helper failure; unknown shapes stay generic."""
    if (
        type(value) is not dict
        or any(type(key) is not str for key in value)
        or set(value)
        != {
            "schema_version",
            "status",
            "error_type",
            "possibly_polluted",
            "diagnostic",
            "primary_diagnostic",
            "cleanup_diagnostic",
        }
    ):
        return False
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["status"]) is not str
        or value["status"] != "failed"
        or type(value["error_type"]) is not str
        or value["error_type"] not in _DIAGNOSTIC_NAMES
        or type(value["possibly_polluted"]) is not bool
        or not _valid_failure_detail(value["diagnostic"])
        or value["diagnostic"]["error_type"] != value["error_type"]
    ):
        return False
    kind = value["error_type"]
    if (
        kind not in {"ImportFailed", "TokenCleanupError"}
        and value["diagnostic"]["stage"] != "operation"
    ):
        return False
    if kind == "TokenCleanupError" and value["diagnostic"]["stage"] not in {
        "operation",
        "token_cleanup",
    }:
        return False
    if kind == "ImportFailed" and not value["possibly_polluted"]:
        return False
    if kind not in {"ImportFailed", "TokenCleanupError"} and value["possibly_polluted"]:
        return False
    for name in ("primary_diagnostic", "cleanup_diagnostic"):
        detail = value[name]
        if detail is not None and (
            kind != "TokenCleanupError" or not _valid_failure_detail(detail)
        ):
            return False
    if value["primary_diagnostic"] is not None and value["primary_diagnostic"][
        "stage"
    ] not in {"operation", *_IMPORT_STAGES}:
        return False
    if value["cleanup_diagnostic"] is not None and value["cleanup_diagnostic"][
        "stage"
    ] not in {"token_cleanup", "token_recovery", "token_journal", "logout"}:
        return False
    if kind == "TokenCleanupError":
        primary, cleanup = value["primary_diagnostic"], value["cleanup_diagnostic"]
        if (
            primary is not None
            and primary["error_type"] == "ImportFailed"
            and not value["possibly_polluted"]
        ):
            return False
        if value["diagnostic"]["cause"] != (
            cleanup["cause"] if cleanup is not None else "cleanup_unverified"
        ):
            return False
    return True


def failure_record(error, *, possibly_polluted):
    """Copy only validated finite details at the public CLI boundary."""
    detail = _failure_detail(error)
    value = {
        "schema_version": 1,
        "status": "failed",
        "error_type": detail["error_type"] if detail is not None else "unknown",
        "possibly_polluted": possibly_polluted,
        "diagnostic": detail,
        "primary_diagnostic": None,
        "cleanup_diagnostic": None,
    }
    if type(error) is TokenCleanupError:
        value["primary_diagnostic"] = error.__dict__.get("primary_diagnostic")
        value["cleanup_diagnostic"] = error.__dict__.get("cleanup_diagnostic")
    if not valid_failure_record(value):
        value.update(
            error_type="unknown",
            diagnostic=None,
            primary_diagnostic=None,
            cleanup_diagnostic=None,
        )
    return value


def _transport_failure(error):
    timeout = type(error) is TimeoutError or (
        type(error) is urllib.error.URLError and type(error.reason) is TimeoutError
    )
    return _diagnosed(
        TransportError("Fess transport failed"),
        cause="request_timeout" if timeout else "transport",
    )
