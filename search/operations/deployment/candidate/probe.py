"""Bounded probes inside already verified API/BFF namespaces, without warmup.

Scripts use only the standard library, including in the older production image.
The host proves container ownership before supplying IDs. Model residency uses
GET only; a query creates one RAM session, sends one turn and resets in finally.
Only public model metadata or the requested result/context leave the namespace.
"""

from __future__ import annotations

import json
import re
import subprocess

from search.planning.contract import checked_query
from search.serialization import strict_json
from ..runtime_docker import InferenceError


CONTAINER = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
QUERY_SECONDS = 130
RESIDENCY_SECONDS = 20
ERRORS = frozenset(
    "shared_backend_" + code
    for code in (
        "probe_invalid",
        "residency_failed",
        "not_resident",
        "residency_changed",
        "query_invalid",
        "query_failed",
        "query_busy",
        "query_timeout",
        "query_identity_changed",
        "query_reset_failed",
    )
)


class SharedBackendProbeError(ValueError):
    """Closed diagnostics without HTTP bodies, tokens or subprocess output."""


_RESIDENCY_SCRIPT = r"""
import http.client
import json
import os
import sys
import threading

def abort():
    os.write(1, b'{"error":"shared_backend_residency_failed"}\n')
    os._exit(0)

watchdog = threading.Timer(15, abort)
watchdog.daemon = True
watchdog.start()
code = "shared_backend_residency_failed"
try:
    expected = json.loads(sys.argv[1])
    models = []
    for item, host in zip(expected, ("scope-model", "answer-model")):
        code = "shared_backend_residency_failed"
        connection = http.client.HTTPConnection(host, 11434, timeout=5)
        try:
            connection.request("GET", "/api/ps", headers={"Connection": "close"})
            response = connection.getresponse()
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise ValueError()
        finally:
            connection.close()
        value = json.loads(raw)
        if type(value) is not dict or type(value.get("models")) is not list:
            raise ValueError()
        rows = value["models"]
        if not rows:
            code = "shared_backend_not_resident"
            raise ValueError()
        code = "shared_backend_residency_changed"
        if len(rows) != 1 or type(rows[0]) is not dict:
            raise ValueError()
        row = rows[0]
        if (item["model"] not in (row.get("name"), row.get("model"))
            or row.get("digest") != item["digest"]
            or type(row.get("context_length")) is not int
            or row["context_length"] != item["num_ctx"]
            or any(type(row.get(key)) is not int or not 0 < row[key] <= 1024**4
                   for key in ("size", "size_vram"))):
            raise ValueError()
        if row["size_vram"] != row["size"]:
            code = "shared_backend_not_resident"
            raise ValueError()
        models.append({**item, "size_bytes": row["size"], "vram_bytes": row["size_vram"]})
    report = {"schema_version": 1, "status": "resident",
              "full_gpu_residency_verified": True, "models": models}
except Exception:
    report = {"error": code}
watchdog.cancel()
print(json.dumps(report, sort_keys=True, allow_nan=False), flush=True)
"""


