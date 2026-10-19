"""Pinned planner identity, request bounds and completion checks without network."""

import json
import unittest
from unittest.mock import Mock
from search.model_policy import (
    PLANNER_MODEL,
    PLANNER_MODEL_DIGEST,
)
from search.transport.http import HTTPResponse
from search.planning.model import LocalPlanner, OPTIONS


class PlanningTransportTests(unittest.TestCase):
    def transport(self, *, digest=PLANNER_MODEL_DIGEST, done_reason="stop"):
        transport = Mock()
        transport.send.side_effect = [
            HTTPResponse(
                200,
                (),
                json.dumps(
                    {"models": [{"name": PLANNER_MODEL, "digest": digest}]}
                ).encode(),
                "unused",
            ),
            HTTPResponse(
                200,
                (),
                json.dumps(
                    {
                        "done": True,
                        "done_reason": done_reason,
                        "message": {
                            "content": '{"action":"clarify","time":null,"place":null,"formats":[],"reason":"ambiguous_time"}',
                            "thinking": "PRIVATE-THINKING",
                        },
                        "prompt_eval_count": 1000,
                        "eval_count": 100,
                    }
                ).encode(),
                "unused",
            ),
        ]
        return transport

    def test_generate_uses_caller_contract_with_fixed_budgets_and_no_tools(self):
        transport = self.transport()
        schema = {"type": "object", "required": ["action"]}
        prompt = "Plan a dataset search with the supplied schema."
        result = LocalPlanner(transport).generate(
            prompt, "找去年水質資料", schema, think="low"
        )
        chat_call = transport.send.call_args_list[1]
        request = chat_call.args[0]
        body = json.loads(request.body)
        self.assertEqual(body["messages"][0]["content"], prompt)
        self.assertEqual(body["messages"][1]["content"], "找去年水質資料")
        self.assertEqual(body["format"], schema)
        self.assertEqual(body["options"], OPTIONS)
        self.assertEqual(body["think"], "low")
        self.assertEqual(chat_call.kwargs, {"timeout": 90, "max_bytes": 128 * 1024})
        self.assertNotIn("tools", body)
        self.assertNotIn("PRIVATE-THINKING", json.dumps(result))
        self.assertEqual(result["thinking_characters"], len("PRIVATE-THINKING"))

    def test_wrong_model_digest_stops_before_chat(self):
        transport = self.transport(digest="wrong")
        with self.assertRaises(ValueError):
            LocalPlanner(transport)
        self.assertEqual(transport.send.call_count, 1)

    def test_truncation_and_unknown_stop_reason_are_incomplete(self):
        for reason in ("length", None, "unknown"):
            transport = self.transport(done_reason=reason)
            result = LocalPlanner(transport).generate("prompt", "找去年水質資料", {})
            self.assertEqual(result["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
