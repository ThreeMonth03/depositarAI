"""CPU contract oracles for the disposable public-entry fixture."""

from contextlib import ExitStack, redirect_stdout
import ast
import copy
from http.cookiejar import Cookie, MozillaCookieJar
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from search.tests.public_entry import fixture


class Response(io.BytesIO):
    def __init__(self, raw, status=200):
        super().__init__(raw)
        self.status = status
        self.headers = {}
        self.bounds = []

    def read(self, size=-1):
        self.bounds.append(size)
        return super().read(size)


class FixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-fixture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def provider(self):
        events = []
        servers = []

        def server(address, handler):
            events.append(("listener", address))
            original_socket = object()
            value = SimpleNamespace(
                handler=handler,
                socket=original_socket,
                original_socket=original_socket,
                serve_forever=lambda: events.append(("serve", address)),
            )
            servers.append(value)
            return value

        thread = mock.Mock()
        thread.start.side_effect = lambda: events.append("thread-start")
        tls = mock.Mock()
        tls.load_cert_chain.side_effect = lambda *a: events.append(("cert", a))
        tls.wrap_socket.side_effect = (
            lambda *a, **k: events.append(("wrap", a, k)) or "wrapped"
        )
        with (
            mock.patch.object(
                fixture.subprocess,
                "run",
                return_value=SimpleNamespace(stdout="Modulus=010203\n"),
            ) as signing,
            mock.patch.object(
                fixture, "ThreadingHTTPServer", side_effect=server
            ),
            mock.patch.object(
                fixture.threading, "Thread", return_value=thread
            ) as threads,
            mock.patch.object(fixture.ssl, "SSLContext", return_value=tls),
        ):
            fixture.serve(self.root)
        self.assertEqual(signing.call_args.kwargs["timeout"], 5)
        self.assertEqual(signing.call_args.args[0][0:2], ["openssl", "rsa"])
        threads.assert_called_once_with(
            target=servers[0].serve_forever, daemon=True
        )
        self.assertIs(servers[0].handler, servers[1].handler)
        self.assertEqual(
            events,
            [
                ("listener", ("0.0.0.0", 8000)),
                "thread-start",
                ("listener", ("0.0.0.0", 9443)),
                ("cert", (self.root / "tls.crt", self.root / "tls.key")),
                ("wrap", (servers[1].original_socket,), {"server_side": True}),
                ("serve", ("0.0.0.0", 9443)),
            ],
        )
        self.assertEqual(servers[1].socket, "wrapped")
        return servers[0].handler

    def request(self, handler, method, path, headers=None, raw=b""):
        value = object.__new__(handler)
        value.path = path
        value.headers = dict(headers or {})
        value.rfile = Response(raw)
        value.reply = mock.Mock(side_effect=lambda *args: args)
        result = getattr(value, "do_" + method)()
        return result, value

    def authorize(self, handler, account="alice"):
        query = urlencode(
            {
                "client_id": fixture.CLIENT_ID,
                "redirect_uri": fixture.ORIGIN + "/auth/callback",
                "nonce": "expected-nonce",
                "state": "expected-state",
            }
        )
        result, _ = self.request(
            handler,
            "GET",
            "/authorize?" + query,
            {"Cookie": "fixture_account=" + account},
        )
        self.assertEqual(result[0:2], (302, {}))
        parsed = parse_qs(urlsplit(result[2][0][1]).query)
        self.assertEqual(parsed["state"], ["expected-state"])
        return parsed["code"][0]

    def token(self, handler, code, **options):
        body = urlencode(
            {
                "code": code,
                "client_id": fixture.CLIENT_ID,
                "client_secret": fixture.CLIENT_SECRET,
                **options,
            }
        ).encode()
        return self.request(
            handler, "POST", "/token", {"Content-Length": str(len(body))}, body
        )[0]

    def test_http_routes_keep_shared_state_and_protocol_projection(self):
        handler = self.provider()
        result, _ = self.request(handler, "GET", "/v1/version?observed=1")
        self.assertEqual(result, (401, {"error": "invalid_api_key"}))
        result, _ = self.request(handler, "GET", "/__version_calls?read=1")
        self.assertEqual(
            result,
            (
                200,
                {
                    "requests": [
                        {"method": "GET", "path": "/v1/version", "status": 401}
                    ]
                },
            ),
        )
        auth = {"Authorization": "Bearer " + fixture.API_KEY}
        result, _ = self.request(handler, "GET", "/v1/version", auth)
        self.assertEqual(
            result,
            (
                200,
                {
                    "revision": "a" * 40,
                    "catalog_manifest_sha256": "b" * 64,
                    "conversation_contract_version": 4,
                },
            ),
        )
        (self.root / "backend-version.json").write_text('{"override":true}')
        result, _ = self.request(handler, "GET", "/v1/version", auth)
        self.assertEqual(result, (200, {"override": True}))
        result, _ = self.request(handler, "GET", "/jwks?read=1")
        self.assertEqual(
            result,
            (
                200,
                {
                    "keys": [
                        {
                            "kty": "RSA",
                            "use": "sig",
                            "kid": "fixture",
                            "alg": "RS256",
                            "n": "AQID",
                            "e": "AQAB",
                        }
                    ]
                },
            ),
        )
        result, _ = self.request(
            handler, "GET", "/.well-known/openid-configuration"
        )
        self.assertEqual(result[1]["issuer"], fixture.ISSUER)
        self.assertEqual(
            result[1]["token_endpoint"], fixture.ISSUER + "/token"
        )
        self.assertEqual(result[1]["response_types_supported"], ["code"])
        claims = {
            "alice": (fixture.ISSUER, fixture.CLIENT_ID, "expected-nonce"),
            "wrong-issuer": (
                "https://untrusted-issuer.example",
                fixture.CLIENT_ID,
                "expected-nonce",
            ),
            "wrong-audience": (
                fixture.ISSUER,
                "unrelated-client",
                "expected-nonce",
            ),
            "wrong-nonce": (
                fixture.ISSUER,
                fixture.CLIENT_ID,
                "unrelated-authorization-request",
            ),
            "number-audience": (fixture.ISSUER, 42, "expected-nonce"),
            "array-audience": (
                fixture.ISSUER,
                [fixture.CLIENT_ID, "another-client"],
                "expected-nonce",
            ),
            "forged": (fixture.ISSUER, fixture.CLIENT_ID, "expected-nonce"),
        }
        for account, expected in claims.items():
            with (
                self.subTest(account=account),
                mock.patch.object(fixture.time, "time", return_value=100),
                mock.patch.object(
                    fixture.secrets,
                    "token_urlsafe",
                    side_effect=["code", "access"],
                ),
                mock.patch.object(
                    fixture, "signed_token", return_value="signed"
                ) as signed,
            ):
                code = self.authorize(handler, account)
                result = self.token(handler, code)
                self.assertEqual(
                    result,
                    (
                        200,
                        {
                            "access_token": "access",
                            "token_type": "Bearer",
                            "expires_in": 600,
                            "scope": "openid profile email",
                            "id_token": "signed",
                        },
                    ),
                )
                actual, key = signed.call_args.args
                self.assertEqual(
                    (actual["iss"], actual["aud"], actual["nonce"]), expected
                )
                self.assertEqual(
                    (actual["iat"], actual["exp"], actual["auth_time"]),
                    (100, 700, 100),
                )
                self.assertEqual(actual["sub"], account)
                self.assertEqual(
                    key,
                    self.root
                    / ("attacker.key" if account == "forged" else "idp.key"),
                )
                result, _ = self.request(
                    handler,
                    "GET",
                    "/userinfo?read=1",
                    {"Authorization": "Bearer access"},
                )
                self.assertEqual(
                    result,
                    (
                        200,
                        {
                            "iss": fixture.ISSUER,
                            "sub": account,
                            "email": account + "@example.test",
                            "email_verified": True,
                        },
                    ),
                )
        self.assertEqual(
            self.request(handler, "POST", "/v1/turn")[0],
            (401, {"error": "invalid_api_key"}),
        )
        result, _ = self.request(handler, "POST", "/v1/turn", auth)
        self.assertEqual(result[0], 200)
        self.assertEqual(
            result[1]["state"],
            {"topic": "fixture", "time": None, "formats": []},
        )
        self.assertEqual(
            self.request(handler, "GET", "/__calls")[0], (200, {"calls": 1})
        )
        for method, path in [
            ("GET", "/missing"),
            ("POST", "/token?x=1"),
            ("POST", "/v1/turn?x=1"),
        ]:
            self.assertEqual(self.request(handler, method, path)[0], (404, {}))

    def test_post_admission_consumes_code_and_saves_access_before_errors(self):
        handler = self.provider()
        for length, expected in [("4097", (413, {})), ("bad", ValueError)]:
            with self.subTest(length=length):
                value = object.__new__(handler)
                value.path = "/token"
                value.headers = {"Content-Length": length}
                value.rfile = mock.Mock()
                value.reply = mock.Mock(side_effect=lambda *args: args)
                if expected is ValueError:
                    with self.assertRaises(ValueError):
                        value.do_POST()
                else:
                    self.assertEqual(value.do_POST(), expected)
                value.rfile.read.assert_not_called()
        with mock.patch.object(
            fixture.secrets, "token_urlsafe", return_value="code"
        ):
            code = self.authorize(handler)
        self.assertEqual(
            self.token(handler, code, client_secret="wrong"),
            (400, {"error": "invalid_grant"}),
        )
        with mock.patch.object(fixture, "signed_token", return_value="signed"):
            self.assertEqual(
                self.token(handler, code), (400, {"error": "invalid_grant"})
            )
        # A client-header failure must occur before consuming a valid code.
        with mock.patch.object(
            fixture.secrets, "token_urlsafe", return_value="retained-code"
        ):
            code = self.authorize(handler)
        body = urlencode({"code": code}).encode()
        headers = mock.Mock()
        headers.get.side_effect = [str(len(body)), RuntimeError("client")]
        value = object.__new__(handler)
        value.path, value.headers, value.rfile = (
            "/token",
            headers,
            Response(body),
        )
        value.reply = mock.Mock(side_effect=lambda *args: args)
        with self.assertRaisesRegex(RuntimeError, "client"):
            value.do_POST()
        with mock.patch.object(fixture, "signed_token", return_value="signed"):
            self.assertEqual(self.token(handler, code)[0], 200)
        with (
            mock.patch.object(
                fixture.secrets, "token_urlsafe", side_effect=["new", "saved"]
            ),
            mock.patch.object(
                fixture, "signed_token", side_effect=RuntimeError("signing")
            ),
        ):
            code = self.authorize(handler)
            with self.assertRaisesRegex(RuntimeError, "signing"):
                self.token(handler, code)
        result, _ = self.request(
            handler, "GET", "/userinfo", {"Authorization": "Bearer saved"}
        )
        self.assertEqual(result[1]["sub"], "alice")

    def test_burst_snapshots_precede_workers_and_keep_bounds_and_projection(
        self,
    ):
        loaded, jars, outgoing, responses, events = [], [], [], [], []

        def jar_factory(filename=None):
            jar = MozillaCookieJar(filename)
            cookie = Cookie(
                0,
                "session",
                "private-cookie",
                None,
                False,
                "public.test",
                False,
                False,
                "/",
                True,
                True,
                None,
                True,
                None,
                None,
                {},
                False,
            )
            jar.load = mock.Mock(
                side_effect=lambda **_k: (
                    loaded.append(filename),
                    jar.set_cookie(cookie),
                )
            )
            jar.save = mock.Mock(side_effect=AssertionError("worker write"))
            jars.append(jar)
            return jar

        barrier = mock.Mock()
        barrier.wait.side_effect = lambda **k: events.append(("barrier", k))

        def opener_factory(*_handlers):
            opener = mock.Mock()

            def opened(request, timeout):
                self.assertEqual(len(loaded), 2)
                events.append(("open", timeout))
                outgoing.append(request)
                raw = [b'{"error":{"code":"quota"}}', b"\xff", b"[]"][
                    len(outgoing) - 1
                ]
                response = Response(raw, 429 if len(outgoing) == 1 else 200)
                responses.append(response)
                if response.status == 429:
                    raise fixture.HTTPError(
                        request.full_url, 429, "fixture", {}, response
                    )
                return response

            opener.open.side_effect = opened
            return opener

        workers = mock.MagicMock()
        workers.__enter__.return_value = workers
        workers.map.side_effect = lambda fn, items: list(map(fn, items))
        payload = {
            "action": "turn",
            "requests": [
                {"account": "alice", "payload": {"i": 0}},
                {"account": "bob", "payload": {"i": 1}},
                {"account": "alice", "payload": {"i": 2}},
            ],
        }
        with (
            mock.patch.object(
                fixture, "MozillaCookieJar", side_effect=jar_factory
            ),
            mock.patch.object(
                fixture.Path,
                "read_text",
                return_value='{"token":"private-token"}',
            ),
            mock.patch.object(fixture.ssl, "create_default_context"),
            mock.patch.object(fixture, "HTTPSHandler"),
            mock.patch.object(
                fixture, "build_opener", side_effect=opener_factory
            ),
            mock.patch.object(
                fixture.threading, "Barrier", return_value=barrier
            ) as barriers,
            mock.patch.object(
                fixture, "ThreadPoolExecutor", return_value=workers
            ) as pool,
        ):
            result = fixture.burst(self.root, payload)
            self.assertEqual(
                result,
                {
                    "results": [
                        {"account": "alice", "status": 429, "code": "quota"},
                        {"account": "bob", "status": 200, "code": None},
                        {"account": "alice", "status": 200, "code": None},
                    ]
                },
            )
            barriers.assert_called_once_with(3)
            pool.assert_called_once_with(max_workers=3)
            self.assertIs(workers.map.call_args.args[1], payload["requests"])
            self.assertEqual(
                loaded, ["/tmp/alice.cookies", "/tmp/bob.cookies"]
            )
            self.assertEqual(
                events, [("barrier", {"timeout": 5}), ("open", 10)] * 3
            )
            self.assertEqual([r.bounds for r in responses], [[16384]] * 3)
            self.assertEqual(
                [json.loads(r.data) for r in outgoing],
                [{"i": 0}, {"i": 1}, {"i": 2}],
            )
            self.assertEqual(
                [r.full_url for r in outgoing],
                [fixture.ORIGIN + "/api/turn"] * 3,
            )
            self.assertEqual(
                [r.get_header("Authorization") for r in outgoing],
                ["Bearer private-token"] * 3,
            )
            self.assertEqual(len(jars), 5)
            self.assertIsNot(next(iter(jars[0])), next(iter(jars[2])))
            self.assertEqual([c.value for c in jars[2]], ["private-cookie"])
            loaded.clear()
            for count in (0, 13):
                with self.assertRaisesRegex(ValueError, "one through twelve"):
                    fixture.burst(
                        self.root, {"action": "turn", "requests": [{}] * count}
                    )
            self.assertEqual(loaded, [])
            with self.assertRaisesRegex(ValueError, "invalid bounded burst"):
                fixture.burst(
                    self.root,
                    {
                        "action": "turn",
                        "requests": [payload["requests"][0], {}],
                    },
                )
            self.assertEqual(loaded, ["/tmp/alice.cookies"])
            self.assertNotIn("private-", json.dumps(result))

    def test_probe_persistence_redaction_and_request_shaping(self):
        cookie = Cookie(
            0,
            "fixture",
            "private-cookie",
            None,
            False,
            "public.test",
            False,
            False,
            "/",
            True,
            True,
            None,
            True,
            None,
            None,
            {"HttpOnly": None, "SameSite": "Lax"},
            False,
        )
        other = copy.copy(cookie)
        other.domain = "oidc.test"
        cases = [
            ("session", {}, "/api/session", "POST", None, 200),
            ("session", {}, "/api/session", "POST", None, 503),
            (
                "turn",
                {"query": "fixture"},
                "/api/turn",
                "POST",
                "Bearer owned",
                200,
            ),
            ("health", None, "/healthz", "GET", None, 200),
            ("access", None, "/api/access", "GET", None, 200),
            (
                "counter",
                None,
                "http://conversation-api:8000/__calls",
                "GET",
                None,
                200,
            ),
            ("login", None, "/auth/login", "GET", None, 200),
            ("session-status", {}, "/api/session", "GET", "Bearer owned", 200),
            (
                "session-withdraw",
                {},
                "/api/session",
                "PATCH",
                "Bearer owned",
                200,
            ),
            (
                "session-delete",
                {},
                "/api/session",
                "DELETE",
                "Bearer owned",
                200,
            ),
            (
                "path",
                {"path": "/api/turn?test=1", "method": "DELETE"},
                "/api/turn?test=1",
                "DELETE",
                "Bearer owned",
                200,
            ),
        ]
        for action, payload, suffix, method, authorization, status in cases:
            with (
                self.subTest(action=action, status=status),
                ExitStack() as stack,
            ):
                events = []
                jar = mock.Mock(filename="/tmp/alice.cookies")
                jar.__iter__ = mock.Mock(
                    side_effect=lambda: events.append("flags")
                    or iter([cookie, other])
                )
                jar.save.side_effect = lambda **_k: events.append("jar-save")
                response = Response(
                    b'{"token":"private-token","answer":"ok"}', status
                )
                opener = mock.Mock()
                opener.open.return_value = response
                if status == 503:
                    opener.open.side_effect = fixture.HTTPError(
                        fixture.ORIGIN, 503, "fixture", {}, response
                    )
                stack.enter_context(
                    mock.patch.object(
                        fixture, "MozillaCookieJar", return_value=jar
                    )
                )
                stack.enter_context(
                    mock.patch.object(fixture.ssl, "create_default_context")
                )
                stack.enter_context(mock.patch.object(fixture, "HTTPSHandler"))
                stack.enter_context(
                    mock.patch.object(
                        fixture, "build_opener", return_value=opener
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        fixture.Path, "exists", return_value=False
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        fixture.Path,
                        "read_text",
                        return_value='{"token":"owned"}',
                    )
                )
                write = stack.enter_context(
                    mock.patch.object(
                        fixture.Path,
                        "write_text",
                        side_effect=lambda raw: events.append(
                            ("session-save", json.loads(raw))
                        ),
                    )
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    fixture.probe(action, "alice", self.root, payload)
                self.assertEqual(
                    write.call_count,
                    int(action == "session" and status == 200),
                )
                self.assertEqual(events[-2:], ["jar-save", "flags"])
                self.assertEqual(
                    events[:-2],
                    [
                        (
                            "session-save",
                            {"token": "private-token", "answer": "ok"},
                        )
                    ]
                    if action == "session" and status == 200
                    else [],
                )
                actual = json.loads(output.getvalue())
                self.assertEqual(
                    actual,
                    {
                        "status": status,
                        "value": {"answer": "ok"},
                        "cookies": [
                            {
                                "name": "fixture",
                                "secure": True,
                                "path": "/",
                                "domain_specified": False,
                                "http_only": True,
                                "same_site": "Lax",
                            }
                        ],
                    },
                )
                request = opener.open.call_args.args[0]
                self.assertEqual(
                    request.full_url,
                    suffix
                    if suffix.startswith("http:")
                    else fixture.ORIGIN + suffix,
                )
                self.assertEqual(request.method, method)
                self.assertEqual(
                    request.get_header("Authorization"), authorization
                )
                self.assertEqual(opener.open.call_args.kwargs, {"timeout": 10})
                self.assertEqual(response.bounds, [16384])

    def test_challenge_budget_bounds_and_burst_early_return(self):
        output = io.StringIO()
        with (
            mock.patch.object(
                fixture, "burst", return_value={"results": []}
            ) as burst,
            mock.patch.object(fixture, "Path") as path,
            redirect_stdout(output),
        ):
            fixture.probe("burst", "alice", "fixture", {"action": "session"})
        burst.assert_called_once_with("fixture", {"action": "session"})
        path.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {"results": []})
        challenge = {
            "challenge": {"id": "test", "randomData": "fixture"},
            "rules": {"difficulty": 1},
        }
        response = Response(
            (
                '<script id="anubis_challenge">'
                + json.dumps(challenge)
                + "</script>"
            ).encode(),
            403,
        )
        jar, opener = mock.Mock(), mock.Mock()
        jar.filename = "/tmp/alice.cookies"
        opener.open.return_value = response
        with (
            mock.patch.object(fixture, "MozillaCookieJar", return_value=jar),
            mock.patch.object(fixture.Path, "exists", return_value=False),
            mock.patch.object(fixture.ssl, "create_default_context"),
            mock.patch.object(fixture, "HTTPSHandler"),
            mock.patch.object(fixture, "build_opener", return_value=opener),
            mock.patch.object(
                fixture, "range", create=True, return_value=range(2)
            ) as nonce_range,
            mock.patch.object(
                fixture.hashlib,
                "sha256",
                return_value=SimpleNamespace(hexdigest=lambda: "f" * 64),
            ),
            self.assertRaisesRegex(
                ValueError, "fixture challenge budget exceeded"
            ),
        ):
            fixture.probe("verify", "alice", self.root)
        nonce_range.assert_called_once_with(1000000)
        self.assertEqual(response.bounds, [128 * 1024])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Accept-encoding"), "gzip")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 10})
        jar.save.assert_not_called()

    def test_namespace_and_cli_use_only_the_new_component(self):
        root = next(
            p
            for p in Path(__file__).resolve().parents
            if (p / "AGENTS.md").is_file()
        )
        self.assertTrue(
            (root / "search/tests/public_entry/fixture.py").is_file()
        )
        self.assertFalse(
            (root / "search/tests/public_entry_fixture.py").exists()
        )
        initializer = root / "search/tests/public_entry/__init__.py"
        self.assertTrue(initializer.is_file())
        body = ast.parse(initializer.read_text()).body
        self.assertEqual(len(body), 1)
        self.assertIsInstance(body[0], ast.Expr)
        self.assertIsInstance(body[0].value, ast.Constant)
        self.assertIsInstance(body[0].value.value, str)
        child = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "search.tests.public_entry.fixture",
                "--help",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn("{serve,probe}", child.stdout)
        self.assertIn("--directory", child.stdout)
        self.assertIn("--payload", child.stdout)


if __name__ == "__main__":
    unittest.main()
