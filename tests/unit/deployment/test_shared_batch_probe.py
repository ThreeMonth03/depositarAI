"""Ordered BFF cases use fake HTTP/socket channels; no Docker or live services."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import math
import socket
import unittest
from unittest.mock import Mock, patch

from search.operations.deployment import shared_batch_probe as probe


ENDPOINT = "172.24.0.7"
REVISION = "a" * 40
MANIFEST = "b" * 64
TOKEN = "S" * 43
PRIVATE = "PRIVATE_SENTINEL_DO_NOT_REFLECT"


def session(token=TOKEN, revision=REVISION):
    return {
        "token": token,
        "version": 0,
        "expires_in": 1800,
        "max_turns": 20,
        "revision": revision,
    }


def response(version=1, *, status="ok"):
    context = {
        "reference_time": f"2026-10-30T08:{version:02d}:00+00:00",
        "timezone": "Asia/Taipei",
        "local_datetime": f"2026-10-30T16:{version:02d}:00+08:00",
        "reference_date": "2026-10-30",
    }
    search = (
        {
            "revision": REVISION,
            "catalog_manifest_sha256": MANIFEST,
            "context_used": context,
            "timing_ms": {"retrieve": 1.25, "rerank": 2.5},
            "cards": [
                {
                    "title": "公開資料",
                    "temporal_coverage": {"start": "2024", "end": "2024"},
                    "resource_formats": ["CSV"],
                }
            ],
            "ranking": "nvidia-rerank",
            "parsed_conditions": {"formats": ["CSV"]},
        }
        if status == "ok"
        else None
    )
    return {
        "version": version,
        "context_used": context,
        "result": {
            "status": status,
            "revision": REVISION,
            "search": search,
            "message": "實際回覆",
            "state": {
                "topic": "氣候",
                "formats": ["CSV"],
                "time": None,
                "unapplied_time": None,
            },
            "reason": "none",
            "notices": [],
            "timing_ms": {"total": 4.125},
        },
    }


class FakeHTTP:
    def __init__(self, values, *, hook=None, events=None):
        self.values = list(values)
        self.hook = hook
        self.events = [] if events is None else events
        self.calls = []
        self.connections = []
        self.timers = []

    def connection(self, endpoint, timeout):
        fixture = self

        class Connection:
            def __init__(self):
                self.sock = Mock()
                self.channel = self.sock
                self.abort = Mock()
                self.finish = Mock()

            def request(self, method, path, body=None, headers=None):
                fixture.calls.append(
                    {
                        "endpoint": endpoint,
                        "timeout": timeout,
                        "method": method,
                        "path": path,
                        "body": body,
                        "headers": headers,
                    }
                )
                fixture.events.append(path)

            def getresponse(self):
                value = fixture.values.pop(0)
                if isinstance(value, BaseException):
                    raise value
                status, payload = value
                raw = (
                    payload
                    if type(payload) is bytes
                    else json.dumps(payload, ensure_ascii=False).encode()
                )
                self.sock = None  # Connection: close detaches the response channel.

                class Response:
                    def __init__(self):
                        self.status = status
                        self.close = Mock()

                    def read(self, maximum):
                        if fixture.hook is not None:
                            fixture.hook(fixture)
                        return raw[:maximum]

                return Response()

        connection = Connection()
        self.connections.append(connection)
        return connection

    def timer(self, delay, callback):
        timer = Mock()
        timer.delay = delay
        timer.callback = callback
        self.timers.append(timer)
        return timer

    def run(self, queries, **options):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(probe, "_Connection", side_effect=self.connection),
            patch.object(probe.threading, "Timer", side_effect=self.timer),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            report = probe.conversation(
                options.pop("endpoint", ENDPOINT),
                queries,
                options.pop("revision", REVISION),
                options.pop("manifest_sha256", MANIFEST),
                **options,
            )
        assert output.getvalue() == "" and errors.getvalue() == ""
        return report


class ConversationTests(unittest.TestCase):
    def test_raw_whitespace_and_fullwidth_query_are_sent_exactly_after_validation(self):
        queries = ["  找　氣候 ＣＳＶ 資料  ", "\t我要２０２４年的\n"]
        original = list(queries)
        http = FakeHTTP(
            [
                (200, session()),
                (200, response(1)),
                (200, response(2)),
                (200, {"status": "cleared"}),
            ]
        )
        report = http.run(queries)
        self.assertEqual(report["status"], "complete")
        turns = [row for row in http.calls if row["path"] == "/api/turn"]
        self.assertEqual([json.loads(row["body"])["query"] for row in turns], original)
        self.assertEqual(queries, original)
        self.assertTrue(report["session_reset"])

        invalid = FakeHTTP([])
        report = invalid.run([queries[0], "　\u200bＣＳＶ　"])
        self.assertEqual(report["error"], "shared_batch_input_invalid")
        self.assertEqual(invalid.calls, [])

    def test_ordered_turns_preserve_actual_context_timing_cards_and_versions(self):
        values = [response(index) for index in range(1, 4)]
        events = []
        http = FakeHTTP(
            [
                (200, session()),
                *((200, value) for value in values),
                (200, {"status": "cleared"}),
            ],
            events=events,
        )
        report = http.run(
            ["找氣候資料", "只要CSV", "我要2024年的"],
            checkpoint=lambda: events.append("checkpoint"),
        )
        self.assertEqual(set(report), {"status", "turns", "error", "session_reset"})
        self.assertEqual(report["status"], "complete")
        self.assertIsNone(report["error"])
        self.assertIs(report["session_reset"], True)
        self.assertEqual([turn["response"] for turn in report["turns"]], values)
        self.assertEqual(
            events,
            [
                "/api/session",
                "checkpoint",
                "/api/turn",
                "checkpoint",
                "/api/turn",
                "checkpoint",
                "/api/turn",
                "/api/session",
            ],
        )
        turns = [row for row in http.calls if row["path"] == "/api/turn"]
        self.assertEqual(
            [json.loads(row["body"])["version"] for row in turns], [0, 1, 2]
        )
        for row in http.calls:
            self.assertEqual(row["endpoint"], ENDPOINT)
            self.assertEqual(row["headers"]["Host"], "127.0.0.1:18083")
            self.assertEqual(row["headers"]["Origin"], "http://127.0.0.1:18083")
        self.assertNotIn("Authorization", http.calls[0]["headers"])
        self.assertTrue(
            all(
                row["headers"]["Authorization"] == "Bearer " + TOKEN
                for row in http.calls[1:]
            )
        )
        self.assertTrue(all(json.loads(row["body"])["top_k"] == 5 for row in turns))
        self.assertTrue(
            all(json.loads(row["body"])["timezone"] == "Asia/Taipei" for row in turns)
        )
        self.assertTrue(
            all(
                type(turn["wall_seconds"]) is float
                and math.isfinite(turn["wall_seconds"])
                and turn["wall_seconds"] >= 0
                for turn in report["turns"]
            )
        )
        self.assertNotIn(TOKEN, repr(report))
        self.assertTrue(
            all(connection.finish.call_count == 1 for connection in http.connections)
        )
        self.assertTrue(all(timer.cancel.call_count == 1 for timer in http.timers))

    def test_rejection_and_clarification_are_valid_responses_with_continuous_versions(
        self,
    ):
        values = [
            response(1, status="rejected"),
            response(2, status="clarification_required"),
            response(3),
        ]
        http = FakeHTTP(
            [
                (200, session()),
                *((200, value) for value in values),
                (200, {"status": "cleared"}),
            ]
        )
        report = http.run(["問題一", "問題二", "問題三"])
        self.assertEqual(report["status"], "complete")
        self.assertTrue(all(turn["status"] == "ok" for turn in report["turns"]))
        self.assertEqual(
            [turn["response"]["result"]["status"] for turn in report["turns"]],
            ["rejected", "clarification_required", "ok"],
        )

    def test_each_case_gets_its_own_session_and_starts_at_version_zero(self):
        second_token = "T" * 43
        http = FakeHTTP(
            [
                (200, session()),
                (200, response()),
                (200, {"status": "cleared"}),
                (200, session(second_token)),
                (200, response()),
                (200, {"status": "cleared"}),
            ]
        )
        self.assertEqual(http.run(["第一個案例"])["status"], "complete")
        self.assertEqual(http.run(["另一個案例"])["status"], "complete")
        turns = [row for row in http.calls if row["path"] == "/api/turn"]
        self.assertEqual([json.loads(row["body"])["version"] for row in turns], [0, 0])
        self.assertEqual(
            [row["headers"]["Authorization"] for row in turns],
            ["Bearer " + TOKEN, "Bearer " + second_token],
        )

    def test_busy_timeout_and_http_fail_fast_keep_completed_turn_and_reset(self):
        for failure, code in (
            ((429, PRIVATE.encode()), "busy"),
            (socket.timeout(PRIVATE), "timeout"),
            ((503, PRIVATE.encode()), "http_failed"),
            ((302, PRIVATE.encode()), "http_failed"),
        ):
            with self.subTest(code=code):
                http = FakeHTTP(
                    [
                        (200, session()),
                        (200, response()),
                        failure,
                        (200, {"status": "cleared"}),
                    ]
                )
                report = http.run(["第一問", "第二問", "不該送出的第三問"])
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["error"], "shared_batch_" + code)
                self.assertEqual(
                    [turn["status"] for turn in report["turns"]], ["ok", "failed"]
                )
                self.assertEqual(report["turns"][0]["response"], response())
                self.assertTrue(report["session_reset"])
                self.assertEqual(http.calls[-1]["method"], "DELETE")
                self.assertEqual(len(http.calls), 4)
                self.assertNotIn(PRIVATE, repr(report))
                self.assertNotIn(TOKEN, repr(report))

    def test_checkpoint_failure_and_keyboard_interrupt_reset_without_next_turn(self):
        for failure, code in (
            (ValueError(PRIVATE), "checkpoint_failed"),
            (KeyboardInterrupt(PRIVATE), "interrupted"),
            (False, "checkpoint_failed"),
        ):
            with self.subTest(code=code):
                checkpoint = Mock(side_effect=[None, failure])
                http = FakeHTTP(
                    [(200, session()), (200, response()), (200, {"status": "cleared"})]
                )
                report = http.run(["第一問", "第二問"], checkpoint=checkpoint)
                self.assertEqual(report["error"], "shared_batch_" + code)
                self.assertEqual(len(report["turns"]), 2)
                self.assertEqual(report["turns"][1]["status"], "failed")
                self.assertEqual(report["turns"][1]["request_seconds"], 0.0)
                self.assertTrue(report["session_reset"])
                self.assertEqual(len(http.calls), 3)
                self.assertNotIn(PRIVATE, repr(report))

    def test_keyboard_interrupt_during_http_still_resets_session(self):
        http = FakeHTTP(
            [(200, session()), KeyboardInterrupt(PRIVATE), (200, {"status": "cleared"})]
        )
        report = http.run(["問題"])
        self.assertEqual(report["error"], "shared_batch_interrupted")
        self.assertEqual(report["turns"][0]["error"], "shared_batch_interrupted")
        self.assertTrue(report["session_reset"])
        self.assertEqual(http.calls[-1]["method"], "DELETE")

    def test_turn_request_time_is_separate_from_checkpoint_wall_and_backend_timing(
        self,
    ):
        clock = [100.0]

        def hook(http):
            if len(http.calls) == 2:
                clock[0] += 3

        def checkpoint():
            clock[0] += 2

        http = FakeHTTP(
            [(200, session()), (200, response()), (200, {"status": "cleared"})],
            hook=hook,
        )
        with patch.object(probe.time, "monotonic", side_effect=lambda: clock[0]):
            report = http.run(["問題"], checkpoint=checkpoint)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["turns"][0]["wall_seconds"], 5.0)
        self.assertEqual(report["turns"][0]["request_seconds"], 3.0)
        self.assertEqual(
            report["turns"][0]["response"]["result"]["timing_ms"], {"total": 4.125}
        )

    def test_session_identity_drift_is_reset_before_any_turn(self):
        http = FakeHTTP(
            [(200, session(revision="f" * 40)), (200, {"status": "cleared"})]
        )
        report = http.run(["問題"])
        self.assertEqual(report["error"], "shared_batch_identity_changed")
        self.assertEqual(report["turns"], [])
        self.assertTrue(report["session_reset"])
        self.assertEqual([row["method"] for row in http.calls], ["POST", "DELETE"])

    def test_response_identity_schema_context_and_secret_echo_are_rejected(self):
        for mutation in (
            "revision",
            "search-revision",
            "manifest",
            "version",
            "context",
            "framing",
            "token",
        ):
            value = copy.deepcopy(response())
            if mutation == "revision":
                value["result"]["revision"] = "f" * 40
            elif mutation == "search-revision":
                value["result"]["search"]["revision"] = "f" * 40
            elif mutation == "manifest":
                value["result"]["search"]["catalog_manifest_sha256"] = "f" * 64
            elif mutation == "version":
                value["version"] = True
            elif mutation == "context":
                value["context_used"]["reference_date"] = "2020-01-01"
            elif mutation == "framing":
                value["extra"] = PRIVATE
            else:
                value["result"]["message"] = TOKEN
            with self.subTest(mutation=mutation):
                http = FakeHTTP(
                    [(200, session()), (200, value), (200, {"status": "cleared"})]
                )
                report = http.run(["問題"])
                self.assertEqual(report["status"], "failed")
                self.assertNotIn("response", report["turns"][0])
                self.assertTrue(report["session_reset"])
                self.assertNotIn(PRIVATE, repr(report))
                self.assertNotIn(TOKEN, repr(report))

    def test_reset_failure_fails_case_and_preserves_completed_metadata(self):
        http = FakeHTTP([(200, session()), (200, response()), (503, PRIVATE.encode())])
        report = http.run(["問題"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error"], "shared_batch_reset_failed")
        self.assertFalse(report["session_reset"])
        self.assertEqual(report["turns"][0]["response"], response())
        self.assertNotIn(TOKEN, repr(report))

    def test_overlong_and_duplicate_json_fail_with_no_response_body_in_report(self):
        for raw in (
            b"x" * (probe.MAX_RESPONSE_BYTES + 1),
            b'{"version":1,"version":2}',
        ):
            with self.subTest(raw_bytes=len(raw)):
                http = FakeHTTP(
                    [(200, session()), (200, raw), (200, {"status": "cleared"})]
                )
                report = http.run(["問題"])
                self.assertEqual(report["status"], "failed")
                self.assertTrue(report["session_reset"])
                self.assertLess(len(json.dumps(report)), 1024)

    def test_expired_deadline_sends_nothing_and_checkpoint_overrun_sends_only_reset(
        self,
    ):
        clock = [100.0]
        with patch.object(probe.time, "monotonic", side_effect=lambda: clock[0]):
            http = FakeHTTP([])
            report = http.run(["問題"], deadline=99)
            self.assertEqual(report["error"], "shared_batch_timeout")
            self.assertEqual(http.calls, [])
            http = FakeHTTP([(200, session()), (200, {"status": "cleared"})])

            def checkpoint():
                clock[0] = 116.0

            report = http.run(["問題"], checkpoint=checkpoint, deadline=120)
            self.assertEqual(report["turns"][0]["error"], "shared_batch_timeout")
            self.assertTrue(report["session_reset"])
            self.assertEqual(
                [row["path"] for row in http.calls], ["/api/session", "/api/session"]
            )
            self.assertTrue(all(timer.delay <= 5 for timer in http.timers))

    def test_socket_deadline_interrupts_detached_response_then_resets(self):
        def hook(http):
            if len(http.calls) == 2:
                http.timers[-1].callback()
                raise OSError(PRIVATE)

        http = FakeHTTP(
            [(200, session()), (200, response()), (200, {"status": "cleared"})],
            hook=hook,
        )
        report = http.run(["問題"])
        self.assertEqual(report["error"], "shared_batch_timeout")
        self.assertTrue(report["session_reset"])
        http.connections[1].abort.assert_called_once_with()
        self.assertTrue(all(timer.delay <= 125 for timer in http.timers))

    def test_invalid_endpoint_queries_identity_or_deadline_never_open_connection(self):
        cases = [
            (dict(endpoint=value), ["問題"])
            for value in (
                "https://172.24.0.7",
                "172.24.0.7:8000",
                "localhost",
                "127.0.0.1",
                "8.8.8.8",
                "169.254.1.1",
                "100.64.1.1",
                "198.18.0.1",
                "::1",
                "010.1.1.1",
                123,
            )
        ]
        cases += [
            ({}, value)
            for value in (
                [],
                ["x"] * 9,
                ("問題",),
                None,
                [" "],
                ["x" * 513],
                ["a\u200bb"],
                ["\x00"],
            )
        ]
        cases += [
            (options, ["問題"])
            for options in (
                {"revision": "main"},
                {"manifest_sha256": "short"},
                {"deadline": float("nan")},
                {"deadline": True},
                {"checkpoint": PRIVATE},
            )
        ]
        for options, queries in cases:
            with self.subTest(options=options, queries_type=type(queries).__name__):
                http = FakeHTTP([])
                report = http.run(queries, **options)
                self.assertEqual(report["status"], "failed")
                self.assertRegex(report["error"], r"^shared_batch_[a-z_]+$")
                self.assertEqual(http.calls, [])
                self.assertEqual(report["turns"], [])
                self.assertFalse(report["session_reset"])

    def test_all_rfc1918_ranges_and_maximum_eight_turns_are_supported(self):
        for endpoint in ("10.2.3.4", "172.16.0.2", "192.168.1.2"):
            with self.subTest(endpoint=endpoint):
                http = FakeHTTP(
                    [
                        (200, session()),
                        *((200, response(index)) for index in range(1, 9)),
                        (200, {"status": "cleared"}),
                    ]
                )
                report = http.run(["問題"] * 8, endpoint=endpoint)
                self.assertEqual(report["status"], "complete")
                self.assertEqual(len(report["turns"]), 8)
                self.assertLess(len(json.dumps(report).encode()), 3 * 1024 * 1024)


class NumericConnectionTests(unittest.TestCase):
    def test_transport_uses_only_numeric_ipv4_port_8000_and_retains_detached_channel(
        self,
    ):
        channel = Mock()
        with (
            patch.object(probe.socket, "socket", return_value=channel) as create,
            patch.object(
                probe.socket, "getaddrinfo", side_effect=AssertionError("DNS")
            ),
        ):
            connection = probe._Connection(ENDPOINT, 5)
            connection.connect()
            self.assertEqual(connection.debuglevel, 0)
            create.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
            channel.connect.assert_called_once_with((ENDPOINT, 8000))
            connection.sock = None
            connection.abort()
            channel.shutdown.assert_called_once_with(socket.SHUT_RDWR)
            connection.finish()
            self.assertGreaterEqual(channel.close.call_count, 1)


if __name__ == "__main__":
    unittest.main()
