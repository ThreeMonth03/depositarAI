"""Async DRF views with JSON-only parsing and application-owned authentication."""

# DRF resolves settings while importing its view classes.
from . import application as _application  # noqa: F401

from adrf.views import APIView as AsyncAPIView
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.parsers import JSONParser
from rest_framework.renderers import JSONRenderer


class JSONNegotiation(DefaultContentNegotiation):
    def select_renderer(self, request, renderers, format_suffix=None):
        # These endpoints always return JSON, including browser Accept headers.
        del request, format_suffix
        renderer = renderers[0]
        return renderer, renderer.media_type


class APIView(AsyncAPIView):
    authentication_classes = ()
    permission_classes = ()
    throttle_classes = ()
    parser_classes = (JSONParser,)
    renderer_classes = (JSONRenderer,)
    content_negotiation_class = JSONNegotiation
    metadata_class = None
    schema = None
