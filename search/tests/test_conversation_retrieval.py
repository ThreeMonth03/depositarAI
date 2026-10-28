"""Conversation HTTP flow uses one index retriever for every condition shape."""

from copy import deepcopy
import unittest

from search.api.shared.backend import BackendFailure
from search.planning.predicates import classify, project
from search.reranker.contract import MODEL_REVISION
from search.tests.fixtures import (
    ConversationFixture,
    ConversationHTTPFixture,
    condition_document,
    report,
    CONDITIONS,
    IDS,
    PLAN,
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


class MetadataBackend(ConversationFixture):
    """Script retrieval order, but derive eligibility from declared metadata."""

    def __init__(self):
        super().__init__()
        self.rows = report("氣候", count=7)["results"]
        self.metadata = {}
        for row, (start, end, status, formats) in zip(
            self.rows,
            (
                ("2020", "2025", "complete", ["CSV"]),
                ("2020", "2020", "complete", ["CSV"]),
                ("2024", None, "partial", ["CSV"]),
                (None, None, "absent", ["CSV"]),
                ("2024-02-30", "2024", "invalid", ["CSV"]),
                ("2024", "2024", "complete", ["PDF"]),
                ("2026", "2026", "complete", []),
            ),
        ):
            row.update(
                title="2020 年上架的氣候資料",
                metadata_created="2020-01-01T00:00:00",
                resource_formats=formats,
                temporal_coverage={"start": start, "end": end, "status": status},
            )
            self.metadata[row["dataset_id"]] = condition_document(
                start, end, formats=formats
            )

    def condition(self, identifier, conditions):
        return classify(project(self.metadata[identifier]), conditions)

    async def call(self, operation, payload, timeout):
        if operation not in {"retrieve", "condition_states", "rerank"}:
            return await super().call(operation, payload, timeout)
        self.calls.append((operation, deepcopy(payload), timeout))
        if operation == "retrieve":
            value = report(payload["query"], conditions=payload["conditions"])
            value["results"] = [
                deepcopy(row)
                for row in self.rows
                if self.condition(row["dataset_id"], payload["conditions"]) != "outside"
            ][: payload["top_k"]]
            return value
        if operation == "condition_states":
            return {
                "manifest_sha256": "b" * 64,
                "states": {
                    identifier: self.condition(identifier, payload["conditions"])
                    for identifier in payload["ids"]
                },
            }
        return {
            "model_revision": MODEL_REVISION,
            "order": list(reversed(range(len(payload["passages"])))),
        }


class ConversationConditionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.backend = MetadataBackend()
        self.fixture = ConversationHTTPFixture(self, self.backend)

    def turn(self, query, *, state=None, **plan):
        self.backend.plan = {**PLAN, **plan}
        response = self.fixture.turn(query=query, state=state or {}, top_k=20)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_year_followup_preserves_topic_and_dataset_coverage(self):
        row = self.backend.rows[0]
        context = {
            "resource_id": IDS[6],
            "name": "Observations published in 2020",
            "format": "CSV",
            "url": row["url"] + "/resource/" + IDS[6],
            "columns": ["2020 reference", "temperature"],
            "excerpt": "",
        }
        row["resource_context"] = [context]
        first = self.turn(
            "找 2020 到 2025 年的氣候資料",
            time_op="set",
            time={"start": "2020", "end": "2025"},
        )
        self.assertEqual(
            [card["dataset_id"] for card in first["search"]["cards"]],
            [IDS[5], IDS[4], IDS[3], IDS[2], IDS[1], IDS[0]],
        )
        second = self.turn(
            "我要2024年的",
            state=first["state"],
            action="refine",
            topic=None,
            time_op="set",
            time={"start": "2024", "end": "2024"},
        )
        self.assertEqual(second["state"]["topic"], "氣候")
        self.assertEqual(second["state"]["time"], {"start": "2024", "end": "2024"})
        retrieved = [p for op, p, _ in self.backend.calls if op == "retrieve"]
        self.assertEqual([p["query"] for p in retrieved], ["氣候", "氣候"])
        self.assertEqual(
            [p["conditions"] for p in retrieved],
            [
                {"time": {"start": "2020-01-01", "end": "2025-12-31"}},
                {"time": {"start": "2024-01-01", "end": "2024-12-31"}},
            ],
        )
        search = second["search"]
        self.assertEqual(search["parsed_conditions"], search["applied_conditions"])
        self.assertEqual(search["filter_scope"], "index_prefilter")
        self.assertEqual(
            search["condition_counts"],
            {"match": 2, "unknown": 3, "outside": 0, "not_checked": 0},
        )
        cards = search["cards"]
        self.assertEqual(
            [(c["dataset_id"], c["condition_status"]) for c in cards],
            [
                (IDS[5], "match"),
                (IDS[4], "unknown"),
                (IDS[3], "unknown"),
                (IDS[2], "unknown"),
                (IDS[0], "match"),
            ],
        )
        self.assertEqual(
            [c["temporal_coverage"] for c in cards],
            [
                {"start": "2024", "end": "2024", "status": "complete"},
                {"start": "2024-02-30", "end": "2024", "status": "invalid"},
                {"start": None, "end": None, "status": "absent"},
                {"start": "2024", "end": None, "status": "partial"},
                {"start": "2020", "end": "2025", "status": "complete"},
            ],
        )
        self.assertIn("unknown_condition_results_retained", search["warnings"])
        self.assertEqual(cards[-1]["resource_context"], [context])
        self.assertTrue(all(card["resource_context"] == [] for card in cards[:-1]))

    def test_year_and_format_refinements_preserve_the_other_condition(self):
        first = self.turn(
            "找 2020 到 2025 年的氣候 CSV 資料",
            time_op="set",
            time={"start": "2020", "end": "2025"},
            formats_op="set",
            formats=["CSV"],
        )
        second = self.turn(
            "我要2024年的",
            state=first["state"],
            action="refine",
            topic=None,
            time_op="set",
            time={"start": "2024", "end": "2024"},
        )
        self.assertEqual(second["state"]["formats"], ["CSV"])
        self.assertEqual(
            [
                (c["dataset_id"], c["condition_status"])
                for c in second["search"]["cards"]
            ],
            [
                (IDS[4], "unknown"),
                (IDS[3], "unknown"),
                (IDS[2], "unknown"),
                (IDS[0], "match"),
            ],
        )
        third = self.turn(
            "只要 PDF",
            state=second["state"],
            action="refine",
            topic=None,
            formats_op="set",
            formats=["PDF"],
        )
        self.assertEqual(third["state"]["time"], {"start": "2024", "end": "2024"})
        self.assertEqual(third["search"]["applied_conditions"]["formats"], ["PDF"])
        self.assertEqual(
            [
                (c["dataset_id"], c["condition_status"])
                for c in third["search"]["cards"]
            ],
            [(IDS[5], "match")],
        )

    def test_explicit_clears_and_new_search_do_not_reuse_old_conditions(self):
        first = self.turn(
            "找 2024 年的氣候 CSV 資料",
            time_op="set",
            time={"start": "2024", "end": "2024"},
            formats_op="set",
            formats=["CSV"],
        )
        second = self.turn(
            "取消時間限制",
            state=first["state"],
            action="refine",
            topic=None,
            time_op="clear",
        )
        self.assertIsNone(second["state"]["time"])
        self.assertEqual(second["state"]["formats"], ["CSV"])
        self.assertEqual(second["search"]["cards"][0]["dataset_id"], IDS[6])
        self.assertEqual(second["search"]["cards"][0]["condition_status"], "unknown")
        third = self.turn(
            "不限格式",
            state=second["state"],
            action="refine",
            topic=None,
            formats_op="clear",
        )
        self.assertEqual(third["search"]["filter_scope"], "none")
        self.assertIsNone(third["search"]["applied_conditions"])
        self.assertEqual(len(third["search"]["cards"]), 7)
        self.assertEqual(
            {c["condition_status"] for c in third["search"]["cards"]}, {"not_checked"}
        )
        for query, state, topic in (
            ("換找水質資料", first["state"], "水質"),
            ("找氣候資料", None, "氣候"),
        ):
            with self.subTest(query=query):
                fresh = self.turn(query, state=state, topic=topic)
                self.assertEqual(fresh["state"]["topic"], topic)
                self.assertIsNone(fresh["state"]["time"])
                self.assertEqual(fresh["state"]["formats"], [])
                self.assertEqual(fresh["search"]["filter_scope"], "none")
                self.assertEqual(len(fresh["search"]["cards"]), 7)
