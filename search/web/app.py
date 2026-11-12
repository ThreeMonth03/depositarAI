"""Same-origin website API. Browser tokens are not GPU service credentials."""

import asyncio
from contextlib import asynccontextmanager

from django.urls import path
from pydantic import Field, ValidationError, field_validator
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.response import Response

from search.api.shared.admission import Busy, Slots
from search.http.application import application
from search.http.body import bounded_body, replay_body
from search.http.responses import json_response
from search.http.views import APIView
from search.serialization import strict_json
from search.api.shared.schemas import StrictModel
from search.planning.clock import normalize_context
from search.planning.contract import checked_query
from .identity import EdgeIdentity
from .sessions import SessionError, Sessions
from search.trace.contract import CaptureChoice, WithdrawChoice
from search.trace.worker import TraceSink
from .trace import SearchTrace, utc_now
from .turn import admitted_turn
from .upstream import Upstream, WebSettings


class UserTurn(StrictModel):
    query: str = Field(min_length=1, max_length=512)
    top_k: int = Field(default=5, ge=1, le=20)
    timezone: str = Field(default="Asia/Taipei", max_length=64)
    version: int = Field(ge=0, le=20)

    @field_validator("query")
    @classmethod
    def valid_query(cls, value):
        return checked_query(value)


def problem(status, code):
    return Response(
        {"error": {"code": code}},
        status=status,
        headers={"Cache-Control": "no-store", "Retry-After": "2"},
    )


def _headers(raw):
    headers = {}
    duplicate = False
    for name, value in raw:
        if name in headers and name in {
            b"origin",
            b"host",
            b"authorization",
            b"content-length",
            b"content-type",
        }:
            duplicate = True
        headers[name] = value.decode("latin-1")
    return headers, duplicate


def _post_status(headers):
    length = headers.get(b"content-length", "")
    if (
        headers.get(b"content-type", "").split(";", 1)[0] != "application/json"
        or b"content-encoding" in headers
    ):
        return 415, "json_required"
    if not length.isascii() or not length.isdecimal() or len(length) > 5:
        return 411, "length_required"
    if int(length) > 4096:
        return 413, "body_limit"


def _empty_status(headers):
    length = headers.get(b"content-length", "")
    if b"content-length" in headers and (
        not length.isascii()
        or not length.isdecimal()
        or len(length) > 5
        or int(length) != 0
    ):
        return 400, "invalid_request"


class WebBoundary:
    def __init__(self, app, origin, identity):
        self.app, self.origin, self.identity = app, origin, identity

    def _admit(self, scope, headers, duplicate):
        if scope["path"].startswith("/api/"):
            if duplicate or scope.get("query_string"):
                return 400, "invalid_request"
            # Same-origin browser GET requests may omit Origin. Only this
            # read-only metadata checks accept its absence; no CORS wildcard.
            access_check = scope["method"] == "GET" and scope["path"] in {
                "/api/access",
                "/api/session",
            }
            if headers.get(b"origin") != self.origin and not (
                access_check and b"origin" not in headers
            ):
                return 403, "origin_denied"
            try:
                access = self.identity.authenticate(scope["headers"])
            except SessionError as error:
                return error.status, error.code
            state = scope.setdefault("state", {})
            state["access"] = access
            state["owner"] = access.principal.owner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers, duplicate = _headers(scope["headers"])

        async def reject(status, code):
            await json_response(
                {"error": {"code": code}},
                status=status,
                headers={"Cache-Control": "no-store", "Retry-After": "2"},
            )(scope, receive, send)

        rejection = self._admit(scope, headers, duplicate)
        if rejection is not None:
            return await reject(*rejection)
        has_body = scope["method"] in {"POST", "PATCH"}
        length = headers.get(b"content-length", "")
        rejection = (
            _post_status(headers) if has_body else _empty_status(headers)
        )
        if rejection is not None:
            return await reject(*rejection)

        try:
            body = await bounded_body(
                receive, limit=4096 if has_body else 0, timeout=5
            )
            if has_body:
                if len(body) != int(length):
                    raise ValueError("length mismatch")
                strict_json(body)
        except (ValueError, UnicodeError, RecursionError, TimeoutError):
            return await reject(400, "invalid_request")
        await self._forward(scope, receive, send, body)

    async def _forward(self, scope, receive, send, body):
        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] += [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (
                        b"content-security-policy",
                        (
                            b"default-src 'self'; script-src 'self'; "
                            b"style-src 'self' 'unsafe-inline'; img-src "
                            b"'self'; connect-src 'self'; object-src 'none'; "
                            b"base-uri 'none'; frame-ancestors 'none'; "
                            b"form-action 'self'"
                        ),
                    ),
                    (
                        b"permissions-policy",
                        b"geolocation=(), camera=(), microphone=()",
                    ),
                ]
            await send(message)

        await self.app(scope, replay_body(body, receive), secure_send)


