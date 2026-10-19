"""Synchronous test requests on one real, lifespan-managed ASGI event loop."""

from functools import partial

from anyio.from_thread import start_blocking_portal
from asgi_lifespan import LifespanManager
import httpx


class ASGITestClient:
    """Use HTTPX and ASGI Lifespan without a web framework's test transport."""

    def __init__(self, app, *, raise_app_exceptions=True):
        self._portal_context = start_blocking_portal()
        self._portal = self._portal_context.__enter__()
        self._closed = False
        try:
            self._portal.call(self._start, app, raise_app_exceptions)
        except BaseException:
            self._portal_context.__exit__(None, None, None)
            raise

    async def _start(self, app, raise_app_exceptions):
        async def per_request_state(scope, receive, send):
            if scope["type"] == "http":
                # ASGI servers shallow-copy lifespan state for each request.
                scope = {**scope, "state": dict(scope.get("state", {}))}
            await app(scope, receive, send)

        self._lifespan = LifespanManager(per_request_state)
        await self._lifespan.__aenter__()
        try:
            self._client = httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=self._lifespan.app, raise_app_exceptions=raise_app_exceptions
                ),
                base_url="http://testserver",
            )
        except BaseException:
            await self._lifespan.__aexit__(None, None, None)
            raise

    def request(self, method, url, **kwargs):
        return self._portal.call(partial(self._client.request, method, url, **kwargs))

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)

    async def _stop(self):
        try:
            await self._client.aclose()
        finally:
            await self._lifespan.__aexit__(None, None, None)

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._portal.call(self._stop)
            finally:
                self._portal_context.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
