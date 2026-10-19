#!/usr/bin/env python3
"""Verify that the private OpenSearch Security plugin is actually ready."""

from __future__ import annotations

import json
from pathlib import Path
import ssl
import sys
import urllib.error
import urllib.request


CA_PATH = Path("/usr/share/opensearch/config/root-ca.pem")
BASE_URL = "https://127.0.0.1:9200"
HEALTH_URL = BASE_URL + "/_plugins/_security/health"
MAX_RESPONSE_BYTES = 4096


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != BASE_URL:
        return 2
    try:
        request = urllib.request.Request(
            HEALTH_URL,
            headers={"Accept": "application/json"},
        )
        context = ssl.create_default_context(cafile=str(CA_PATH))
        with urllib.request.urlopen(request, context=context, timeout=3) as response:
            if response.status != 200 or response.geturl() != HEALTH_URL:
                return 1
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return 1
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_pairs,
        )
        if not isinstance(payload, dict) or payload.get("status") != "UP":
            return 1
        return 0
    except (OSError, UnicodeError, ValueError, urllib.error.URLError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