class WebsiteView(APIView):
    def handle_exception(self, error):
        if isinstance(error, SessionError):
            return problem(error.status, error.code)
        if isinstance(error, ValidationError):
            return problem(422, "invalid_request")
        if isinstance(error, MethodNotAllowed):
            return Response({"detail": "Method Not Allowed"}, status=405)
        return problem(503, "unavailable")


def _token(request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or len(auth) != 50:
        raise SessionError(401, "session_required")
    return auth[7:]


async def _turn(payload, request, upstream, sessions, slots, trace):
    context = {"reference_time": utc_now(), "timezone": payload.timezone}
    try:
        normalize_context(context)
    except ValueError:
        return problem(422, "invalid_timezone")
    try:
        async with slots.claim():
            session = sessions.begin(
                _token(request),
                payload.version,
                request.scope["state"]["owner"],
            )
            return await admitted_turn(
                payload, session, upstream, trace, context, problem
            )
    except Busy:
        return problem(429, "busy")


def _lifecycle(sessions, sink, limits):
    @asynccontextmanager
    async def lifespan(_app):
        async def reap():
            while True:
                await asyncio.sleep(30)
                sessions.reap()

        try:
            sink.start()
        except Exception:
            pass
        task = asyncio.create_task(reap())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            sessions.items.clear()
            try:
                sink.close(limits.shutdown_seconds)
            except Exception:
                pass

    return lifespan


def _session_view(sessions, trace, settings):
    class SessionView(WebsiteView):
        http_method_names = ["get", "post", "patch", "delete"]

        async def get(self, request):
            session = sessions.get(
                _token(request), request.scope["state"]["owner"]
            )
            return Response({"trace": trace.status(session)})

        async def post(self, request):
            choice = CaptureChoice.model_validate(request.data)
            if choice.capture and settings.trace_root is None:
                return problem(503, "capture_unavailable")
            token = sessions.create(
                request.scope["state"]["owner"],
                capture=choice.capture,
                consented_at=utc_now(),
            )
            return Response(
                {
                    "token": token,
                    "version": 0,
                    "expires_in": sessions.ttl,
                    "max_turns": sessions.max_turns,
                    "revision": settings.revision,
                    "trace": trace.status(
                        sessions.get(token, request.scope["state"]["owner"])
                    ),
                }
            )

        async def patch(self, request):
            WithdrawChoice.model_validate(request.data)
            session = sessions.get(
                _token(request), request.scope["state"]["owner"]
            )
            return Response({"trace": trace.withdraw(session)})

        async def delete(self, request):
            token, owner = _token(request), request.scope["state"]["owner"]
            session = sessions.items.get(token)
            value = None
            if session is not None and session.owner == owner:
                value = trace.withdraw(session)
            sessions.delete(token, owner)
            return Response({"status": "cleared", "trace": value})

    return SessionView


def create_app(settings=None, upstream=None, sessions=None, trace_sink=None):
    settings = settings or WebSettings.from_environment()
    identity = EdgeIdentity(settings.edge_secret_file)
    upstream, sessions = upstream or Upstream(settings), sessions or Sessions()
    slots = Slots(1)
    sink = trace_sink or TraceSink(settings.trace_root, settings.trace_limits)
    trace = SearchTrace(settings, sink)

    class HealthView(WebsiteView):
        http_method_names = ["get"]

        async def get(self, request):
            return Response({"status": "ok"})

    class AccessView(WebsiteView):
        http_method_names = ["get"]

        async def get(self, request):
            return Response(
                {
                    **request.scope["state"]["access"].payload(),
                    "trace_policy": trace.policy(),
                }
            )

    class TurnView(WebsiteView):
        http_method_names = ["post"]

        async def post(self, request):
            return await _turn(
                UserTurn.model_validate(request.data),
                request,
                upstream,
                sessions,
                slots,
                trace,
            )

    return WebBoundary(
        application(
            [
                path("healthz", HealthView.as_view()),
                path("api/access", AccessView.as_view()),
                path(
                    "api/session",
                    _session_view(sessions, trace, settings).as_view(),
                ),
                path("api/turn", TurnView.as_view()),
            ],
            lifespan=_lifecycle(sessions, sink, settings.trace_limits),
        ),
        settings.origin,
        identity,
    )
