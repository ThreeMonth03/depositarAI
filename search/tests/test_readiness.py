"""Readiness is bounded/cached and never treats liveness as backend proof."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from search.api.shared.admission import Busy
from search.api.shared.backend import BackendFailure
from search.api.shared.readiness import BackendReadiness
from search.api.shared.config import Settings
from search.api import worker
from search.model_policy import PLANNER_MODEL, PLANNER_MODEL_DIGEST
from search.reranker.contract import MODEL_REVISION
from search.tests.fixtures import report


class Backend:
    def __init__(self):
        self.calls = []
        self.result = {"ready": True, "documents": 5}
        self.hook = lambda: None

    async def call(self, operation, payload, timeout):
        self.calls.append((operation, payload, timeout))
        self.hook()
        if isinstance(self.result, Exception):
            raise self.result
        return copy.deepcopy(self.result)


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tick = 0
        self.backend = Backend()
        self.readiness = BackendReadiness(
            self.backend, clock=lambda: self.tick
        )

    async def test_positive_cache_expires_and_does_not_mutate(self):
        first = await self.readiness.check()
        first["documents"] = 1
        self.tick = 29
        self.assertEqual(5, (await self.readiness.check())["documents"])
        self.assertEqual([("ready", {}, 25)], self.backend.calls)
        self.tick = 30
        await self.readiness.check()
        self.assertEqual(2, len(self.backend.calls))

    async def test_invalid_result_is_not_cached_or_returned(self):
        for value in (
            None,
            [],
            {"ready": True},
            {"ready": 1, "documents": 5},
            {"ready": False, "documents": 5},
            {"ready": True, "documents": True},
            {"ready": True, "documents": 0},
            {"ready": True, "documents": 10001},
            {"ready": True, "documents": 5, "reranker_ready": 1},
            {"ready": True, "documents": 5, "secret": "do not echo"},
        ):
            self.backend.result = value
            with (
                self.subTest(value=type(value).__name__),
                self.assertRaisesRegex(
                    BackendFailure,
                    "invalid readiness result$",
                ),
            ):
                await self.readiness.check()
        self.assertIsNone(self.readiness._value)

    async def test_failure_releases_slot_and_next_call_can_succeed(self):
        self.backend.result = BackendFailure("internal failure")
        with self.assertRaises(BackendFailure):
            await self.readiness.check()
        self.assertEqual(0, self.readiness.slots.used)
        self.backend.result = {"ready": True, "documents": 5}
        self.assertTrue((await self.readiness.check())["ready"])

    async def test_overload_does_not_queue_or_run_backend(self):
        async with self.readiness.slots.claim():
            with self.assertRaises(Busy):
                await self.readiness.check()
        self.assertEqual([], self.backend.calls)

    async def test_optional_reranker_failure_not_misreported(self):
        self.backend.result["reranker_ready"] = False
        self.assertIs((await self.readiness.check())["reranker_ready"], False)

    async def test_worker_that_ignores_deadline_cannot_return_success(self):
        self.backend.hook = lambda: setattr(self, "tick", 25)
        with self.assertRaisesRegex(BackendFailure, "deadline exceeded$"):
            await self.readiness.check()
        self.assertIsNone(self.readiness._value)

    async def test_backwards_and_nonfinite_clock_rejected(self):
        self.tick = 1
        self.backend.hook = lambda: setattr(self, "tick", 0)
        with self.assertRaises(BackendFailure):
            await self.readiness.check()
        self.backend.hook = lambda: None
        self.tick = float("nan")
        with self.assertRaises(BackendFailure):
            await self.readiness.check()

    async def test_cancelled_backend_releases_slot(self):
        async def cancelled(*_args):
            raise asyncio.CancelledError()

        self.backend.call = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.readiness.check()
        self.assertEqual(0, self.readiness.slots.used)

    async def test_backend_ignoring_timeout_is_actively_cancelled(self):
        cancelled = asyncio.Event()

        async def slow(*_args):
            try:
                await asyncio.sleep(1)
            finally:
                cancelled.set()

        self.backend.call = slow
        with patch("search.api.shared.readiness.BACKEND_SECONDS", 0.02):
            with self.assertRaisesRegex(BackendFailure, "deadline exceeded$"):
                await self.readiness.check()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(0, self.readiness.slots.used)


class WorkerReadinessTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            "a" * 40,
            "b" * 64,
            Path("/catalog"),
            Path("/key"),
            rerank_enabled=True,
        )
        self.bundle = self.patch("search.api.worker.CatalogBundle.load")
        self.bundle.return_value = SimpleNamespace(documents=[{}] * 5)
        self.query = self.patch("search.api.retrieval.index.search")
        self.query.side_effect = lambda payload, _settings: report(
            payload["query"], count=1, conditions=payload["conditions"]
        )
        self.models = self.patch("search.api.worker.ModelTransport")
        self.models.return_value.send.return_value = SimpleNamespace(
            status=200,
            body=json.dumps(
                {
                    "models": [
                        {
                            "name": worker.OllamaScopeClassifier.model,
                            "digest": (
                                worker.OllamaScopeClassifier.model_digest
                            ),
                        },
                        {
                            "name": PLANNER_MODEL,
                            "digest": PLANNER_MODEL_DIGEST,
                        },
                    ]
                }
            ).encode(),
        )
        self.peer = self.patch("search.api.worker.service_address")
        self.peer.return_value = "http://172.18.0.4:8001"
        self.health = self.patch("search.api.worker.UrllibTransport")
        self.health.return_value.send.return_value = SimpleNamespace(
            status=200,
            url="http://172.18.0.4:8001/healthz",
            body=json.dumps(
                {"ready": True, "model_revision": MODEL_REVISION}
            ).encode(),
        )

    def patch(self, target):
        patcher = patch(target)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def test_retrieve_uses_one_adapter_for_all_condition_shapes(
        self,
    ):
        for conditions in ({}, {"formats": ["CSV"]}):
            payload = {"query": "水質", "top_k": 5, "conditions": conditions}
            value = worker.run("retrieve", payload, self.settings)
            self.assertEqual(
                value, report("水質", count=1, conditions=conditions)
            )
            self.query.assert_called_with(payload, self.settings)
        self.bundle.assert_not_called()
        self.models.assert_not_called()
        self.peer.assert_not_called()

    def test_ready_checks_condition_shapes_and_pinned_models(
        self,
    ):
        self.assertEqual(
            worker.run("ready", {}, self.settings),
            {"ready": True, "documents": 5, "reranker_ready": True},
        )
        self.assertEqual(
            self.query.call_args_list,
            [
                call(
                    {"query": "氣候", "top_k": 1, "conditions": {}},
                    self.settings,
                ),
                call(
                    {
                        "query": "氣候",
                        "top_k": 1,
                        "conditions": {
                            "time": {"start": "2025", "end": "2025"}
                        },
                    },
                    self.settings,
                ),
            ],
        )
        self.bundle.assert_called_once_with(
            self.settings.bundle_path,
            expected_manifest_sha256=self.settings.manifest_sha256,
        )
        self.assertEqual(
            self.models.call_args_list, [call("scope"), call("answer")]
        )
        self.peer.assert_called_once_with("reranker", 8001)

    def test_either_index_probe_failure_stops_before_models_and_health(self):
        for results in ([ValueError("PRIVATE")], [{}, ValueError("PRIVATE")]):
            with self.subTest(failing_probe=len(results)):
                self.query.reset_mock()
                self.query.side_effect = results
                with self.assertRaises(ValueError):
                    worker.run("ready", {}, self.settings)
                self.assertEqual(self.query.call_count, len(results))
                self.models.assert_not_called()
                self.health.assert_not_called()

    def test_model_identity_failure_cannot_become_ready(self):
        self.models.return_value.send.return_value.body = b'{"models":[]}'
        with self.assertRaisesRegex(ValueError, "model identity mismatch"):
            worker.run("ready", {}, self.settings)
        self.assertEqual(self.query.call_count, 2)
        self.health.assert_not_called()

    def test_reranker_failure_remains_an_explicit_optional_degradation(self):
        self.health.return_value.send.side_effect = TimeoutError("PRIVATE")
        self.assertEqual(
            worker.run("ready", {}, self.settings),
            {"ready": True, "documents": 5, "reranker_ready": False},
        )

    def test_ready_reads_settings_once_in_required_probe_order(self):
        events = Mock()
        original = self.settings

        class ObservedSettings:
            @property
            def bundle_path(self):
                events.setting("bundle_path")
                return original.bundle_path

            @property
            def manifest_sha256(self):
                events.setting("manifest_sha256")
                return original.manifest_sha256

            @property
            def rerank_enabled(self):
                events.setting("rerank_enabled")
                return original.rerank_enabled

        class Documents:
            def __len__(self):
                events.documents()
                return 5

        self.bundle.return_value.documents = Documents()
        for name, mock in (
            ("catalog", self.bundle),
            ("search", self.query),
            ("model", self.models),
            ("peer", self.peer),
            ("health", self.health),
        ):
            events.attach_mock(mock, name)
        settings = ObservedSettings()
        self.assertEqual(
            worker.run("ready", {}, settings),
            {"ready": True, "documents": 5, "reranker_ready": True},
        )
        tags = worker.HTTPRequest(
            "GET", worker.OLLAMA_BASE_URL + "/api/tags", ()
        )
        self.assertEqual(
            events.mock_calls,
            [
                call.setting("bundle_path"),
                call.setting("manifest_sha256"),
                call.catalog(
                    original.bundle_path,
                    expected_manifest_sha256=original.manifest_sha256,
                ),
                call.search(
                    {"query": "氣候", "top_k": 1, "conditions": {}}, settings
                ),
                call.search(
                    {
                        "query": "氣候",
                        "top_k": 1,
                        "conditions": {
                            "time": {"start": "2025", "end": "2025"}
                        },
                    },
                    settings,
                ),
                call.model("scope"),
                call.model().send(tags, timeout=3, max_bytes=128 * 1024),
                call.model("answer"),
                call.model().send(tags, timeout=3, max_bytes=128 * 1024),
                call.documents(),
                call.setting("rerank_enabled"),
                call.peer("reranker", 8001),
                call.health(),
                call.health().send(
                    worker.HTTPRequest(
                        "GET", "http://172.18.0.4:8001/healthz", ()
                    ),
                    timeout=2,
                    max_bytes=worker.MAX_RESPONSE_BYTES,
                ),
            ],
        )

    def test_disabled_reranker_and_flag_failure_keep_the_optional_boundary(
        self,
    ):
        original = self.settings
        disabled = SimpleNamespace(
            bundle_path=original.bundle_path,
            manifest_sha256=original.manifest_sha256,
            rerank_enabled=False,
        )
        self.assertEqual(
            worker.run("ready", {}, disabled),
            {"ready": True, "documents": 5},
        )
        error = RuntimeError("flag unavailable")

        class InvalidFlag:
            bundle_path = original.bundle_path
            manifest_sha256 = original.manifest_sha256

            @property
            def rerank_enabled(self):
                raise error

        with self.assertRaises(RuntimeError) as caught:
            worker.run("ready", {}, InvalidFlag())
        self.assertIs(caught.exception, error)
        self.assertEqual(self.query.call_count, 4)
        self.assertEqual(self.models.call_count, 4)
        self.peer.assert_not_called()
        self.health.assert_not_called()

    def test_catalog_load_failure_stops_before_all_other_probes(self):
        error = ValueError("catalog unavailable")
        self.bundle.side_effect = error
        with self.assertRaises(ValueError) as caught:
            worker.run("ready", {}, self.settings)
        self.assertIs(caught.exception, error)
        self.query.assert_not_called()
        self.models.assert_not_called()
        self.peer.assert_not_called()
        self.health.assert_not_called()


if __name__ == "__main__":
    unittest.main()
