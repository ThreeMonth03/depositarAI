"""Pinned-model warmup mocked at the HTTP boundary, with no real inference."""

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from search.model_policy import (
    PLANNER_MODEL,
    PLANNER_MODEL_DIGEST,
    OLLAMA_BASE_URL,
    SCOPE_MODEL,
    SCOPE_MODEL_DIGEST,
)
from search.transport.http import HTTPRequest, HTTPResponse
from search.operations.shared.runtime_probe import ProbeTransport, probe_models


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ModelHTTP:
    def __init__(self, role, clock):
        self.role = role
        self.clock = clock
        self.model = SCOPE_MODEL if role == "scope" else PLANNER_MODEL
        self.digest = SCOPE_MODEL_DIGEST if role == "scope" else PLANNER_MODEL_DIGEST
        self.ctx = 4096 if role == "scope" else 16384
        self.requests = []
        self.loaded = False
        self.tags_digest = self.digest
        self.ps_digest = self.digest
        self.vram = 1000
        self.size = 1000
        self.delay = 11.78
        self.completion_reason = "stop"
        self.private_content = False
        self.duplicate_resident = False
        self.fail_path = None

    def send(self, request, *, timeout, max_bytes):
        self.requests.append((request, timeout, max_bytes))
        path = request.url.removeprefix(OLLAMA_BASE_URL)
        if path == self.fail_path:
            raise OSError("PRIVATE UPSTREAM ERROR")
        if path == "/api/tags":
            value = {"models": [{"name": self.model, "digest": self.tags_digest}]}
        elif path == "/api/ps":
            value = {"models": []}
            if self.loaded:
                row = {
                    "name": self.model,
                    "digest": self.ps_digest,
                    "size": self.size,
                    "size_vram": self.vram,
                    "context_length": self.ctx,
                }
                value["models"] = [row, row] if self.duplicate_resident else [row]
        elif path == "/api/chat":
            self.clock.now += self.delay
            self.loaded = True
            output = {"decision": "search"} if self.role == "scope" else {"ready": True}
            value = {
                "model": self.model,
                "done": True,
                "done_reason": self.completion_reason,
                "message": {
                    "role": "assistant",
                    "content": "PRIVATE CONTENT"
                    if self.private_content
                    else json.dumps(output),
                    "thinking": "PRIVATE THINKING",
                },
                "load_duration": 9_610_000_000,
                "prompt_eval_duration": 300_000_000,
                "eval_duration": 411_750_000,
                "prompt_eval_count": 100,
                "eval_count": 12,
            }
        else:
            raise AssertionError("Unapproved request")
        return HTTPResponse(
            200,
            (("Content-Type", "application/json"),),
            json.dumps(value).encode(),
            request.url,
        )


class RuntimeProbeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.scope = ModelHTTP("scope", self.clock)
        self.planner = ModelHTTP("planner", self.clock)
        self.transports = {"scope": self.scope, "planner": self.planner}

    def run_probe(self, **kwargs):
        return probe_models(transports=self.transports, clock=self.clock, **kwargs)

    def test_loads_exact_two_pins_reports_full_gpu_and_no_generated_text(self):
        report = self.run_probe()
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["keep_alive"], -1)
        self.assertEqual(report["gpu_uuid_verification"], "external_required")
        self.assertFalse(report["search_quality_verified"])
        self.assertEqual(report["total_ms"], 23560)
        for row in report["models"]:
            self.assertEqual(row["processor"], "GPU")
            self.assertFalse(row["resident_before"])
            self.assertEqual(row["load_ms"], 9610)
            self.assertEqual(row["decode_ms"], 411.75)
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertEqual(
            [row["model"] for row in report["models"]], [SCOPE_MODEL, PLANNER_MODEL]
        )

    def test_warmup_context_and_inference_options_are_server_owned(self):
        self.run_probe()
        for upstream in (self.scope, self.planner):
            chats = [
                request
                for request, _, _ in upstream.requests
                if request.method == "POST"
            ]
            self.assertEqual(len(chats), 1)
            payload = json.loads(chats[0].body)
            self.assertEqual(payload["model"], upstream.model)
            self.assertEqual(payload["options"]["num_ctx"], upstream.ctx)
            self.assertEqual(
                payload["options"]["num_predict"],
                64 if upstream.role == "scope" else 2048,
            )
            self.assertEqual(payload["keep_alive"], -1)
            self.assertFalse(payload["stream"])
            self.assertNotIn("tools", payload)
            self.assertNotIn("tool_choice", payload)
        self.assertEqual(json.loads(self.planner.requests[2][0].body)["think"], "low")

    def test_pre_and_post_pin_checks_and_post_residency_are_required(self):
        self.run_probe()
        for upstream in (self.scope, self.planner):
            self.assertEqual(
                [
                    request.url.removeprefix(OLLAMA_BASE_URL)
                    for request, _, _ in upstream.requests
                ],
                ["/api/tags", "/api/ps", "/api/chat", "/api/ps", "/api/tags"],
            )

    def test_mismatched_pin_cannot_trigger_inference_or_download(self):
        self.scope.tags_digest = "a" * 64
        report = self.run_probe()
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(
            any(request.method == "POST" for request, _, _ in self.scope.requests)
        )
        self.assertEqual(report["models"][0]["code"], "warmup_failed")

    def test_cpu_fallback_or_mixed_residency_is_not_ready(self):
        for vram, processor in ((0, "CPU"), (500, "CPU+GPU")):
            self.scope.vram = vram
            with self.subTest(vram=vram):
                report = self.run_probe()
                row = report["models"][0]
                self.assertEqual(report["status"], "blocked")
                self.assertEqual(row["processor"], processor)
                self.assertEqual(row["code"], "full_gpu_residency_required")

    def test_wrong_context_resident_digest_duplicate_and_metrics_block(self):
        for field, invalid in (
            ("ctx", 8192),
            ("ps_digest", "a" * 64),
            ("duplicate_resident", True),
            ("size", True),
        ):
            before = getattr(self.planner, field)
            setattr(self.planner, field, invalid)
            with self.subTest(field=field):
                self.assertEqual(self.run_probe()["status"], "blocked")
            setattr(self.planner, field, before)

    def test_incomplete_or_private_output_is_not_readiness(self):
        for field, invalid in (
            ("completion_reason", "length"),
            ("private_content", True),
        ):
            previous = getattr(self.scope, field)
            setattr(self.scope, field, invalid)
            with self.subTest(field=field):
                report = self.run_probe()
                self.assertEqual(report["status"], "blocked")
                self.assertNotIn("PRIVATE", json.dumps(report))
            setattr(self.scope, field, previous)

    def test_timeout_exhausts_total_budget_without_retrying_or_probing_next_model(self):
        self.scope.delay = 50
        report = self.run_probe(budget_seconds=20)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(
            len(
                [
                    request
                    for request, _, _ in self.scope.requests
                    if request.method == "POST"
                ]
            ),
            1,
        )
        self.assertEqual(self.planner.requests, [])
        self.assertLessEqual(self.scope.requests[-1][1], 20)

    def test_upstream_exceptions_redirects_and_size_are_fail_closed(self):
        self.scope.fail_path = "/api/ps"
        report = self.run_probe()
        self.assertEqual(report["status"], "blocked")
        self.assertNotIn("PRIVATE", json.dumps(report))
        response = HTTPResponse(200, (), b"{}", "http://evil.example")
        bad = Mock()
        bad.send.return_value = response
        report = probe_models(
            transports={"scope": bad, "planner": bad}, clock=self.clock
        )
        self.assertEqual(report["status"], "blocked")
        bad.send.return_value = HTTPResponse(
            200, (), b" " * (128 * 1024 + 1), OLLAMA_BASE_URL + "/api/tags"
        )
        self.assertEqual(
            probe_models(transports={"scope": bad, "planner": bad}, clock=self.clock)[
                "status"
            ],
            "blocked",
        )

    def test_budget_is_bounded_not_bool_nan_negative_or_infinite(self):
        for budget in (True, 0, -1, 601, float("nan"), float("inf"), "300"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                self.run_probe(budget_seconds=budget)

    def test_existing_resident_model_still_runs_inference_not_tags_only(self):
        self.scope.loaded = True
        self.planner.loaded = True
        result = self.run_probe()
        self.assertEqual(result["status"], "ready")
        self.assertTrue(all(row["resident_before"] for row in result["models"]))
        self.assertTrue(
            any(request.method == "POST" for request, _, _ in self.scope.requests)
        )

    def test_probe_transport_accepts_only_role_dns_not_arbitrary_urls(self):
        with patch(
            "search.operations.shared.runtime_probe.service_address",
            return_value="http://172.20.0.2:11434",
        ) as resolve:
            transport = ProbeTransport("scope")
            resolve.assert_called_once_with("scope-model", 11434)
            with self.assertRaises(ValueError):
                ProbeTransport("evil")
        transport.transport = Mock()
        for request in (
            HTTPRequest("POST", OLLAMA_BASE_URL + "/api/pull", (), b"{}"),
            HTTPRequest("GET", OLLAMA_BASE_URL + "/api/ps?x=1", ()),
            HTTPRequest("GET", "http://evil.example/api/ps", ()),
        ):
            with self.subTest(url=request.url), self.assertRaises(ValueError):
                transport.send(request, timeout=5, max_bytes=1024)
        transport.transport.send.assert_not_called()

    def test_probe_import_does_not_load_worker_or_application_configuration(self):
        root = Path(__file__).resolve().parents[3]
        script = """
import sys
sys.path.insert(0, sys.argv[1])
from search.operations.shared import runtime_probe
from search.api.shared import peers
assert runtime_probe.service_address is peers.service_address
for name in (
    "search.api.worker", "search.api.selector", "search.api.shared.config",
    "search.api.retrieval.fess", "fastapi", "django", "rest_framework", "adrf", "pydantic", "torch", "haystack",
):
    assert not any(key == name or key.startswith(name + ".") for key in sys.modules), name
"""
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script, str(root)],
            capture_output=True,
            timeout=10,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
