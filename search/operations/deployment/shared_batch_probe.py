"""Bounded multi-turn cases against a host-authenticated private BFF IPv4.

The caller verifies the Docker snapshot and checkpoints shared model ownership.
This module neither discovers endpoints nor accesses Docker or service keys.
Each invocation owns one RAM-only session and returns public BFF responses only.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import re
import socket
import threading
import time

from search.operations.shared.bounded_http import AbortableConnection
from search.planning.clock import normalize_context
from search.planning.contract import checked_query
from search.serialization import strict_json


MAX_RESPONSE_BYTES = 256 * 1024
MAX_TURNS = 8
ORIGIN = "http://127.0.0.1:18083"
PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    )
)
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
RESULT_FIELDS = {
    "status",
    "message",
    "state",
    "search",
    "reason",
    "timing_ms",
    "revision",
    "notices",
}


class _Failure(ValueError):
    """Only fixed operator codes, never HTTP data or session material."""


def _require(condition, code):
    if not condition:
        raise _Failure("shared_batch_" + code)


def _remaining(deadline):
    left = deadline - time.monotonic()
    _require(left > 0, "timeout")
    return left


class _Connection(AbortableConnection, http.client.HTTPConnection):
    debuglevel = 0

    def __init__(self, endpoint, timeout):
        http.client.HTTPConnection.__init__(self, endpoint, 8000, timeout=timeout)
        self._init_abortable()

    def connect(self):
        # Numeric AF_INET only: no DNS, proxy, redirects or caller-chosen port.
        channel = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        channel.settimeout(self.timeout)
        self._publish_channel(channel)
        try:
            channel.connect((self.host, self.port))
        except BaseException:
            channel.close()
            raise


def _request(endpoint, method, path, *, deadline, token=None, payload=None):
    connection = _Connection(endpoint, min(5, _remaining(deadline)))
    expired = threading.Event()

    def abort():
        expired.set()
        connection.abort()

    timer = threading.Timer(_remaining(deadline), abort)
    timer.daemon = True
    response = None
    headers = {
        "Host": "127.0.0.1:18083",
        "Origin": ORIGIN,
        "Accept": "application/json",
        "Connection": "close",
    }
    body = None
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
    timer.start()
    try:
        connection.request(method, path, body=body, headers=headers)
        # Connection setup stays short; the absolute watchdog also interrupts
        # header/body trickles and Connection: close detached response sockets.
        connection.sock.settimeout(_remaining(deadline))
        response = connection.getresponse()
        _require(response.status != 429, "busy")
        _require(response.status == 200, "http_failed")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        _remaining(deadline)
        _require(len(raw) <= MAX_RESPONSE_BYTES, "response_limit")
        try:
            value = strict_json(raw)
        except (ValueError, TypeError, RecursionError):
            raise _Failure("shared_batch_response_invalid") from None
        _require(type(value) is dict, "response_invalid")
        return value
    except _Failure:
        raise
    except (TimeoutError, socket.timeout):
        raise _Failure("shared_batch_timeout") from None
    except (OSError, http.client.HTTPException, ValueError, TypeError, RecursionError):
        code = "timeout" if expired.is_set() else "http_failed"
        raise _Failure("shared_batch_" + code) from None
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.finish()


def _context(value):
    _require(
        type(value) is dict
        and set(value)
        == {
            "reference_time",
            "timezone",
            "local_datetime",
            "reference_date",
        },
        "response_invalid",
    )
    _require(value["timezone"] == "Asia/Taipei", "response_invalid")
    try:
        expected = normalize_context(
            {key: value[key] for key in ("reference_time", "timezone")}
        )
    except (ValueError, TypeError, OverflowError):
        raise _Failure("shared_batch_response_invalid") from None
    _require(value == expected, "response_invalid")


def _timings(value):
    return type(value) is dict and all(
        type(key) is str
        and type(number) in (int, float)
        and math.isfinite(number)
        and number >= 0
        for key, number in value.items()
    )


def _turn(value, version, revision, manifest, token):
    _require(
        set(value) == {"version", "result", "context_used"}
        and type(value["version"]) is int
        and value["version"] == version,
        "response_invalid",
    )
    result = value["result"]
    _require(type(result) is dict and set(result) == RESULT_FIELDS, "response_invalid")
    _require(result["revision"] == revision, "identity_changed")
    _require(
        result["status"] in {"ok", "rejected", "clarification_required"}
        and type(result["message"]) is str
        and type(result["reason"]) is str
        and type(result["state"]) is dict
        and _timings(result["timing_ms"])
        and type(result["notices"]) is list
        and len(result["notices"]) <= 4
        and all(type(item) is str for item in result["notices"]),
        "response_invalid",
    )
    _context(value["context_used"])
    search = result["search"]
    if search is not None:
        _require(type(search) is dict, "response_invalid")
        _require(
            search.get("revision") == revision
            and search.get("catalog_manifest_sha256") == manifest,
            "identity_changed",
        )
        _require(
            type(search.get("cards")) is list
            and len(search["cards"]) <= 5
            and all(type(card) is dict for card in search["cards"])
            and _timings(search.get("timing_ms"))
            and search.get("context_used") == value["context_used"],
            "response_invalid",
        )
    # A malformed reviewed service must not turn a session secret into metadata.
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    _require(token not in encoded, "response_invalid")
    # At most eight compact UTF-8 responses plus fixed per-turn report fields
    # stay below 3 MiB, including after JSON number/string normalization.
    _require(len(encoded.encode("utf-8")) <= MAX_RESPONSE_BYTES, "response_limit")


def conversation(
    endpoint, queries, revision, manifest_sha256, *, checkpoint=None, deadline=None
):
    """Run one fresh session, preserving ordered BFF responses and partial work.

    ``endpoint`` is a previously authenticated RFC1918 IPv4, never a URL.
    ``checkpoint()`` runs before every turn and must enforce its own IO budget;
    its time counts toward this case and the turn's wall_seconds. request_seconds
    measures only the turn HTTP request, not the checkpoint or backend timings.
    The optional deadline is absolute monotonic time. Five seconds reserve reset.
    Ordinary failure and KeyboardInterrupt return a closed failed report.
    """
    report = {"status": "failed", "turns": [], "error": None, "session_reset": False}
    token = None
    case_end = None
    try:
        try:
            address = ipaddress.IPv4Address(endpoint)
        except (ValueError, TypeError):
            raise _Failure("shared_batch_endpoint_invalid") from None
        _require(
            type(endpoint) is str
            and str(address) == endpoint
            and any(address in network for network in PRIVATE_NETWORKS),
            "endpoint_invalid",
        )
        _require(
            type(queries) is list
            and 1 <= len(queries) <= MAX_TURNS
            and (checkpoint is None or callable(checkpoint)),
            "input_invalid",
        )
        try:
            queries = [checked_query(text) for text in queries]
        except (ValueError, TypeError):
            raise _Failure("shared_batch_input_invalid") from None
        _require(
            type(revision) is str
            and HEX40.fullmatch(revision)
            and type(manifest_sha256) is str
            and HEX64.fullmatch(manifest_sha256),
            "input_invalid",
        )
        _require(
            deadline is None
            or type(deadline) in (int, float)
            and math.isfinite(deadline),
            "input_invalid",
        )
        case_end = time.monotonic() + len(queries) * 130
        if deadline is not None:
            case_end = min(case_end, deadline)
        work_end = case_end - 5
        _remaining(work_end)
        session = _request(
            endpoint,
            "POST",
            "/api/session",
            payload={},
            deadline=min(work_end, time.monotonic() + 5),
        )
        # Retain a valid token before the remaining identity checks, so an
        # unexpected revision also gets a reset without sending a query.
        _require(
            type(session.get("token")) is str and TOKEN.fullmatch(session["token"]),
            "session_invalid",
        )
        token = session["token"]
        _require(
            set(session) == {"token", "version", "expires_in", "max_turns", "revision"}
            and type(session["version"]) is int
            and session["version"] == 0
            and type(session["expires_in"]) is int
            and 1 <= session["expires_in"] <= 3600
            and type(session["max_turns"]) is int
            and len(queries) <= session["max_turns"] <= 20,
            "session_invalid",
        )
        _require(session["revision"] == revision, "identity_changed")
        for index, text in enumerate(queries):
            started = time.monotonic()
            turn = {"status": "failed", "wall_seconds": 0.0, "request_seconds": 0.0}
            report["turns"].append(turn)
            try:
                turn_end = min(work_end, started + 125)
                _remaining(turn_end)
                if checkpoint is not None:
                    try:
                        _require(checkpoint() is not False, "checkpoint_failed")
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        raise _Failure("shared_batch_checkpoint_failed") from None
                _remaining(turn_end)
                requested = time.monotonic()
                try:
                    value = _request(
                        endpoint,
                        "POST",
                        "/api/turn",
                        token=token,
                        deadline=turn_end,
                        payload={
                            "query": text,
                            "version": index,
                            "top_k": 5,
                            "timezone": "Asia/Taipei",
                        },
                    )
                finally:
                    turn["request_seconds"] = round(
                        max(0.0, time.monotonic() - requested), 3
                    )
                _turn(value, index + 1, revision, manifest_sha256, token)
                turn.update(status="ok", response=value)
            except _Failure as error:
                turn["error"] = str(error)
                raise
            except KeyboardInterrupt:
                turn["error"] = "shared_batch_interrupted"
                raise
            except Exception:
                turn["error"] = "shared_batch_response_invalid"
                raise _Failure("shared_batch_response_invalid") from None
            finally:
                turn["wall_seconds"] = round(max(0.0, time.monotonic() - started), 3)
        report["status"] = "complete"
    except _Failure as error:
        report["error"] = str(error)
    except KeyboardInterrupt:
        report["error"] = "shared_batch_interrupted"
    except Exception:
        report["error"] = "shared_batch_failed"
    finally:
        if token is not None:
            try:
                value = _request(
                    endpoint,
                    "DELETE",
                    "/api/session",
                    token=token,
                    deadline=min(case_end, time.monotonic() + 5),
                )
                _require(value == {"status": "cleared"}, "reset_failed")
                report["session_reset"] = True
            except (Exception, KeyboardInterrupt):
                report["error"] = "shared_batch_reset_failed"
        token = None
        if report["error"] is not None:
            report["status"] = "failed"
    return report
