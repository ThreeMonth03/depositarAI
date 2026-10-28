"""Bounded private release HTTP acceptance from existing container namespaces.

Only fixed probe code and public source/catalog identities enter Docker argv.
The API credential and RAM session tokens stay inside their respective process;
receipts contain no requests, generated text, cards, headers or HTTP error body.
Successful smoke checks establish one search/refinement/reset path, not quality.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import time

from search.model_policy import (
    PLANNER_MODEL,
    PLANNER_MODEL_DIGEST,
    SCOPE_MODEL,
    SCOPE_MODEL_DIGEST,
    SCOPE_NUM_CTX,
)
from search.planning.model import OPTIONS as PLANNER_OPTIONS


MAX_OUTPUT_BYTES = 4096
WARMUP_SECONDS = 305
API_SECONDS = 100
WEB_SECONDS = 285
SERVICES = frozenset({"web", "conversation-api", "filtered-embedding"})


class ReleaseProbeError(ValueError):
    """Fixed diagnostics without captured subprocess or HTTP output."""


_WARMUP_SCRIPT = r"""
import json
import math
import sys
import time

from search.model_policy import (
    PLANNER_MODEL, PLANNER_MODEL_DIGEST, SCOPE_MODEL, SCOPE_MODEL_DIGEST, SCOPE_NUM_CTX,
)
from search.operations.shared.runtime_probe import probe_models
from search.planning.model import OPTIONS as PLANNER_OPTIONS

REVISION, BUNDLE_ID, MANIFEST = sys.argv[1:4]
started = time.monotonic()
report = {
    "schema_version": 1,
    "revision": REVISION,
    "bundle_id": BUNDLE_ID,
    "manifest_sha256": MANIFEST,
    "model_warmup_verified": False,
    "full_gpu_residency_verified": False,
    "gpu_uuid_verification": "external_required",
    "keep_alive": -1,
    "models": [],
    "elapsed_ms": 0,
    "code": "warmup_failed",
}

try:
    value = probe_models(budget_seconds=300)
    fields = {
        "role", "model", "digest", "status", "code", "resident_before",
        "num_ctx", "size_bytes", "vram_bytes", "processor", "load_ms",
        "prompt_ms", "decode_ms", "prompt_eval_count", "eval_count", "elapsed_ms",
    }
    if (
        type(value) is not dict
        or set(value) != {
            "schema_version", "status", "keep_alive", "gpu_uuid_verification",
            "search_quality_verified", "models", "total_ms",
        }
        or type(value["schema_version"]) is not int or value["schema_version"] != 1
        or value["status"] != "ready"
        or type(value["keep_alive"]) is not int or value["keep_alive"] != -1
        or value["gpu_uuid_verification"] != "external_required"
        or value["search_quality_verified"] is not False
        or type(value["models"]) is not list or len(value["models"]) != 2
        or any(
            type(row) is not dict or set(row) != fields
            or row["status"] != "ready" or row["code"] != "ready"
            or row["processor"] != "GPU"
            or type(row["resident_before"]) is not bool
            for row in value["models"]
        )
    ):
        raise ValueError("warmup_contract")
    for row, expected in zip(value["models"], (
        ("scope", SCOPE_MODEL, SCOPE_MODEL_DIGEST, SCOPE_NUM_CTX),
        ("planner", PLANNER_MODEL, PLANNER_MODEL_DIGEST, PLANNER_OPTIONS["num_ctx"]),
    )):
        if (
            (row["role"], row["model"], row["digest"], row["num_ctx"]) != expected
            or type(row["num_ctx"]) is not int
            or any(
                type(row[key]) is not int or not 0 < row[key] <= 1024**4
                for key in ("size_bytes", "vram_bytes")
            )
            or row["size_bytes"] != row["vram_bytes"]
            or any(
                type(row[key]) not in {int, float} or not math.isfinite(row[key])
                or not 0 <= row[key] <= 300000
                for key in ("load_ms", "elapsed_ms")
            )
        ):
            raise ValueError("warmup_identity")
    report["models"] = [
        {key: row[key] for key in (
            "role", "model", "digest", "num_ctx", "size_bytes", "vram_bytes",
            "processor", "load_ms", "elapsed_ms",
        )}
        for row in value["models"]
    ]
    report.update(
        model_warmup_verified=True, full_gpu_residency_verified=True, code="ready",
    )
