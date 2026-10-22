"""Private conversation API with bounded, authenticated search turns."""

from django.http import JsonResponse
from django.urls import path, re_path
from pydantic import ValidationError
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from search.http.application import application
from search.http.views import APIView

from ..shared.config import CONVERSATION_CONTRACT_VERSION, Settings
from ..shared.admission import Busy
from ..shared.backend import BackendFailure
from ..shared.readiness import BackendReadiness
from ..shared.middleware import BoundaryMiddleware, error_details
from .contract import TurnRequest
from .engine import ConversationEngine
from .worker import ConversationBackend


def error_response(status, request_id):
    data, headers = error_details(status, request_id)
    return Response(data, status=status, headers=headers)


class PrivateView(APIView):
    """Keep every framework and validation error within the private contract."""

    def handle_exception(self, error):
        status = error.status_code if isinstance(error, APIException) else 500
        return error_response(status, self.request.scope["state"]["request_id"])


def create_app(settings=None, backend=None):
    settings = settings or Settings.from_environment()
    backend = backend or ConversationBackend()
    engine = ConversationEngine(settings, backend)
    readiness = BackendReadiness(backend)

    class Health(PrivateView):
        http_method_names = ["get"]

        async def get(self, _request):
            return Response({"status": "ok"})

    class Version(PrivateView):
        http_method_names = ["get"]

        async def get(self, _request):
            return Response(
                {
                    "revision": settings.revision,
                    "catalog_manifest_sha256": settings.manifest_sha256,
                    "conversation_contract_version": CONVERSATION_CONTRACT_VERSION,
                    "geography": False,
                    "persistent_memory": False,
                    "generated_answers": False,
                    "filter_scope": "index_prefilter",
                }
            )

    class Ready(PrivateView):
        http_method_names = ["get"]

        async def get(self, request):
            try:
                return Response(await readiness.check())
            except (BackendFailure, Busy):
                return error_response(503, request.scope["state"]["request_id"])

    class Turn(PrivateView):
        http_method_names = ["post"]

        async def post(self, request):
            request_id = request.scope["state"]["request_id"]
            try:
                payload = TurnRequest.model_validate(request.data)
            except ValidationError:
                return error_response(422, request_id)
            try:
                value = await engine.turn(payload, request_id)
            except Busy:
                return error_response(429, request_id)
            return Response(
                value.model_dump(),
                status=503 if value.status == "unavailable" else 200,
            )

    async def missing_endpoint(request):
        data, headers = error_details(404, request.scope["state"]["request_id"])
        return JsonResponse(data, status=404, headers=headers)

    app = application(
        [
            path("healthz", Health.as_view()),
            path("v1/version", Version.as_view()),
            path("readyz", Ready.as_view()),
            path("v1/turn", Turn.as_view()),
            re_path(r"^", missing_endpoint),
        ]
    )
    return BoundaryMiddleware(app, key_file=settings.key_file, max_body_bytes=8192)
