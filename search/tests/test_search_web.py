"""RAM session isolation, same-origin boundary, failure atomicity and deadlines."""

import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from search.tests.asgi_client import ASGITestClient
from search.api.conversation.contract import SearchState, TurnRequest, TurnResponse
from search.api.conversation.engine import ConversationEngine
from search.api.shared.config import Settings
from search.tests.fixtures import ConversationFixture
from search.web.app import create_app
from search.web.sessions import SessionError, Sessions
from search.web.upstream import Upstream, UpstreamError, WebSettings
from search.tests.web_support import (
    FakeUpstream as FakeUpstream,
    ORIGIN as ORIGIN,
    SETTINGS as SETTINGS,
)


class SessionTests(unittest.TestCase):
    def test_unique_tokens_and_isolation(self):
        store = Sessions()
        a, b = store.create(), store.create()
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 43)
        store.get(a).state.topic = "private session"
        self.assertEqual(store.get(b).state.topic, "")

    def test_absolute_expiry_and_capacity(self):
        now = [0]
        store = Sessions(capacity=1, ttl=10, clock=lambda: now[0])
        token = store.create()
        with self.assertRaises(SessionError) as caught:
            store.create()
        self.assertEqual(caught.exception.status, 429)
        now[0] = 10
        with self.assertRaises(SessionError) as caught:
            store.get(token)
        self.assertEqual(caught.exception.status, 410)
        self.assertNotEqual(store.create(), token)

    def test_turn_limit_stale_version_and_busy(self):
        store = Sessions(max_turns=1)
        token = store.create()
        with self.assertRaises(SessionError) as caught:
            store.begin(token, 1)
        self.assertEqual(caught.exception.status, 409)
        item = store.begin(token, 0)
        with self.assertRaises(SessionError) as caught:
            store.begin(token, 0)
        self.assertEqual(caught.exception.status, 429)
        item.busy, item.version = False, 1
        with self.assertRaises(SessionError) as caught:
            store.begin(token, 1)
        self.assertEqual(caught.exception.status, 410)

    def test_delete_is_idempotent(self):
        store = Sessions()
        token = store.create()
        store.delete(token)
        store.delete(token)
        self.assertEqual(store.items, {})


