"""Canonical metadata encoding and strict, bounded JSON scalar validation."""

import hashlib
import json
import math
from typing import (
    Any,
    Dict,
    List,
    Tuple,
)

from . import policy


def canonical_json(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise policy.BundleError("value cannot be encoded as canonical JSON") from exc
    return text.encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def check_json_nesting(data: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth > policy.MAX_NESTING:
                raise policy.BundleError("JSON nesting exceeds the configured limit")
        elif byte in (0x7D, 0x5D):
            depth -= 1
            if depth < 0:
                raise policy.BundleError("malformed JSON nesting")


def reject_json_constant(value: str) -> Any:
    raise policy.BundleError("non-finite JSON number %s is not allowed" % value)


def parse_json_int(value: str) -> int:
    if len(value) > 128:
        raise policy.BundleError("JSON integer exceeds the numeric token limit")
    return int(value)


def parse_json_float(value: str) -> float:
    if len(value) > 128:
        raise policy.BundleError("JSON number exceeds the numeric token limit")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise policy.BundleError("non-finite JSON number is not allowed")
    return parsed


def unique_json_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise policy.BundleError("duplicate JSON object key is not allowed")
        result[key] = value
    return result


def reject_surrogates(value: Any) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                raise policy.BundleError(
                    "JSON strings must contain Unicode scalar values"
                )
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
