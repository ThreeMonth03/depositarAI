"""Current retrieval/filter/rerank contracts, independent of HTTP and models."""

import asyncio
import copy
import dataclasses
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.api.retrieval.engine import RetrievalEngine
from search.api.retrieval.schemas import RetrievalRequest, RetrievalResponse
from search.api.retrieval.worker import RetrievalBackend, run as worker_run
from search.api.shared.backend import BackendFailure, BackendTimeout, SubprocessBackend
from search.api.shared.config import Settings
from search.reranker.contract import MODEL_REVISION
from search.tests.fixtures import CONDITIONS, IDS, RetrievalFixture, report


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = RetrievalFixture()
        self.settings = Settings(
            "a" * 40, "b" * 64, Path("/catalog"), Path("/key"), True
        )
        self.engine = RetrievalEngine(self.backend, self.settings)

    async def search(self, **request):
        result = await self.engine.search(
            RetrievalRequest(query="氣候", **request), "test"
        )
        RetrievalResponse.model_validate(result)
        return result

    async def test_filter_before_rerank_top_k_and_no_model_selection(self):
        self.backend.retrieval = report("氣候", count=3, conditions=CONDITIONS)
        self.backend.states = {IDS[0]: "unknown", IDS[1]: "match", IDS[2]: "match"}
        self.backend.rerank["order"] = [2, 1, 0]
        result = await self.search(
            top_k=2, conditions={"time": {"start": "2025-01-01", "end": "2025-12-31"}}
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidate_count"], 3)
        self.assertEqual(
            result["condition_counts"],
            {"match": 2, "unknown": 1, "outside": 0, "not_checked": 0},
        )
        self.assertEqual([c["dataset_id"] for c in result["cards"]], [IDS[2], IDS[1]])
        self.assertEqual(
            [c["condition_status"] for c in result["cards"]], ["match", "match"]
        )
        self.assertEqual(
            [c[0] for c in self.backend.calls],
            ["retrieve", "condition_states", "rerank"],
        )
        self.assertEqual(self.backend.calls[0][1]["top_k"], 10)
        self.assertEqual(self.backend.calls[0][1]["conditions"], CONDITIONS)
        self.assertEqual(result["filter_scope"], "index_prefilter")
        self.assertEqual(len(self.backend.calls[2][1]["passages"]), 3)
        self.assertEqual(result["answer"], {"status": "not_requested", "match": None})
        self.assertNotIn("_condition_states", result)

    async def test_server_owned_candidate_count_and_top_k(self):
        result = await self.search(top_k=2)
        self.assertEqual(self.backend.calls[0][1]["top_k"], 10)
        self.assertEqual(self.backend.calls[0][1]["conditions"], {})
        self.assertEqual(result["filter_scope"], "none")
        self.assertEqual([c["dataset_id"] for c in result["cards"]], [IDS[4], IDS[3]])
        self.assertEqual([c["rank"] for c in result["cards"]], [1, 2])
        self.assertEqual(result["cards"][0]["url"], report("氣候")["results"][4]["url"])
        payload = self.backend.calls[1][1]
        self.assertEqual(set(payload), {"query", "passages"})
        self.assertNotIn(IDS[0], str(payload))
        self.assertNotIn("https://", str(payload))
        self.backend.calls.clear()
        await self.search(top_k=20)
        self.assertEqual(self.backend.calls[0][1]["top_k"], 20)

    async def test_disabled_reranking_uses_requested_bound_and_no_llm(self):
        self.engine = RetrievalEngine(
            self.backend, dataclasses.replace(self.settings, rerank_enabled=False)
        )
        self.backend.retrieval = report("氣候", count=1)
        result = await self.search(top_k=1)
        self.assertEqual([c[0] for c in self.backend.calls], ["retrieve"])
        self.assertEqual(self.backend.calls[0][1]["top_k"], 1)
        self.assertEqual(result["rerank_status"], "disabled")
        self.assertEqual(result["cards"][0]["condition_status"], "not_checked")

    async def test_rerank_failure_preserves_exact_original_cards(self):
        self.backend.rerank = BackendTimeout("PRIVATE")
        original = (await self.search())["cards"]
        for value in (
            BackendTimeout("PRIVATE"),
            BackendFailure("PRIVATE"),
            {"order": [0, 1], "model_revision": MODEL_REVISION},
            {"order": [0, 1, 2, 3, 4], "model_revision": "wrong"},
            {
                "order": [0, 1, 2, 3, 4],
                "model_revision": MODEL_REVISION,
                "url": "https://evil.invalid",
            },
        ):
            self.backend.rerank = value
            result = await self.search()
            self.assertEqual(result["cards"], original)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["ranking"], "opensearch-haystack-rrf")
            self.assertEqual(result["rerank_status"], "fallback")
            self.assertTrue(result["warnings"])
            self.assertNotIn("PRIVATE", str(result))

    async def test_empty_results_do_not_call_reranker_or_pad_cards(self):
        self.backend.retrieval = report("氣候", count=0)
        result = await self.search()
        self.assertEqual(result["cards"], [])
        self.assertEqual(result["rerank_status"], "not_run")
        self.assertEqual([c[0] for c in self.backend.calls], ["retrieve"])

    async def test_retrieval_identity_or_url_drift_discards_results(self):
        original = report("氣候")
        for field, value in (
            ("query_sha256", "c" * 64),
            ("manifest_sha256", "c" * 64),
            ("conditions", {"formats": ["CSV"]}),
            ("url", "https://evil.invalid"),
        ):
            self.backend.retrieval = copy.deepcopy(original)
            if field == "url":
                self.backend.retrieval["results"][0][field] = value
            else:
                self.backend.retrieval[field] = value
            result = await self.search()
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["cards"], [])
            self.assertIn("retrieval_unavailable", result["warnings"])

    async def test_filter_failure_never_returns_unfiltered_cards(self):
        self.backend.state_error = BackendTimeout("PRIVATE")
        result = await self.search(conditions={"formats": ["CSV"]})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["cards"], [])
        self.assertIsNone(result["applied_conditions"])
        self.assertEqual(
            [c[0] for c in self.backend.calls], ["retrieve", "condition_states"]
        )
        self.assertNotIn("PRIVATE", str(result))

    async def test_condition_identity_and_labels_are_exact(self):
        original = copy.deepcopy(self.backend.states)
        for states in (
            {IDS[0]: "match"},
            {**original, "unknown-id": "match"},
            {**original, IDS[0]: True},
        ):
            self.backend.states = states
            result = await self.search(conditions={"formats": ["CSV"]})
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["cards"], [])
        self.backend.states = original
        self.backend.state_digest = "c" * 64
        result = await self.search(conditions={"formats": ["CSV"]})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["cards"], [])

    async def test_unknowns_remain_visible_and_prefilter_disagreement_fails_closed(
        self,
    ):
        self.backend.states = dict.fromkeys(self.backend.states, "unknown")
        result = await self.search(conditions={"formats": ["CSV"]})
        self.assertEqual(len(result["cards"]), 5)
        self.assertTrue(
            all(c["condition_status"] == "unknown" for c in result["cards"])
        )
        self.assertIn("unknown_condition_results_retained", result["warnings"])
        self.backend.calls.clear()
        self.backend.states = dict.fromkeys(self.backend.states, "outside")
        result = await self.search(top_k=20, conditions={"formats": ["CSV"]})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["cards"], [])
        self.assertIn("condition_filter_unavailable", result["warnings"])
        self.assertEqual(
            [c[0] for c in self.backend.calls], ["retrieve", "condition_states"]
        )

    async def test_worker_deadlines_shrink_with_remaining_request_budget(self):
        elapsed, original = [0], self.backend.call

        async def call(operation, payload, timeout):
            self.assertLessEqual(timeout, 120 - elapsed[0])
            result = await original(operation, payload, timeout)
            elapsed[0] += 115 if operation == "retrieve" else 1
            return result

        self.backend.call = call
        with patch(
            "search.api.retrieval.engine.time",
            SimpleNamespace(monotonic=lambda: elapsed[0]),
        ):
            result = await self.search(conditions={"formats": ["CSV"]})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.backend.calls[1][2], 5)
        self.assertEqual(self.backend.calls[2][2], 4)

    async def test_busy_reranker_keeps_original_cards_without_queuing(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.backend.call

        async def call(operation, payload, timeout):
            if operation == "rerank":
                entered.set()
                await release.wait()
            return await original(operation, payload, timeout)

        self.backend.call = call
        first = asyncio.create_task(self.search())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            second = await asyncio.wait_for(self.search(), 1)
            self.assertEqual(second["rerank_status"], "fallback")
            self.assertEqual(second["warnings"], ["rerank_busy"])
            self.assertEqual(second["cards"][0]["dataset_id"], IDS[0])
        finally:
            release.set()
            result = await first
        self.assertEqual(result["rerank_status"], "applied")
        self.assertEqual(self.engine.rerank_slots.used, 0)

    async def test_cancellation_releases_retrieval_capacity(self):
        entered = asyncio.Event()

        async def slow(*_args):
            entered.set()
            await asyncio.Event().wait()

        self.backend.call = slow
        task = asyncio.create_task(self.search())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.engine.retrieval_slots.used, 0)