class WebHTTPTests(unittest.TestCase):
    def setUp(self):
        self.store, self.upstream = Sessions(), FakeUpstream()
        self.client = ASGITestClient(
            create_app(SETTINGS, self.upstream, self.store),
            raise_app_exceptions=False,
        )
        self.addCleanup(self.client.close)
        self.headers = {"Origin": ORIGIN}
        self.token = self.client.post(
            "/api/session", json={}, headers=self.headers
        ).json()["token"]
        self.headers["Authorization"] = "Bearer " + self.token

    def turn(self, **kwargs):
        return self.client.post(
            "/api/turn",
            json={"query": "氣候", "version": 0, **kwargs},
            headers=self.headers,
        )

    def test_success_commits_state_and_server_clock(self):
        response = self.turn(top_k=20)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value["version"], 1)
        self.assertEqual(value["context_used"]["timezone"], "Asia/Taipei")
        self.assertEqual(self.store.get(self.token).state.formats, ["CSV"])
        self.turn(query="改為 PDF", version=1)
        self.assertEqual(self.upstream.calls[-1]["state"]["topic"], "氣候")
        self.assertEqual(self.upstream.calls[0]["top_k"], 20)
        self.assertNotIn("messages", self.upstream.calls[-1])

    def test_error_does_not_advance_or_poison_state(self):
        self.upstream.error = UpstreamError()
        self.assertEqual(self.turn().status_code, 503)
        item = self.store.get(self.token)
        self.assertEqual(item.version, 0)
        self.assertFalse(item.busy)
        self.assertEqual(item.state.topic, "")

    def test_clarification_keeps_one_pending_query(self):
        self.upstream.status = "clarification_required"
        self.turn(query="最近的水質")
        self.turn(query="請再說一次", version=1)
        item = self.store.get(self.token)
        self.assertEqual(item.pending, "最近的水質")
        self.assertEqual(item.state.topic, "")
        self.upstream.status = "ok"
        self.turn(query="2025年", version=2)
        self.assertIsNone(item.pending)

    def test_rejection_does_not_change_state(self):
        self.upstream.status = "rejected"
        self.turn()
        item = self.store.get(self.token)
        self.assertEqual(item.state.topic, "")
        self.assertIsNone(item.pending)
        self.assertEqual(item.version, 1)

    def test_origin_is_required_and_exact(self):
        for origin in (
            None,
            "null",
            "https://evil.test",
            ORIGIN + ".evil",
            ORIGIN + "/",
        ):
            headers = {"Origin": origin} if origin else {}
            self.assertEqual(
                self.client.post("/api/session", json={}, headers=headers).status_code,
                403,
            )
        self.assertFalse(self.upstream.calls)

    def test_api_secrets_and_model_knobs_not_browser_inputs(self):
        for extra in (
            {"state": {}},
            {"context": {}},
            {"upstream": "http://evil"},
            {"model": "x"},
            {"top_k": 21},
            {"version": True},
            {"timezone": "bad/zone"},
        ):
            with self.subTest(extra=extra):
                self.assertEqual(self.turn(**extra).status_code, 422)
        self.assertFalse(self.upstream.calls)

    def test_missing_expired_and_stale_tokens(self):
        self.assertEqual(
            self.client.post(
                "/api/turn",
                json={"query": "氣候", "version": 0},
                headers={"Origin": ORIGIN},
            ).status_code,
            401,
        )
        self.assertEqual(self.turn(version=1).status_code, 409)
        self.store.delete(self.token)
        self.assertEqual(self.turn().status_code, 410)

    def test_reset_deletes_and_new_session_is_empty(self):
        self.turn()
        self.assertEqual(
            self.client.delete("/api/session", headers=self.headers).status_code, 200
        )
        new = self.client.post(
            "/api/session", json={}, headers={"Origin": ORIGIN}
        ).json()["token"]
        self.assertEqual(self.store.get(new).state.topic, "")
        self.assertNotIn(self.token, self.store.items)

    def test_security_headers_and_no_store(self):
        headers = self.turn().headers
        self.assertIn("no-store", headers["cache-control"])
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertIn("geolocation=()", headers["permissions-policy"])
        self.assertNotIn("access-control-allow-origin", headers)

    def test_json_routes_keep_their_methods_and_ignore_browser_accept(self):
        response = self.client.get("/healthz", headers={"Accept": "text/html"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        for method, path in (
            ("HEAD", "/healthz"),
            ("GET", "/api/session"),
            ("OPTIONS", "/api/turn"),
        ):
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, headers=self.headers)
                self.assertEqual(response.status_code, 405)

    def test_non_post_bodies_are_rejected_before_views(self):
        for method, path in (
            ("GET", "/healthz"),
            ("GET", "/api/access"),
            ("HEAD", "/"),
            ("DELETE", "/api/session"),
        ):
            for length in ("0", "1"):
                with self.subTest(method=method, path=path, length=length):
                    response = self.client.request(
                        method,
                        path,
                        content=b"x",
                        headers={**self.headers, "Content-Length": length},
                    )
                    self.assertEqual(response.status_code, 400)
        self.assertFalse(self.upstream.calls)
        self.assertIn(self.token, self.store.items)
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(
            self.client.get("/api/access", headers=self.headers).status_code, 200
        )
        self.assertEqual(
            self.client.delete("/api/session", headers=self.headers).status_code, 200
        )

    def test_body_bounds_duplicate_keys_and_compression(self):
        for raw, expected in ((b"a" * 4097, 413), (b'{"query":"a","query":"b"}', 400)):
            response = self.client.post(
                "/api/turn",
                content=raw,
                headers={**self.headers, "Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, expected)
        response = self.client.post(
            "/api/session",
            json={},
            headers={**self.headers, "Content-Encoding": "gzip"},
        )
        self.assertEqual(response.status_code, 415)

    def test_ambiguous_headers_query_strings_and_length_mismatch_are_rejected(self):
        for name, value in (
            ("Origin", ORIGIN),
            ("Host", "testserver"),
            ("Authorization", "Bearer " + self.token),
            ("Content-Type", "application/json"),
            ("Content-Length", "2"),
        ):
            headers = [
                ("Origin", ORIGIN),
                (name, value),
                (name, value),
                ("Content-Type", "application/json"),
            ]
            with self.subTest(name=name):
                self.assertEqual(
                    self.client.post(
                        "/api/session", content=b"{}", headers=headers
                    ).status_code,
                    400,
                )
        self.assertEqual(
            self.client.post(
                "/api/session?extra=1", json={}, headers=self.headers
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/session",
                content=b"{}",
                headers={
                    **self.headers,
                    "Content-Type": "application/json",
                    "Content-Length": "1",
                },
            ).status_code,
            400,
        )
        self.assertFalse(self.upstream.calls)


class WebConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_turn_releases_session_and_the_only_slot(self):
        store, upstream = Sessions(), FakeUpstream()
        entered, release = asyncio.Event(), asyncio.Event()
        original_turn = upstream.turn

        async def blocked_turn(payload):
            entered.set()
            await release.wait()
            return await original_turn(payload)

        upstream.turn = blocked_turn
        token = store.create()
        app = create_app(SETTINGS, upstream, store)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            headers = {"Origin": ORIGIN, "Authorization": "Bearer " + token}
            payload = {"query": "氣候", "version": 0}
            task = asyncio.create_task(
                client.post("/api/turn", json=payload, headers=headers)
            )
            try:
                await asyncio.wait_for(entered.wait(), 2)
                busy = await client.post("/api/turn", json=payload, headers=headers)
                self.assertEqual(busy.status_code, 429)
                self.assertEqual(busy.json()["error"]["code"], "busy")
                self.assertTrue(store.get(token).busy)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertFalse(store.get(token).busy)
            self.assertEqual(store.get(token).version, 0)
            release.set()
            response = await client.post("/api/turn", json=payload, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(store.get(token).version, 1)


class WebBodyDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_bodies_cancel_receive_and_keep_the_website_error_contract(self):
        store, upstream = Sessions(), FakeUpstream()
        token = store.create()
        app = create_app(SETTINGS, upstream, store)
        original_wait = asyncio.wait_for

        async def shortened_wait(awaitable, timeout):
            self.assertEqual(timeout, 5)
            return await original_wait(awaitable, timeout=0.01)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            for method, path in (
                ("GET", "/healthz"),
                ("HEAD", "/"),
                ("DELETE", "/api/session"),
                ("POST", "/api/session"),
            ):
                with self.subTest(method=method, path=path):
                    entered, cancelled = asyncio.Event(), asyncio.Event()

                    async def stalled_body():
                        entered.set()
                        try:
                            await asyncio.Event().wait()
                        finally:
                            cancelled.set()
                        yield b""

                    headers = {
                        "Origin": ORIGIN,
                        "Authorization": "Bearer " + token,
                        "Content-Length": "2" if method == "POST" else "0",
                        "Content-Type": "application/json",
                    }
                    with patch(
                        "search.http.body.asyncio.wait_for", side_effect=shortened_wait
                    ):
                        response = await client.request(
                            method, path, content=stalled_body(), headers=headers
                        )
                    self.assertEqual(response.status_code, 400)
                    if method != "HEAD":
                        self.assertEqual(
                            response.json()["error"]["code"], "invalid_request"
                        )
                    self.assertTrue(entered.is_set())
                    self.assertTrue(cancelled.is_set())
        self.assertEqual(len(store.items), 1)
        self.assertFalse(store.get(token).busy)
        self.assertFalse(upstream.calls)


class WebsiteRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = Sessions()
        self.client = ASGITestClient(create_app(SETTINGS, FakeUpstream(), self.store))
        self.addCleanup(self.client.close)

    def test_bff_does_not_serve_frontend_files_or_fallback_pages(self):
        for path in ("/", "/index.html", "/assets/site.css", "/missing"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)

    def test_api_routes_are_exact_and_missing_routes_do_not_create_sessions(self):
        for path in ("/api/access/", "/api/missing", "/api/assets/site.css"):
            with self.subTest(path=path):
                response = self.client.get(path, headers={"Origin": ORIGIN})
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.store.items, {})
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})
        self.assertEqual(
            self.client.get("/api/access").json(),
            {
                "mode": "private",
                "principal": {"kind": "private", "id": None},
                "can_search": True,
                "login_url": None,
                "logout_url": None,
                "limits": {"turns": None, "window_seconds": None},
            },
        )

    def test_shutdown_clears_ram_sessions(self):
        self.store.create()
        self.assertTrue(self.store.items)
        self.client.close()
        self.assertEqual(self.store.items, {})


class ConfigurationTests(unittest.TestCase):
    def test_optional_upstream_ca_environment_and_https_boundary(self):
        with patch.dict(
            os.environ,
            {
                "WEB_ORIGIN": ORIGIN,
                "WEB_UPSTREAM": "https://private-api.test",
                "WEB_UPSTREAM_CA_FILE": "/run/certs/upstream-ca.pem",
            },
            clear=True,
        ):
            configured = WebSettings.from_environment()
            self.assertEqual(
                configured.upstream_ca_file, Path("/run/certs/upstream-ca.pem")
            )
            os.environ["WEB_UPSTREAM_CA_FILE"] = ""
            self.assertIsNone(WebSettings.from_environment().upstream_ca_file)
            del os.environ["WEB_UPSTREAM_CA_FILE"]
            self.assertIsNone(WebSettings.from_environment().upstream_ca_file)
        for upstream, ca in (
            ("https://private-api.test", Path("relative.pem")),
            ("http://conversation-api:8000", Path("/ca.pem")),
        ):
            with self.subTest(upstream=upstream), self.assertRaises(ValueError):
                WebSettings(ORIGIN, upstream, Path("/key"), upstream_ca_file=ca)

    def test_http_fixture_imports_do_not_load_test_suites_or_test_clients(self):
        from search.tests import web_support

        self.assertIs(FakeUpstream, web_support.FakeUpstream)
        self.assertIs(SETTINGS, web_support.SETTINGS)
        self.assertEqual(ORIGIN, web_support.ORIGIN)
        root = Path(__file__).resolve().parents[2]
        script = (
            "import importlib, sys\n"
            f"sys.path.insert(0, {str(root)!r})\n"
            "for name in ('fixtures', 'web_support', 'web_fixture'):\n"
            "    importlib.import_module('search.tests.' + name)\n"
            "assert not any(name.startswith('search.tests.test_') "
            "for name in sys.modules), 'test suite imported by HTTP fixtures'\n"
            "assert 'search.tests.asgi_client' not in sys.modules\n"
            "print('HTTP fixtures import independently')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("HTTP fixtures import independently\n", completed.stdout)

    def test_no_plaintext_remote_upstream_or_url_credentials(self):
        for peer in (
            "http://backend.example.test:8000",
            "https://user:pass@backend.example.test",
            "https://backend.example.test/path",
            "file:///etc/passwd",
        ):
            with self.assertRaises(ValueError):
                WebSettings(ORIGIN, peer, Path("/key"))

    def test_no_public_plaintext_website_origin(self):
        for origin in (
            "http://0.0.0.0:18081",
            "http://website.example",
            "https://site.test/path",
            "https://site.test?x=1",
        ):
            with self.assertRaises(ValueError):
                WebSettings(origin, "http://conversation-api:8000", Path("/key"))


class TransportTests(unittest.TestCase):
    def search_response(self):
        engine = ConversationEngine(
            Settings("a" * 40, "b" * 64, Path("/catalog"), Path("/unused"), True, 20),
            ConversationFixture(),
        )
        response = asyncio.run(
            engine.turn(TurnRequest(query="氣候"), "bff-transport-test")
        )
        self.assertEqual(response.status, "ok")
        self.assertTrue(response.search.cards)
        return response.model_dump()

    def invoke(self, handler):
        factory = httpx.AsyncClient
        with (
            patch("search.web.upstream.read_secret", return_value="1" * 64),
            patch(
                "search.web.upstream.httpx.AsyncClient",
                side_effect=lambda **kwargs: factory(
                    transport=httpx.MockTransport(handler), **kwargs
                ),
            ) as constructor,
        ):
            value = asyncio.run(Upstream(SETTINGS).turn({"query": "氣候"}))
            self.assertFalse(constructor.call_args.kwargs["trust_env"])
            self.assertFalse(constructor.call_args.kwargs["follow_redirects"])
            context = constructor.call_args.kwargs["verify"]
            self.assertIsInstance(context, ssl.SSLContext)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            self.assertEqual(
                context.get_ca_certs(binary_form=True),
                httpx.create_ssl_context(trust_env=False).get_ca_certs(
                    binary_form=True
                ),
            )
            return value

    def test_fixed_endpoint_credential_and_schema(self):
        def handler(request):
            self.assertEqual(str(request.url), "http://conversation-api:8000/v1/turn")
            self.assertEqual(request.headers["authorization"], "Bearer " + "1" * 64)
            return httpx.Response(
                200,
                json=TurnResponse(
                    status="rejected",
                    message="不支援",
                    state=SearchState(),
                    revision="a" * 40,
                ).model_dump(),
            )

        self.assertEqual(self.invoke(handler).status, "rejected")

    def test_current_search_response_passes_the_bff_boundary(self):
        value = self.search_response()
        self.assertTrue(
            {"answer", "planning_status", "clarification"}.isdisjoint(value["search"])
        )
        result = self.invoke(lambda _request: httpx.Response(200, json=value))
        self.assertEqual(result.model_dump(), value)

    def test_removed_search_fields_are_rejected_instead_of_ignored(self):
        value = self.search_response()
        for field, previous in (
            ("answer", {"status": "not_requested", "match": None}),
            ("planning_status", "ready"),
            ("clarification", None),
        ):
            with self.subTest(field=field), self.assertRaises(UpstreamError) as caught:
                self.invoke(
                    lambda _request: httpx.Response(
                        200,
                        json={**value, "search": {**value["search"], field: previous}},
                    )
                )
            self.assertEqual(caught.exception.status, 503)

    def test_redirect_never_followed(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(307, headers={"Location": "https://evil.test"})

        with self.assertRaises(UpstreamError):
            self.invoke(handler)
        self.assertEqual(len(calls), 1)

    def test_oversized_or_malformed_response(self):
        for content in (
            b"x" * (256 * 1024 + 1),
            b"not json",
            b'{"status":"ok","status":"rejected"}',
        ):
            with self.subTest(size=len(content)), self.assertRaises(UpstreamError):
                self.invoke(lambda _request: httpx.Response(200, content=content))

    def test_timeout_is_sanitized(self):
        def handler(_request):
            raise httpx.ReadTimeout("internal secret")

        with self.assertRaises(UpstreamError) as caught:
            self.invoke(handler)
        self.assertNotIn("secret", str(caught.exception))

    def test_busy_and_inconsistent_status(self):
        with self.assertRaises(UpstreamError) as caught:
            self.invoke(lambda _request: httpx.Response(429))
        self.assertEqual(caught.exception.status, 429)
        value = TurnResponse(
            status="unavailable", message="暫停", state=SearchState(), revision="a" * 40
        ).model_dump()
        with self.assertRaises(UpstreamError):
            self.invoke(lambda _request: httpx.Response(200, json=value))


class PrivateUpstreamTLSTests(unittest.TestCase):
    """Actual local TLS proves private trust never disables identity checking."""

    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-web-ca-test-")
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)

        def openssl(*args):
            subprocess.run(
                ["openssl", *args],
                cwd=cls.root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=15,
            )

        for name in ("ca", "wrong-ca"):
            openssl(
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                name + ".key",
                "-out",
                name + ".pem",
                "-days",
                "1",
                "-subj",
                "/CN=isolated-" + name,
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
            )
        openssl(
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            "server.key",
            "-out",
            "server.csr",
            "-subj",
            "/CN=127.0.0.1",
        )
        (cls.root / "server.ext").write_text(
            "subjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
        )
        openssl(
            "x509",
            "-req",
            "-in",
            "server.csr",
            "-CA",
            "ca.pem",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-out",
            "server.pem",
            "-days",
            "1",
            "-extfile",
            "server.ext",
        )
        cls.key_file = cls.root / "api-key"
        cls.key_file.write_text("1" * 64 + "\n")
        cls.key_file.chmod(0o600)
        cls.requests = []
        body = json.dumps(
            TurnResponse(
                status="rejected",
                message="isolated TLS fixture",
                state=SearchState(),
                revision="a" * 40,
            ).model_dump()
        ).encode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                cls.requests.append((self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.addClassCleanup(cls.server.server_close)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.root / "server.pem", cls.root / "server.key")
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.shutdown)
        cls.settings = WebSettings(
            ORIGIN,
            f"https://127.0.0.1:{cls.server.server_port}",
            cls.key_file,
            upstream_ca_file=cls.root / "ca.pem",
        )

    def setUp(self):
        self.requests.clear()

    def test_private_ca_allows_tls_turn_without_environment_proxy_or_ca(self):
        with patch.dict(
            os.environ,
            {
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "SSL_CERT_FILE": str(self.root / "wrong-ca.pem"),
            },
        ):
            result = asyncio.run(Upstream(self.settings).turn({"query": "氣候"}))
        self.assertEqual(result.status, "rejected")
        self.assertEqual(self.requests, [("/v1/turn", "Bearer " + "1" * 64)])

    def test_missing_or_wrong_ca_rejects_tls_before_sending_service_key(self):
        for ca in (None, self.root / "wrong-ca.pem"):
            with (
                self.subTest(ca=ca),
                patch.dict(os.environ, {"SSL_CERT_FILE": str(self.root / "ca.pem")}),
                self.assertRaises(UpstreamError),
            ):
                asyncio.run(
                    Upstream(replace(self.settings, upstream_ca_file=ca)).turn(
                        {"query": "氣候"}
                    )
                )
            self.assertEqual(self.requests, [])

    def test_private_ca_does_not_disable_hostname_verification(self):
        settings = replace(
            self.settings, upstream=f"https://localhost:{self.server.server_port}"
        )
        with self.assertRaises(UpstreamError):
            asyncio.run(Upstream(settings).turn({"query": "氣候"}))
        self.assertEqual(self.requests, [])

    def test_missing_or_invalid_ca_fails_before_serving(self):
        invalid = self.root / "invalid.pem"
        invalid.write_text("not a CA certificate")
        for ca in (self.root / "missing.pem", invalid, self.root):
            with self.subTest(ca=ca), self.assertRaises(OSError):
                Upstream(replace(self.settings, upstream_ca_file=ca))
        self.assertEqual(self.requests, [])
