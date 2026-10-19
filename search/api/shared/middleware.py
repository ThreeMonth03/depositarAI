"""Authenticate before parsing; cap body bytes and never echo invalid input."""

import hmac
import uuid

from search.http.body import bounded_body
from search.http.responses import json_response
from search.secrets import read_secret
from search.serialization import strict_json


ERRORS = {
    400: ("invalid_request", "Invalid request."),
    401: ("unauthorized", "A valid service API key is required."),
    404: ("not_found", "Endpoint not found."),
    405: ("method_not_allowed", "Method not allowed."),
    408: ("request_timeout", "Request body deadline exceeded."),
    411: ("length_required", "Content-Length is required."),
    413: ("request_too_large", "Request body exceeds the allowed size."),
    415: ("unsupported_media_type", "Use application/json without compression."),
    422: (
        "invalid_request",
        "Request fields do not match the search contract.",
    ),
    429: ("busy", "Service busy. Retry later."),
    500: ("internal_error", "Search could not be completed."),
    503: ("unavailable", "Search is temporarily unavailable."),
}


def error_details(status, request_id):
    code, message = ERRORS.get(status, ERRORS[500])
    headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if status in (429, 503):
        headers["Retry-After"] = "2"
    return (
        {"error": {"code": code, "message": message}, "request_id": request_id},
        headers,
    )


class BoundaryMiddleware:
    def __init__(self, app, key_file, max_body_bytes=8192):
        self.app, self.key_file = app, key_file
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        headers = {}
        duplicate = False
        for key, value in scope["headers"]:
            name = key.decode("latin-1").lower()
            if name in headers and name in {
                "authorization",
                "content-length",
                "content-type",
            }:
                duplicate = True
            headers[name] = value.decode("latin-1")

        async def reject(status):
            data, headers = error_details(status, request_id)
            await json_response(data, status=status, headers=headers)(
                scope, receive, send
            )

        if duplicate or scope.get("query_string"):
            return await reject(400)
        if scope["path"] != "/healthz":
            try:
                key = read_secret(self.key_file)
            except (OSError, ValueError, UnicodeError):
                return await reject(503)
            supplied = headers.get("authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + key).encode()):
                return await reject(401)
        post = scope["method"] == "POST"
        if post:
            if (
                headers.get("content-type", "").split(";", 1)[0].lower()
                != "application/json"
                or "content-encoding" in headers
            ):
                return await reject(415)
            length = headers.get("content-length", "")
            if not length.isascii() or not length.isdecimal() or len(length) > 8:
                return await reject(411)
            if int(length) > self.max_body_bytes:
                return await reject(413)
        else:
            length = headers.get("content-length")
            if (
                "transfer-encoding" in headers
                or length is not None
                and (
                    not length.isascii()
                    or not length.isdecimal()
                    or len(length) > 8
                    or int(length) != 0
                )
            ):
                return await reject(400)
        try:
            body = await bounded_body(receive, limit=self.max_body_bytes if post else 0)
            if post:
                if len(body) != int(length):
                    raise ValueError("wrong body length")
                strict_json(body)
        except TimeoutError:
            return await reject(408)
        except (ValueError, UnicodeError, RecursionError):
            return await reject(400)
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def send_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower()
                    not in {
                        b"x-request-id",
                        b"cache-control",
                        b"x-content-type-options",
                    }
                ] + [
                    (b"x-request-id", request_id.encode()),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ]
            await send(message)

        await self.app(scope, replay, send_headers)
