"""Namespace probes execute against fake HTTP only; no Docker, models or GPU."""

import copy
from http import client as http_client
import io
import json
import socket
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from search.operations.deployment.candidate import probe
from search.operations.deployment import image_build, runtime_docker


CONTAINER = "a" * 64
REVISION = "b" * 40
TOKEN = "S" * 43
POLICY = {
    "scope": {
        "model": "scope-model-pin",
        "model_digest": "c" * 64,
        "options": {"num_ctx": 4096},
    },
    "planner": {
        "model": "planner-model-pin",
        "model_digest": "d" * 64,
        "options": {"num_ctx": 16384},
    },
}
CONTEXT = {
    "reference_time": "2026-10-29T08:00:00+00:00",
    "timezone": "Asia/Taipei",
    "local_datetime": "2026-10-29T16:00:00+08:00",
    "reference_date": "2026-10-29",
}
RESULT = {
    "status": "ok",
    "revision": REVISION,
    "search": {"revision": REVISION, "cards": [{"title": "Public dataset"}]},
}


class FakeDocker:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def call(self, arguments, *, timeout):
        self.calls.append((arguments, timeout))
        if isinstance(self.value, Exception):
            raise self.value
        return (
            self.value if type(self.value) is bytes else json.dumps(self.value).encode()
        )


class FakeHTTP:
    def __init__(self, responses, *, read_hook=None, detach_socket=False):
        self.responses = list(responses)
        self.calls = []
        self.reads = []
        self.closed = 0
        self.read_hook = read_hook
        self.detach_socket = detach_socket
        self.sockets = []

    def connection(self, host, port, timeout):
        fixture = self

        class Connection:
            def __init__(self):
                self.sock = Mock()
                fixture.sockets.append(self.sock)

            def request(self, method, path, body=None, headers=None):
                fixture.calls.append((host, port, timeout, method, path, body, headers))

            def getresponse(self):
                value = fixture.responses.pop(0)
                if isinstance(value, Exception):
                    raise value
                status, payload = value
                raw = (
                    payload if type(payload) is bytes else json.dumps(payload).encode()
                )
                if fixture.detach_socket:
                    self.sock = None

                class Response:
                    def read(self, maximum):
                        fixture.reads.append(maximum)
                        if fixture.read_hook is not None:
                            fixture.read_hook()
                        return raw[:maximum]

                response = Response()
                response.status = status
                return response

            def close(self):
                fixture.closed += 1

        return Connection()


def run_script(script, arguments, http, *, timers=None):
    output = io.StringIO()

    def timer(delay, callback):
        if timers is not None:
            timers.append((delay, callback))
        return Mock()

    with (
        patch.object(http_client, "HTTPConnection", side_effect=http.connection),
        patch("threading.Timer", side_effect=timer),
        patch.object(sys, "argv", ["namespace-probe", *arguments]),
        patch("sys.stdout", output),
    ):
        exec(
            compile(script, "<reviewed-namespace-probe>", "exec"),
            {"__name__": "__main__"},
        )
    return json.loads(output.getvalue()), output.getvalue()


def resident(role):
    policy = POLICY[role]
    return {
        "name": policy["model"],
        "digest": policy["model_digest"],
        "context_length": policy["options"]["num_ctx"],
        "size": 123456,
        "size_vram": 123456,
    }


def session(revision=REVISION):
    return {
        "token": TOKEN,
        "version": 0,
        "expires_in": 1800,
        "max_turns": 20,
        "revision": revision,
    }


class ResidencyScriptTests(unittest.TestCase):
    def run_probe(self, values):
        http = FakeHTTP([(200, value) for value in values])
        result, output = run_script(
            probe._RESIDENCY_SCRIPT, [json.dumps(probe._expected(POLICY))], http
        )
        return result, output, http

    def test_fixed_gets_prove_full_gpu_context_without_loading_models(self):
        result, _output, http = self.run_probe(
            [{"models": [resident("scope")]}, {"models": [resident("planner")]}]
        )
        self.assertEqual(result["status"], "resident")
        self.assertIs(result["full_gpu_residency_verified"], True)
        self.assertEqual([row["num_ctx"] for row in result["models"]], [4096, 16384])
        self.assertEqual(
            [(row[0], row[1], row[3], row[4]) for row in http.calls],
            [
                ("scope-model", 11434, "GET", "/api/ps"),
                ("answer-model", 11434, "GET", "/api/ps"),
            ],
        )
        self.assertTrue(all(row[5] is None for row in http.calls))
        self.assertEqual(http.closed, 2)
        self.assertEqual(
            probe.verify_residency(FakeDocker(result), CONTAINER, POLICY), result
        )

    def test_missing_partial_cpu_and_changed_residency_fail_without_post_or_retry(self):
        for mutation in ("missing", "digest", "context", "cpu", "extra", "bool"):
            with self.subTest(mutation=mutation):
                row = resident("scope")
                rows = [row]
                if mutation == "missing":
                    rows = []
                elif mutation == "digest":
                    row["digest"] = "e" * 64
                elif mutation == "context":
                    row["context_length"] = 8192
                elif mutation == "cpu":
                    row["size_vram"] -= 1
                elif mutation == "extra":
                    rows.append(resident("planner"))
                else:
                    row["context_length"] = True
                result, output, http = self.run_probe([{"models": rows}])
                self.assertIn(
                    result["error"],
                    {"shared_backend_not_resident", "shared_backend_residency_changed"},
                )
                self.assertEqual(len(http.calls), 1)
                self.assertNotIn("PRIVATE_SENTINEL", output)

    def test_network_and_malformed_responses_have_fixed_errors(self):
        for response in (
            TimeoutError("PRIVATE_SENTINEL"),
            (200, b"PRIVATE_SENTINEL"),
            (302, b"PRIVATE_SENTINEL"),
        ):
            with self.subTest(response=response):
                http = FakeHTTP([response])
                result, output = run_script(
                    probe._RESIDENCY_SCRIPT, [json.dumps(probe._expected(POLICY))], http
                )
                self.assertEqual(result, {"error": "shared_backend_residency_failed"})
                self.assertNotIn("PRIVATE_SENTINEL", output)


