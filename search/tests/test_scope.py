"""Pinned query classification and bounded model-response validation."""

import hashlib
import json
import unittest

from search.model_policy import (
    OLLAMA_BASE_URL,
    SCOPE_MODEL,
    SCOPE_MODEL_DIGEST,
    SCOPE_NUM_CTX,
    SCOPE_NUM_PREDICT,
    PLANNER_MODEL,
    PLANNER_MODEL_DIGEST,
)
from search.scope import (
    OllamaAdapterError,
    OllamaInputError,
    OllamaScopeClassifier,
    SCOPE_SYSTEM_PROMPT_SHA256,
    _SCOPE_SYSTEM_PROMPT,
    scope_schema,
)
from search.transport.http import HTTPResponse, UrllibTransport
from search.tests.fixtures import ClosingResponse, SequenceOpener

UPSTREAM_SECRET = "upstream-secret-must-not-leak"


def response(url, value, *, status=200, content_type="application/json"):
    body = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    return HTTPResponse(
        status=status,
        headers=(("Content-Type", content_type),),
        body=body,
        url=url,
    )


def tags(model, digest):
    return response(
        OLLAMA_BASE_URL + "/api/tags",
        {"models": [{"name": model, "model": model, "digest": digest}]},
    )


def chat(model, content, *, done_reason="stop", thinking="private reasoning"):
    return response(
        OLLAMA_BASE_URL + "/api/chat",
        {
            "model": model,
            "message": {
                "role": "assistant",
                "content": content,
                "thinking": thinking,
            },
            "done": True,
            "done_reason": done_reason,
            "prompt_eval_count": 100,
            "eval_count": 12,
        },
    )


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def send(self, request, *, timeout, max_bytes):
        self.requests.append(
            {"request": request, "timeout": timeout, "max_bytes": max_bytes}
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(request, timeout, max_bytes)
        return item


class ScopeAdapterTests(unittest.TestCase):
    def test_current_model_identities_prompt_and_schema_are_pinned(self):
        self.assertEqual("ministral-3:8b-instruct-2512-q4_K_M", SCOPE_MODEL)
        self.assertEqual(
            "1922accd5827ebe6829e536369195db25eaf664528dc66206d646ea3bb386b71",
            SCOPE_MODEL_DIGEST,
        )
        self.assertEqual((4096, 64), (SCOPE_NUM_CTX, SCOPE_NUM_PREDICT))
        self.assertEqual("gpt-oss:20b", PLANNER_MODEL)
        self.assertEqual(
            "17052f91a42e97930aa6e28a6c6c06a983e6a58dbb00434885a0cf5313e376f7",
            PLANNER_MODEL_DIGEST,
        )
        self.assertEqual(
            "f8a5e0dc07d482104a68533236b8bebf4a3561d617e5e67c80114f14c9cef281",
            SCOPE_SYSTEM_PROMPT_SHA256,
        )
        self.assertEqual(
            SCOPE_SYSTEM_PROMPT_SHA256,
            hashlib.sha256(_SCOPE_SYSTEM_PROMPT.encode()).hexdigest(),
        )
        self.assertEqual(
            "0353d92ced6d3250e36a74d3097521e6c00a3a067ee08d17d7432982c6b4d754",
            hashlib.sha256(
                json.dumps(
                    scope_schema(),
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        )

    def test_scope_request_is_fixed_structured_toolless_and_digest_bound(self):
        transport = FakeTransport(
            tags(SCOPE_MODEL, SCOPE_MODEL_DIGEST),
            chat(SCOPE_MODEL, '{"decision":"search"}', thinking=""),
        )
        classifier = OllamaScopeClassifier(transport=transport)
        self.assertEqual("search", classifier.classify("找臺灣水質 CSV"))
        self.assertEqual(2, len(transport.requests))
        tags_request = transport.requests[0]["request"]
        self.assertEqual("GET", tags_request.method)
        self.assertEqual(OLLAMA_BASE_URL + "/api/tags", tags_request.url)
        request = transport.requests[1]["request"]
        self.assertEqual("POST", request.method)
        self.assertEqual(OLLAMA_BASE_URL + "/api/chat", request.url)
        payload = json.loads(request.body)
        self.assertEqual(
            {
                "model",
                "messages",
                "stream",
                "think",
                "truncate",
                "shift",
                "format",
                "keep_alive",
                "options",
            },
            set(payload),
        )
        self.assertEqual(SCOPE_MODEL, payload["model"])
        self.assertEqual("10m", payload["keep_alive"])
        self.assertIs(False, payload["think"])
        self.assertIs(False, payload["stream"])
        self.assertIs(False, payload["truncate"])
        self.assertIs(False, payload["shift"])
        self.assertEqual(
            {
                "num_ctx": SCOPE_NUM_CTX,
                "num_predict": SCOPE_NUM_PREDICT,
                "temperature": 0,
                "seed": 42,
            },
            payload["options"],
        )
        self.assertNotIn("tools", payload)
        self.assertEqual(
            ["search", "out_of_scope", "unsafe"],
            payload["format"]["properties"]["decision"]["enum"],
        )

    def test_wrong_digest_fails_before_chat(self):
        transport = FakeTransport(tags(SCOPE_MODEL, "0" * 64))
        with self.assertRaisesRegex(OllamaAdapterError, "model_digest_mismatch"):
            OllamaScopeClassifier(transport=transport).classify("找水質資料")
        self.assertEqual(1, len(transport.requests))

    def test_scope_output_requires_exact_object_and_normal_completion(self):
        bad_contents = (
            '{"decision":"search","answer":"oops"}',
            '{"decision":"search","decision":"unsafe"}',
            '{"decision":"other"}',
            "free model prose",
            '{"decision":"search","extra":{"value":1,"value":2}}',
            '{"decision":NaN}',
            '{"decision":Infinity}',
            '{"decision":-Infinity}',
            '{"decision":"search"} trailing prose',
        )
        for content in bad_contents:
            with self.subTest(content=content):
                transport = FakeTransport(
                    tags(SCOPE_MODEL, SCOPE_MODEL_DIGEST), chat(SCOPE_MODEL, content)
                )
                with self.assertRaises(OllamaAdapterError):
                    OllamaScopeClassifier(transport=transport).classify("找資料")

        transport = FakeTransport(
            tags(SCOPE_MODEL, SCOPE_MODEL_DIGEST),
            chat(SCOPE_MODEL, '{"decision":"search"}', done_reason="length"),
        )
        with self.assertRaisesRegex(OllamaAdapterError, "incomplete_upstream_response"):
            OllamaScopeClassifier(transport=transport).classify("找資料")

    def test_provider_json_rejects_non_utf8_duplicate_keys_and_nonfinite_values(self):
        for body in (
            b"\xff",
            b'{"models":[],"models":[]}',
            b'{"models":[],"extra":{"key":1,"key":2}}',
            b'{"models":[],"extra":NaN}',
            b'{"models":[],"extra":Infinity}',
            b'{"models":[],"extra":-Infinity}',
        ):
            with self.subTest(body=body):
                transport = FakeTransport(response(OLLAMA_BASE_URL + "/api/tags", body))
                with self.assertRaisesRegex(
                    OllamaAdapterError, "invalid_upstream_response"
                ):
                    OllamaScopeClassifier(transport=transport).classify("找資料")
                self.assertEqual(len(transport.requests), 1)

    def test_query_bounds_fail_before_any_transport_call(self):
        for query in ("", "x\u202ey", "x\ny", "測" * 513, "水" * 683):
            with self.subTest(query=query):
                transport = FakeTransport()
                with self.assertRaises(OllamaInputError):
                    OllamaScopeClassifier(transport=transport).classify(query)
                self.assertEqual([], transport.requests)

    def test_closed_response_with_short_declared_body_still_fails_closed(self):
        tags_body = json.dumps(
            {
                "models": [
                    {
                        "name": SCOPE_MODEL,
                        "model": SCOPE_MODEL,
                        "digest": SCOPE_MODEL_DIGEST,
                    }
                ]
            },
            separators=(",", ":"),
        ).encode()
        chat_body = b'{"done":true}'
        transport = UrllibTransport()
        transport._opener = SequenceOpener(
            ClosingResponse(tags_body, OLLAMA_BASE_URL + "/api/tags"),
            ClosingResponse(
                chat_body,
                OLLAMA_BASE_URL + "/api/chat",
                declared_length=len(chat_body) + 1,
            ),
        )

        with self.assertRaisesRegex(OllamaAdapterError, "invalid_upstream_response"):
            OllamaScopeClassifier(transport=transport).classify("找水質資料")

    def test_transport_and_upstream_body_are_sanitized(self):
        transport = FakeTransport(RuntimeError(UPSTREAM_SECRET))
        with self.assertRaises(OllamaAdapterError) as context:
            OllamaScopeClassifier(transport=transport).classify("找資料")
        self.assertNotIn(UPSTREAM_SECRET, str(context.exception))
        self.assertIsNone(context.exception.__cause__)

        raw = json.dumps({"error": UPSTREAM_SECRET}).encode("utf-8")
        transport = FakeTransport(
            response(
                OLLAMA_BASE_URL + "/api/tags",
                raw,
                status=500,
                content_type="text/plain",
            )
        )
        with self.assertRaises(OllamaAdapterError) as context:
            OllamaScopeClassifier(transport=transport).classify("找資料")
        self.assertNotIn(UPSTREAM_SECRET, str(context.exception))

    def test_deadline_and_response_caps_fail_closed(self):
        class Clock:
            value = 0.0

            def __call__(self):
                return self.value

        clock = Clock()

        def consume_deadline(request, timeout, max_bytes):
            del timeout, max_bytes
            clock.value = 91.0
            return tags(SCOPE_MODEL, SCOPE_MODEL_DIGEST)

        selector = OllamaScopeClassifier(
            transport=FakeTransport(consume_deadline), clock=clock
        )
        with self.assertRaisesRegex(OllamaAdapterError, "deadline_exceeded"):
            selector.classify("找資料")

        oversized = b"{" + b"x" * (128 * 1024) + b"}"
        selector = OllamaScopeClassifier(
            transport=FakeTransport(
                tags(SCOPE_MODEL, SCOPE_MODEL_DIGEST),
                response(OLLAMA_BASE_URL + "/api/chat", oversized),
            )
        )
        with self.assertRaisesRegex(OllamaAdapterError, "upstream_response_too_large"):
            selector.classify("找資料")

    def test_provider_prompt_count_must_be_exact_and_leave_context_reserve(self):
        invalid_counts = (True, 0, SCOPE_NUM_CTX - SCOPE_NUM_PREDICT - 511)
        for prompt_count in invalid_counts:
            with self.subTest(prompt_count=prompt_count):
                content = json.dumps({"decision": "search"})
                envelope = chat(SCOPE_MODEL, content)
                payload = json.loads(envelope.body)
                payload["prompt_eval_count"] = prompt_count
                selector = OllamaScopeClassifier(
                    transport=FakeTransport(
                        tags(SCOPE_MODEL, SCOPE_MODEL_DIGEST),
                        response(OLLAMA_BASE_URL + "/api/chat", payload),
                    )
                )
                with self.assertRaisesRegex(
                    OllamaAdapterError, "context_budget_exceeded"
                ):
                    selector.classify("找資料")

    def test_hidden_thinking_and_tool_calls_are_never_returned(self):
        classifier = OllamaScopeClassifier(
            transport=FakeTransport(
                tags(SCOPE_MODEL, SCOPE_MODEL_DIGEST),
                chat(SCOPE_MODEL, '{"decision":"search"}', thinking=UPSTREAM_SECRET),
            )
        )
        result = classifier.classify("找資料")
        self.assertEqual(result, "search")
        self.assertNotIn(UPSTREAM_SECRET, repr(result))

        bad = response(
            OLLAMA_BASE_URL + "/api/chat",
            {
                "model": SCOPE_MODEL,
                "message": {
                    "role": "assistant",
                    "content": '{"decision":"search"}',
                    "tool_calls": [{"function": {"name": "shell"}}],
                },
                "done": True,
                "done_reason": "stop",
                "eval_count": 12,
            },
        )
        selector = OllamaScopeClassifier(
            transport=FakeTransport(tags(SCOPE_MODEL, SCOPE_MODEL_DIGEST), bad)
        )
        with self.assertRaises(OllamaAdapterError):
            selector.classify("找資料")


if __name__ == "__main__":
    unittest.main()
