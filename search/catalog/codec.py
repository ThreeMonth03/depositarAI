"""Strict JSON and immutable wire-value handling for catalog boundaries."""

from __future__ import annotations

import json
from typing import (
    Any,
    Mapping,
    Optional,
)

from search.serialization import strict_json

from .errors import ProtocolError


def _strict_json(raw: bytes) -> Any:
    try:
        return strict_json(raw)
    except (UnicodeError, ValueError, RecursionError):
        raise ProtocolError("received malformed JSON") from None


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise ProtocolError("cannot encode a bounded request") from None


def _exact_int(value: Any, *, minimum: int = 0, maximum: Optional[int] = None) -> bool:
    return (
        type(value) is int
        and value >= minimum
        and (maximum is None or value <= maximum)
    )


def _exact_json_equal(left: Any, right: Any) -> bool:
    """Compare decoded JSON without Python's bool/int equivalence."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _exact_json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _exact_json_equal(a, b) for a, b in zip(left, right)
        )
    return bool(left == right)


def _json_wire_copy(value: Any) -> Any:
    """Return mutable JSON containers at serialization/output boundaries.

    Sealed catalog documents use mapping proxies and tuples so callers cannot
    alter the data after manifest validation.  The stdlib JSON encoder does not
    accept ``MappingProxyType`` and direct Python consumers expect JSON arrays
    to remain lists, so explicitly thaw a copy without exposing the originals.
    """

    if isinstance(value, Mapping):
        return {key: _json_wire_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_wire_copy(item) for item in value]
    return value
