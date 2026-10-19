"""Operator transport contracts; fake HTTP and loopback-only real TLS."""

from __future__ import annotations

import base64
import http.server
import io
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from search.operations.catalog import transport


INDEX = "fess.20261008000000"
PEER = "172.30.0.2"
PASSWORD = "a" * 64


class Response:
    def __init__(self, body=b'{"ok":true}', *, status=200, headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.closed = False
        self.close_count = 0
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            **(headers or {}),
        }

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def isclosed(self):
        return self.closed

    def close(self):
        self.closed = True
        self.close_count += 1
        self.body.close()

    def read1(self, count):
        data = self.body.read(count)
        self.closed = self.body.tell() == len(self.body.getvalue())
        return data


class OperatorTransportTests(unittest.TestCase):
    def setUp(self):
        self.context = Mock()
        self.context_patch = patch.object(transport.ssl, "SSLContext")
        self.context_patch.start().return_value = self.context
        self.addCleanup(self.context_patch.stop)
        self.response = Response()
        self.connection = Mock()
        self.connection.getresponse.side_effect = lambda: self.response
        self.factory_patch = patch.object(transport, "_PinnedHTTPSConnection")
        self.factory = self.factory_patch.start()
        self.factory.return_value = self.connection
        self.addCleanup(self.factory_patch.stop)
        self.client = transport.OpenSearchClient(PEER, b"synthetic CA", PASSWORD)

    def request(self, **kwargs):
        return self.client.request("POST", "/" + INDEX + "/_search", {}, **kwargs)

    def test_tls_context_enforces_hostname_and_certificate(self):
        self.context.load_verify_locations.assert_called_once_with(
            cadata="synthetic CA"
        )
        self.assertIs(self.context.check_hostname, True)
        self.assertEqual(ssl.CERT_REQUIRED, self.context.verify_mode)

    def test_success_uses_fixed_peer_no_url_and_private_auth(self):
        self.assertEqual((200, {"ok": True}), self.request())
        self.factory.assert_called_once()
        self.assertEqual((PEER,), self.factory.call_args.args)
        kwargs = self.connection.request.call_args.kwargs
        self.assertEqual(b"{}\n", kwargs["body"])
        self.assertEqual("identity", kwargs["headers"]["Accept-Encoding"])
        expected = base64.b64encode(("admin:" + PASSWORD).encode()).decode()
        self.assertEqual("Basic " + expected, kwargs["headers"]["Authorization"])
        self.connection.finish.assert_called_once()
        self.assertEqual(1, self.response.close_count)

    def test_query_probes_only_accept_fixed_username(self):
        self.assertEqual(
            (200, {"ok": True}),
            self.request(
                username="depositar_filtered",
                password="b" * 64,
            ),
        )
        for kwargs in (
            {"username": "admin", "password": PASSWORD},
            {"username": "other", "password": PASSWORD},
            {"username": "depositar_filtered"},
            {"password": PASSWORD},
            {"username": "depositar_filtered", "password": True},
        ):
            with (
                self.subTest(kwargs=list(kwargs)),
                self.assertRaisesRegex(
                    ValueError,
                    "credentials_invalid",
                ),
            ):
                self.request(**kwargs)

    def test_query_credentials_allow_search_and_exact_denial_probes(self):
        for method, path, payload, status in (
            ("POST", "/" + INDEX + "/_search", {"size": 0}, 200),
            ("GET", "/" + INDEX + "/_mapping", None, 403),
            ("PUT", "/" + INDEX + "/_mapping", {"properties": {}}, 403),
            (
                "PUT",
                "/" + INDEX + "/_doc/authorization-probe",
                {"authorization_probe": True},
                403,
            ),
            ("POST", "/fess_user.user/_search", {"size": 0}, 403),
            ("GET", transport.SECURITY + "internalusers", None, 403),
        ):
            with self.subTest(method=method, path=path):
                self.response = Response(status=status)
                expected = (200, {"ok": True}) if status == 200 else (403, None)
                self.assertEqual(
                    expected,
                    self.client.request(
                        method,
                        path,
                        payload,
                        username=transport.QUERY_USER,
                        password="b" * 64,
                    ),
                )
                self.assertEqual(1, self.response.close_count)

    def test_query_credentials_reject_operator_operations_before_connect(self):
        for method, path, payload, ndjson in (
            ("GET", "/fess.search/_mapping", None, False),
            ("POST", "/" + INDEX + "/_bulk", "{}\n", True),
            ("POST", "/" + INDEX + "/_refresh", None, False),
            ("GET", transport.SECURITY + "roles/depositar_filtered", None, False),
            (
                "PUT",
                transport.SECURITY + "roles/depositar_filtered",
                {"index_permissions": []},
                False,
            ),
            (
                "PUT",
                transport.SECURITY + "internalusers/depositar_filtered",
                {"password": "b" * 64},
                False,
            ),
            (
                "PUT",
                transport.SECURITY + "rolesmapping/depositar_filtered",
                {"users": []},
                False,
            ),
        ):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(
                    transport.TransportError, "credentials_operation_invalid"
                ),
            ):
                self.client.request(
                    method,
                    path,
                    payload,
                    username=transport.QUERY_USER,
                    password="b" * 64,
                    ndjson=ndjson,
                )
        self.factory.assert_not_called()

    def test_admin_credentials_cannot_issue_query_denial_probes(self):
        for method, path, payload in (
            ("GET", transport.SECURITY + "internalusers", None),
            ("POST", "/fess_user.user/_search", {"size": 0}),
            (
                "PUT",
                "/" + INDEX + "/_doc/authorization-probe",
                {"authorization_probe": True},
            ),
        ):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(
                    transport.TransportError, "credentials_operation_invalid"
                ),
            ):
                self.client.request(method, path, payload)
        self.factory.assert_not_called()

    def test_peer_and_budget_fail_before_tls(self):
        self.context.load_verify_locations.reset_mock()
        for peer in ("127.0.0.1", "192.0.2.20", "::1", "172.030.0.2", "host", None):
            with self.subTest(peer=peer), self.assertRaises(ValueError):
                transport.OpenSearchClient(peer, b"CA", PASSWORD)
        for budget in (True, 0, -1, 601, float("nan"), "20"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                transport.OpenSearchClient(PEER, b"CA", PASSWORD, budget_seconds=budget)
        self.context.load_verify_locations.assert_not_called()

    def test_invalid_operations_never_open_connection(self):
        for method, path in (
            ("GET", "https://evil.example/_mapping"),
            ("DELETE", "/" + INDEX),
            ("POST", "/" + INDEX + "/_search?pretty"),
            ("POST", "/" + INDEX + "/_search#fragment"),
            ("GET", "/_cluster/settings"),
            ("POST", "/_bulk"),
            ("PUT", "/" + INDEX + "/_doc/arbitrary"),
            ("GET", transport.SECURITY + "internalusers/other"),
        ):
            with (
                self.subTest(method=method, path=path),
                self.assertRaisesRegex(
                    ValueError,
                    "operation_invalid",
                ),
            ):
                self.client.request(method, path)
        self.factory.assert_not_called()

    def test_ndjson_only_for_physical_bulk(self):
        self.assertEqual(
            (200, {"ok": True}),
            self.client.request(
                "POST",
                "/" + INDEX + "/_bulk",
                '{"update":{}}\n',
                ndjson=True,
            ),
        )
        self.assertEqual(
            "application/x-ndjson",
            self.connection.request.call_args.kwargs["headers"]["Content-Type"],
        )
        for method, path, payload, ndjson in (
            ("POST", "/" + INDEX + "/_bulk", {}, False),
            ("POST", "/" + INDEX + "/_bulk", "{}", True),
            ("POST", "/" + INDEX + "/_search", "{}\n", True),
            ("GET", "/" + INDEX + "/_mapping", {}, False),
            ("POST", "/" + INDEX + "/_search", [], False),
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.client.request(method, path, payload, ndjson=ndjson)

    def test_denial_body_is_never_returned(self):
        for status in (401, 403, 404, 500):
            self.response = Response(b"private diagnostic", status=status)
            with self.subTest(status=status):
                self.assertEqual((status, None), self.request())

    def test_redirect_encoding_length_content_type_and_schema_rejected(self):
        for response in (
            Response(status=302),
            Response(headers={"Content-Encoding": "gzip"}),
            Response(headers={"Content-Length": "-1"}),
            Response(headers={"Content-Length": "100000000"}),
            Response(headers={"Content-Length": "1"}),
            Response(headers={"Content-Type": "text/html"}),
            Response(b"[]"),
            Response(b'{"key":1,"key":2}'),
            Response(b'{"key":NaN}'),
            Response(b"not JSON"),
        ):
            self.response = response
            with (
                self.subTest(status=response.status),
                self.assertRaisesRegex(
                    ValueError,
                    "request_failed",
                ),
            ):
                self.request()
            self.assertEqual(1, response.close_count)
        self.assertEqual(10, self.connection.finish.call_count)

    def test_output_and_request_caps_even_without_length(self):
        self.response = Response(b" " * 50)
        self.response.headers.pop("Content-Length")
        with (
            patch.object(transport, "MAX_RESPONSE_BYTES", 20),
            self.assertRaises(ValueError),
        ):
            self.request()
        with (
            patch.object(transport, "MAX_REQUEST_BYTES", 1),
            self.assertRaisesRegex(
                ValueError,
                "payload_invalid",
            ),
        ):
            self.request()

    def test_failed_network_is_closed_without_echoing_error(self):
        self.connection.request.side_effect = OSError("private diagnostic")
        with self.assertRaisesRegex(ValueError, "catalog_transport_request_failed$"):
            self.request()
        self.connection.finish.assert_called_once()

    def test_deep_response_json_has_only_fixed_error_code(self):
        self.response = Response(
            b'{"private":"must-not-echo","nested":'
            + b"[" * 2048
            + b"0"
            + b"]" * 2048
            + b"}"
        )
        with self.assertRaisesRegex(
            transport.TransportError, "catalog_transport_request_failed$"
        ):
            self.request()
        self.connection.finish.assert_called_once()
        self.assertEqual(1, self.response.close_count)

    def test_depth_limit_does_not_depend_on_json_decoder_recursion_limit(self):
        value = {}
        for _ in range(transport.MAX_JSON_DEPTH):
            value = {"nested": [value]}
        with (
            patch.object(transport, "strict_json", return_value=value),
            self.assertRaisesRegex(
                transport.TransportError, "catalog_transport_request_failed$"
            ),
        ):
            self.request()
        self.connection.finish.assert_called_once()
        self.assertEqual(1, self.response.close_count)

    def test_depth_boundary_accepts_reviewed_nested_response(self):
        with patch.object(transport, "MAX_JSON_DEPTH", 3):
            transport._response_depth({"nested": [{"vector": 1.0}]})
            with self.assertRaises(transport.TransportError):
                transport._response_depth({"nested": [{"vector": [1.0]}]})

    def test_giant_numeric_token_fails_before_python39_integer_decoding(self):
        self.response = Response(b'{"number":' + b"1" * 100000 + b"}")
        with (
            patch.object(transport, "strict_json") as decoder,
            self.assertRaisesRegex(
                transport.TransportError, "catalog_transport_request_failed$"
            ),
        ):
            self.request()
        decoder.assert_not_called()
        self.connection.finish.assert_called_once()
        self.assertEqual(1, self.response.close_count)

    def test_deep_wire_json_fails_before_decoder_recursion(self):
        self.response = Response(b'{"value":' + b"[" * 64 + b"0" + b"]" * 64 + b"}")
        with (
            patch.object(transport, "strict_json") as decoder,
            self.assertRaisesRegex(
                transport.TransportError, "catalog_transport_request_failed$"
            ),
        ):
            self.request()
        decoder.assert_not_called()
        self.connection.finish.assert_called_once()

    def test_vector_floats_escaped_text_and_long_quoted_digits_are_valid(self):
        value = {
            "vectors": [1.23456789012345, -2.220446049e-20, 1.25e-8, 0.0] * 192,
            "text": '\\"[{' + "1" * 100000,
            "integer_boundary": int("1" * 64),
        }
        self.response = Response(json.dumps(value).encode())
        self.assertEqual((200, value), self.request())
        self.connection.finish.assert_called_once()
        self.assertEqual(1, self.response.close_count)

    def test_exponent_overflow_cannot_enter_a_successful_response(self):
        self.response = Response(b'{"value":1e999}')
        with self.assertRaisesRegex(
            transport.TransportError, "catalog_transport_request_failed$"
        ):
            self.request()
        self.connection.finish.assert_called_once()

    def test_response_parser_overflow_is_sanitized_and_closed(self):
        with (
            patch.object(
                transport,
                "strict_json",
                side_effect=OverflowError("private diagnostic"),
            ),
            self.assertRaisesRegex(
                transport.TransportError, "catalog_transport_request_failed$"
            ),
        ):
            self.request()
        self.connection.finish.assert_called_once()
        self.assertEqual(1, self.response.close_count)

    def test_parser_finishing_after_watchdog_cannot_return_success(self):
        expired = threading.Event()
        self.connection.abort.side_effect = expired.set
        client = transport.OpenSearchClient(
            PEER, b"synthetic CA", PASSWORD, budget_seconds=0.05
        )

        def delayed_parse(_data):
            self.assertTrue(expired.wait(1))
            return {"ok": True}

        with (
            patch.object(transport, "strict_json", side_effect=delayed_parse),
            self.assertRaisesRegex(
                transport.TransportError, "catalog_transport_request_failed$"
            ),
        ):
            client.request("POST", "/" + INDEX + "/_search", {})
        self.connection.abort.assert_called_once()
        self.connection.finish.assert_called_once()
        self.assertEqual(1, self.response.close_count)

    def test_expired_or_backwards_clock_never_opens_connection(self):
        for clock in (iter((0, 600)).__next__, iter((1, 0)).__next__):
            client = transport.OpenSearchClient(PEER, b"CA", PASSWORD, clock=clock)
            with self.assertRaisesRegex(ValueError, "deadline_exceeded"):
                client.request("POST", "/" + INDEX + "/_search", {})
        self.factory.assert_not_called()

    def test_total_budget_includes_body_reading(self):
        ticks = iter((0, 0, 0, 599, 600))
        client = transport.OpenSearchClient(PEER, b"CA", PASSWORD, clock=ticks.__next__)
        with self.assertRaisesRegex(ValueError, "request_failed"):
            client.request("POST", "/" + INDEX + "/_search", {})
        self.connection.finish.assert_called_once()

    def test_connection_uses_pinned_ip_and_fixed_sni(self):
        # Stop the constructor mock temporarily to exercise real connect code.
        self.factory_patch.stop()
        raw, context = Mock(), Mock()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        with patch.object(
            transport.socket, "create_connection", return_value=raw
        ) as connect:
            connection = transport._PinnedHTTPSConnection(
                PEER, context=context, timeout=3
            )
            connection.connect()
        connect.assert_called_once_with((PEER, 9200), 3)
        context.wrap_socket.assert_called_once_with(
            raw,
            server_hostname="opensearch-search",
            do_handshake_on_connect=False,
        )
        context.wrap_socket.return_value.do_handshake.assert_called_once_with()


@unittest.skipUnless(shutil.which("openssl"), "loopback TLS fixture requires OpenSSL")
class RealTLSContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-operator-tls-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        key, certificate = root / "key.pem", root / "ca.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(certificate),
                "-days",
                "1",
                "-subj",
                "/CN=opensearch-search",
                "-addext",
                "subjectAltName=DNS:opensearch-search",
            ],
            check=True,
            capture_output=True,
            timeout=15,
            umask=0o077,
        )
        self.ca = certificate.read_bytes()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certificate), str(key))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        original_connect = socket.create_connection

        def loopback_only(address, timeout):
            self.assertEqual((PEER, 9200), address)
            return original_connect(self.server.server_address, timeout)

        mocked = patch.object(
            transport.socket, "create_connection", side_effect=loopback_only
        )
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_real_tls_hostname_verified_with_connection_close(self):
        client = transport.OpenSearchClient(PEER, self.ca, PASSWORD)
        self.assertEqual(
            (200, {"ok": True}),
            client.request(
                "POST",
                "/" + INDEX + "/_search",
                {},
            ),
        )

    def test_real_tls_wrong_hostname_is_not_bypassed_by_ip(self):
        client = transport.OpenSearchClient(PEER, self.ca, PASSWORD)
        with (
            patch.object(transport, "HOST", "wrong-host"),
            self.assertRaisesRegex(
                ValueError,
                "catalog_transport_request_failed$",
            ),
        ):
            client.request("POST", "/" + INDEX + "/_search", {})

    def assert_slow_framing_obeys_deadline(self, prefix, suffix):
        stopped = threading.Event()

        def slow_response(handler):
            handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
            try:
                handler.connection.sendall(prefix)
                # Each byte arrives faster than the socket inactivity timeout.
                # The complete frame takes 3.2 seconds without an absolute stop.
                for byte in b"x" * 160:
                    if stopped.wait(0.02):
                        return
                    handler.connection.sendall(bytes((byte,)))
                handler.connection.sendall(suffix)
            except OSError:
                # The watchdog's expected shutdown must not print handler errors.
                pass

        with patch.object(self.server.RequestHandlerClass, "do_POST", slow_response):
            client = transport.OpenSearchClient(
                PEER, self.ca, PASSWORD, budget_seconds=1
            )
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(
                    transport.TransportError, "catalog_transport_request_failed$"
                ):
                    client.request("POST", "/" + INDEX + "/_search", {})
                elapsed = time.monotonic() - started
            finally:
                stopped.set()
        self.assertGreaterEqual(elapsed, 0.8)
        self.assertLess(elapsed, 1.8)

    def test_slow_drip_response_header_cannot_extend_total_deadline(self):
        self.assert_slow_framing_obeys_deadline(
            b"HTTP/1.1 200 OK\r\nX-Slow: ",
            b"\r\nContent-Type: application/json\r\nContent-Length: 2\r\n"
            b"Connection: close\r\n\r\n{}",
        )

    def test_slow_drip_chunk_size_line_cannot_extend_total_deadline(self):
        self.assert_slow_framing_obeys_deadline(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n2;",
            b"\r\n{}\r\n0\r\n\r\n",
        )

    def test_invalid_ca_is_rejected_before_connect(self):
        with self.assertRaisesRegex(ValueError, "ca_invalid"):
            transport.OpenSearchClient(PEER, b"not a certificate", PASSWORD)


if __name__ == "__main__":
    unittest.main()
