"""Metadata transport and pagination contracts with no network or files."""

from email.message import Message
import hashlib
import itertools
import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from search.catalog.build.policy import BundleError
from search.operations.metadata import source


def dataset(number=1, **changes):
    return {
        "id": f"00000000-0000-4000-8000-{number:012d}",
        "name": f"dataset-{number}",
        "title": f"Dataset {number}",
        "private": False,
        "state": "active",
        "type": "dataset",
        "resources": [],
        **changes,
    }


def wire(result=None, *, success=True):
    result = {"count": 1, "results": []} if result is None else result
    return json.dumps({"success": success, "result": result}).encode()


class Response:
    def __init__(
        self,
        body,
        events,
        *,
        status=200,
        headers=None,
        chunk_size=65536,
        read_error=None,
    ):
        self.body, self.events = body, events
        self.status = status
        self.headers = Message()
        for name, value in {
            "Content-Type": "application/json",
            **(headers or {}),
        }.items():
            if value is not None:
                self.headers[name] = value
        self.chunk_size = chunk_size
        self.read_error = read_error
        self.offset = 0
        self.closed = 0

    def __enter__(self):
        self.events.append(("enter",))
        return self

    def __exit__(self, kind, _error, _traceback):
        self.events.append(("exit", kind.__name__ if kind else None))
        self.closed += 1
        return False

    def read1(self, size):
        self.events.append(("read", size))
        if self.read_error is not None:
            raise self.read_error
        end = self.offset + min(size, self.chunk_size)
        part = self.body[self.offset : end]
        self.offset += len(part)
        return part


class FetchPageTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.deadline = 10

    def response(self, raw=None, **options):
        return Response(wire() if raw is None else raw, self.events, **options)

    def fetch(self, response, *, ticks=None, parser=None):
        self.events.clear()
        ticks = itertools.repeat(0.0) if ticks is None else iter(ticks)
        parse = source.strict_json if parser is None else parser

        def clock():
            value = next(ticks)
            self.events.append(("clock", value))
            return value

        def open_response(request, timeout):
            self.events.append(("open", timeout))
            return response

        def parse_response(raw):
            self.events.append(("parse", raw))
            return parse(raw)

        self.opener = Mock()
        self.opener.open.side_effect = open_response
        with (
            patch.object(
                source.urllib.request, "build_opener", return_value=self.opener
            ) as builder,
            patch.object(source.time, "monotonic", side_effect=clock) as timer,
            patch.object(source, "strict_json", side_effect=parse_response),
        ):
            self.builder, self.timer = builder, timer
            return source.fetch_page(5, 3, self.deadline)

    def event_names(self):
        return [event[0] for event in self.events]

    def read_sizes(self):
        return [event[1] for event in self.events if event[0] == "read"]

    def test_request_is_anonymous_and_ignores_environment_proxies(self):
        with patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://untrusted.example.test:1234"},
        ):
            self.fetch(self.response())
        request = self.opener.open.call_args.args[0]
        url = urlsplit(request.full_url)
        self.assertEqual("https", url.scheme)
        self.assertEqual("data.depositar.io", url.hostname)
        self.assertEqual("/api/3/action/package_search", url.path)
        self.assertEqual(
            {
                "include_private": ["false"],
                "sort": ["id asc"],
                "start": ["5"],
                "rows": ["3"],
            },
            parse_qs(url.query),
        )
        self.assertEqual("application/json", request.get_header("Accept"))
        self.assertEqual("identity", request.get_header("Accept-encoding"))
        self.assertEqual(
            "depositar-public-metadata-sync/1",
            request.get_header("User-agent"),
        )
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Cookie"))
        proxy, redirect = self.builder.call_args.args
        self.assertEqual({}, proxy.proxies)
        self.assertIs(type(redirect), source.NoRedirect)

    def test_open_timeout_uses_initial_remaining_time_and_cap(self):
        for initial, expected in ((4, 6), (-50, 30)):
            with self.subTest(initial=initial):
                self.fetch(self.response(), ticks=[initial, 0, 0])
                self.assertEqual(
                    expected, self.opener.open.call_args.kwargs["timeout"]
                )
                self.assertEqual(1, self.opener.open.call_count)

    def test_expired_before_open_does_not_build_or_open(self):
        with self.assertRaisesRegex(
            source.MetadataSourceError, "^refresh deadline exceeded$"
        ):
            self.fetch(self.response(), ticks=[10])
        self.builder.assert_not_called()
        self.opener.open.assert_not_called()
        self.assertEqual(["clock"], self.event_names())

    def test_headers_win_over_length_and_expired_body_deadline(self):
        cases = (
            {"status": 503},
            {"headers": {"Content-Type": "text/html"}},
            {"headers": {"Content-Encoding": "gzip"}},
        )
        for options in cases:
            with self.subTest(options=options):
                headers = {"Content-Length": "999999999"}
                headers.update(options.get("headers", {}))
                response = self.response(
                    status=options.get("status", 200), headers=headers
                )
                with self.assertRaisesRegex(
                    source.MetadataSourceError,
                    "^unexpected CKAN response headers$",
                ):
                    self.fetch(response, ticks=[0, 11])
                self.assertEqual([], self.read_sizes())
                self.assertEqual(1, self.timer.call_count)
                self.assertEqual(1, response.closed)
                self.assertNotIn("parse", self.event_names())

    def test_missing_type_and_non_identity_encoding_are_rejected(self):
        for headers in (
            {"Content-Type": None},
            {"Content-Encoding": ""},
            {"Content-Encoding": "IDENTITY"},
        ):
            with self.subTest(headers=headers):
                response = self.response(headers=headers)
                with self.assertRaisesRegex(
                    source.MetadataSourceError, "response headers"
                ):
                    self.fetch(response)
                self.assertEqual([], self.read_sizes())
                self.assertEqual(1, response.closed)

    def test_content_type_parameters_and_decimal_lengths_are_accepted(self):
        raw = wire()
        arabic = str(len(raw)).translate(
            str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
        )
        for declared in (None, str(len(raw)), arabic):
            with self.subTest(declared=declared):
                response = self.response(
                    raw,
                    headers={
                        "Content-Type": "application/json; charset=utf-8",
                        "Content-Length": declared,
                    },
                )
                self.assertEqual(
                    {"count": 1, "results": []}, self.fetch(response)
                )
                self.assertEqual(1, response.closed)

    def test_invalid_declared_lengths_refuse_before_read(self):
        for declared in ("", "-1", "+1", "1.0", "one", "²", "999999999"):
            with self.subTest(declared=declared):
                response = self.response(headers={"Content-Length": declared})
                with self.assertRaisesRegex(
                    source.MetadataSourceError, "page exceeds its byte limit"
                ):
                    self.fetch(response)
                self.assertEqual([], self.read_sizes())
                self.assertEqual(1, response.closed)

    def test_zero_declared_length_reaches_post_close_json_error(self):
        response = self.response(b"", headers={"Content-Length": "0"})
        with self.assertRaises(json.JSONDecodeError):
            self.fetch(response)
        self.assertEqual(1, response.closed)
        self.assertEqual(["exit", "parse"], self.event_names()[-2:])

    def test_short_chunks_keep_read_sizes_and_parse_after_close(self):
        response = self.response(chunk_size=3)
        result = self.fetch(response)
        self.assertEqual({"count": 1, "results": []}, result)
        self.assertEqual(["clock", "open", "enter"], self.event_names()[:3])
        self.assertEqual(1, self.event_names().count("enter"))
        self.assertGreater(len(self.read_sizes()), 2)
        self.assertEqual({65536}, set(self.read_sizes()))
        self.assertEqual(1, response.closed)
        self.assertEqual(["exit", "parse"], self.event_names()[-2:])

    def test_exact_cap_requires_the_one_byte_eof_probe(self):
        raw = wire()
        response = self.response(raw)
        with patch.object(source, "MAX_PAGE_BYTES", len(raw)):
            self.fetch(response)
        self.assertEqual([len(raw) + 1, 1], self.read_sizes())
        self.assertEqual(1, response.closed)

    def test_cap_plus_one_never_reaches_parser(self):
        raw = wire()
        response = self.response(raw + b" ")
        with (
            patch.object(source, "MAX_PAGE_BYTES", len(raw)),
            self.assertRaisesRegex(
                source.MetadataSourceError, "page exceeds its byte limit"
            ),
        ):
            self.fetch(response)
        self.assertEqual([len(raw) + 1], self.read_sizes())
        self.assertNotIn("parse", self.event_names())
        self.assertEqual(1, response.closed)

    def test_declared_length_mismatch_precedes_json_parsing(self):
        for declared in (1, 3):
            with self.subTest(declared=declared):
                response = self.response(
                    b"{}", headers={"Content-Length": str(declared)}
                )
                with self.assertRaisesRegex(
                    source.MetadataSourceError,
                    "^CKAN response body is truncated$",
                ):
                    self.fetch(response)
                self.assertNotIn("parse", self.event_names())
                self.assertEqual(1, response.closed)

    def test_mid_read_expiry_stops_before_the_next_read(self):
        response = self.response(chunk_size=1)
        with self.assertRaisesRegex(
            source.MetadataSourceError, "^refresh deadline exceeded$"
        ):
            self.fetch(response, ticks=[0, 9, 10])
        self.assertEqual([65536], self.read_sizes())
        self.assertNotIn("parse", self.event_names())
        self.assertEqual(1, response.closed)

    def test_exact_cap_does_not_bypass_deadline_before_eof(self):
        raw = wire()
        response = self.response(raw)
        with (
            patch.object(source, "MAX_PAGE_BYTES", len(raw)),
            self.assertRaisesRegex(
                source.MetadataSourceError, "^refresh deadline exceeded$"
            ),
        ):
            self.fetch(response, ticks=[0, 9, 10])
        self.assertEqual([len(raw) + 1], self.read_sizes())
        self.assertEqual(1, response.closed)

    def test_read_error_keeps_original_exception_and_closes_once(self):
        error = OSError("synthetic read failure")
        response = self.response(read_error=error)
        with self.assertRaises(OSError) as caught:
            self.fetch(response)
        self.assertIs(error, caught.exception)
        self.assertEqual(1, response.closed)
        self.assertEqual(("exit", "OSError"), self.events[-1])
        self.assertNotIn("parse", self.event_names())

    def test_open_error_has_no_response_context_or_retry(self):
        error = OSError("synthetic open failure")
        opener = Mock()
        opener.open.side_effect = error
        with (
            patch.object(source.time, "monotonic", return_value=0),
            patch.object(
                source.urllib.request, "build_opener", return_value=opener
            ),
            self.assertRaises(OSError) as caught,
        ):
            source.fetch_page(0, 1, 10)
        self.assertIs(error, caught.exception)
        self.assertEqual(1, opener.open.call_count)

    def test_strict_json_failures_escape_only_after_response_exit(self):
        cases = (
            (b"\xff", UnicodeDecodeError),
            (b"{", json.JSONDecodeError),
            (b'{"success":true,"result":{},"result":{}}', BundleError),
            (b"NaN", BundleError),
            (b"1e999", BundleError),
            (b"9" * 129, BundleError),
            (b'"\\ud800"', BundleError),
            (b"[" * 33 + b"0" + b"]" * 33, BundleError),
        )
        for raw, kind in cases:
            with self.subTest(raw=raw[:20]):
                response = self.response(raw)
                with self.assertRaises(kind) as caught:
                    self.fetch(response)
                self.assertIs(kind, type(caught.exception))
                self.assertEqual(["exit", "parse"], self.event_names()[-2:])
                self.assertEqual(1, response.closed)

    def test_action_success_requires_true_and_result_requires_dict(self):
        cases = (
            (b"[]", "CKAN action failed"),
            (wire(success=False), "CKAN action failed"),
            (wire(success=1), "CKAN action failed"),
            (b'{"success":true}', "CKAN returned an invalid page"),
            (b'{"success":true,"result":[]}', "CKAN returned an invalid page"),
        )
        for raw, message in cases:
            with self.subTest(raw=raw):
                response = self.response(raw)
                with self.assertRaises(source.MetadataSourceError) as caught:
                    self.fetch(response)
                self.assertEqual((message,), caught.exception.args)
                self.assertEqual(1, response.closed)

    def test_result_dictionary_is_returned_without_copy(self):
        result = {"count": 1, "results": []}
        parser = Mock(return_value={"success": True, "result": result})
        self.assertIs(result, self.fetch(self.response(), parser=parser))
        parser.assert_called_once_with(wire())

    def test_redirect_callback_always_refuses(self):
        with self.assertRaises(source.MetadataSourceError) as caught:
            source.NoRedirect().redirect_request(
                None, None, 302, "Found", {}, "https://example.test/"
            )
        self.assertEqual(
            ("CKAN redirects are not accepted",), caught.exception.args
        )


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def collect(self, pages, *, rows=1, ticks=None):
        self.calls.clear()
        pages = iter(pages)

        def fetch(start, count, deadline):
            self.calls.append((start, count, deadline))
            page = next(pages)
            if isinstance(page, Exception):
                raise page
            return page

        clock = itertools.repeat(0) if ticks is None else iter(ticks)
        with patch.object(source.time, "monotonic", side_effect=clock):
            return source.collect(fetch, 10, rows=rows)

    def test_invalid_rows_refuse_before_fetch(self):
        for rows in (True, False, 0, 101, "1", None):
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(
                    source.MetadataSourceError, "^page size exceeds its cap$"
                ):
                    self.collect([], rows=rows)
                self.assertEqual([], self.calls)

    def test_expired_deadline_refuses_before_fetch(self):
        with self.assertRaisesRegex(
            source.MetadataSourceError, "^refresh deadline exceeded$"
        ):
            self.collect([], ticks=[10])
        self.assertEqual([], self.calls)

    def test_non_dictionary_page_keeps_invalid_page_diagnostic(self):
        for page in (None, [], 1):
            with self.subTest(page=page):
                with self.assertRaises(source.MetadataSourceError) as caught:
                    self.collect([page])
                self.assertEqual(
                    ("CKAN returned an invalid page",), caught.exception.args
                )
                self.assertEqual([(0, 1, 10)], self.calls)

    def test_invalid_count_and_results_refuse_before_normalization(self):
        pages = [
            {"count": count, "results": [dataset()]}
            for count in (True, False, 0, 10001, 1.0, "1", None)
        ]
        pages.extend(
            {"count": 1, "results": items}
            for items in (None, (), [], [dataset(), dataset(2)])
        )
        for page in pages:
            with (
                self.subTest(page=page),
                patch.object(
                    source.normalize,
                    "normalize_package",
                    side_effect=AssertionError,
                ),
            ):
                with self.assertRaises(source.MetadataSourceError) as caught:
                    self.collect([page])
                self.assertEqual(
                    ("incomplete or oversized CKAN page",),
                    caught.exception.args,
                )

    def test_count_change_is_rejected_after_page_shape_validation(self):
        pages = (
            {"count": 2, "results": [dataset()]},
            {"count": 3, "results": [dataset(2)]},
        )
        with self.assertRaises(source.MetadataSourceError) as caught:
            self.collect(pages)
        self.assertEqual(
            ("CKAN count changed during download",), caught.exception.args
        )
        self.assertEqual([(0, 1, 10), (1, 1, 10)], self.calls)

    def test_wrong_page_size_wins_over_simultaneous_count_change(self):
        pages = (
            {"count": 2, "results": [dataset()]},
            {"count": 3, "results": []},
        )
        with self.assertRaises(source.MetadataSourceError) as caught:
            self.collect(pages)
        self.assertEqual(
            ("incomplete or oversized CKAN page",), caught.exception.args
        )
        self.assertEqual([(0, 1, 10), (1, 1, 10)], self.calls)

    def test_count_below_offset_still_reports_shape_failure(self):
        pages = (
            {"count": 3, "results": [dataset(), dataset(2)]},
            {"count": 1, "results": []},
        )
        with self.assertRaises(source.MetadataSourceError) as caught:
            self.collect(pages, rows=2)
        self.assertEqual(
            ("incomplete or oversized CKAN page",), caught.exception.args
        )
        self.assertEqual([(0, 2, 10), (2, 2, 10)], self.calls)

    def test_multiple_pages_return_original_objects_in_order(self):
        items = [
            dataset(index, unindexed={"n": index}) for index in range(1, 6)
        ]
        pages = [
            {"count": 5, "results": items[:2]},
            {"count": 5, "results": items[2:4]},
            {"count": 5, "results": items[4:]},
        ]
        result = self.collect(pages, rows=2)
        self.assertEqual([(0, 2, 10), (2, 2, 10), (4, 2, 10)], self.calls)
        self.assertEqual(5, len(result))
        for expected, actual in zip(items, result):
            self.assertIs(expected, actual)

    def test_invalid_record_rejects_without_normalizer_call(self):
        with patch.object(
            source.normalize, "normalize_package", side_effect=AssertionError
        ):
            with self.assertRaisesRegex(
                source.MetadataSourceError, "^invalid CKAN record$"
            ):
                self.collect([{"count": 1, "results": [None]}])

    def test_repeated_and_reversed_ids_are_not_sorted_or_skipped(self):
        for items in ([dataset(), dataset()], [dataset(2), dataset()]):
            with self.subTest(ids=[item["id"] for item in items]):
                with self.assertRaises(source.MetadataSourceError) as caught:
                    self.collect([{"count": 2, "results": items}], rows=2)
                self.assertEqual(
                    ("CKAN IDs are repeated or not in stable order",),
                    caught.exception.args,
                )
                self.assertEqual([(0, 2, 10)], self.calls)

    def test_record_normalization_error_precedes_duplicate_id(self):
        items = [dataset(), dataset(private=True)]
        with self.assertRaises(BundleError) as caught:
            self.collect([{"count": 2, "results": items}], rows=2)
        self.assertEqual(
            ("every dataset must have private=false",), caught.exception.args
        )

    def test_line_limit_counts_original_utf8_bytes_and_newline(self):
        item = dataset(unindexed="水")
        size = len(source.canonical(item) + b"\n")
        with patch.object(source.policy, "MAX_LINE_BYTES", size):
            self.assertIs(
                item, self.collect([{"count": 1, "results": [item]}])[0]
            )
        with (
            patch.object(source.policy, "MAX_LINE_BYTES", size - 1),
            self.assertRaisesRegex(
                source.MetadataSourceError, "snapshot exceeds its byte limit"
            ),
        ):
            self.collect([{"count": 1, "results": [item]}])

    def test_expanded_limit_counts_both_pages_without_dropping_items(self):
        items = [dataset(), dataset(2)]
        size = sum(len(source.canonical(item) + b"\n") for item in items)
        pages = [{"count": 2, "results": [item]} for item in items]
        with patch.object(source.policy, "MAX_EXPANDED_BYTES", size):
            self.assertEqual(items, self.collect(pages))
        with (
            patch.object(source.policy, "MAX_EXPANDED_BYTES", size - 1),
            self.assertRaisesRegex(
                source.MetadataSourceError, "snapshot exceeds its byte limit"
            ),
        ):
            self.collect(pages)
        self.assertEqual([(0, 1, 10), (1, 1, 10)], self.calls)

    def test_clock_expiry_before_second_page_does_not_fetch_it(self):
        with self.assertRaisesRegex(
            source.MetadataSourceError, "^refresh deadline exceeded$"
        ):
            self.collect([{"count": 2, "results": [dataset()]}], ticks=[0, 10])
        self.assertEqual([(0, 1, 10)], self.calls)

    def test_fetch_error_is_not_retried_or_translated(self):
        error = OSError("synthetic metadata fetch failure")
        with self.assertRaises(OSError) as caught:
            self.collect([error])
        self.assertIs(error, caught.exception)
        self.assertEqual([(0, 1, 10)], self.calls)


