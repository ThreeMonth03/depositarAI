"""Protect complete-file acquisition and fixed public endpoint boundaries."""

import hashlib
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
        changed = dict(
            item, description="updated description", dataset_id="other"
        )
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
                with (
                    self.subTest(values=values),
                    self.assertRaises(source.SourceError),
                ):
                    source.fetch(resource(**values))
            run.assert_not_called()

    def test_curl_policy_disables_config_proxy_redirects_and_remote_protocols(
        self,
    ):
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
            {
                "content_type": "text/csv",
                "etag": '"v1"',
                "last_modified": None,
            },
            headers,
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
                    headers=[
                        b"Content-Length: 3",
                        b"Transfer-Encoding: chunked",
                    ],
                ),
                "document_source_headers_invalid",
            ),
            (
                response(
                    b"abc",
                    headers=[b"Content-Length: 3", b"Content-Length: 3"],
                ),
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
                    source,
                    "_run",
                    return_value=(status, b"private remote body"),
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
                with self.assertRaisesRegex(
                    source.SourceError, "^" + expected + "$"
                ):
                    source._run(command)

    def test_run_uses_minimal_environment_and_captures_bytes(self):
        with patch.dict(
            os.environ,
            {"HTTPS_PROXY": "https://user:password@example.invalid"},
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
            source.subprocess,
            "Popen",
            side_effect=FileNotFoundError("private path"),
        ):
            with self.assertRaisesRegex(
                source.SourceError, "^document_source_tool_unavailable$"
            ):
                source._run(["missing"])

    def test_url_rejection_precedes_metadata_and_identity_hashing(self):
        cases = (
            (
                resource(url="https://example.test/data.csv", id="\ud800"),
                "document_source_url_unsupported",
            ),
            (
                resource(url="https://data.depositar.io/\ud800", id=""),
                "document_source_url_unsupported",
            ),
            (resource(id="\ud800"), "document_source_metadata_invalid"),
            (
                resource(last_modified="\ud800"),
                "document_source_metadata_invalid",
            ),
        )
        with patch.object(source.hashlib, "sha256") as digest:
            for item, expected in cases:
                with (
                    self.subTest(expected=expected),
                    self.assertRaises(source.SourceError) as caught,
                ):
                    source.source_identity(item)
                self.assertIs(type(caught.exception), source.SourceError)
                self.assertEqual(caught.exception.args, (expected,))
                self.assertEqual(caught.exception.code, expected)
            digest.assert_not_called()

    def test_identity_preserves_original_utf8_url_and_declared_types(self):
        url = "https://DATA.DEPOSITAR.IO:443/水質.csv"
        expected_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        for size in (None, 1, 1.5):
            with self.subTest(size=size):
                item = resource(url=url, format=" pdf ", size=size)
                identity = source.source_identity(item)
                self.assertEqual(
                    identity,
                    {
                        "resource_id": item["id"],
                        "source_url_sha256": expected_hash,
                        "format": "PDF",
                        "declared_size": size,
                        "declared_last_modified": item["last_modified"],
                    },
                )
                self.assertIs(type(identity["declared_size"]), type(size))
        self.assertNotEqual(
            expected_hash, hashlib.sha256(url.lower().encode()).hexdigest()
        )

    def test_huge_declared_size_is_identity_data_until_fetch_admission(self):
        item = resource(size=10**400)
        identity = source.source_identity(item)
        self.assertEqual(identity["declared_size"], 10**400)
        self.assertIs(type(identity["declared_size"]), int)
        with patch.object(source, "_run") as run:
            with self.assertRaises(source.SourceError) as caught:
                source.fetch(item)
            run.assert_not_called()
        self.assertIs(type(caught.exception), source.SourceError)
        self.assertEqual(caught.exception.args, ("document_source_too_large",))

    def test_non_success_status_precedes_header_parse_and_body_checks(self):
        with patch.object(source.http.client, "parse_headers") as parse:
            for status in (b"HTTP/2 302", b"HTTP/2 206"):
                with (
                    self.subTest(status=status),
                    self.assertRaises(source.SourceError) as caught,
                ):
                    source._response(
                        response(
                            b"",
                            status=status,
                            headers=[b"Content-Length: invalid"],
                        )
                    )
                self.assertIs(type(caught.exception), source.SourceError)
                self.assertEqual(
                    caught.exception.args, ("document_source_http_failed",)
                )
            parse.assert_not_called()

    def test_informational_blocks_share_the_count_and_header_byte_budget(self):
        interim = response(
            b"", status=b"HTTP/1.1 103 Early Hints", headers=[b"X: ignored"]
        )
        body = b"abc"
        final = response(body, headers=[b"Content-Length: 3"])
        raw = interim * 4 + final
        header_bytes = len(raw) - len(body)
        with patch.object(source, "MAX_HEADER_BYTES", header_bytes):
            self.assertEqual(source._response(raw)[0], body)
        for payload, limit in (
            (raw, header_bytes - 1),
            (interim * 5 + final, source.MAX_HEADER_BYTES),
        ):
            with (
                self.subTest(limit=limit),
                patch.object(source, "MAX_HEADER_BYTES", limit),
                self.assertRaises(source.SourceError) as caught,
            ):
                source._response(payload)
            self.assertIs(type(caught.exception), source.SourceError)
            self.assertEqual(
                caught.exception.args, ("document_source_headers_invalid",)
            )

    def test_content_length_failures_precede_body_caps_and_empty_checks(self):
        cases = (
            (
                b"abcd",
                [b"Content-Length: 3"],
                "document_source_incomplete",
            ),
            (b"abc", [b"Content-Length: 4"], "document_source_too_large"),
            (b"", [b"Content-Length: 1"], "document_source_incomplete"),
            (b"", [b"Content-Length: 0"], "document_source_empty"),
            (b"abcd", [], "document_source_too_large"),
            (
                b"abc",
                [b"Content-Length: 4", b"Content-Encoding: gzip"],
                "document_source_headers_invalid",
            ),
        )
        with patch.object(source, "MAX_FILE_BYTES", 3):
            for body, headers, expected in cases:
                with (
                    self.subTest(body=body, headers=headers),
                    self.assertRaises(source.SourceError) as caught,
                ):
                    source._response(response(body, headers=headers))
                self.assertIs(type(caught.exception), source.SourceError)
                self.assertEqual(caught.exception.args, (expected,))

    def test_body_admission_precedes_metadata_header_projection(self):
        duplicate_etag = [b"ETag: one", b"ETag: two"]
        framing = ["Content-Length", "Content-Encoding", "Transfer-Encoding"]
        for body, expected, names in (
            (b"", "document_source_empty", framing),
            (
                b"abc",
                "document_source_headers_invalid",
                framing + ["Content-Type", "ETag"],
            ),
        ):
            with (
                self.subTest(body=body),
                patch.object(
                    source, "_header_value", wraps=source._header_value
                ) as header,
                self.assertRaises(source.SourceError) as caught,
            ):
                source._response(response(body, headers=duplicate_etag))
            self.assertIs(type(caught.exception), source.SourceError)
            self.assertEqual(caught.exception.args, (expected,))
            self.assertEqual(
                [call.args[1] for call in header.call_args_list], names
            )
        with patch.object(
            source, "_header_value", wraps=source._header_value
        ) as header:
            observed, metadata = source._response(response(b"abc"))
        self.assertEqual(observed, b"abc")
        self.assertEqual(
            metadata, {"content_type": "", "etag": None, "last_modified": None}
        )
        self.assertEqual(
            [call.args[1] for call in header.call_args_list],
            framing + ["Content-Type", "ETag", "Last-Modified"],
        )

    def test_binary_bodies_and_bounded_headers_keep_exact_values(self):
        body = b"\xff\x00"
        observed, metadata = source._response(
            response(body, headers=[b"Content-Length: 2", b"ETag: \xff"])
        )
        self.assertEqual(observed, body)
        self.assertIs(type(observed), bytes)
        self.assertEqual(metadata["etag"], "ÿ")
        for name, key, limit in (
            ("Content-Type", "content_type", 256),
            ("ETag", "etag", 512),
            ("Last-Modified", "last_modified", 128),
        ):
            with self.subTest(header=name):
                prefix = name.encode("ascii") + b": "
                _, headers = source._response(
                    response(body, headers=[prefix + b"a" * limit])
                )
                self.assertEqual(headers[key], "a" * limit)
                with self.assertRaises(source.SourceError) as caught:
                    source._response(
                        response(body, headers=[prefix + b"a" * (limit + 1)])
                    )
                self.assertIs(type(caught.exception), source.SourceError)
                self.assertEqual(
                    caught.exception.args,
                    ("document_source_headers_invalid",),
                )

    def test_header_parser_errors_preserve_closed_and_unknown_failures(self):
        for error in (
            source.http.client.HTTPException("synthetic parser failure"),
            ValueError("synthetic parser failure"),
        ):
            with (
                self.subTest(error=type(error)),
                patch.object(
                    source.http.client, "parse_headers", side_effect=error
                ),
                self.assertRaises(source.SourceError) as caught,
            ):
                source._response(response(b"abc"))
            self.assertIs(type(caught.exception), source.SourceError)
            self.assertEqual(
                caught.exception.args, ("document_source_headers_invalid",)
            )
            self.assertTrue(caught.exception.__suppress_context__)
        error = RuntimeError("synthetic parser failure")
        with (
            patch.object(
                source.http.client, "parse_headers", side_effect=error
            ),
            self.assertRaises(RuntimeError) as caught,
        ):
            source._response(response(b"abc"))
        self.assertIs(caught.exception, error)

    def test_acquisition_forwards_cache_diagnostics_without_reclassification(
        self,
    ):
        for code in (
            "document_cache_io_failed",
            "document_cache_record_invalid",
            "document_cache_body_invalid",
        ):
            with (
                self.subTest(code=code),
                patch.object(
                    source.cache,
                    "locked",
                    side_effect=source.cache.CacheError(code),
                ),
                patch.object(source, "fetch") as fetch,
                self.assertRaises(source.SourceError) as caught,
            ):
                source.acquire("synthetic-unused-cache", resource(), now=100)
            self.assertIs(type(caught.exception), source.SourceError)
            self.assertEqual(caught.exception.args, (code,))
            self.assertEqual(caught.exception.code, code)
            self.assertTrue(caught.exception.__suppress_context__)
            fetch.assert_not_called()

    def test_literal_glob_paths_keep_exact_url_and_source_identity(self):
        def guarded_transport(command):
            if "--globoff" not in command:
                return 3, b"synthetic curl URL glob error"
            return 0, response(headers=[b"Content-Length: 17"])

        for path in (
            "report[final].csv",
            "report{draft.csv",
            "report{draft,final}.csv",
            "report[1-2].csv",
            "report%5Bfinal%5D.csv",
            "報告[final].csv",
        ):
            url = "https://data.depositar.io/synthetic-literal/" + path
            item = resource(url=url)
            before = source.source_identity(item)
            with (
                self.subTest(path=path),
                patch.object(
                    source, "_run", side_effect=guarded_transport
                ) as run,
            ):
                body, _ = source.fetch(item)
                run.assert_called_once()
                command = run.call_args.args[0]
                self.assertIn("--globoff", command)
                self.assertEqual(command[-1].encode("utf-8"), url.encode())
                self.assertEqual(body, b"station,pH\nA,7.2\n")
                self.assertEqual(source.source_identity(item), before)
                self.assertEqual(
                    before["source_url_sha256"],
                    hashlib.sha256(url.encode("utf-8")).hexdigest(),
                )
