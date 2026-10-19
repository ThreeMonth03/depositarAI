"""Production residency changes neither inference settings nor model policy."""

import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from search.api.shared.model_transport import ModelTransport
from search.api.worker import run
from search.transport.http import HTTPRequest, HTTPResponse
from search.model_policy import OLLAMA_BASE_URL


ROOT = Path(__file__).resolve().parents[3]


class RuntimeModelPolicyTests(unittest.TestCase):
    def transport(self, keep_alive=None):
        with patch(
            "search.api.shared.model_transport.service_address",
            return_value="http://172.20.0.2:11434",
        ):
            result = ModelTransport("scope", keep_alive=keep_alive)
        result.transport = Mock()

        def send(request, **_kwargs):
            return HTTPResponse(200, (), b"{}", request.url)

        result.transport.send.side_effect = send
        return result

    def request(self, body):
        return HTTPRequest(
            "POST",
            OLLAMA_BASE_URL + "/api/chat",
            (("Content-Type", "application/json"),),
            body,
        )

    def test_default_preserves_exact_historical_body_even_noncanonical_json(self):
        transport = self.transport()
        body = b'{ "options": { "num_ctx": 16384 }, "keep_alive": "10m" }\n'
        request = self.request(body)
        result = transport.send(request, timeout=5, max_bytes=1024)
        sent = transport.transport.send.call_args.args[0]
        self.assertEqual(sent.body, body)
        self.assertEqual(request.body, body)
        self.assertEqual(result.url, request.url)

    def test_explicit_residency_changes_only_keep_alive(self):
        transport = self.transport(-1)
        payload = {
            "model": "gpt-oss:20b",
            "messages": [{"role": "user", "content": "找資料"}],
            "stream": False,
            "think": "low",
            "format": {"type": "object"},
            "options": {
                "num_ctx": 16384,
                "num_predict": 2048,
                "temperature": 0,
                "seed": 11,
            },
            "keep_alive": "10m",
        }
        request = self.request(json.dumps(payload, ensure_ascii=False).encode())
        transport.send(request, timeout=12, max_bytes=4096)
        sent = transport.transport.send.call_args.args[0]
        expected = {**payload, "keep_alive": -1}
        self.assertEqual(json.loads(sent.body), expected)
        self.assertEqual(json.loads(request.body), payload)
        self.assertEqual(sent.headers, request.headers)
        self.assertEqual(
            transport.transport.send.call_args.kwargs,
            {"timeout": 12, "max_bytes": 4096},
        )

    def test_get_tags_has_no_body_rewrite(self):
        transport = self.transport(-1)
        request = HTTPRequest("GET", OLLAMA_BASE_URL + "/api/tags", ())
        transport.send(request, timeout=5, max_bytes=1024)
        self.assertIsNone(transport.transport.send.call_args.args[0].body)

    def test_residency_does_not_enable_ps_tools_or_generic_origins(self):
        transport = self.transport(-1)
        for request in (
            HTTPRequest("GET", OLLAMA_BASE_URL + "/api/ps", ()),
            HTTPRequest("POST", OLLAMA_BASE_URL + "/api/pull", (), b"{}"),
            HTTPRequest("POST", "http://evil.example/api/chat", (), b"{}"),
        ):
            with self.subTest(url=request.url), self.assertRaises(ValueError):
                transport.send(request, timeout=5, max_bytes=1024)
        transport.transport.send.assert_not_called()

    def test_residency_input_bounds_and_json_are_fail_closed(self):
        transport = self.transport(-1)
        for body in (
            None,
            b"[]",
            b'{"keep_alive":0,"keep_alive":-1}',
            b"{",
            b" " * (96 * 1024 + 1),
        ):
            with (
                self.subTest(size=None if body is None else len(body)),
                self.assertRaises(ValueError),
            ):
                transport.send(self.request(body), timeout=5, max_bytes=1024)
        transport.transport.send.assert_not_called()

    def test_only_explicit_negative_one_is_allowed_without_dns_on_failure(self):
        with patch("search.api.shared.model_transport.service_address") as resolve:
            for value in (True, False, "-1", "10m", 0, 120, -2):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    ModelTransport("scope", keep_alive=value)
        resolve.assert_not_called()

    def test_production_scope_worker_explicitly_requests_residency(self):
        with patch("search.api.worker.ModelTransport") as transport:
            with patch("search.api.worker.OllamaScopeClassifier") as classifier:
                classifier.return_value.classify.return_value = "search"
                self.assertEqual(
                    run("scope", {"query": "降雨"}, None), {"decision": "search"}
                )
        transport.assert_called_once_with("scope", keep_alive=-1)

    def test_conversation_planner_requests_residency_without_changing_default_adapters(
        self,
    ):
        for relative in ("search/api/conversation/worker.py",):
            with self.subTest(path=relative):
                self.assertIn(
                    'ModelTransport("answer", keep_alive=-1)',
                    (ROOT / relative).read_text(),
                )
        self.assertIn(
            '"keep_alive": "10m"',
            (ROOT / "search/scope.py").read_text(),
        )
        self.assertIn(
            '"keep_alive": "10m"', (ROOT / "search/planning/model.py").read_text()
        )


if __name__ == "__main__":
    unittest.main()