class StabilityTests(unittest.TestCase):
    def stable(self, pages):
        pages = iter(pages)
        self.calls = []

        def fetch(start, rows, deadline):
            self.calls.append((start, rows, deadline))
            page = next(pages)
            if isinstance(page, Exception):
                raise page
            return page

        with patch.object(source.time, "monotonic", return_value=0):
            return source.stable_download(10, fetch)

    def test_two_complete_paginated_scans_return_first_scan_objects(self):
        first = [dataset(index) for index in range(1, 102)]
        second = [dict(item) for item in first]
        pages = [
            {"count": 101, "results": items[begin : begin + 100]}
            for items in (first, second)
            for begin in (0, 100)
        ]
        result = self.stable(pages)
        self.assertEqual([(0, 100, 10), (100, 100, 10)] * 2, self.calls)
        for expected, actual in zip(first, result):
            self.assertIs(expected, actual)

    def test_changed_unindexed_metadata_rejects_after_both_scans(self):
        pages = [
            {"count": 1, "results": [dataset(unindexed=value)]}
            for value in ("before", "after")
        ]
        with self.assertRaisesRegex(
            source.MetadataSourceError,
            "^CKAN metadata changed between complete scans$",
        ):
            self.stable(pages)
        self.assertEqual([(0, 100, 10)] * 2, self.calls)

    def test_failure_in_either_scan_keeps_original_error_and_stops(self):
        error = OSError("synthetic scan failure")
        valid = {"count": 1, "results": [dataset()]}
        for pages in ([error], [valid, error]):
            with self.subTest(failed_scan=len(pages)):
                with self.assertRaises(OSError) as caught:
                    self.stable(pages)
                self.assertIs(error, caught.exception)
                self.assertEqual([(0, 100, 10)] * len(pages), self.calls)

    def test_incomplete_first_scan_never_starts_second_scan(self):
        first = [dataset(index) for index in range(1, 101)]
        error = OSError("synthetic later page failure")
        with self.assertRaises(OSError) as caught:
            self.stable([{"count": 101, "results": first}, error])
        self.assertIs(error, caught.exception)
        self.assertEqual([(0, 100, 10), (100, 100, 10)], self.calls)

    def test_default_transport_is_used_for_both_scans(self):
        page = {"count": 1, "results": [dataset()]}
        with (
            patch.object(source.time, "monotonic", return_value=0),
            patch.object(source, "fetch_page", return_value=page) as fetch,
        ):
            source.stable_download(10)
        self.assertEqual(
            [(0, 100, 10)] * 2, [c.args for c in fetch.call_args_list]
        )


class JsonContractTests(unittest.TestCase):
    def test_canonical_is_utf8_sorted_and_has_no_newline(self):
        self.assertEqual(
            '{"a":1,"主題":"水"}'.encode(),
            source.canonical({"主題": "水", "a": 1}),
        )
        with self.assertRaises(ValueError):
            source.canonical({"number": float("nan")})

    def test_strict_json_accepts_exact_depth_and_number_token_caps(self):
        self.assertEqual(int("9" * 128), source.strict_json(b"9" * 128))
        nested = 0
        for _ in range(32):
            nested = [nested]
        self.assertEqual(
            nested, source.strict_json(b"[" * 32 + b"0" + b"]" * 32)
        )

    def test_record_digest_uses_raw_canonical_lines(self):
        items = [dataset(unindexed="水"), dataset(2, unindexed="土")]
        expected = hashlib.sha256(
            b"".join(source.canonical(item) + b"\n" for item in items)
        ).hexdigest()
        self.assertEqual(expected, source.records_digest(items))


if __name__ == "__main__":
    unittest.main()