_QUERY_SCRIPT = r"""
import http.client
import json
import os
import re
import socket
import sys
import threading
import time

ORIGIN = "http://127.0.0.1:18083"
started = time.monotonic()
active = [None]
active_socket = [None]
timed_out = [False]

class ProbeError(Exception):
    pass

def interrupt():
    timed_out[0] = True
    connection = active[0]
    sock = active_socket[0] or (connection.sock if connection is not None else None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

def abort():
    os.write(1, b'{"error":"shared_backend_query_timeout"}\n')
    os._exit(0)

def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError()
        value[key] = item
    return value

def request(method, path, *, payload=None, token=None, cleanup=False):
    left = started + (129 if cleanup else 124) - time.monotonic()
    if left <= 0:
        raise ProbeError("shared_backend_query_timeout")
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=min(left, 3 if cleanup else 123))
    active[0] = connection
    headers = {"Host": "127.0.0.1:18083", "Origin": ORIGIN,
               "Accept": "application/json", "Connection": "close"}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    try:
        connection.request(method, path, body=body, headers=headers)
        active_socket[0] = connection.sock
        response = connection.getresponse()
        if response.status == 429:
            raise ProbeError("shared_backend_query_busy")
        if response.status != 200:
            raise ProbeError("shared_backend_query_failed")
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ProbeError("shared_backend_query_failed")
        return json.loads(raw, object_pairs_hook=pairs)
    except (TimeoutError, socket.timeout):
        raise ProbeError("shared_backend_query_timeout") from None
    except OSError:
        if timed_out[0]:
            raise ProbeError("shared_backend_query_timeout") from None
        raise
    finally:
        active[0] = None
        active_socket[0] = None
        connection.close()

watchdog = threading.Timer(129, abort)
watchdog.daemon = True
watchdog.start()
interrupter = threading.Timer(124, interrupt)
interrupter.daemon = True
interrupter.start()
token = None
try:
    revision, text = sys.argv[1], json.loads(sys.argv[2])
    session = request("POST", "/api/session", payload={})
    if (type(session) is not dict or type(session.get("token")) is not str
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", session["token"]) is None):
        raise ProbeError("shared_backend_query_failed")
    token = session["token"]
    if (set(session) != {"token", "version", "expires_in", "max_turns", "revision"}
        or type(session["version"]) is not int or session["version"] != 0
        or type(session["expires_in"]) is not int or not 1 <= session["expires_in"] <= 3600
        or type(session["max_turns"]) is not int or not 1 <= session["max_turns"] <= 20
        or session["revision"] != revision):
        raise ProbeError("shared_backend_query_identity_changed")
    value = request("POST", "/api/turn", token=token,
                    payload={"query": text, "version": 0, "top_k": 5, "timezone": "Asia/Taipei"})
    if (type(value) is not dict or set(value) != {"version", "result", "context_used"}
        or type(value["version"]) is not int or value["version"] != 1
        or type(value["result"]) is not dict or type(value["context_used"]) is not dict):
        raise ProbeError("shared_backend_query_failed")
    result, context = value["result"], value["context_used"]
    if (result.get("revision") != revision
        or (result.get("search") is not None
            and (type(result["search"]) is not dict or result["search"].get("revision") != revision))):
        raise ProbeError("shared_backend_query_identity_changed")
    if result.get("status") not in {"ok", "rejected", "clarification_required"}:
        raise ProbeError("shared_backend_query_failed")
    if context.get("timezone") != "Asia/Taipei":
        raise ProbeError("shared_backend_query_failed")
    report = {"result": result, "context_used": context}
except ProbeError as error:
    report = {"error": str(error)}
except Exception:
    report = {"error": "shared_backend_query_timeout" if timed_out[0] else "shared_backend_query_failed"}
finally:
    interrupter.cancel()
    if token is not None:
        try:
            if request("DELETE", "/api/session", token=token, cleanup=True) != {"status": "cleared"}:
                raise ValueError()
        except Exception:
            report = {"error": "shared_backend_query_reset_failed"}
    token = None
watchdog.cancel()
try:
    encoded = json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False)
except Exception:
    encoded = '{"error":"shared_backend_query_failed"}'
print(encoded, flush=True)
"""


