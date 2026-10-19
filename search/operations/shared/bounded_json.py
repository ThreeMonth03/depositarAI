"""Fixed predecoder limits shared by trusted operator transports.

Callers separately bound their response bytes. Limits are not request options:
JSON nesting is at most 64 and a numeric token is at most 64 bytes. In particular
Python 3.9 must not convert a megabyte integer before discovering it is invalid.
Quoted text is excluded from token/nesting checks; the strict decoder remains
responsible for JSON syntax, UTF-8, duplicate keys and invalid constants.
"""

from __future__ import annotations

import math


MAX_DEPTH = 64
MAX_NUMBER_BYTES = 64


class BoundsError(ValueError):
    """No input text, values or credentials in errors."""


def predecode(raw):
    if type(raw) is not bytes:
        raise BoundsError("operator_json_bounds_invalid")
    quoted, escaped, depth, number = False, False, 0, 0
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
            continue
        if byte == 34:
            quoted, number = True, 0
        elif byte in (91, 123):
            depth += 1
            number = 0
            if depth > MAX_DEPTH:
                raise BoundsError("operator_json_bounds_invalid")
        elif byte in (93, 125):
            depth -= 1
            number = 0
            if depth < 0:
                raise BoundsError("operator_json_bounds_invalid")
        elif byte in b"-+0123456789.eE":
            number += 1
            if number > MAX_NUMBER_BYTES:
                raise BoundsError("operator_json_bounds_invalid")
        else:
            number = 0


def decoded_depth(value):
    """Observe depth/finite numbers, stopping above the fixed supported depth.

    Callers map the observed depth to their domain error codes. The supported
    limit is fixed and does not accept a decoder callback.
    """
    observed = 0
    stack = [(iter((value,)), 0)]
    while stack:
        items, depth = stack[-1]
        try:
            item = next(items)
        except StopIteration:
            stack.pop()
            continue
        if type(item) in {dict, list}:
            observed = max(observed, depth + 1)
            if observed > MAX_DEPTH:
                return observed
            children = item.values() if type(item) is dict else item
            stack.append((iter(children), depth + 1))
        elif type(item) is float and not math.isfinite(item):
            raise BoundsError("operator_json_bounds_invalid")
    return observed
