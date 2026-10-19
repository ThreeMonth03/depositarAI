"""Isolate Django application routes and state within each ASGI request."""

import asyncio
from contextlib import AsyncExitStack
from threading import Lock
from types import ModuleType

from asgiref.sync import markcoroutinefunction
from django.conf import Settings, settings
from django.core.asgi import get_asgi_application
from django.core.exceptions import ImproperlyConfigured


urlpatterns = ()
_configuration_lock = Lock()
_handler_lock = Lock()
_handler = None


def configure():
    """Configure the shared framework once, without per-application globals."""
    with _configuration_lock:
        if settings.configured:
            if not getattr(settings, "DEPOSITAR_HTTP", False):
                raise ImproperlyConfigured(
                    "Django settings belong to another application"
                )
            return
        settings.configure(default_settings=Settings("search.http.settings"))


class ApplicationMiddleware:
    """Select the calling application's URLconf without changing settings."""

    async_capable = True
    sync_capable = False

    def __init__(self, get_response):
        self.get_response = get_response
        markcoroutinefunction(self)

    async def __call__(self, request):
        selected = request.scope["depositar.application"]
        request.urlconf = selected.urlconf
        request.app_state = selected.state
        # The existing JSON contracts require UTF-8, independent of an untrusted
        # Content-Type charset parameter.
        request.encoding = "utf-8"
        return await self.get_response(request)


class Application:
    def __init__(self, patterns, *, state=None, lifespan=None):
        global _handler
        configure()
        with _handler_lock:
            if _handler is None:
                _handler = get_asgi_application()
        self.handler = _handler
        self.state = state
        self.lifespan = lifespan
        # Module objects are valid, identity-hashed Django URLconfs. Keeping one
        # per factory avoids both global route replacement and app-state leaks.
        self.urlconf = ModuleType("depositar_routes")
        self.urlconf.urlpatterns = tuple(patterns)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self._lifespan(receive, send)
        if scope["type"] != "http":
            raise ValueError("Unsupported ASGI scope")
        scoped = {**scope, "depositar.application": self}
        disconnected = False
        pending_receive = None

        async def receive_until_disconnect():
            nonlocal pending_receive
            if disconnected:
                return {"type": "http.disconnect"}
            pending_receive = asyncio.create_task(receive())
            try:
                return await pending_receive
            except asyncio.CancelledError:
                if disconnected:
                    return {"type": "http.disconnect"}
                raise
            finally:
                pending_receive = None

        task = asyncio.create_task(self.handler(scoped, receive_until_disconnect, send))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Django joins its view task on http.disconnect. Shield its handler
            # so server-side cancellation follows that same cleanup path.
            disconnected = True
            if pending_receive is not None:
                pending_receive.cancel()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            # Retrieve any failure before propagating the caller's cancellation.
            if not task.cancelled():
                task.exception()
            raise

    async def _lifespan(self, receive, send):
        if (await receive())["type"] != "lifespan.startup":
            raise ValueError("Expected ASGI lifespan startup")
        phase = "startup"
        try:
            async with AsyncExitStack() as stack:
                if self.lifespan is not None:
                    await stack.enter_async_context(self.lifespan(self))
                await send({"type": "lifespan.startup.complete"})
                phase = "shutdown"
                if (await receive())["type"] != "lifespan.shutdown":
                    raise ValueError("Expected ASGI lifespan shutdown")
        except asyncio.CancelledError:
            raise
        except Exception:
            await send(
                {
                    "type": "lifespan." + phase + ".failed",
                    "message": "Application lifecycle failed.",
                }
            )
            raise RuntimeError("Application lifecycle failed.") from None
        await send({"type": "lifespan.shutdown.complete"})


def application(patterns, *, state=None, lifespan=None):
    """Create a Django ASGI application with private routes, state and lifetime."""
    return Application(patterns, state=state, lifespan=lifespan)


# DRF resolves default classes during import. Configure the shared framework
# before importing views; Django app setup and handler creation stay lazy.
configure()
