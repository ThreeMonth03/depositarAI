"""Host-only fixtures; no socket, Docker, models, dependency installs or secrets.

The shared private probe's existing schema fixtures exercise strict turn
semantics without vendoring a second wire model. Production lazy imports the
real pinned Pydantic models; dependency absence is separately proven closed.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.request

from search.transport.http import HTTPResponse, UrllibTransport
from search.operations.deployment.gateway import probe
from search.operations.deployment.release import probe as release_probe
from tests.unit.deployment.release import test_probe as private


class GatewayHTTPTests(unittest.TestCase):
    def setUp(self):
        self.contract = private.WebControlFlowTests()
        self.contract.setUp()
        self.contract.token, self.contract.replacement = "T" * 43, "R" * 43

    def verify(self, responses=None, *, transport=None, clock=lambda: 1.0):
        fixture = (
            private.ScriptFixture(
                self.contract.responses(include_assets=True)
                if responses is None
                else responses
            )
            if transport is None
            else transport
        )
        output, errors = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(sys.modules, self.contract.modules),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            report = probe.verify_gateway_http(
                revision=private.REVISION,
                bundle_id=private.BUNDLE,
                manifest_sha256=private.MANIFEST,
                transport=fixture,
                clock=clock,
            )
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        encoded = json.dumps(report, allow_nan=False)
        for secret in (private.PRIVATE, self.contract.token, self.contract.replacement):
            self.assertNotIn(secret, encoded)
        self.assertNotIn("尋找", encoded)
        self.assertNotIn("http://", encoded)
        release_probe._web(
            report["website"], private.REVISION, private.BUNDLE, private.MANIFEST
        )
        return report, fixture

    def test_fixed_gateway_two_turns_refinement_and_reset_without_authority(self):
        report, fixture = self.verify()
        self.assertEqual(report["status"], "ready")
        self.assertTrue(report["gateway_http_verified"])
        self.assertEqual(
            report["website"],
            private.web_ready()
            | {
                "turns": [
                    {"version": 1, "cards": 1, "elapsed_ms": 0.0},
                    {"version": 2, "cards": 1, "elapsed_ms": 0.0},
                ]
            },
        )
        for field in (
            "model_warmup_verified",
            "readiness_verified",
            "confers_runtime_authority",
            "active_changed",
            "search_quality_verified",
        ):
            self.assertIs(report[field], False)
        self.assertIs(report["private_release_acceptance_required"], True)
        self.assertIs(report["gateway_runtime_validation_required"], True)
        self.assertEqual(len(fixture.calls), 10)
        for request, timeout, maximum in fixture.calls:
            self.assertTrue(request.url.startswith("http://127.0.0.1:18083/"))
            self.assertEqual(dict(request.headers)["Origin"], probe.ORIGIN)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 130)
            self.assertEqual(maximum, 256 * 1024)
            self.assertNotIn("readyz", request.url)
            self.assertNotIn("version", request.url)
        turns = [
            json.loads(row.body)
            for row, _, _ in fixture.calls
            if row.method == "POST" and row.url.endswith("/api/turn")
        ]
        self.assertEqual([row["version"] for row in turns], [0, 1, 2])
        self.assertEqual(turns[1]["query"], "CSV")

    def test_session_revision_drift_rejects_before_any_gpu_turn(self):
        responses = self.contract.responses(include_assets=True)[:3]
        responses[2][2]["revision"] = "f" * 40
        report, fixture = self.verify(responses)
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(report["website"]["website_ready"])
        self.assertFalse(report["website"]["two_turn_search_verified"])
        self.assertEqual(len(fixture.calls), 3)

    def test_missing_or_invalid_nginx_homepage_blocks_before_session_creation(self):
        for status, page in (
            (404, private.PRIVATE.encode()),
            (200, b'<div id="root"></div>'),
            (200, b"<title>depositar AI</title>"),
        ):
            with self.subTest(status=status, page=page):
                responses = self.contract.responses(include_assets=True)[:2]
                responses[1] = ("/", status, page)
                report, fixture = self.verify(responses)
                self.assertEqual(report["status"], "blocked")
                self.assertFalse(report["website"]["website_ready"])
                self.assertEqual(report["website"]["turns"], [])
                self.assertEqual(len(fixture.calls), 2)
                self.assertTrue(
                    all(request.method == "GET" for request, _, _ in fixture.calls)
                )

    def test_shared_private_probe_keeps_original_8000_target_and_settings_gate(self):
        fixture = private.ScriptFixture(self.contract.responses())
        report = private.execute(
            release_probe._WEB_SCRIPT, fixture, modules=self.contract.modules
        )
        self.assertEqual(report["code"], "ready")
        self.assertTrue(
            all(
                request.url.startswith("http://127.0.0.1:8000/")
                for request, _, _ in fixture.calls
            )
        )
        self.assertEqual(len(fixture.calls), 9)
        self.assertFalse(
            any(request.url.endswith("8000/") for request, _, _ in fixture.calls)
        )
        settings = self.contract.modules["search.web.upstream"].WebSettings
        settings.from_environment = lambda: SimpleNamespace(
            revision="f" * 40, origin=probe.ORIGIN
        )
        blocked = private.ScriptFixture([])
        report = private.execute(
            release_probe._WEB_SCRIPT, blocked, modules=self.contract.modules
        )
        self.assertEqual(report["code"], "web_probe_failed")
        self.assertEqual(blocked.calls, [])

    def test_typed_source_manifest_rerank_and_card_identity_drift_fail_closed(self):
        cases = (
            (lambda result: setattr(result, "revision", "f" * 40)),
            (lambda result: setattr(result.search, "revision", "f" * 40)),
            (
                lambda result: setattr(
                    result.search, "catalog_manifest_sha256", "f" * 64
                )
            ),
            (lambda result: setattr(result.search, "rerank_status", "fallback")),
            (lambda result: setattr(result.search.cards[0], "dataset_id", "private")),
            (
                lambda result: setattr(
                    result.search.cards[0], "url", "https://evil.example/private"
                )
            ),
        )
        for change in cases:
            with self.subTest(change=change):
                self.setUp()
                change(self.contract.results[0])
                responses = self.contract.responses(include_assets=True)[:4] + [
                    ("/api/session", 200, {"status": "cleared"})
                ]
                report, fixture = self.verify(responses)
                self.assertEqual(report["status"], "blocked")
                self.assertEqual(report["website"]["turns"], [])
                self.assertEqual(fixture.calls[-1][0].method, "DELETE")

    def test_refinement_must_keep_topic_and_apply_csv_matches(self):
        for path, value in (
            ("topic", "changed subject"),
            ("formats", []),
            ("condition", "unknown"),
        ):
            with self.subTest(path=path):
                self.setUp()
                result = self.contract.results[1]
                if path == "condition":
                    result.search.cards[0].condition_status = value
                else:
                    setattr(result.state, path, value)
                responses = self.contract.responses(include_assets=True)[:5] + [
                    ("/api/session", 200, {"status": "cleared"})
                ]
                report, _ = self.verify(responses)
                self.assertEqual(report["status"], "blocked")
                self.assertFalse(report["website"]["two_turn_search_verified"])
                self.assertEqual(len(report["website"]["turns"]), 2)

    def test_second_turn_failure_preserves_partial_counts_and_cleans(self):
        responses = self.contract.responses(include_assets=True)[:5]
        responses[4] = ("/api/turn", 503, {"error": private.PRIVATE})
        responses.append(("/api/session", 200, {"status": "cleared"}))
        report, fixture = self.verify(responses)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(len(report["website"]["turns"]), 1)
        self.assertEqual(fixture.calls[-1][0].method, "DELETE")

    def test_old_token_must_return_exact_410_and_new_token_must_differ(self):
        for change in ("old_status", "old_body", "replacement", "final_cleanup"):
            with self.subTest(change=change):
                self.setUp()
                responses = self.contract.responses(include_assets=True)
                if change == "old_status":
                    responses[6] = ("/api/turn", 200, {"error": private.PRIVATE})
                    responses = responses[:7] + responses[-1:]
                elif change == "old_body":
                    responses[6] = ("/api/turn", 410, {"error": {"code": "other"}})
                    responses = responses[:7] + responses[-1:]
                elif change == "replacement":
                    responses[7][2]["token"] = self.contract.token
                else:
                    responses[9] = ("/api/session", 503, {"error": private.PRIVATE})
                report, _ = self.verify(responses)
                self.assertEqual(report["status"], "blocked")
                self.assertFalse(report["website"]["session_reset_verified"])

    def test_invalid_json_duplicate_keys_nonfinite_and_oversized_bodies_reject(self):
        for body in (
            b'{"status":"ok","status":"ok"}',
            b'{"status":NaN}',
            private.PRIVATE.encode(),
            b"x" * (256 * 1024 + 1),
        ):
            with self.subTest(size=len(body)):
                report, fixture = self.verify([("/healthz", 200, body)])
                self.assertEqual(report["status"], "blocked")
                self.assertEqual(len(fixture.calls), 1)

    def test_redirected_response_url_or_http_status_reject_without_retry(self):
        class DriftTransport:
            def __init__(self, *, changed_url):
                self.calls = []
                self.changed_url = changed_url

            def send(self, request, **kwargs):
                self.calls.append((request, kwargs))
                return HTTPResponse(
                    200 if self.changed_url else 302,
                    (),
                    b'{"status":"ok"}',
                    "http://127.0.0.1:8000/healthz"
                    if self.changed_url
                    else request.url,
                )

        for changed_url in (True, False):
            with self.subTest(changed_url=changed_url):
                transport = DriftTransport(changed_url=changed_url)
                report, _ = self.verify(transport=transport)
                self.assertEqual(report["status"], "blocked")
                self.assertEqual(len(transport.calls), 1)

    def test_timeout_and_raw_exception_canary_never_escape(self):
        transport = SimpleNamespace(
            send=mock.Mock(side_effect=TimeoutError(private.PRIVATE))
        )
        report, _ = self.verify(transport=transport)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(transport.send.call_count, 1)

    def test_global_deadline_prevents_cleanup_request_and_cannot_pass(self):
        now = [0.0]
        fixture = private.ScriptFixture(self.contract.responses(include_assets=True))
        send = fixture.send

        def slow_send(request, **kwargs):
            value = send(request, **kwargs)
            if request.method == "POST" and request.url.endswith("/api/turn"):
                now[0] += 141
            return value

        fixture.send = slow_send
        report, _ = self.verify(transport=fixture, clock=lambda: now[0])
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(len(fixture.calls), 5)
        self.assertFalse(report["website"]["session_reset_verified"])
        # The second request sees only 139 seconds left, and its own 130 cap.
        self.assertEqual(fixture.calls[4][1], 130)

    def test_missing_runtime_dependencies_blocks_before_any_http(self):
        transport = private.ScriptFixture([])
        with mock.patch.dict(sys.modules, {"search.api.conversation.contract": None}):
            report = probe.verify_gateway_http(
                revision=private.REVISION,
                bundle_id=private.BUNDLE,
                manifest_sha256=private.MANIFEST,
                transport=transport,
            )
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["code"], "gateway_probe_dependencies_unavailable")
        self.assertEqual(transport.calls, [])

    def test_invalid_identity_or_initial_clock_reject_before_any_http(self):
        for field, value in (
            ("revision", "main"),
            ("bundle_id", "x"),
            ("manifest_sha256", True),
        ):
            values = {
                "revision": private.REVISION,
                "bundle_id": private.BUNDLE,
                "manifest_sha256": private.MANIFEST,
            }
            values[field] = value
            with self.subTest(field=field), self.assertRaises(probe.GatewayProbeError):
                probe.verify_gateway_http(**values)
        for value in (True, float("nan"), float("inf"), "1", 10**1000):
            with self.subTest(value=value), self.assertRaises(probe.GatewayProbeError):
                self.verify(clock=lambda: value)
        with self.assertRaises(probe.GatewayProbeError) as caught:
            self.verify(clock=mock.Mock(side_effect=RuntimeError(private.PRIVATE)))
        self.assertEqual(str(caught.exception), "gateway_probe_clock_invalid")

    def test_regressive_clock_fails_closed_after_response(self):
        values = iter((2.0, 3.0, 2.5, 3.0))
        report, fixture = self.verify(clock=lambda: next(values))
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(len(fixture.calls), 1)


class NativeTransportTests(unittest.TestCase):
    """Exercise the production urllib transport using a fake local opener."""

    def test_proxy_environment_is_ignored_and_redirect_handler_never_follows(self):
        with mock.patch.dict(
            os.environ,
            {
                "http_proxy": "http://secret.proxy.invalid:9999",
                "HTTPS_PROXY": "http://secret.proxy.invalid:9999",
            },
        ):
            transport = UrllibTransport()
        self.assertFalse(
            any(
                isinstance(handler, urllib.request.ProxyHandler)
                for handler in transport._opener.handlers
            )
        )
        redirects = [
            handler
            for handler in transport._opener.handlers
            if isinstance(handler, urllib.request.HTTPRedirectHandler)
        ]
        self.assertEqual(len(redirects), 1)
        self.assertIsNone(
            redirects[0].redirect_request(
                None, None, 302, "", {}, "http://evil.invalid/"
            )
        )

    def test_native_transport_body_cap_and_slow_read_deadline_block_host(self):
        for slow in (False, True):
            with self.subTest(slow=slow):
                fixture = private.WebControlFlowTests()
                fixture.setUp()
                body = bytearray(b"x" * (256 * 1024 + 1))
                caps = []

                def read1(cap):
                    caps.append(cap)
                    part = bytes(body[:cap])
                    del body[:cap]
                    return part

                response = SimpleNamespace(
                    code=200,
                    headers={},
                    isclosed=lambda: False,
                    fp=SimpleNamespace(
                        raw=SimpleNamespace(
                            _sock=SimpleNamespace(settimeout=mock.Mock())
                        )
                    ),
                    read1=read1,
                    geturl=lambda: probe.ORIGIN + "/healthz",
                    close=mock.Mock(),
                )
                transport = UrllibTransport()
                transport._opener = SimpleNamespace(
                    open=mock.Mock(return_value=response)
                )
                native_clock = mock.Mock(side_effect=[0, 0, 6]) if slow else lambda: 0
                with (
                    mock.patch.dict(sys.modules, fixture.modules),
                    mock.patch("search.transport.http.time.monotonic", native_clock),
                ):
                    report = probe.verify_gateway_http(
                        revision=private.REVISION,
                        bundle_id=private.BUNDLE,
                        manifest_sha256=private.MANIFEST,
                        transport=transport,
                        clock=lambda: 0,
                    )
                self.assertEqual(report["status"], "blocked")
                transport._opener.open.assert_called_once()
                response.close.assert_called_once()
                self.assertTrue(all(0 < cap <= 64 * 1024 for cap in caps))
                if slow:
                    self.assertEqual(len(caps), 1)
                else:
                    self.assertEqual(caps[-1], 1)

    def test_default_host_uses_real_transport_fixed_url_and_native_bounded_reads(self):
        fixture = private.WebControlFlowTests()
        fixture.setUp()
        requests, responses = [], fixture.responses(include_assets=True)
        sockets = []

        class Response:
            def __init__(self, status, body, url):
                self.code, self._body, self.url = status, body, url
                self.headers, self.closed = {}, False
                socket = SimpleNamespace(settimeout=mock.Mock())
                sockets.append(socket)
                self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=socket))

            def isclosed(self):
                return self.closed

            def read1(self, cap):
                self._body, value = self._body[cap:], self._body[:cap]
                return value

            def geturl(self):
                return self.url

            def close(self):
                self.closed = True

        def open_request(request, *, timeout):
            requests.append((request, timeout))
            expected, status, body = responses.pop(0)
            self.assertEqual(request.full_url, probe.ORIGIN + expected)
            self.assertEqual(request.get_header("Origin"), probe.ORIGIN)
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            return Response(status, raw, request.full_url)

        with (
            mock.patch.dict(sys.modules, fixture.modules),
            mock.patch("urllib.request.OpenerDirector.open", side_effect=open_request),
        ):
            report = probe.verify_gateway_http(
                revision=private.REVISION,
                bundle_id=private.BUNDLE,
                manifest_sha256=private.MANIFEST,
            )
        self.assertEqual(report["status"], "ready")
        self.assertEqual(len(requests), 10)
        self.assertTrue(all(socket.settimeout.called for socket in sockets))
        self.assertTrue(all(0 < timeout <= 130 for _, timeout in requests))


if __name__ == "__main__":
    unittest.main()