class QueryScriptTests(unittest.TestCase):
    def run_query(self, responses):
        http = FakeHTTP(responses)
        result, output = run_script(
            probe._QUERY_SCRIPT, [REVISION, json.dumps("找 CSV 資料")], http
        )
        return result, output, http

    def test_one_turn_identity_check_and_final_reset_keep_token_in_namespace(self):
        result, output, http = self.run_query(
            [
                (200, session()),
                (200, {"version": 1, "result": RESULT, "context_used": CONTEXT}),
                (200, {"status": "cleared"}),
            ]
        )
        self.assertEqual(result, {"result": RESULT, "context_used": CONTEXT})
        self.assertNotIn(TOKEN, output)
        self.assertEqual(
            [(row[3], row[4]) for row in http.calls],
            [
                ("POST", "/api/session"),
                ("POST", "/api/turn"),
                ("DELETE", "/api/session"),
            ],
        )
        self.assertTrue(all(row[:2] == ("127.0.0.1", 8000) for row in http.calls))
        for row in http.calls:
            self.assertEqual(row[6]["Host"], "127.0.0.1:18083")
            self.assertEqual(row[6]["Origin"], "http://127.0.0.1:18083")
        self.assertNotIn("Authorization", http.calls[0][6])
        self.assertEqual(http.calls[1][6]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(
            json.loads(http.calls[1][5]),
            {
                "query": "找 CSV 資料",
                "version": 0,
                "top_k": 5,
                "timezone": "Asia/Taipei",
            },
        )

    def test_wrong_session_revision_resets_without_sending_a_turn(self):
        result, output, http = self.run_query(
            [(200, session("f" * 40)), (200, {"status": "cleared"})]
        )
        self.assertEqual(result, {"error": "shared_backend_query_identity_changed"})
        self.assertNotIn(TOKEN, output)
        self.assertEqual([row[3] for row in http.calls], ["POST", "DELETE"])

    def test_busy_timeout_and_bad_turn_reset_without_retry_or_error_body(self):
        for response, expected in (
            ((429, b"PRIVATE_SENTINEL"), "shared_backend_query_busy"),
            (socket.timeout("PRIVATE_SENTINEL"), "shared_backend_query_timeout"),
            ((503, b"PRIVATE_SENTINEL"), "shared_backend_query_failed"),
            ((200, b"PRIVATE_SENTINEL"), "shared_backend_query_failed"),
            ((200, b"x" * (1024 * 1024 + 1)), "shared_backend_query_failed"),
        ):
            with self.subTest(expected=expected):
                result, output, http = self.run_query(
                    [(200, session()), response, (200, {"status": "cleared"})]
                )
                self.assertEqual(result, {"error": expected})
                self.assertNotIn(TOKEN, output)
                self.assertNotIn("PRIVATE_SENTINEL", output)
                self.assertEqual(
                    [row[3] for row in http.calls], ["POST", "POST", "DELETE"]
                )

    def test_result_or_search_revision_drift_fails_and_resets(self):
        for target in ("result", "search"):
            value = copy.deepcopy(RESULT)
            (value if target == "result" else value["search"])["revision"] = "f" * 40
            with self.subTest(target=target):
                result, _output, http = self.run_query(
                    [
                        (200, session()),
                        (200, {"version": 1, "result": value, "context_used": CONTEXT}),
                        (200, {"status": "cleared"}),
                    ]
                )
                self.assertEqual(
                    result, {"error": "shared_backend_query_identity_changed"}
                )
                self.assertEqual(http.calls[-1][3], "DELETE")

    def test_reset_failure_prevents_a_success_result(self):
        result, output, _http = self.run_query(
            [
                (200, session()),
                (200, {"version": 1, "result": RESULT, "context_used": CONTEXT}),
                (503, b"PRIVATE_SENTINEL"),
            ]
        )
        self.assertEqual(result, {"error": "shared_backend_query_reset_failed"})
        self.assertNotIn("Public dataset", output)
        self.assertNotIn(TOKEN, output)

    def test_deadline_interrupts_detached_response_socket_then_resets_session(self):
        timers = []

        def read_hook():
            if len(http.calls) == 2:
                self.assertEqual(timers[1][0], 124)
                timers[1][1]()
                raise OSError("PRIVATE_SENTINEL")

        http = FakeHTTP(
            [
                (200, session()),
                (200, {"version": 1, "result": RESULT, "context_used": CONTEXT}),
                (200, {"status": "cleared"}),
            ],
            read_hook=read_hook,
            detach_socket=True,
        )
        result, output = run_script(
            probe._QUERY_SCRIPT, [REVISION, json.dumps("找資料")], http, timers=timers
        )
        self.assertEqual(result, {"error": "shared_backend_query_timeout"})
        self.assertEqual(http.calls[-1][3], "DELETE")
        http.sockets[1].shutdown.assert_called_once_with(socket.SHUT_RDWR)
        self.assertEqual(timers[0][0], 129)
        self.assertNotIn("PRIVATE_SENTINEL", output)
        self.assertNotIn(TOKEN, output)


class HostWrapperTests(unittest.TestCase):
    def test_real_runtime_wrapper_preserves_query_timeout_from_fake_runners(self):
        for error in (
            subprocess.TimeoutExpired("PRIVATE_SENTINEL", 130),
            image_build.ImageBuildError("image_build_command_timeout"),
        ):
            with self.subTest(error=type(error).__name__):
                runner = Mock(side_effect=error)
                docker = runtime_docker.Docker("/private-client", runner)
                with self.assertRaisesRegex(
                    probe.SharedBackendProbeError, "^shared_backend_query_timeout$"
                ) as caught:
                    probe.query(docker, CONTAINER, "找資料", REVISION)
                self.assertEqual(runner.call_count, 1)
                self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))

    def test_query_uses_verified_namespace_and_returns_only_interactive_result(self):
        docker = FakeDocker({"result": RESULT, "context_used": CONTEXT})
        result = probe.query(docker, CONTAINER, "  找 CSV 資料  ", REVISION)
        self.assertEqual(set(result), {"result", "context_used"})
        arguments, timeout = docker.calls[0]
        self.assertEqual(
            arguments[:7], ["exec", CONTAINER, "python", "-I", "-S", "-B", "-c"]
        )
        self.assertEqual(timeout, 130)
        self.assertEqual(arguments[-2], REVISION)
        self.assertEqual(json.loads(arguments[-1]), "找 CSV 資料")
        self.assertNotIn(TOKEN, str(arguments))

    def test_invalid_input_never_executes_docker(self):
        docker = FakeDocker({})
        for text in ("", " " * 10, "x" * 513, "a\u200bb", "\x00", None):
            with (
                self.subTest(text=text),
                self.assertRaises(probe.SharedBackendProbeError),
            ):
                probe.query(docker, CONTAINER, text, REVISION)
        for container, revision in (("short", REVISION), (CONTAINER, "main")):
            with self.assertRaises(probe.SharedBackendProbeError):
                probe.query(docker, container, "找資料", revision)
        with self.assertRaises(probe.SharedBackendProbeError):
            probe.verify_residency(docker, CONTAINER, {})
        self.assertEqual(docker.calls, [])

    def test_subprocess_and_untrusted_stdout_errors_are_closed_and_not_retried(self):
        for value in (
            ValueError("PRIVATE_SENTINEL"),
            subprocess.TimeoutExpired("PRIVATE_SENTINEL", 130),
            b"PRIVATE_SENTINEL",
            {"error": "PRIVATE_SENTINEL"},
            {"result": RESULT, "context_used": CONTEXT, "token": TOKEN},
        ):
            with self.subTest(value=value):
                docker = FakeDocker(value)
                with self.assertRaises(probe.SharedBackendProbeError) as caught:
                    probe.query(docker, CONTAINER, "找資料", REVISION)
                self.assertRegex(str(caught.exception), r"^shared_backend_[a-z_]+$")
                self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertEqual(len(docker.calls), 1)

    def test_residency_summary_identity_and_full_gpu_metrics_are_rechecked(self):
        models = [
            {**item, "size_bytes": 12, "vram_bytes": 12}
            for item in probe._expected(POLICY)
        ]
        original = {
            "schema_version": 1,
            "status": "resident",
            "full_gpu_residency_verified": True,
            "models": models,
        }
        for mutation in ("digest", "vram", "extra", "flag"):
            value = copy.deepcopy(original)
            if mutation == "digest":
                value["models"][0]["digest"] = "f" * 64
            elif mutation == "vram":
                value["models"][1]["vram_bytes"] = 0
            elif mutation == "extra":
                value["models"][0]["PRIVATE_SENTINEL"] = TOKEN
            else:
                value["full_gpu_residency_verified"] = False
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(probe.SharedBackendProbeError),
            ):
                probe.verify_residency(FakeDocker(value), CONTAINER, POLICY)


if __name__ == "__main__":
    unittest.main()
