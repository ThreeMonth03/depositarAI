"""Exercise the image-owned gateway SCRIPT with bounded offline HTTP fixtures."""

from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from search.transport.http import HTTPResponse
from search.operations.deployment.gateway import probe_runtime as probe
from search.operations.deployment.release import probe as release_probe
from tests.unit.deployment.release import test_probe as private


class GatewayScriptTests(unittest.TestCase):
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
        errors = io.StringIO()
        with (
            mock.patch.object(probe.time, "monotonic", side_effect=clock),
            redirect_stderr(errors),
        ):
            report = private.execute(
                probe.SCRIPT, fixture, modules=self.contract.modules
            )
        self.assertEqual(errors.getvalue(), "")
        encoded = json.dumps(report, allow_nan=False)
        for secret in (private.PRIVATE, self.contract.token, self.contract.replacement):
            self.assertNotIn(secret, encoded)
        self.assertNotIn("尋找", encoded)
        self.assertNotIn("http://", encoded)
        release_probe._web(report, private.REVISION, private.BUNDLE, private.MANIFEST)
        return report, fixture

    def test_two_turns_refinement_and_reset_use_fixed_gateway_without_ops_import(self):
        report, fixture = self.verify()
        self.assertEqual(report["code"], "ready")
        self.assertEqual(
            report,
            private.web_ready()
            | {
                "turns": [
                    {"version": 1, "cards": 1, "elapsed_ms": 0.0},
                    {"version": 2, "cards": 1, "elapsed_ms": 0.0},
                ]
            },
        )
        self.assertEqual(len(fixture.calls), 10)
        self.assertNotIn("search.operations", probe.SCRIPT)
        self.assertIn(release_probe._WEB_ACCEPTANCE_SCRIPT, probe.SCRIPT)
        self.assertIn(release_probe._STATIC_ACCEPTANCE_SCRIPT, probe.SCRIPT)
        self.assertTrue(probe.SCRIPT.startswith(release_probe._COMMON_SCRIPT))
        for request, timeout, maximum in fixture.calls:
            self.assertTrue(request.url.startswith("http://127.0.0.1:18083/"))
            self.assertEqual(dict(request.headers)["Origin"], "http://127.0.0.1:18083")
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
        self.assertEqual(report["code"], "web_probe_failed")
        self.assertTrue(report["website_ready"])
        self.assertFalse(report["two_turn_search_verified"])
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
                self.assertEqual(report["code"], "web_probe_failed")
                self.assertFalse(report["website_ready"])
                self.assertEqual(report["turns"], [])
                self.assertEqual(len(fixture.calls), 2)
                self.assertTrue(
                    all(request.method == "GET" for request, _, _ in fixture.calls)
                )

    def test_second_turn_failure_preserves_partial_counts_and_cleans(self):
        responses = self.contract.responses(include_assets=True)[:5]
        responses[4] = ("/api/turn", 503, {"error": private.PRIVATE})
        responses.append(("/api/session", 200, {"status": "cleared"}))
        report, fixture = self.verify(responses)
        self.assertEqual(report["code"], "web_probe_failed")
        self.assertEqual(len(report["turns"]), 1)
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
                self.assertEqual(report["code"], "web_probe_failed")
                self.assertFalse(report["session_reset_verified"])

    def test_invalid_json_duplicate_keys_nonfinite_and_oversized_bodies_reject(self):
        for body in (
            b'{"status":"ok","status":"ok"}',
            b'{"status":NaN}',
            private.PRIVATE.encode(),
            b"x" * (256 * 1024 + 1),
        ):
            with self.subTest(size=len(body)):
                report, fixture = self.verify([("/healthz", 200, body)])
                self.assertEqual(report["code"], "web_probe_failed")
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
                self.assertEqual(report["code"], "web_probe_failed")
                self.assertEqual(len(transport.calls), 1)

    def test_timeout_and_raw_exception_canary_never_escape(self):
        transport = SimpleNamespace(
            send=mock.Mock(side_effect=TimeoutError(private.PRIVATE))
        )
        report, _ = self.verify(transport=transport)
        self.assertEqual(report["code"], "web_probe_failed")
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
        self.assertEqual(report["code"], "web_probe_failed")
        self.assertEqual(len(fixture.calls), 5)
        self.assertFalse(report["session_reset_verified"])
        # The second request sees only 139 seconds left, and its own 130 cap.
        self.assertEqual(fixture.calls[4][1], 130)


if __name__ == "__main__":
    unittest.main()