def _invoke(docker, container, script, arguments, *, timeout, maximum, failure):
    if type(container) is not str or CONTAINER.fullmatch(container) is None:
        raise SharedBackendProbeError("shared_backend_probe_invalid")
    try:
        raw = docker.call(
            ["exec", container, "python", "-I", "-S", "-B", "-c", script, *arguments],
            timeout=timeout,
        )
        if type(raw) is not bytes or len(raw) > maximum:
            raise ValueError()
        value = strict_json(raw)
        if type(value) is not dict:
            raise ValueError()
    except subprocess.TimeoutExpired:
        raise SharedBackendProbeError(
            "shared_backend_query_timeout"
            if failure == "query_failed"
            else "shared_backend_residency_failed"
        ) from None
    except InferenceError as error:
        code = (
            "query_timeout"
            if failure == "query_failed" and str(error) == "inference_command_timeout"
            else failure
        )
        raise SharedBackendProbeError("shared_backend_" + code) from None
    except (ValueError, OSError, TypeError, KeyError):
        raise SharedBackendProbeError("shared_backend_" + failure) from None
    if set(value) == {"error"}:
        if type(value["error"]) is str and value["error"] in ERRORS:
            raise SharedBackendProbeError(value["error"])
        raise SharedBackendProbeError("shared_backend_" + failure)
    return value


def _expected(policy):
    try:
        expected = [
            {
                "role": role,
                "model": policy[role]["model"],
                "digest": policy[role]["model_digest"],
                "num_ctx": policy[role]["options"]["num_ctx"],
            }
            for role in ("scope", "planner")
        ]
        if any(
            type(row["model"]) is not str
            or not 1 <= len(row["model"]) <= 128
            or type(row["digest"]) is not str
            or DIGEST.fullmatch(row["digest"]) is None
            or type(row["num_ctx"]) is not int
            or not 1 <= row["num_ctx"] <= 1024**2
            for row in expected
        ):
            raise ValueError()
        return expected
    except (ValueError, KeyError, TypeError):
        raise SharedBackendProbeError("shared_backend_probe_invalid") from None


def verify_residency(docker, api_container_id, policy, *, timeout=RESIDENCY_SECONDS):
    """GET existing model residency; never load, warm, generate or stop models."""
    if type(timeout) not in {int, float} or not 0 < timeout <= RESIDENCY_SECONDS:
        raise SharedBackendProbeError("shared_backend_probe_invalid")
    expected = _expected(policy)
    value = _invoke(
        docker,
        api_container_id,
        _RESIDENCY_SCRIPT,
        [json.dumps(expected, sort_keys=True)],
        timeout=timeout,
        maximum=4096,
        failure="residency_failed",
    )
    try:
        if (
            set(value)
            != {"schema_version", "status", "full_gpu_residency_verified", "models"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["status"] != "resident"
            or value["full_gpu_residency_verified"] is not True
            or type(value["models"]) is not list
            or len(value["models"]) != 2
        ):
            raise ValueError()
        for actual, item in zip(value["models"], expected):
            if (
                type(actual) is not dict
                or set(actual) != set(item) | {"size_bytes", "vram_bytes"}
                or {key: actual[key] for key in item} != item
                or type(actual["size_bytes"]) is not int
                or not 0 < actual["size_bytes"] <= 1024**4
                or type(actual["vram_bytes"]) is not int
                or actual["vram_bytes"] != actual["size_bytes"]
            ):
                raise ValueError()
        return value
    except (ValueError, KeyError, TypeError):
        raise SharedBackendProbeError("shared_backend_residency_failed") from None


def query(docker, bff_container_id, text, revision):
    """Perform one private BFF turn and reset its session, without retries.

    The result is intended for the interactive CLI only. The host must not put
    query text, returned cards or context into durable operation receipts.
    """
    try:
        text = checked_query(text)
        if type(revision) is not str or REVISION.fullmatch(revision) is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise SharedBackendProbeError("shared_backend_query_invalid") from None
    value = _invoke(
        docker,
        bff_container_id,
        _QUERY_SCRIPT,
        [revision, json.dumps(text, ensure_ascii=False)],
        timeout=QUERY_SECONDS,
        maximum=1024 * 1024 + 4096,
        failure="query_failed",
    )
    if (
        set(value) != {"result", "context_used"}
        or type(value["result"]) is not dict
        or type(value["context_used"]) is not dict
        or value["result"].get("revision") != revision
    ):
        raise SharedBackendProbeError("shared_backend_query_failed")
    return value