class RetrievalBoundaryTests(unittest.TestCase):
    def test_settings_reject_invalid_reranker_configuration(self):
        settings = Settings("a" * 40, "b" * 64, Path("/catalog"), Path("/key"))
        for changes in (
            {"rerank_enabled": "1"},
            {"rerank_candidates": 21},
            {"rerank_candidates": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                dataclasses.replace(settings, **changes)

    def test_internal_request_has_no_model_mode_or_spatial_override(self):
        for extra in (
            {"mode": "semantic"},
            {"model": "arbitrary"},
            {"top_k": True},
            {"conditions": {"place": "七股"}},
            {"conditions": {"bbox": [1, 1, 2, 2]}},
            {"conditions": {"time": {"start": "2025", "end": "2024"}}},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                RetrievalRequest(query="氣候", **extra)

    def test_worker_allowlist_contains_only_current_operations(self):
        self.assertEqual(
            SubprocessBackend.operations, {"scope", "retrieve", "rerank", "ready"}
        )
        self.assertEqual(
            RetrievalBackend.operations,
            SubprocessBackend.operations | {"condition_states"},
        )
        for operation, payload in (
            ("plan", {}),
            ("answer", {}),
            ("shell", {}),
            ("retrieve_filtered", {}),
            ("condition_states", {"ids": [], "conditions": {}}),
            ("condition_states", {"ids": ["x"] * 21, "conditions": {}}),
            ("condition_states", {"ids": ["x", "x"], "conditions": {}}),
            ("condition_states", {"ids": ["x"], "conditions": {"bbox": [1, 1, 2, 2]}}),
        ):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                worker_run(operation, payload, None)
