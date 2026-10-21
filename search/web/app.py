"""Same-origin website API. Browser tokens are not GPU service credentials."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from django.urls import path
from pydantic import Field, ValidationError, field_validator
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.response import Response

from search.api.shared.admission import Busy, Slots
from search.http.application import application
from search.http.body import bounded_body
from search.http.responses import json_response
from search.http.views import APIView
from search.serialization import strict_json
from search.api.conversation.contract import TurnRequest
from search.api.shared.schemas import StrictModel
from search.planning.clock import normalize_context
from search.planning.contract import checked_query
from .identity import EdgeIdentity
from .sessions import SessionError, Sessions
from .upstream import Upstream, UpstreamError, WebSettings


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


class WebBoundary:
    def __init__(self, app, origin, identity):
        self.app, self.origin, self.identity = app, origin, identity

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {}
        duplicate = False
        for name, value in scope["headers"]:
            if name in headers and name in {
                b"origin",
                b"host",
                b"authorization",
                b"content-length",
                b"content-type",
            }:
                duplicate = True
            headers[name] = value.decode("latin-1")

        async def reject(status, code):
            await json_response(
                {"error": {"code": code}},
                status=status,
                headers={"Cache-Control": "no-store", "Retry-After": "2"},
            )(scope, receive, send)

        if scope["path"].startswith("/api/"):
            if duplicate or scope.get("query_string"):
                return await reject(400, "invalid_request")
            # Same-origin browser GET requests may omit Origin. Only this
            # read-only identity check accepts its absence; no CORS wildcard.
            access_check = scope["method"] == "GET" and scope["path"] == "/api/access"
            if headers.get(b"origin") != self.origin and not (
                access_check and b"origin" not in headers
            ):
                return await reject(403, "origin_denied")
            try:
                access = self.identity.authenticate(scope["headers"])
            except SessionError as error:
                return await reject(error.status, error.code)
            state = scope.setdefault("state", {})
            state["access"] = access
            state["owner"] = access.principal.owner
        is_post = scope["method"] == "POST"
        length = headers.get(b"content-length", "")
        if is_post:
            if (
                headers.get(b"content-type", "").split(";", 1)[0] != "application/json"
                or b"content-encoding" in headers
            ):
                return await reject(415, "json_required")
            if not length.isascii() or not length.isdecimal() or len(length) > 5:
                return await reject(411, "length_required")
            if int(length) > 4096:
                return await reject(413, "body_limit")

        elif b"content-length" in headers and (
            not length.isascii()
            or not length.isdecimal()
            or len(length) > 5
            or int(length) != 0
        ):
            return await reject(400, "invalid_request")

        try:
            body = await bounded_body(receive, limit=4096 if is_post else 0, timeout=5)
            if is_post:
                if len(body) != int(length):
                    raise ValueError("length mismatch")
                strict_json(body)
        except (ValueError, UnicodeError, RecursionError, TimeoutError):
            return await reject(400, "invalid_request")
        consumed = False
        original_receive = receive

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await original_receive()

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message["headers"] += [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                    ),
                    (
                        b"permissions-policy",
                        b"geolocation=(), camera=(), microphone=()",
                    ),
                ]
            await send(message)

        await self.app(scope, replay, secure_send)


def create_app(settings=None, upstream=None, sessions=None):
    settings = settings or WebSettings.from_environment()
    identity = EdgeIdentity(settings.edge_secret_file)
    upstream, sessions = upstream or Upstream(settings), sessions or Sessions()
    slots = Slots(1)

    @asynccontextmanager
    async def lifespan(_app):
        async def reap():
            while True:
                await asyncio.sleep(30)
                sessions.reap()

        task = asyncio.create_task(reap())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            sessions.items.clear()

    class WebsiteView(APIView):
        def handle_exception(self, error):
            if isinstance(error, SessionError):
                return problem(error.status, error.code)
            if isinstance(error, ValidationError):
                return problem(422, "invalid_request")
            if isinstance(error, MethodNotAllowed):
                return Response({"detail": "Method Not Allowed"}, status=405)
            return problem(503, "unavailable")

    def token(request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or len(auth) != 50:
            raise SessionError(401, "session_required")
        return auth[7:]

    class HealthView(WebsiteView):
        http_method_names = ["get"]

        async def get(self, request):
            return Response({"status": "ok"})

    class AccessView(WebsiteView):
        http_method_names = ["get"]

        async def get(self, request):
            return Response(request.scope["state"]["access"].payload())

    class SessionView(WebsiteView):
        http_method_names = ["post", "delete"]

        async def post(self, request):
            if request.data != {}:
                return problem(422, "invalid_request")
            return Response(
                {
                    "token": sessions.create(request.scope["state"]["owner"]),
                    "version": 0,
                    "expires_in": sessions.ttl,
                    "max_turns": sessions.max_turns,
                    "revision": settings.revision,
                }
            )

        async def delete(self, request):
            sessions.delete(token(request), request.scope["state"]["owner"])
            return Response({"status": "cleared"})

    async def turn(payload, request):
        stamp = datetime.now(timezone.utc).isoformat()
        context = {"reference_time": stamp, "timezone": payload.timezone}
        try:
            normalize_context(context)
        except ValueError:
            return problem(422, "invalid_timezone")
        try:
            async with slots.claim():
                session = sessions.begin(
                    token(request), payload.version, request.scope["state"]["owner"]
                )
                try:
                    data = TurnRequest(
                        query=payload.query,
                        state=session.state,
                        pending=session.pending,
                        context=context,
                        top_k=payload.top_k,
                    ).model_dump()
                    result = await upstream.turn(data)
                    if result.status == "unavailable":
                        return problem(503, "upstream_unavailable")
                    if result.status == "ok":
                        session.state, session.pending = result.state, None
                    elif result.status == "clarification_required":
                        session.pending = session.pending or payload.query
                    # Rejections cannot poison state or pending clarification.
                    session.version += 1
                    return Response(
                        {
                            "version": session.version,
                            "result": result.model_dump(),
                            "context_used": normalize_context(context),
                        }
                    )
                finally:
                    session.busy = False
        except Busy:
            return problem(429, "busy")
        except UpstreamError as error:
            return problem(error.status, "upstream_unavailable")

    class TurnView(WebsiteView):
        http_method_names = ["post"]

        async def post(self, request):
            return await turn(UserTurn.model_validate(request.data), request)

    return WebBoundary(
        application(
            [
                path("healthz", HealthView.as_view()),
                path("api/access", AccessView.as_view()),
                path("api/session", SessionView.as_view()),
                path("api/turn", TurnView.as_view()),
            ],
            lifespan=lifespan,
        ),
        settings.origin,
        identity,
    )
