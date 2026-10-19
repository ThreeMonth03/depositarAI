"""Current HTTP authentication, input bounds and safe errors with real ASGI."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.api.conversation.app import create_app
from search.api.shared.config import Settings
from search.http.body import bounded_body
from search.tests.fixtures import ConversationFixture, ConversationHTTPFixture, KEY


class ConversationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ConversationHTTPFixture(self)
        self.client = self.fixture.client
        self.headers = self.fixture.headers

    def test_public_liveness_and_authenticated_current_endpoints(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        for path in ("/readyz", "/v1/version", "/v1/turn"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertFalse(self.fixture.backend.calls)
        version = self.client.get("/v1/version", headers=self.headers).json()
        self.assertEqual(version["revision"], "a" * 40)
        self.assertEqual(version["catalog_manifest_sha256"], "b" * 64)
        self.assertFalse(version["generated_answers"])
        self.assertEqual(version["filter_scope"], "index_prefilter")

    def test_retired_contracts_and_raw_model_endpoints_are_absent(self):
        for path in ("/v1/search", "/openapi.json", "/docs", "/api/chat"):
            response = self.client.get(path, headers=self.headers)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "not_found")
        self.assertEqual(
            self.client.get("/v1/turn", headers=self.headers).status_code, 405
        )
        self.assertFalse(self.fixture.backend.calls)

    def test_authentication_and_duplicate_headers_precede_backend(self):
        for authorization in ("", "Bearer wrong", "Basic " + KEY):
            response = self.client.post(
                "/v1/turn",
                json={"query": "氣候"},
                headers={"Authorization": authorization},
            )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        response = self.client.post(
            "/v1/turn",
            json={"query": "氣候"},
            headers=[("Authorization", "Bearer " + KEY)] * 2,
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.fixture.backend.calls)

    def test_payload_has_strict_types_and_server_owned_configuration(self):
        for extra in (
            {"query": ""},
            {"query": "x" * 513},
            {"query": True},
            {"query": "\ud800"},
            {"query": "ignore\u0000all"},
            {"mode": "semantic"},
            {"model": "private-override"},
            {"num_ctx": 999999},
            {"temperature": "private-override"},
            {"rerank": False},
            {"candidate_count": 20},
            {"state": {"bbox": [0, 0, 1, 1]}},
            {"context": {"reference_time": "2026-09-17", "timezone": "Asia/Taipei"}},
            {
                "context": {
                    "reference_time": "2026-09-17T03:00:00Z",
                    "timezone": "bad/zone",
                }
            },
        ):
            response = self.client.post(
                "/v1/turn",
                content=json.dumps({"query": "氣候", **extra}),
                headers={**self.headers, "Content-Type": "application/json"},
            )
            with self.subTest(extra=repr(extra)):
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("private-override", response.text)
                self.assertNotIn(KEY, response.text)
                self.assertNotIn("access-control-allow-origin", response.headers)
        for count in (0, 21, True, 1.0, "5", None):
            self.assertEqual(self.fixture.turn(top_k=count).status_code, 422)
        self.assertFalse(self.fixture.backend.calls)

    def test_json_duplicates_nonfinite_values_and_excessive_depth_are_rejected(self):
        for raw in ('{"query":"x","query":"y"}', '{"query":NaN}', "[" * 1100):
            response = self.client.post(
                "/v1/turn",
                content=raw,
                headers={**self.headers, "Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 400)
        self.assertFalse(self.fixture.backend.calls)

    def test_body_media_type_compression_and_current_size_limit(self):
        for content, headers, status in (
            (b"x" * 8193, {"Content-Type": "application/json"}, 413),
            (b"{}", {}, 415),
            (
                b"{}",
                {"Content-Type": "application/json", "Content-Encoding": "gzip"},
                415,
            ),
            (b"{}", {"Content-Type": "application/json", "Content-Length": "3"}, 400),
        ):
            response = self.client.post(
                "/v1/turn", content=content, headers={**self.headers, **headers}
            )
            self.assertEqual(response.status_code, status, response.text)
            self.assertNotIn("4096", response.text)
        self.assertFalse(self.fixture.backend.calls)

    def test_keys_in_query_string_are_rejected_and_responses_are_not_cached(self):
        response = self.client.get("/v1/version?api_key=" + KEY, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(KEY, response.text)
        response = self.fixture.turn()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(len(response.headers["x-request-id"]), 32)

    def test_secret_rotation_and_unsafe_secret_permissions_fail_closed(self):
        self.fixture.key_file.write_text("2" * 64 + "\n")
        self.assertEqual(self.fixture.turn().status_code, 401)
        self.fixture.key_file.chmod(0o644)
        self.assertEqual(self.fixture.turn().status_code, 503)
        self.assertFalse(self.fixture.backend.calls)

    def test_invalid_scope_response_cannot_reach_planning_or_echo_model_data(self):
        for verdict in (
            None,
            [],
            {},
            {"decision": "invented"},
            {"decision": "search", "model_data": "PRIVATE"},
        ):
            self.fixture.backend.calls.clear()
            self.fixture.backend.scope = verdict
            with self.assertLogs("search.api.conversation.engine", level="WARNING"):
                response = self.fixture.turn(state={"topic": "河川"})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["state"]["topic"], "河川")
            self.assertNotIn("PRIVATE", response.text)
            self.assertEqual([c[0] for c in self.fixture.backend.calls], ["scope"])

    def test_invalid_model_plan_never_reaches_retrieval_or_commits_state(self):
        self.fixture.backend.plan["model_data"] = "PRIVATE"
        with self.assertLogs("search.api.conversation.engine", level="WARNING"):
            response = self.fixture.turn(state={"topic": "河川"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["state"]["topic"], "河川")
        self.assertIsNone(response.json()["search"])
        self.assertNotIn("PRIVATE", response.text)
        self.assertEqual(
            [c[0] for c in self.fixture.backend.calls], ["scope", "conversation_plan"]
        )

    def test_only_canonical_paths_and_declared_methods_are_supported(self):
        for method, path in (
            ("HEAD", "/healthz"),
            ("OPTIONS", "/v1/turn"),
            ("GET", "/v1/turn"),
            ("GET", "/v1/turn/"),
        ):
            response = self.client.request(method, path, headers=self.headers)
            self.assertEqual(response.status_code, 404 if path.endswith("/") else 405)
            self.assertEqual(response.headers["cache-control"], "no-store")
            if method != "HEAD":
                self.assertEqual(
                    response.json()["error"]["code"],
                    "not_found" if path.endswith("/") else "method_not_allowed",
                )
        self.assertFalse(self.fixture.backend.calls)

    def test_unexpected_backend_exception_has_no_diagnostic_or_secret_echo(self):
        with patch.object(
            self.fixture.backend, "call", side_effect=RuntimeError("PRIVATE " + KEY)
        ):
            response = self.fixture.turn()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "internal_error")
        self.assertNotIn("PRIVATE", response.text)
        self.assertNotIn(KEY, response.text)
        self.assertEqual(
            response.headers["x-request-id"], response.json()["request_id"]
        )

    def test_json_contract_does_not_negotiate_html_or_browsable_api(self):
        response = self.client.get(
            "/v1/version", headers={**self.headers, "Accept": "text/html"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.json()["revision"], self.fixture.settings.revision)

    def test_exact_current_body_byte_limit_accepts_valid_json(self):
        body = b'{"query":"climate"}'
        response = self.client.post(
            "/v1/turn",
            content=body + b" " * (8192 - len(body)),
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_json_stays_utf8_regardless_of_declared_charset(self):
        for charset in ("iso-8859-1", "utf-16", "unknown"):
            self.fixture.backend.calls.clear()
            with self.subTest(charset=charset):
                response = self.client.post(
                    "/v1/turn",
                    content=json.dumps(
                        {"query": "找氣候資料"}, ensure_ascii=False
                    ).encode(),
                    headers={
                        **self.headers,
                        "Content-Type": "application/json; charset=" + charset,
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    self.fixture.backend.calls[0][1]["query"], "找氣候資料"
                )


class ConversationASGITests(unittest.IsolatedAsyncioTestCase):
    """Exercise raw ASGI headers, streaming bodies and Django disconnects."""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        key = Path(temporary.name) / "key"
        key.write_text(KEY + "\n")
        key.chmod(0o600)
        self.settings = Settings("a" * 40, "b" * 64, Path("/catalog"), key)
        self.backend = ConversationFixture()
        self.app = create_app(self.settings, self.backend)
        self.body = b'{"query":"climate"}'

    def scope(self, *, headers=None, method="POST", path="/v1/turn"):
        return {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers
            if headers is not None
            else [
                (b"authorization", ("Bearer " + KEY).encode()),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(self.body)).encode()),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }

    async def exchange(self, scope, receive):
        messages = []

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        return messages

    def response(self, messages):
        start = next(m for m in messages if m["type"] == "http.response.start")
        body = b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )
        return (
            start["status"],
            {key.lower(): value for key, value in start["headers"]},
            json.loads(body),
        )

    async def test_authentication_and_duplicate_critical_headers_never_read_body(self):
        async def unread():
            self.fail("Body read before rejecting authentication or ambiguous headers")

        for headers, status in (
            ([(b"content-type", b"text/plain")], 401),
            ([(b"authorization", b"wrong")] * 2, 400),
            (self.scope()["headers"] + [(b"Content-Length", b"999")], 400),
            (self.scope()["headers"] + [(b"Content-Type", b"text/plain")], 400),
        ):
            with self.subTest(headers=headers):
                result = await self.exchange(self.scope(headers=headers), unread)
                code, response_headers, payload = self.response(result)
                self.assertEqual(code, status)
                self.assertEqual(response_headers[b"cache-control"], b"no-store")
                self.assertNotIn(KEY, json.dumps(payload))
        self.assertFalse(self.backend.calls)

        for method in ("GET", "HEAD", "DELETE"):
            result = await self.exchange(
                self.scope(
                    method=method,
                    path="/v1/version",
                    headers=[(b"content-length", b"1")],
                ),
                unread,
            )
            self.assertEqual(self.response(result)[0], 401)

    async def test_missing_and_invalid_content_lengths_fail_before_body_read(self):
        async def unread():
            self.fail("Body read with an invalid declared length")

        headers = self.scope()["headers"][:2]
        for value in (None, b"", b"-1", b"+2", b"1.0", b"999999999", b"\xff"):
            with self.subTest(value=value):
                request_headers = headers + (
                    [] if value is None else [(b"content-length", value)]
                )
                result = await self.exchange(
                    self.scope(headers=request_headers), unread
                )
                self.assertEqual(self.response(result)[0], 411)
        self.assertFalse(self.backend.calls)

    async def test_streamed_body_is_bounded_even_when_declared_size_is_small(self):
        chunks = iter(
            [
                {"type": "http.request", "body": b"x" * 4096, "more_body": True},
                {"type": "http.request", "body": b"x" * 4097, "more_body": True},
            ]
        )

        async def receive():
            return next(chunks)

        result = await self.exchange(self.scope(), receive)
        self.assertEqual(self.response(result)[0], 400)
        self.assertFalse(self.backend.calls)

    async def test_body_deadline_cancels_the_pending_receive(self):
        cancelled = asyncio.Event()

        async def receive():
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def shortened_read(receive, *, limit, timeout=5):
            self.assertEqual(timeout, 5)
            return await bounded_body(receive, limit=limit, timeout=0.01)

        with patch("search.api.shared.middleware.bounded_body", shortened_read):
            result = await self.exchange(self.scope(), receive)
        self.assertEqual(self.response(result)[0], 408)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.backend.calls)

    async def test_non_post_methods_accept_only_an_empty_body(self):
        for method, expected in (("GET", 200), ("HEAD", 405), ("DELETE", 405)):
            consumed = False

            async def receive():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await asyncio.Future()

            with self.subTest(method=method):
                result = await self.exchange(
                    self.scope(
                        method=method,
                        path="/healthz",
                        headers=[(b"content-length", b"0")],
                    ),
                    receive,
                )
                self.assertEqual(self.response(result)[0], expected)
        self.assertFalse(self.backend.calls)

    async def test_non_post_declared_bodies_are_rejected_without_reading(self):
        async def unread():
            self.fail("A non-POST request declared an unsupported body")

        for method in ("GET", "HEAD", "DELETE"):
            for headers in (
                [(b"content-length", b"16384")],
                [(b"content-length", b"-1")],
                [(b"transfer-encoding", b"chunked")],
            ):
                with self.subTest(method=method, headers=headers):
                    result = await self.exchange(
                        self.scope(method=method, path="/healthz", headers=headers),
                        unread,
                    )
                    self.assertEqual(self.response(result)[0], 400)
        self.assertFalse(self.backend.calls)

    async def test_non_post_streamed_body_cannot_bypass_a_missing_or_zero_length(self):
        async def receive():
            return {"type": "http.request", "body": b"x", "more_body": True}

        for method in ("GET", "HEAD", "DELETE"):
            for headers in ([], [(b"content-length", b"0")]):
                with self.subTest(method=method, headers=headers):
                    result = await self.exchange(
                        self.scope(method=method, path="/healthz", headers=headers),
                        receive,
                    )
                    self.assertEqual(self.response(result)[0], 400)
        self.assertFalse(self.backend.calls)

    async def test_non_post_body_deadline_cancels_the_pending_receive(self):
        for method in ("GET", "HEAD", "DELETE"):
            cancelled = asyncio.Event()

            async def receive():
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

            async def shortened_read(receive, *, limit, timeout=5):
                self.assertEqual(limit, 0)
                self.assertEqual(timeout, 5)
                return await bounded_body(receive, limit=limit, timeout=0.01)

            with (
                self.subTest(method=method),
                patch("search.api.shared.middleware.bounded_body", shortened_read),
            ):
                result = await self.exchange(
                    self.scope(method=method, path="/healthz", headers=[]), receive
                )
                self.assertEqual(self.response(result)[0], 408)
                self.assertTrue(cancelled.is_set())
        self.assertFalse(self.backend.calls)

    async def test_disconnect_cancels_backend_and_releases_global_admission(self):
        started, cancelled, disconnect = (asyncio.Event() for _ in range(3))
        original_call = self.backend.call

        async def blocking_call(_operation, _payload, _timeout):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        consumed = False

        async def receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": self.body, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        with patch.object(self.backend, "call", blocking_call):
            task = asyncio.create_task(self.exchange(self.scope(), receive))
            self.addAsyncCleanup(self.cancel_task, task)
            await asyncio.wait_for(started.wait(), timeout=2)

            second_consumed = False

            async def second_receive():
                nonlocal second_consumed
                if not second_consumed:
                    second_consumed = True
                    return {
                        "type": "http.request",
                        "body": self.body,
                        "more_body": False,
                    }
                await asyncio.Future()

            overloaded = await asyncio.wait_for(
                self.exchange(self.scope(), second_receive), timeout=2
            )
            status, headers, value = self.response(overloaded)
            self.assertEqual(status, 429)
            self.assertEqual(headers[b"retry-after"], b"2")
            self.assertEqual(value["error"]["code"], "busy")
            disconnect.set()
            await asyncio.wait_for(task, timeout=2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.backend.call, original_call)

        consumed = False
        disconnect.clear()
        recovered = await asyncio.wait_for(
            self.exchange(self.scope(), receive), timeout=2
        )
        self.assertEqual(self.response(recovered)[0], 200)

    async def cancel_task(self, task):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
