"""One-shot pinned-model warmup inside the private inference network.

No Docker socket, credentials, caller-supplied URL, arbitrary prompt or tools.
The probe tests inference and Ollama-reported full GPU residency, not search
quality or physical GPU UUID isolation (which the deployment validator checks).
Production transports must also retain keep_alive=-1; startup warmup alone
cannot prevent an ordinary request from restoring a ten-minute unload timer.
"""

from __future__ import annotations

import dataclasses
import json
import math
import time
import urllib.parse

from search.serialization import strict_json
from search.api.shared.peers import service_address
from search.model_policy import (
    PLANNER_MODEL,
    PLANNER_MODEL_DIGEST,
    OLLAMA_BASE_URL,
    SCOPE_NUM_CTX,
    SCOPE_NUM_PREDICT,
    SCOPE_MODEL,
    SCOPE_MODEL_DIGEST,
)
from search.scope import scope_messages, scope_schema
from search.planning.model import OPTIONS as PLANNER_OPTIONS
from search.transport.http import HTTPRequest, HTTPResponse, UrllibTransport


MAX_RESPONSE_BYTES = 128 * 1024
TOTAL_BUDGET_SECONDS = 300
SCOPE_BUDGET_SECONDS = 120
PLANNER_BUDGET_SECONDS = 180
KEEP_ALIVE = -1


class ProbeTransport:
    """Private role-only diagnostics; do not widen the query-worker allowlist."""

    def __init__(self, role):
        if role not in {"scope", "planner"}:
            raise ValueError("probe_role_invalid")
        host = "scope-model" if role == "scope" else "answer-model"
        self.origin = service_address(host, 11434)
        self.transport = UrllibTransport()

    def send(self, request, *, timeout, max_bytes):
        parsed = urllib.parse.urlsplit(request.url)
        if (
            request.url != OLLAMA_BASE_URL + parsed.path
            or parsed.query
            or parsed.fragment
            or (request.method, parsed.path)
            not in {("GET", "/api/tags"), ("GET", "/api/ps"), ("POST", "/api/chat")}
        ):
            raise ValueError("probe_operation_invalid")
        upstream = dataclasses.replace(request, url=self.origin + parsed.path)
        response = self.transport.send(upstream, timeout=timeout, max_bytes=max_bytes)
        if response.url != upstream.url:
            raise ValueError("probe_redirect_refused")
        return dataclasses.replace(response, url=request.url)


def _payload(role):
    if role == "scope":
        return {
            "model": SCOPE_MODEL,
            "messages": scope_messages("尋找公開的降雨資料集"),
            "format": scope_schema(),
            "options": {
                "num_ctx": SCOPE_NUM_CTX,
                "num_predict": SCOPE_NUM_PREDICT,
                "temperature": 0,
                "seed": 42,
            },
            "think": False,
            "stream": False,
            "truncate": False,
            "shift": False,
            "keep_alive": KEEP_ALIVE,
        }
    return {
        "model": PLANNER_MODEL,
        "messages": [
            {
                "role": "system",
                "content": 'Return exactly {"ready":true}. No tools or commentary.',
            },
            {"role": "user", "content": "Confirm that inference is ready."},
        ],
        "format": {
            "type": "object",
            "properties": {"ready": {"type": "boolean", "const": True}},
            "required": ["ready"],
            "additionalProperties": False,
        },
        "options": dict(PLANNER_OPTIONS),
        "think": "low",
        "stream": False,
        "truncate": False,
        "shift": False,
        "keep_alive": KEEP_ALIVE,
    }


def _rows(value):
    if (
        type(value) is not dict
        or type(value.get("models")) is not list
        or len(value["models"]) > 128
    ):
        raise ValueError("probe_inventory_invalid")
    if any(type(row) is not dict for row in value["models"]):
        raise ValueError("probe_inventory_invalid")
    return value["models"]


def _identity(value, model, digest):
    matches = [
        row.get("digest")
        for row in _rows(value)
        if model in (row.get("name"), row.get("model"))
    ]
    if matches != [digest]:
        raise ValueError("probe_model_identity")


def _resident(value, model, digest):
    rows = [row for row in _rows(value) if model in (row.get("name"), row.get("model"))]
    if not rows:
        return None
    if len(rows) != 1 or rows[0].get("digest") != digest:
        raise ValueError("probe_resident_identity")
    row = rows[0]
    for field in ("size", "size_vram", "context_length"):
        if type(row.get(field)) is not int or not 0 <= row[field] <= 1024**4:
            raise ValueError("probe_resident_metrics")
    if row["size"] <= 0 or row["context_length"] <= 0:
        raise ValueError("probe_resident_metrics")
    return row


