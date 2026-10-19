"""Protect async Django application isolation and request cleanup."""

import asyncio
from contextlib import asynccontextmanager
import json
import sys
import unittest

from asgiref.testing import ApplicationCommunicator
from asgi_lifespan import LifespanManager
from django.conf import settings
from django.urls import path
import httpx
from rest_framework.response import Response

from search.http.application import application
from search.http.body import bounded_body
from search.http.responses import json_response
from search.http.views import APIView


def http_scope():
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
    }


class HTTPApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_factories_keep_routes_and_state_separate(self):
        entered = {name: asyncio.Event() for name in ("api", "web")}
        release = asyncio.Event()

        def create(name):
            class Endpoint(APIView):
                async def get(self, request):
                    entered[name].set()
                    await release.wait()
                    return Response({"route": name, "state": request.app_state["name"]})

            return application([path("", Endpoint.as_view())], state={"name": name})

        apps = [create("api"), create("web")]
        self.assertIs(apps[0].handler, apps[1].handler)
        self.assertIsNot(apps[0].urlconf, apps[1].urlconf)
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(apps[0]), base_url="http://testserver"
            ) as api,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(apps[1]), base_url="http://testserver"
            ) as web,
        ):
            tasks = [asyncio.create_task(client.get("/")) for client in (api, web)]
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(event.wait() for event in entered.values())), 2
                )
            finally:
                release.set()
            responses = await asyncio.gather(*tasks)
        self.assertEqual(
            [response.json() for response in responses],
            [{"route": "api", "state": "api"}, {"route": "web", "state": "web"}],
        )
        self.assertEqual(settings.INSTALLED_APPS, [])

    async def test_json_contract_ignores_accept_and_charset(self):
        class Endpoint(APIView):
            async def post(self, request):
                return Response(request.data)

        app = application([path("", Endpoint.as_view())])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/",
                content='{"query":"臺灣"}'.encode(),
                headers={
                    "Accept": "text/html",
                    "Content-Type": "application/json; charset=iso-8859-1",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.json(), {"query": "臺灣"})

    async def test_native_disconnect_joins_cancelled_view(self):
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        class Endpoint(APIView):
            async def get(self, request):
                del request
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

        app = application([path("", Endpoint.as_view())])
        connection = ApplicationCommunicator(app, http_scope())
        await connection.send_input({"type": "http.request", "body": b""})
        await asyncio.wait_for(entered.wait(), 2)
        await connection.send_input({"type": "http.disconnect"})
        await connection.wait(timeout=2)
        self.assertTrue(cleaned.is_set())
        self.assertTrue(connection.output_queue.empty())

    async def test_server_task_cancellation_waits_for_subprocess_cleanup(self):
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        processes = []

        class Endpoint(APIView):
            async def get(self, request):
                del request
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", "import time; time.sleep(30)"
                )
                processes.append(process)
                entered.set()
                try:
                    await process.wait()
                finally:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
                    cleaned.set()

        app = application([path("", Endpoint.as_view())])
        initial_tasks = asyncio.all_tasks()
        connection = ApplicationCommunicator(app, http_scope())
        await connection.send_input({"type": "http.request", "body": b""})
        try:
            await asyncio.wait_for(entered.wait(), 2)
            connection.future.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(connection.future, 2)
            self.assertTrue(cleaned.is_set())
            self.assertIsNotNone(processes[0].returncode)
            self.assertTrue(connection.output_queue.empty())
            self.assertEqual(asyncio.all_tasks() - initial_tasks, set())
        finally:
            for process in processes:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            await connection.wait(timeout=2)

    async def test_application_lifetimes_are_independent(self):
        running = set()

        @asynccontextmanager
        async def lifespan(app):
            running.add(app.state)
            try:
                yield
            finally:
                running.remove(app.state)

        api = application([], state="api", lifespan=lifespan)
        web = application([], state="web", lifespan=lifespan)
        async with LifespanManager(api):
            self.assertEqual(running, {"api"})
            async with LifespanManager(web):
                self.assertEqual(running, {"api", "web"})
            self.assertEqual(running, {"api"})
        self.assertEqual(running, set())

    async def test_lifespan_failures_are_reported_without_exception_details(self):
        for phase in ("startup", "shutdown"):
            with self.subTest(phase=phase):

                @asynccontextmanager
                async def lifespan(app):
                    del app
                    if phase == "startup":
                        raise RuntimeError("private configuration")
                    yield
                    raise RuntimeError("private configuration")

                messages = iter(
                    [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
                )
                sent = []

                async def receive():
                    return next(messages)

                async def send(message):
                    sent.append(message)

                with self.assertRaisesRegex(
                    RuntimeError, r"^Application lifecycle failed\.$"
                ):
                    await application([], lifespan=lifespan)(
                        {"type": "lifespan"}, receive, send
                    )
                self.assertEqual(
                    sent[-1],
                    {
                        "type": "lifespan." + phase + ".failed",
                        "message": "Application lifecycle failed.",
                    },
                )
                self.assertNotIn("private configuration", json.dumps(sent))

    async def test_raw_json_rejection_never_reads_untrusted_body(self):
        sent = []

        async def receive():
            self.fail("a boundary rejection must not consume the request body")

        async def send(message):
            sent.append(message)

        await json_response(
            {"error": "拒絕"}, status=413, headers={"X-Request-Id": "bounded-id"}
        )(http_scope(), receive, send)
        self.assertEqual(sent[0]["status"], 413)
        self.assertIn((b"x-request-id", b"bounded-id"), sent[0]["headers"])
        self.assertEqual(json.loads(sent[1]["body"]), {"error": "拒絕"})
        with self.assertRaises(ValueError):
            json_response({"value": float("nan")})


class BoundedBodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunks_are_capped_by_actual_total_bytes(self):
        messages = asyncio.Queue()
        await messages.put({"type": "http.request", "body": b"ab", "more_body": True})
        await messages.put({"type": "http.request", "body": b"cd"})
        self.assertEqual(await bounded_body(messages.get, limit=4), b"abcd")

        await messages.put({"type": "http.request", "body": b"ab", "more_body": True})
        await messages.put({"type": "http.request", "body": b"cde", "more_body": True})
        await messages.put({"type": "http.request", "body": b"unread"})
        with self.assertRaises(ValueError):
            await bounded_body(messages.get, limit=4)
        self.assertEqual(messages.qsize(), 1)

    async def test_zero_limit_accepts_only_an_empty_body(self):
        messages = asyncio.Queue()
        await messages.put({"type": "http.request"})
        self.assertEqual(await bounded_body(messages.get, limit=0), b"")
        await messages.put({"type": "http.request", "body": b"x"})
        with self.assertRaises(ValueError):
            await bounded_body(messages.get, limit=0)

    async def test_disconnect_and_invalid_asgi_messages_are_rejected(self):
        for message in (
            {"type": "http.disconnect"},
            {"type": "websocket.receive", "bytes": b"data"},
            {},
            {"type": "http.request", "body": "text"},
            {"type": "http.request", "more_body": "true"},
            None,
        ):
            with self.subTest(message=message):
                messages = asyncio.Queue()
                await messages.put(message)
                with self.assertRaises(ValueError):
                    await bounded_body(messages.get, limit=4)

    async def test_deadline_covers_all_chunks_and_cancels_pending_receive(self):
        cancelled = asyncio.Event()
        chunks = 0

        async def receive():
            nonlocal chunks
            try:
                await asyncio.sleep(0.005)
                chunks += 1
                return {"type": "http.request", "more_body": True}
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with self.assertRaises(TimeoutError):
            await bounded_body(receive, limit=0, timeout=0.025)
        self.assertGreater(chunks, 0)
        self.assertTrue(cancelled.is_set())

    async def test_caller_cancellation_joins_pending_receive(self):
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        initial_tasks = asyncio.all_tasks()

        async def receive():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        task = asyncio.create_task(bounded_body(receive, limit=0))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cleaned.is_set())
        self.assertEqual(asyncio.all_tasks() - initial_tasks, set())