except Exception:
    pass
report["elapsed_ms"] = round(max(0, time.monotonic() - started) * 1000, 3)
print(json.dumps(report, sort_keys=True, allow_nan=False))
"""


_HTTP_SCRIPT = r"""
def remaining(cap):
    now = time.monotonic()
    left = DEADLINE - now
    if not math.isfinite(now) or now < started or left <= 0:
        raise ValueError("deadline")
    return min(cap, left)

def request(method, path, *, payload=None, token=None, expected=200, timeout=5):
    headers = [("Accept", "application/json")]
    if ORIGIN is not None:
        headers.append(("Origin", ORIGIN))
    if token is not None:
        headers.append(("Authorization", "Bearer " + token))
    body = None
    if payload is not None:
        headers.append(("Content-Type", "application/json"))
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    url = BASE_URL + path
    response = transport.send(
        HTTPRequest(method, url, tuple(headers), body),
        timeout=remaining(timeout), max_bytes=MAX_RESPONSE_BYTES,
    )
    remaining(timeout)
    if (
        not isinstance(response, HTTPResponse)
        or type(response.status) is not int
        or response.status != expected
        or response.url != url
        or type(response.body) is not bytes
        or len(response.body) > MAX_RESPONSE_BYTES
    ):
        raise ValueError("http_contract")
    if path == "/":
        return response.body
    return strict_json(response.body)

def emit(report):
    print(json.dumps(report, sort_keys=True, allow_nan=False))
"""

_COMMON_SCRIPT = (
    r"""
import json
import math
import sys
import time

from search.serialization import strict_json
from search.transport.http import HTTPRequest, HTTPResponse, UrllibTransport

REVISION, BUNDLE_ID, MANIFEST = sys.argv[1:4]
MAX_RESPONSE_BYTES = 256 * 1024
transport = UrllibTransport()
started = time.monotonic()

BASE_URL = "http://127.0.0.1:8000"
"""
    + _HTTP_SCRIPT
)

_API_SCRIPT = (
    _COMMON_SCRIPT
    + r"""
from search.api.shared.config import Settings
from search.transport.version import CONVERSATION_CONTRACT_VERSION
from search.secrets import read_secret
from search.catalog.bundle import CatalogBundle

DEADLINE = started + 95
ORIGIN = None
report = {
    "schema_version": 1,
    "revision": REVISION,
    "bundle_id": BUNDLE_ID,
    "manifest_sha256": MANIFEST,
    "api_version_verified": False,
    "backend_ready": False,
    "pinned_backend_models_verified": False,
    "reranker_ready": False,
    "documents": 0,
    "code": "api_probe_failed",
}