def probe_models(
    *, transports=None, clock=time.monotonic, budget_seconds=TOTAL_BUDGET_SECONDS
):
    """Warm scope and planner once, return bounded sanitized evidence.

    The caller runs this privately before opening ingress. This is deliberately
    not an HTTP endpoint or a periodic request-time warmup, and never retries a
    failed warmup automatically. Total elapsed time is bounded by the injected
    transports' remaining deadline as well as a final monotonic deadline check.
    """
    if (
        type(budget_seconds) not in {int, float}
        or not math.isfinite(budget_seconds)
        or not 0 < budget_seconds <= 600
    ):
        raise ValueError("probe_budget_invalid")
    started = clock()
    if not math.isfinite(started):
        raise ValueError("probe_clock_invalid")
    report = {
        "schema_version": 1,
        "status": "blocked",
        "keep_alive": KEEP_ALIVE,
        "gpu_uuid_verification": "external_required",
        "search_quality_verified": False,
        "models": [],
    }
    for role, model, digest, role_budget in (
        ("scope", SCOPE_MODEL, SCOPE_MODEL_DIGEST, SCOPE_BUDGET_SECONDS),
        ("planner", PLANNER_MODEL, PLANNER_MODEL_DIGEST, PLANNER_BUDGET_SECONDS),
    ):
        row = {
            "role": role,
            "model": model,
            "digest": digest,
            "status": "blocked",
            "code": "warmup_failed",
        }
        report["models"].append(row)
        tick = clock()
        deadline = min(started + budget_seconds, tick + role_budget)

        def remaining(cap):
            now = clock()
            left = deadline - now
            if not math.isfinite(now) or now < tick or left <= 0:
                raise ValueError("probe_deadline_exceeded")
            return min(cap, left)

        try:
            transport = ProbeTransport(role) if transports is None else transports[role]

            def request(method, path, *, payload=None, timeout=5):
                body = (
                    None
                    if payload is None
                    else json.dumps(
                        payload,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode()
                )
                logical = HTTPRequest(
                    method,
                    OLLAMA_BASE_URL + path,
                    (
                        ("Accept", "application/json"),
                        ("Content-Type", "application/json"),
                    ),
                    body,
                )
                response = transport.send(
                    logical, timeout=remaining(timeout), max_bytes=MAX_RESPONSE_BYTES
                )
                remaining(timeout)
                if (
                    not isinstance(response, HTTPResponse)
                    or response.status != 200
                    or response.url != logical.url
                    or type(response.body) is not bytes
                    or len(response.body) > MAX_RESPONSE_BYTES
                ):
                    raise ValueError("probe_response_invalid")
                return strict_json(response.body)

            _identity(request("GET", "/api/tags"), model, digest)
            before = _resident(request("GET", "/api/ps"), model, digest)
            row["resident_before"] = before is not None
            payload = _payload(role)
            row["num_ctx"] = payload["options"]["num_ctx"]
            raw = request("POST", "/api/chat", payload=payload, timeout=role_budget)
            if (
                type(raw) is not dict
                or raw.get("model") != model
                or raw.get("done") is not True
                or raw.get("done_reason") != "stop"
            ):
                raise ValueError("probe_completion_invalid")
            message = raw.get("message")
            if (
                type(message) is not dict
                or type(message.get("content")) is not str
                or message.get("role") != "assistant"
            ):
                raise ValueError("probe_completion_invalid")
            visible = strict_json(message["content"].encode())
            expected = {"decision": "search"} if role == "scope" else {"ready": True}
            if visible != expected or (
                role == "planner" and type(visible["ready"]) is not bool
            ):
                raise ValueError("probe_visible_invalid")
            for field in (
                "load_duration",
                "prompt_eval_duration",
                "eval_duration",
                "prompt_eval_count",
                "eval_count",
            ):
                if type(raw.get(field)) is not int or not 0 <= raw[field] <= 1024**4:
                    raise ValueError("probe_completion_metrics")
            if (
                raw["prompt_eval_count"] <= 0
                or not 0 < raw["eval_count"] <= payload["options"]["num_predict"]
                or raw["prompt_eval_count"] + payload["options"]["num_predict"]
                > row["num_ctx"]
            ):
                raise ValueError("probe_context_invalid")
            after = _resident(request("GET", "/api/ps"), model, digest)
            if after is None or after["context_length"] != row["num_ctx"]:
                raise ValueError("probe_context_not_resident")
            row["size_bytes"] = after["size"]
            row["vram_bytes"] = after["size_vram"]
            row["processor"] = (
                "GPU"
                if after["size_vram"] == after["size"]
                else "CPU+GPU"
                if after["size_vram"]
                else "CPU"
            )
            if row["processor"] != "GPU":
                row["code"] = "full_gpu_residency_required"
                raise ValueError("probe_gpu_not_full")
            _identity(request("GET", "/api/tags"), model, digest)
            row.update(
                status="ready",
                code="ready",
                load_ms=round(raw["load_duration"] / 1_000_000, 3),
                prompt_ms=round(raw["prompt_eval_duration"] / 1_000_000, 3),
                decode_ms=round(raw["eval_duration"] / 1_000_000, 3),
                prompt_eval_count=raw["prompt_eval_count"],
                eval_count=raw["eval_count"],
            )
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            pass
        row["elapsed_ms"] = round(max(0, clock() - tick) * 1000, 3)
    report["total_ms"] = round(max(0, clock() - started) * 1000, 3)
    if all(row["status"] == "ready" for row in report["models"]):
        report["status"] = "ready"
    return report


def main():
    """Private one-shot entry; output never contains generated text/thinking."""
    try:
        result = probe_models()
    except (OSError, ValueError, RuntimeError):
        result = {"schema_version": 1, "status": "blocked", "code": "warmup_failed"}
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
