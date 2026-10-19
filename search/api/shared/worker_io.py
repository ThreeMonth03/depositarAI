"""Shared bounded worker IO, independent of operation and model policy.

Callbacks own their allowlist and lazily load their settings. This envelope
buffers validation and serialization before publishing a successful response;
it never prints exception details or generated diagnostics.
"""

import json
import sys

from .backend import MAX_WORKER_BYTES
from search.serialization import strict_json


def run_worker(worker_callable, argv=None, stdin=None, stdout=None):
    """Run one ``callback(operation, payload)`` using binary streams.

    Defaults are resolved at call time, preserving tests which patch sys.stdin
    or sys.stdout immediately before main(). Explicit argv omits the executable
    name, like sys.argv[1:]. No exception text is written to either output stream.
    """
    try:
        source = sys.stdin.buffer if stdin is None else stdin
        destination = sys.stdout.buffer if stdout is None else stdout
        arguments = sys.argv[1:] if argv is None else argv
        raw = source.read(MAX_WORKER_BYTES + 1)
        if len(raw) > MAX_WORKER_BYTES:
            raise ValueError("worker input bound")
        value = worker_callable(arguments[0], strict_json(raw))
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > MAX_WORKER_BYTES:
            raise ValueError("worker output bound")
        destination.write(encoded)
        return 0
    except Exception:
        return 1
