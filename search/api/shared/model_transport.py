"""Bind model requests to reviewed peers without changing caller deadlines.

Only explicit production residency rewrites keep_alive. Readiness requests keep
their original request bytes; model identity and prompt policy stay in adapters.
"""

import dataclasses
import json
import urllib.parse

from search.model_policy import MAX_REQUEST_BYTES, OLLAMA_BASE_URL
from search.serialization import strict_json
from search.transport.http import HTTPResponse, UrllibTransport
from .peers import service_address


class ModelTransport:
    """Map the reviewed logical loopback contract onto one fixed Docker peer."""

    def __init__(self, role, *, keep_alive=None):
        if role not in {"scope", "answer"}:
            raise ValueError("invalid model role")
        if keep_alive is not None and (type(keep_alive) is not int or keep_alive != -1):
            raise ValueError("invalid model residency policy")
        # Readiness transports retain their original request bytes.
        # Only an explicit production factory opts into dedicated residency.
        self.keep_alive = keep_alive
        self.origin = service_address(role + "-model", 11434)
        self.transport = UrllibTransport()

    def send(self, request, *, timeout, max_bytes):
        parsed = urllib.parse.urlsplit(request.url)
        if (
            request.url != OLLAMA_BASE_URL + parsed.path
            or parsed.query
            or parsed.fragment
            or (request.method, parsed.path)
            not in {("GET", "/api/tags"), ("POST", "/api/chat")}
        ):
            raise ValueError("unapproved model operation")
        body = request.body
        if self.keep_alive == -1 and request.method == "POST":
            if not isinstance(body, bytes) or len(body) > MAX_REQUEST_BYTES:
                raise ValueError("invalid model request size")
            payload = strict_json(body)
            if type(payload) is not dict:
                raise ValueError("invalid model request shape")
            payload["keep_alive"] = -1
            body = json.dumps(
                payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
            if len(body) > MAX_REQUEST_BYTES:
                raise ValueError("invalid model request size")
        upstream = dataclasses.replace(
            request, url=self.origin + parsed.path, body=body
        )
        result = self.transport.send(upstream, timeout=timeout, max_bytes=max_bytes)
        if result.url != upstream.url:
            raise ValueError("model redirect refused")
        return HTTPResponse(result.status, result.headers, result.body, request.url)
