"""Shared HTTP deadlines, bounded reads, redirects and response cleanup."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error

from search.transport import http
from search.transport.http import HTTPRequest, UrllibTransport
from search.tests.fixtures import ClosingResponse, StaticOpener


class SharedHTTPTransportTests(unittest.TestCase):
    def setUp(self):
        self.request = http.HTTPRequest("GET", "http://127.0.0.1:8000/healthz", ())

    def response(self, body=b"{}"):
        return SimpleNamespace(
            code=200,
            headers={"Content-Type": "application/json"},
            fp=SimpleNamespace(raw=SimpleNamespace(_sock=mock.Mock())),
            read1=mock.Mock(side_effect=[body, b""]),
            geturl=lambda: self.request.url,
            close=mock.Mock(),
        )

    def transport(self, response):
        transport = http.UrllibTransport()
        transport._opener = SimpleNamespace(open=mock.Mock(return_value=response))
        return transport

    def test_invalid_bounds_fail_before_opening_a_connection(self):
        transport = self.transport(self.response())
        for timeout, cap in (
            (0, 1),
            (-1, 1),
            (float("nan"), 1),
            (float("inf"), 1),
            (1, 0),
        ):
            with self.subTest(timeout=timeout, cap=cap):
                with self.assertRaisesRegex(OSError, "^invalid transport bound$"):
                    transport.send(self.request, timeout=timeout, max_bytes=cap)
        transport._opener.open.assert_not_called()

    def test_connection_error_retains_the_sanitized_diagnostic(self):
        transport = self.transport(self.response())
        transport._opener.open.side_effect = RuntimeError("private-upstream-sentinel")
        with self.assertRaisesRegex(OSError, "^Ollama transport failed$") as caught:
            transport.send(self.request, timeout=1, max_bytes=64)
        self.assertTrue(caught.exception.__suppress_context__)
        transport._opener.open.assert_called_once()

    def test_absolute_deadline_expiration_closes_the_opened_response(self):
        response = self.response()
        transport = self.transport(response)
        with (
            mock.patch.object(http.time, "monotonic", side_effect=[10, 11]),
            self.assertRaisesRegex(TimeoutError, "^Ollama transport deadline expired$"),
        ):
            transport.send(self.request, timeout=1, max_bytes=64)
        response.read1.assert_not_called()
        response.close.assert_called_once()

    def test_unbounded_body_readers_are_rejected_and_closed(self):
        for missing in ("socket", "read1"):
            response = self.response()
            if missing == "socket":
                response.fp.raw._sock = None
                expected = "cannot enforce Ollama transport deadline"
            else:
                response.read1 = None
                expected = "cannot enforce bounded Ollama body reads"
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(OSError, "^" + expected + "$"):
                    self.transport(response).send(self.request, timeout=1, max_bytes=64)
                response.close.assert_called_once()

    def test_over_cap_response_keeps_the_bound_and_closes(self):
        response = self.response(b"xx")
        with self.assertRaisesRegex(OSError, "^Ollama response exceeded its cap$"):
            self.transport(response).send(self.request, timeout=1, max_bytes=1)
        response.read1.assert_called_once_with(2)
        response.close.assert_called_once()

    def test_close_error_does_not_replace_a_successful_response(self):
        response = self.response()
        response.close.side_effect = RuntimeError("private-close-sentinel")
        result = self.transport(response).send(self.request, timeout=1, max_bytes=64)
        self.assertEqual(
            result,
            http.HTTPResponse(
                200, tuple(response.headers.items()), b"{}", self.request.url
            ),
        )
        response.close.assert_called_once()


class UrllibTransportTests(unittest.TestCase):
    def loopback(self, status, body, *, drip=False, location=None):
        """Use real urllib HTTPError/socket wrappers, never a model or service."""
        stop = threading.Event()
        paths = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                paths.append(self.path)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Transport-Fixture", "loopback")
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                try:
                    if drip:
                        for value in body:
                            self.wfile.write(bytes([value]))
                            self.wfile.flush()
                            if stop.wait(0.04):
                                break
                    else:
                        self.wfile.write(body)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
        )
        thread.start()

        def cleanup():
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

        self.addCleanup(cleanup)
        transport = UrllibTransport()
        native_opener = transport._opener
        errors = []

        def open_request(request, *, timeout):
            try:
                return native_opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                errors.append(error)
                raise

        transport._opener = SimpleNamespace(open=open_request)
        url = f"http://127.0.0.1:{server.server_port}/expired"
        return transport, HTTPRequest("GET", url, ()), errors, paths

    def test_real_http_error_preserves_status_body_headers_url_and_closes(self):
        for body in (b'{"error":{"code":"session_expired"}}', b""):
            with self.subTest(body=body):
                transport, request, errors, _ = self.loopback(410, body)
                result = transport.send(request, timeout=1, max_bytes=1024)
                self.assertEqual(result.status, 410)
                self.assertEqual(result.body, body)
                self.assertEqual(result.url, request.url)
                self.assertIn(("X-Transport-Fixture", "loopback"), result.headers)
                self.assertEqual(len(errors), 1)
                self.assertTrue(errors[0].closed)

    def test_real_http_error_over_cap_is_rejected_and_closed(self):
        transport, request, errors, _ = self.loopback(413, b"x" * 65)
        with self.assertRaisesRegex(OSError, "response exceeded its cap"):
            transport.send(request, timeout=1, max_bytes=64)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].closed)

    def test_real_trickled_http_error_keeps_absolute_deadline_and_closes(self):
        transport, request, errors, _ = self.loopback(503, b"x" * 80, drip=True)
        started = time.monotonic()
        with self.assertRaisesRegex(OSError, "timed out|deadline expired"):
            transport.send(request, timeout=0.25, max_bytes=1024)
        # Each byte arrives before the original timeout; renewing that timeout
        # per read would consume the whole >3-second body and miss this bound.
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].closed)

    def test_real_redirect_is_returned_without_following_or_environment_proxy(self):
        with mock.patch.dict(
            os.environ,
            {
                "http_proxy": "http://127.0.0.1:1",
                "HTTP_PROXY": "http://127.0.0.1:1",
                "no_proxy": "",
                "NO_PROXY": "",
            },
        ):
            transport, request, errors, paths = self.loopback(
                302, b"redirect", location="/must-not-follow"
            )
            result = transport.send(request, timeout=1, max_bytes=1024)
        self.assertEqual(result.status, 302)
        self.assertEqual(result.body, b"redirect")
        self.assertEqual(result.url, request.url)
        self.assertEqual(paths, ["/expired"])
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].closed)

    def test_small_response_that_closes_after_first_read_is_normal_eof(self):
        url = "http://127.0.0.1:11435/api/chat"
        body = b'{"done":true}'
        transport = UrllibTransport()
        transport._opener = StaticOpener(ClosingResponse(body, url))

        response_value = transport.send(
            HTTPRequest(
                method="POST",
                url=url,
                headers=(("Content-Type", "application/json"),),
                body=b"{}",
            ),
            timeout=1.0,
            max_bytes=1024,
        )

        self.assertEqual(200, response_value.status)
        self.assertEqual(body, response_value.body)
        self.assertEqual(url, response_value.url)


if __name__ == "__main__":
    unittest.main()