def run():
    settings = Settings.from_environment()
    if (
        settings.revision != REVISION
        or settings.manifest_sha256 != MANIFEST
        or settings.rerank_enabled is not True
    ):
        raise ValueError("settings")
    bundle = CatalogBundle.load(
        settings.bundle_path, expected_manifest_sha256=MANIFEST,
    )
    if bundle.bundle_id != BUNDLE_ID or not 1 <= len(bundle.documents) <= 10000:
        raise ValueError("bundle")
    token = read_secret(settings.key_file)
    version = request("GET", "/v1/version", token=token)
    expected_version = {
        "revision": REVISION,
        "catalog_manifest_sha256": MANIFEST,
        "conversation_contract_version": CONVERSATION_CONTRACT_VERSION,
        "geography": False,
        "persistent_memory": False,
        "generated_answers": False,
        "filter_scope": "index_prefilter",
    }
    if (
        type(version) is not dict or set(version) != set(expected_version)
        or version != expected_version
        or type(version["conversation_contract_version"]) is not int
        or any(version[key] is not False for key in (
            "geography", "persistent_memory", "generated_answers",
        ))
    ):
        raise ValueError("version")
    report["api_version_verified"] = True
    for attempt in range(3):
        try:
            value = request("GET", "/readyz", token=token, timeout=30)
            if (
                type(value) is not dict
                or set(value) != {"ready", "documents", "reranker_ready"}
                or value["ready"] is not True
                or value["reranker_ready"] is not True
                or type(value["documents"]) is not int
                or value["documents"] != len(bundle.documents)
            ):
                raise ValueError("readiness")
            report.update(
                backend_ready=True,
                pinned_backend_models_verified=True,
                reranker_ready=True,
                documents=value["documents"],
                code="ready",
            )
            return
        except (OSError, ValueError, TypeError, KeyError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(remaining(1))

try:
    run()
except Exception:
    pass
emit(report)
"""
)

_STATIC_ACCEPTANCE_SCRIPT = r"""
def check_assets():
    # Gateways serve the React build; direct BFF acceptance skips this hook.
    page = request("GET", "/")
    if b'<div id="root"></div>' not in page or b"<title>depositar AI</title>" not in page:
        raise ValueError("website_assets")
"""

_WEB_ACCEPTANCE_SCRIPT = r"""
import re
from urllib.parse import urlsplit
from search.api.conversation.contract import TurnResponse
from search.api.retrieval.schemas import ContextUsed

report = {
    "schema_version": 1,
    "revision": REVISION,
    "bundle_id": BUNDLE_ID,
    "manifest_sha256": MANIFEST,
    "website_ready": False,
    "two_turn_search_verified": False,
    "session_reset_verified": False,
    "search_quality_verified": False,
    "turns": [],
    "code": "web_probe_failed",
}

def session():
    value = request("POST", "/api/session", payload={})
    if (
        type(value) is not dict
        or set(value) != {"token", "version", "expires_in", "max_turns", "revision"}
        or type(value["token"]) is not str
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", value["token"]) is None
        or type(value["version"]) is not int or value["version"] != 0
        or type(value["expires_in"]) is not int or not 1 <= value["expires_in"] <= 3600
        or type(value["max_turns"]) is not int or not 2 <= value["max_turns"] <= 20
        or value["revision"] != REVISION
    ):
        raise ValueError("session")
    return value["token"]

def turn(token, query, version):
    tick = time.monotonic()
    value = request(
        "POST", "/api/turn", token=token,
        payload={"query": query, "version": version, "top_k": 3, "timezone": "Asia/Taipei"},
        timeout=130,
    )
    if (
        type(value) is not dict or set(value) != {"version", "result", "context_used"}
        or type(value["version"]) is not int or value["version"] != version + 1
    ):
        raise ValueError("turn")
    context = ContextUsed.model_validate(value["context_used"])
    result = TurnResponse.model_validate(value["result"])
    search = result.search
    if (
        context.timezone != "Asia/Taipei"
        or result.status != "ok" or result.reason != "none"
        or result.revision != REVISION or not result.state.topic
        or search is None or search.status != "ok" or search.revision != REVISION
        or search.catalog_manifest_sha256 != MANIFEST or search.mode != "ai"
        or search.decision != "search"
        or search.filter_scope != ("none" if version == 0 else "index_prefilter")
        or search.ranking != "nvidia-rerank" or search.rerank_status != "applied"
        or search.context_used != context
        or not 1 <= len(search.cards) <= 3
        or not len(search.cards) <= search.candidate_count <= 20
        or any(card.rank != index for index, card in enumerate(search.cards, 1))
        or len({card.dataset_id for card in search.cards}) != len(search.cards)
        or (search.applied_conditions is None) != (version == 0)
        or search.parsed_conditions is None
        or any(not math.isfinite(n) or n < 0 for n in result.timing_ms.values())
        or any(not math.isfinite(n) or n < 0 for n in search.timing_ms.values())
    ):
        raise ValueError("search_contract")
    if any(
        re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", card.dataset_id) is None
        or urlsplit(card.url).scheme != "https"
        or urlsplit(card.url).netloc != "data.depositar.io"
        or not urlsplit(card.url).path.startswith("/dataset/")
        or not urlsplit(card.url).path.removeprefix("/dataset/")
        or urlsplit(card.url).query or urlsplit(card.url).fragment
        for card in search.cards
    ):
        raise ValueError("card_identity")
    report["turns"].append({
        "version": version + 1,
        "cards": len(search.cards),
        "elapsed_ms": round((time.monotonic() - tick) * 1000, 3),
    })
    return result

def run():
    check_website()
    if request("GET", "/healthz") != {"status": "ok"}:
        raise ValueError("website_health")
    check_assets()
    report["website_ready"] = True
    token = session()
    try:
        first = turn(token, "尋找公開的降雨資料集", 0)
        second = turn(token, "CSV", 1)
        if (
            first.state.formats != [] or first.state.time is not None
            or first.state.unapplied_time is not None
            or second.state.topic != first.state.topic
            or second.state.time != first.state.time
            or second.state.unapplied_time != first.state.unapplied_time
            or second.state.formats != ["CSV"]
            or second.search.applied_conditions.formats != ["CSV"]
            or any(card.condition_status != "match" for card in second.search.cards)
        ):
            raise ValueError("refinement")
        report["two_turn_search_verified"] = True
        if request("DELETE", "/api/session", token=token) != {"status": "cleared"}:
            raise ValueError("reset")
        # Reject the old token before a new turn can reach GPU inference.
        if request(
            "POST", "/api/turn", token=token, expected=410,
            payload={"query": "尋找公開的降雨資料集", "version": 2},
        ) != {"error": {"code": "session_expired"}}:
            raise ValueError("reset_old_token")
        replacement = session()
        try:
            if replacement == token:
                raise ValueError("session_reuse")
        finally:
            request("DELETE", "/api/session", token=replacement)
    finally:
        # Cleanup is safe after deletion and also on partial search failure.
        if request("DELETE", "/api/session", token=token) != {"status": "cleared"}:
            raise ValueError("cleanup")
    report["session_reset_verified"] = True
    report["code"] = "ready"

try:
    run()
except Exception:
    pass
emit(report)
"""

_WEB_SCRIPT = (
    _COMMON_SCRIPT
    + r"""
from search.web.upstream import WebSettings

DEADLINE = started + 280
ORIGIN = "http://127.0.0.1:18083"

def check_website():
    if WebSettings.from_environment().revision != REVISION:
        raise ValueError("website_revision")
    if WebSettings.from_environment().origin != ORIGIN:
        raise ValueError("website_origin")

def check_assets():
    # The private BFF serves JSON; Nginx owns the separate asset acceptance.
    pass
"""
    + _WEB_ACCEPTANCE_SCRIPT
)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("number")

    if type(raw) is not bytes or len(raw) > MAX_OUTPUT_BYTES:
        raise ReleaseProbeError("release_probe_output_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ReleaseProbeError("release_probe_output_invalid") from None
    if type(value) is not dict:
        raise ReleaseProbeError("release_probe_output_invalid")
    return value


def _identity(value, revision, bundle_id, manifest_sha256):
    return (
        type(value.get("schema_version")) is int
        and value["schema_version"] == 1
        and value.get("revision") == revision
        and value.get("bundle_id") == bundle_id
        and value.get("manifest_sha256") == manifest_sha256
    )


def _warmup(value, revision, bundle_id, manifest_sha256):
    fields = {
        "schema_version",
        "revision",
        "bundle_id",
        "manifest_sha256",
        "model_warmup_verified",
        "full_gpu_residency_verified",
        "gpu_uuid_verification",
        "keep_alive",
        "models",
        "elapsed_ms",
        "code",
    }
    if (
        set(value) != fields
        or not _identity(value, revision, bundle_id, manifest_sha256)
        or type(value["model_warmup_verified"]) is not bool
        or type(value["full_gpu_residency_verified"]) is not bool
        or value["gpu_uuid_verification"] != "external_required"
        or type(value["keep_alive"]) is not int
        or value["keep_alive"] != -1
        or type(value["models"]) is not list
        or type(value["elapsed_ms"]) not in {int, float}
        or not math.isfinite(value["elapsed_ms"])
        or not 0 <= value["elapsed_ms"] <= WARMUP_SECONDS * 1000
        or value["code"] not in {"ready", "warmup_failed"}
        or value["model_warmup_verified"] != value["full_gpu_residency_verified"]
        or (value["code"] == "ready") != value["model_warmup_verified"]
        or len(value["models"]) != (2 if value["model_warmup_verified"] else 0)
    ):
        raise ReleaseProbeError("release_probe_warmup_contract_invalid")
    expected_models = (
        ("scope", SCOPE_MODEL, SCOPE_MODEL_DIGEST, SCOPE_NUM_CTX),
        ("planner", PLANNER_MODEL, PLANNER_MODEL_DIGEST, PLANNER_OPTIONS["num_ctx"]),
    )
    for row, expected in zip(value["models"], expected_models):
        if (
            type(row) is not dict
            or set(row)
            != {
                "role",
                "model",
                "digest",
                "num_ctx",
                "size_bytes",
                "vram_bytes",
                "processor",
                "load_ms",
                "elapsed_ms",
            }
            or (row["role"], row["model"], row["digest"], row["num_ctx"]) != expected
            or type(row["num_ctx"]) is not int
            or row["processor"] != "GPU"
            or any(
                type(row[key]) is not int or not 0 < row[key] <= 1024**4
                for key in ("size_bytes", "vram_bytes")
            )
            or row["size_bytes"] != row["vram_bytes"]
            or any(
                type(row[key]) not in {int, float}
                or not math.isfinite(row[key])
                or not 0 <= row[key] <= 300000
                for key in ("load_ms", "elapsed_ms")
            )
        ):
            raise ReleaseProbeError("release_probe_warmup_contract_invalid")
    return value


def _api(value, revision, bundle_id, manifest_sha256):
    flags = {
        "api_version_verified",
        "backend_ready",
        "pinned_backend_models_verified",
        "reranker_ready",
    }
    if (
        set(value)
        != flags
        | {
            "schema_version",
            "revision",
            "bundle_id",
            "manifest_sha256",
            "documents",
            "code",
        }
        or not _identity(value, revision, bundle_id, manifest_sha256)
        or any(type(value[key]) is not bool for key in flags)
        or type(value["documents"]) is not int
        or not 0 <= value["documents"] <= 10000
        or value["code"] not in {"ready", "api_probe_failed"}
        or (value["code"] == "ready")
        != (all(value[key] for key in flags) and value["documents"] > 0)
        or value["backend_ready"]
        and value["code"] != "ready"
        or not value["backend_ready"]
        and (
            value["documents"] != 0
            or any(value[key] for key in flags - {"api_version_verified"})
        )
    ):
        raise ReleaseProbeError("release_probe_api_contract_invalid")
    return value


def _web(value, revision, bundle_id, manifest_sha256):
    flags = {
        "website_ready",
        "two_turn_search_verified",
        "session_reset_verified",
        "search_quality_verified",
    }
    if (
        set(value)
        != flags
        | {
            "schema_version",
            "revision",
            "bundle_id",
            "manifest_sha256",
            "turns",
            "code",
        }
        or not _identity(value, revision, bundle_id, manifest_sha256)
        or any(type(value[key]) is not bool for key in flags)
        or value["search_quality_verified"] is not False
        or type(value["turns"]) is not list
        or len(value["turns"]) > 2
        or value["code"] not in {"ready", "web_probe_failed"}
    ):
        raise ReleaseProbeError("release_probe_web_contract_invalid")
    for version, row in enumerate(value["turns"], 1):
        if (
            type(row) is not dict
            or set(row) != {"version", "cards", "elapsed_ms"}
            or type(row["version"]) is not int
            or row["version"] != version
            or type(row["cards"]) is not int
            or not 1 <= row["cards"] <= 3
            or type(row["elapsed_ms"]) not in {int, float}
            or not math.isfinite(row["elapsed_ms"])
            or not 0 <= row["elapsed_ms"] <= WEB_SECONDS * 1000
        ):
            raise ReleaseProbeError("release_probe_web_contract_invalid")
    if (
        (value["code"] == "ready")
        != (
            all(value[key] for key in flags - {"search_quality_verified"})
            and len(value["turns"]) == 2
        )
        or value["session_reset_verified"]
        and not value["two_turn_search_verified"]
        or value["two_turn_search_verified"]
        and (not value["website_ready"] or len(value["turns"]) != 2)
    ):
        raise ReleaseProbeError("release_probe_web_contract_invalid")
    return value


def verify_private_release(
    docker,
    container_ids,
    *,
    revision,
    bundle_id,
    manifest_sha256,
    search=True,
    clock=time.monotonic,
):
    """Return closed readiness and optional warmup/two-turn acceptance evidence.

    ``docker.call(arguments, timeout=...)`` must bound output and kill the whole
    command process group on timeout. The host orchestrator independently proves
    the container/image/network policy before and after this probe. No ingress,
    extra network member or arbitrary user prompt is introduced. Search acceptance
    first warms both pinned models once with the existing fixed private probe.
    ``search=False`` checks readiness only and makes no warmup/search claim.
    """
    if (
        type(container_ids) is not dict
        or set(container_ids) != SERVICES
        or any(
            type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in container_ids.values()
        )
        or len(set(container_ids.values())) != 3
        or type(revision) is not str
        or re.fullmatch(r"[0-9a-f]{40}", revision) is None
        or any(
            type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (bundle_id, manifest_sha256)
        )
        or type(search) is not bool
    ):
        raise ReleaseProbeError("release_probe_identity_invalid")
    started = clock()
    if type(started) not in {int, float} or not math.isfinite(started):
        raise ReleaseProbeError("release_probe_clock_invalid")
    previous = started
    total_seconds = API_SECONDS + (WARMUP_SECONDS + WEB_SECONDS if search else 0)

    def run(service, script, timeout):
        nonlocal previous
        try:
            raw = docker.call(
                [
                    "exec",
                    container_ids[service],
                    "python",
                    "-B",
                    "-c",
                    script,
                    revision,
                    bundle_id,
                    manifest_sha256,
                ],
                timeout=timeout,
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            raise ReleaseProbeError("release_probe_command_failed") from None
        now = clock()
        if (
            type(now) not in {int, float}
            or not math.isfinite(now)
            or now < previous
            or now - started > total_seconds
        ):
            raise ReleaseProbeError("release_probe_deadline_exceeded")
        previous = now
        return _json(raw)

    warmup, api = None, None
    if search:
        warmup = _warmup(
            run("conversation-api", _WARMUP_SCRIPT, WARMUP_SECONDS),
            revision,
            bundle_id,
            manifest_sha256,
        )
    if not search or warmup["model_warmup_verified"]:
        api = _api(
            run("conversation-api", _API_SCRIPT, API_SECONDS),
            revision,
            bundle_id,
            manifest_sha256,
        )
    website = None
    if search and api and api["backend_ready"]:
        website = _web(
            run("web", _WEB_SCRIPT, WEB_SECONDS), revision, bundle_id, manifest_sha256
        )
    return {
        "schema_version": 1,
        "revision": revision,
        "bundle_id": bundle_id,
        "manifest_sha256": manifest_sha256,
        "status": "ready"
        if api and api["backend_ready"] and (not search or website["code"] == "ready")
        else "blocked",
        "model_warmup_verified": bool(warmup and warmup["model_warmup_verified"]),
        "readiness_verified": bool(api and api["backend_ready"]),
        "search_requested": search,
        "two_turn_search_verified": bool(
            website and website["two_turn_search_verified"]
        ),
        "session_reset_verified": bool(website and website["session_reset_verified"]),
        "search_quality_verified": False,
        "gateway_verified": False,
        "warmup": warmup,
        "api": api,
        "website": website,
    }
