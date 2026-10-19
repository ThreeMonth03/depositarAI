"""Fixed host-loopback HTTP smoke acceptance; never runtime or route authority.

The future installer must run this in the existing API/runtime dependency
environment, including its pinned Pydantic wire models. Missing dependencies
fail closed; this module never installs packages or reads the host WebSettings.
The caller still verifies the gateway/runtime profile and CAS before and after,
and separately requires private model warmup and backend readiness acceptance.
No readyz/version route, Docker operation, environment change or file write is
used. Only the fixed public search/refinement/reset path is requested.
"""

from __future__ import annotations

import json
import math
import re
import time
from types import SimpleNamespace

from search.serialization import strict_json
from search.transport.http import HTTPRequest, HTTPResponse, UrllibTransport
from search.operations.deployment.release import probe as release_probe


ORIGIN = "http://127.0.0.1:18083"
TOTAL_SECONDS = 280
MAX_RESPONSE_BYTES = 256 * 1024


class GatewayProbeError(ValueError):
    """Fixed errors without upstream text, URLs, tokens or HTTP bodies."""


def verify_gateway_http(
    *, revision, bundle_id, manifest_sha256, transport=None, clock=time.monotonic
):
    """Return informational, sanitized HTTP acceptance for the fixed gateway.

    ``transport`` and ``clock`` are trusted offline-test seams, not endpoint or
    schema overrides. The production transport disables proxies and redirects
    and enforces bounded body reads under each request's remaining time budget.
    The shared private probe alone defines the strict session/turn wire contract.
    """
    if (
        type(revision) is not str
        or re.fullmatch(r"[0-9a-f]{40}", revision) is None
        or any(
            type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (bundle_id, manifest_sha256)
        )
    ):
        raise GatewayProbeError("gateway_probe_identity_invalid")
    previous = None

    def monotonic():
        nonlocal previous
        try:
            value = clock()
            valid = (
                type(value) in {int, float}
                and math.isfinite(value)
                and (previous is None or value >= previous)
            )
        except Exception:
            raise GatewayProbeError("gateway_probe_clock_invalid") from None
        if not valid:
            raise GatewayProbeError("gateway_probe_clock_invalid")
        previous = value
        return value

    started = monotonic()
    website = {
        "schema_version": 1,
        "revision": revision,
        "bundle_id": bundle_id,
        "manifest_sha256": manifest_sha256,
        "website_ready": False,
        "two_turn_search_verified": False,
        "session_reset_verified": False,
        "search_quality_verified": False,
        "turns": [],
        "code": "web_probe_failed",
    }
    code = "gateway_probe_failed"
    namespace = {
        "json": json,
        "math": math,
        "time": SimpleNamespace(monotonic=monotonic),
        "strict_json": strict_json,
        "HTTPRequest": HTTPRequest,
        "HTTPResponse": HTTPResponse,
        "transport": transport,
        "REVISION": revision,
        "BUNDLE_ID": bundle_id,
        "MANIFEST": manifest_sha256,
        "BASE_URL": ORIGIN,
        "ORIGIN": ORIGIN,
        "started": started,
        "DEADLINE": started + TOTAL_SECONDS,
        "MAX_RESPONSE_BYTES": MAX_RESPONSE_BYTES,
        # Host environment cannot prove the container's WebSettings. Session
        # and both typed responses still prove source identity over HTTP;
        # the caller's separate native runtime profile proves configuration.
        "check_website": lambda: None,
    }
    emitted = []
    try:
        namespace["transport"] = UrllibTransport() if transport is None else transport
        # Both strings are fixed repository code, never caller or HTTP input.
        # Override emit after defining the shared request helpers so no stdout
        # capture or raw response publication is needed on the host.
        exec(release_probe._HTTP_SCRIPT, namespace)
        namespace["emit"] = emitted.append
        exec(release_probe._WEB_ACCEPTANCE_SCRIPT, namespace)
        if monotonic() - started > TOTAL_SECONDS or len(emitted) != 1:
            raise GatewayProbeError("gateway_probe_deadline_exceeded")
        website = release_probe._web(
            release_probe._json(
                json.dumps(emitted[0], allow_nan=False).encode("utf-8")
            ),
            revision,
            bundle_id,
            manifest_sha256,
        )
        if website["code"] == "ready":
            code = "ready"
    except ImportError:
        code = "gateway_probe_dependencies_unavailable"
    except Exception:
        # Never serialize exception text, schema errors, HTTP bodies or locals.
        pass
    passed = code == "ready"
    return {
        "schema_version": 1,
        "revision": revision,
        "bundle_id": bundle_id,
        "manifest_sha256": manifest_sha256,
        "status": "ready" if passed else "blocked",
        "code": code,
        "gateway_http_verified": passed,
        "website": website,
        "model_warmup_verified": False,
        "readiness_verified": False,
        "private_release_acceptance_required": True,
        "gateway_runtime_validation_required": True,
        "confers_runtime_authority": False,
        "active_changed": False,
        "search_quality_verified": False,
    }
