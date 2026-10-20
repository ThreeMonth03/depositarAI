"""Conversation HTTP flow uses one index retriever for every condition shape."""

import unittest

from search.api.shared.backend import BackendFailure
from search.tests.fixtures import (
    ConversationFixture,
    ConversationHTTPFixture,
    report,
    CONDITIONS,
)


class ConversationRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.backend = ConversationFixture()
        self.backend.plan.update(time_op="set", time={"start": "2025", "end": "2025"})
        self.fixture = ConversationHTTPFixture(self, self.backend)

    def test_scope_planner_prefilter_and_rerank_without_selection(self):
        response = self.fixture.turn(top_k=3)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()["search"]
        self.assertEqual(value["filter_scope"], "index_prefilter")
        self.assertEqual(value["condition_counts"]["outside"], 0)
        self.assertEqual(
            [c[0] for c in self.backend.calls],
            [
                "scope",
                "conversation_plan",
                "retrieve",
                "condition_states",
                "rerank",
            ],
        )
        self.assertEqual(self.backend.calls[2][1]["conditions"], CONDITIONS)
        self.assertTrue(
            {"answer", "planning_status", "clarification"}.isdisjoint(value)
        )
        self.assertEqual(value["warnings"], ["unknown_condition_results_retained"])

    def test_retrieval_failure_never_falls_back_to_another_backend(self):
        self.backend.retrieval = BackendFailure("PRIVATE")
        response = self.fixture.turn()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("PRIVATE", response.text)
        self.assertEqual(
            [c[0] for c in self.backend.calls],
            ["scope", "conversation_plan", "retrieve"],
        )
        self.assertIsNone(response.json()["search"])
        self.assertEqual(response.json()["state"]["topic"], "")

    def test_index_state_disagreement_does_not_commit_new_state(self):
        original = self.backend.call

        async def disagree(operation, payload, timeout):
            value = await original(operation, payload, timeout)
            if operation == "condition_states":
                value["states"][next(iter(value["states"]))] = "outside"
            return value

        self.backend.call = disagree
        response = self.fixture.turn()
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(response.json()["search"])
        self.assertEqual(response.json()["state"]["topic"], "")

    def test_missing_conditions_use_the_same_retriever_with_empty_conditions(self):
        self.backend.plan.update(time_op="keep", time=None)
        response = self.fixture.turn()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["search"]["filter_scope"], "none")
        self.assertEqual(self.backend.calls[2][1]["conditions"], {})
        self.assertEqual(
            [c[0] for c in self.backend.calls],
            ["scope", "conversation_plan", "retrieve", "rerank"],
        )

    def test_unknowns_are_visible_and_not_reported_as_confirmed_matches(self):
        original = self.backend.call

        async def unknown(operation, payload, timeout):
            value = await original(operation, payload, timeout)
            if operation == "condition_states":
                value["states"] = dict.fromkeys(value["states"], "unknown")
            return value

        self.backend.call = unknown
        response = self.fixture.turn()
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()["search"]
        self.assertTrue(all(c["condition_status"] == "unknown" for c in value["cards"]))
        self.assertEqual(value["condition_counts"]["match"], 0)
        self.assertIn("unknown_condition_results_retained", value["warnings"])

    def test_clearing_the_last_condition_keeps_the_same_retrieval_operation(self):
        first = self.fixture.turn()
        self.assertEqual(first.status_code, 200, first.text)
        self.backend.plan.update(
            action="refine", topic=None, time_op="clear", time=None
        )
        second = self.fixture.turn(query="不限年份", state=first.json()["state"])
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["search"]["filter_scope"], "none")
        requests = [
            payload
            for operation, payload, _ in self.backend.calls
            if operation == "retrieve"
        ]
        self.assertEqual(
            [payload["conditions"] for payload in requests], [CONDITIONS, {}]
        )
        self.assertTrue(all(payload["query"] == "氣候" for payload in requests))

    def test_index_manifest_mismatch_stops_before_ranker(self):
        self.backend.retrieval = report("氣候", conditions=CONDITIONS)
        self.backend.retrieval["manifest_sha256"] = "c" * 64
        response = self.fixture.turn()
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(response.json()["search"])
        self.assertEqual(
            [c[0] for c in self.backend.calls],
            ["scope", "conversation_plan", "retrieve"],
        )
