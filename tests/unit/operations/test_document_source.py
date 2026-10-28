"""Protect complete-file acquisition and fixed public endpoint boundaries."""

import os
import sys
import unittest
from unittest.mock import patch

from search.operations.documents import source


def resource(**updates):
    value = {
        "id": "public-resource",
        "url": "https://data.depositar.io/data.csv",
        "format": "CSV",
        "size": None,
        "last_modified": "2026-10-28T00:00:00",
    }
    value.update(updates)
    return value


def response(body=b"station,pH\nA,7.2\n", *, status=b"HTTP/2 200", headers=()):
    lines = [status, *headers]
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


class DocumentSourceTests(unittest.TestCase):
    def test_rejects_unsupported_addresses_before_process_creation(self):
        urls = (
            "http://data.depositar.io/data.csv",
            "https://localhost/data.csv",
            "https://data.depositar.io:444/data.csv",
            "https://user@data.depositar.io/data.csv",
            "https://@data.depositar.io/data.csv",
            "https://data.depositar.io/data.csv?secret=private",
            "https://data.depositar.io/data.csv#fragment",
            "https://data.depositar.io.evil.test/data.csv",
            "https://data.depositar.io/data\n.csv",
            "file:///private/file.csv",
            "https://[broken/data.csv",
            "https://data.depositar.io/\ud800.csv",
            "",
        )
        with patch.object(source, "_run") as run:
            for url in urls:
                with (
                    self.subTest(url=url),
                    self.assertRaisesRegex(
                        source.SourceError, "^document_source_url_unsupported$"
                    ),
                ):
                    source.fetch(resource(url=url))
            run.assert_not_called()

    def test_key_identity_contains_no_raw_url_or_unrelated_metadata(self):
        item = resource(url="https://data.depositar.io:443/data.csv")
        identity = source.source_identity(item)
        self.assertNotIn("url", identity)
        self.assertNotIn(item["url"], str(identity))
        changed = dict(item, description="updated description", dataset_id="other")
        self.assertEqual(identity, source.source_identity(changed))

    def test_declared_size_and_metadata_are_bounded(self):
        with patch.object(source, "_run") as run:
            for values in (
                {"size": source.MAX_FILE_BYTES + 1},
                {"size": -1},
                {"size": True},
                {"size": float("inf")},
                {"size": 10**400},
                {"format": "ZIP"},
                {"id": ""},
                {"last_modified": []},
                {"last_modified": "\ud800"},
            ):
                with self.subTest(values=values), self.assertRaises(source.SourceError):
                    source.fetch(resource(**values))
            run.assert_not_called()

    def test_curl_policy_disables_config_proxy_redirects_and_remote_protocols(self):
        with patch.object(
            source,
            "_run",
            return_value=(
                0,
                response(headers=[b"Content-Type: text/csv", b'ETag: "v1"']),
            ),
        ) as run:
            body, headers = source.fetch(resource())
        command = run.call_args.args[0]
        self.assertEqual(["curl", "-q"], command[:2])
        for flag, value in (
            ("--proxy", ""),
            ("--noproxy", "*"),
            ("--proto", "=https"),
            ("--proto-redir", "=https"),
            ("--max-redirs", "0"),
            ("--max-filesize", str(source.MAX_FILE_BYTES)),
            ("--max-time", "25"),
        ):
            self.assertEqual(value, command[command.index(flag) + 1])
        self.assertNotIn("--location", command)
        self.assertNotIn("--insecure", command)
        self.assertEqual(b"station,pH\nA,7.2\n", body)
        self.assertEqual(
            {"content_type": "text/csv", "etag": '"v1"', "last_modified": None}, headers
        )

    def test_informational_response_and_exact_content_length(self):
        raw = b"HTTP/1.1 103 Early Hints\r\nLink: ignored\r\n\r\n" + response(
            b"abc", headers=[b"Content-Length: 3"]
        )
        body, _ = source._response(raw)
        self.assertEqual(b"abc", body)

    def test_complete_transfer_without_length_keeps_full_body(self):
        body = b"a,b\n1,2\n\r\nHTTP/2 200\r\nnot a header\n"
        observed, _ = source._response(
            response(body, headers=[b"Transfer-Encoding: chunked"])
        )
        self.assertEqual(body, observed)

    def test_rejects_redirect_partial_empty_and_incomplete_responses(self):
        cases = (
            (response(status=b"HTTP/2 302"), "document_source_http_failed"),
            (response(status=b"HTTP/2 206"), "document_source_http_failed"),
            (response(b""), "document_source_empty"),
            (
                response(b"abc", headers=[b"Content-Length: 4"]),
                "document_source_incomplete",
            ),
            (
                response(b"abc", headers=[b"Content-Length: 2"]),
                "document_source_incomplete",
            ),
            (
                response(b"abc", headers=[b"Content-Encoding: gzip"]),
                "document_source_headers_invalid",
            ),
            (
                response(
                    b"abc",
                    headers=[b"Content-Length: 3", b"Transfer-Encoding: chunked"],
                ),
                "document_source_headers_invalid",
            ),
            (
                response(b"abc", headers=[b"Content-Length: 3", b"Content-Length: 3"]),
                "document_source_headers_invalid",
            ),
            (
                response(b"abc", headers=[b"Content-Length: -3"]),
                "document_source_headers_invalid",
            ),
            (b"not http\r\n\r\nabc", "document_source_headers_invalid"),
        )
        for raw, code in cases:
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(source.SourceError, "^" + code + "$"),
            ):
                source._response(raw)

    def test_independent_header_and_body_caps(self):
        with patch.object(source, "MAX_FILE_BYTES", 3):
            self.assertEqual(b"abc", source._response(response(b"abc"))[0])
            with self.assertRaisesRegex(
                source.SourceError, "^document_source_too_large$"
            ):
                source._response(response(b"abcd"))
        with patch.object(source, "MAX_HEADER_BYTES", 32):
            with self.assertRaisesRegex(
                source.SourceError, "^document_source_headers_invalid$"
            ):
                source._response(response(headers=[b"X-Long: " + b"x" * 40]))

    def test_transport_failures_do_not_echo_remote_diagnostics(self):
        for status, expected in (
            (63, "document_source_too_large"),
            (28, "document_source_timeout"),
            (22, "document_source_http_failed"),
            (7, "document_source_fetch_failed"),
        ):
            with (
                self.subTest(status=status),
                patch.object(
                    source, "_run", return_value=(status, b"private remote body")
                ),
            ):
                with self.assertRaisesRegex(
                    source.SourceError, "^" + expected + "$"
                ) as caught:
                    source.fetch(resource())
                self.assertEqual(expected, caught.exception.code)

    def test_run_has_a_total_deadline_and_output_cap(self):
        for command, expected in (
            (
                [sys.executable, "-c", "import time; time.sleep(5)"],
                "document_source_timeout",
            ),
            (
                [sys.executable, "-c", "import os; os.write(1,b'x'*4096)"],
                "document_source_too_large",
            ),
        ):
            with (
                self.subTest(expected=expected),
                patch.object(source, "DEADLINE_SECONDS", 0.1),
                patch.object(source, "MAX_CAPTURE_BYTES", 64),
            ):
                with self.assertRaisesRegex(source.SourceError, "^" + expected + "$"):
                    source._run(command)

    def test_run_uses_minimal_environment_and_captures_bytes(self):
        with patch.dict(
            os.environ, {"HTTPS_PROXY": "https://user:password@example.invalid"}
        ):
            status, raw = source._run(
                [
                    sys.executable,
                    "-c",
                    "import os; print(os.environ.get('HTTPS_PROXY','absent'))",
                ]
            )
        self.assertEqual(0, status)
        self.assertEqual(b"absent\n", raw)

    def test_missing_curl_is_a_fixed_diagnostic(self):
        with patch.object(
            source.subprocess, "Popen", side_effect=FileNotFoundError("private path")
        ):
            with self.assertRaisesRegex(
                source.SourceError, "^document_source_tool_unavailable$"
            ):
                source._run(["missing"])
