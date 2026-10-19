"""Strict JSON decoding shared by HTTP, workers and operator readers.

Byte, nesting and numeric-token limits remain caller policy. This decoder only
accepts UTF-8 JSON, rejecting duplicate keys and non-finite number constants.
It reads no configuration and performs no IO.
"""

import json


def strict_json(raw: bytes):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def invalid(_value):
        raise ValueError("invalid JSON number")

    return json.loads(
        raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid
    )
