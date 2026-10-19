"""Closed host receipts and private probe control flow; no Docker/GPU/secrets.

The web control-flow fixtures stand in for Pydantic's already-tested wire models.
They test chaining, state/refinement/reset and sanitization independently of model
inference; production probe code always uses the real strict runtime models.
"""

from __future__ import annotations

from contextlib import redirect_stdout
import copy
import io
import json
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from search.api.shared.config import Settings
from search.transport.http import HTTPResponse
from search.catalog.bundle import CatalogBundle
from search.operations.deployment.release import probe


REVISION = "a" * 40
BUNDLE = "b" * 64
MANIFEST = "c" * 64
IDS = {"web": "1" * 64, "conversation-api": "2" * 64, "filtered-embedding": "3" * 64}
PRIVATE = "never-emit-private-http-body"


def identity():
    return {
        "schema_version": 1,
        "revision": REVISION,
        "bundle_id": BUNDLE,
        "manifest_sha256": MANIFEST,
    }


def api_ready():
    return {
        **identity(),
        "api_version_verified": True,
        "backend_ready": True,
        "pinned_backend_models_verified": True,
        "reranker_ready": True,
        "documents": 7,
        "code": "ready",
    }


def warmup_ready():
    return {
        **identity(),
        "model_warmup_verified": True,
        "full_gpu_residency_verified": True,
        "gpu_uuid_verification": "external_required",
        "keep_alive": -1,
        "models": [
            {
                "role": role,
                "model": model,
                "digest": digest,
                "num_ctx": context,
                "size_bytes": 1024,
                "vram_bytes": 1024,
                "processor": "GPU",
                "load_ms": 5000,
                "elapsed_ms": 6000,
            }
            for role, model, digest, context in (
                (
                    "scope",
                    probe.SCOPE_MODEL,
                    probe.SCOPE_MODEL_DIGEST,
                    probe.SCOPE_NUM_CTX,
                ),
                (
                    "planner",
                    probe.PLANNER_MODEL,
                    probe.PLANNER_MODEL_DIGEST,
                    probe.PLANNER_OPTIONS["num_ctx"],
                ),
            )
        ],
        "elapsed_ms": 12000,
        "code": "ready",
    }


def warmup_failed():
    return {
        **warmup_ready(),
        "model_warmup_verified": False,
        "full_gpu_residency_verified": False,
        "models": [],
        "code": "warmup_failed",
    }


def web_ready():
    return {
        **identity(),
        "website_ready": True,
        "two_turn_search_verified": True,
        "session_reset_verified": True,
        "search_quality_verified": False,
        "turns": [
            {"version": 1, "cards": 2, "elapsed_ms": 15000.125},
            {"version": 2, "cards": 1, "elapsed_ms": 15000.125},
        ],
        "code": "ready",
    }


class FakeDocker:
    def __init__(self, api=None, website=None, warmup=None):
        self.outputs = [
            json.dumps(warmup_ready() if warmup is None else warmup).encode(),
            json.dumps(api_ready() if api is None else api).encode(),
            json.dumps(web_ready() if website is None else website).encode(),
        ]
        self.calls = []
        self.error = None

    def call(self, arguments, *, timeout):
        self.calls.append((arguments, timeout))
        if self.error:
            raise self.error
        return self.outputs.pop(0)


class HostProbeTests(unittest.TestCase):
    def verify(self, docker=None, **kwargs):
        return probe.verify_private_release(
            FakeDocker() if docker is None else docker,
            kwargs.pop("container_ids", IDS),
            revision=kwargs.pop("revision", REVISION),
            bundle_id=kwargs.pop("bundle_id", BUNDLE),
            manifest_sha256=kwargs.pop("manifest_sha256", MANIFEST),
            **kwargs,
        )

    def test_ready_uses_existing_namespaces_and_public_identity_only(self):
        docker = FakeDocker()
        result = self.verify(docker)
        self.assertEqual(result["status"], "ready")
        for flag in (
            "model_warmup_verified",
            "readiness_verified",
            "two_turn_search_verified",
            "session_reset_verified",
        ):
            self.assertIs(result[flag], True)
        self.assertIs(result["search_quality_verified"], False)
        self.assertIs(result["gateway_verified"], False)
        self.assertEqual([item[1] for item in docker.calls], [305, 100, 285])
        for (args, _), service in zip(
            docker.calls, ("conversation-api", "conversation-api", "web")
        ):
            self.assertEqual(args[:5], ["exec", IDS[service], "python", "-B", "-c"])
            self.assertEqual(args[-3:], [REVISION, BUNDLE, MANIFEST])
            self.assertNotIn("--network", args)
            self.assertNotIn("--privileged", args)
            self.assertNotIn(PRIVATE, json.dumps(args))
        self.assertNotIn("token", json.dumps(result))
        self.assertNotIn("query", json.dumps(result))

    def test_readiness_only_does_not_warm_models_or_claim_search_or_gateway(self):
        docker = FakeDocker()
        docker.outputs.pop(0)
        result = self.verify(docker, search=False)
        self.assertEqual(len(docker.calls), 1)
        self.assertEqual(result["status"], "ready")
        self.assertIsNone(result["website"])
        self.assertIs(result["search_requested"], False)
        self.assertIs(result["model_warmup_verified"], False)
        self.assertIsNone(result["warmup"])
        self.assertIs(result["two_turn_search_verified"], False)
        self.assertIs(result["session_reset_verified"], False)

    def test_failed_readiness_after_warmup_does_not_consume_search_turns(self):
        value = api_ready()
        value.update(
            backend_ready=False,
            pinned_backend_models_verified=False,
            reranker_ready=False,
            documents=0,
            code="api_probe_failed",
        )
        docker = FakeDocker(api=value)
        result = self.verify(docker)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["readiness_verified"])
        self.assertIsNone(result["website"])
        self.assertTrue(result["model_warmup_verified"])
        self.assertEqual(len(docker.calls), 2)

    def test_failed_warmup_blocks_readiness_and_search_without_retry(self):
        docker = FakeDocker(warmup=warmup_failed())
        result = self.verify(docker)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["model_warmup_verified"])
        self.assertFalse(result["readiness_verified"])
        self.assertIsNone(result["api"])
        self.assertIsNone(result["website"])
        self.assertEqual(len(docker.calls), 1)

    def test_warmup_rejects_identity_residency_and_nonclosed_evidence(self):
        values = []
        for changes in (
            {"revision": "f" * 40},
            {"model_warmup_verified": 1},
            {"full_gpu_residency_verified": False},
            {"keep_alive": True},
            {"keep_alive": "10m"},
            {"gpu_uuid_verification": "verified"},
            {"models": []},
            {"elapsed_ms": float("inf")},
            {"elapsed_ms": True},
            {"elapsed_ms": 305001},
            {"private": PRIVATE},
        ):
            values.append({**warmup_ready(), **changes})
        for changes in (
            {"model": PRIVATE},
            {"digest": "f" * 64},
            {"num_ctx": True},
            {"num_ctx": 16384},
            {"processor": "CPU+GPU"},
            {"vram_bytes": 512},
            {"size_bytes": True},
            {"load_ms": float("nan")},
            {"elapsed_ms": -1},
            {"private": PRIVATE},
        ):
            value = warmup_ready()
            value["models"][0].update(changes)
            values.append(value)
        value = warmup_ready()
        value["models"].reverse()
        values.append(value)
        for value in values:
            docker = FakeDocker(warmup=value)
            with (
                self.subTest(value=value),
                self.assertRaises(probe.ReleaseProbeError) as caught,
            ):
                self.verify(docker)
            self.assertEqual(len(docker.calls), 1)
            self.assertNotIn(PRIVATE, str(caught.exception))

    def test_search_failure_keeps_readiness_separate(self):
        value = web_ready()
        value.update(
            two_turn_search_verified=False,
            session_reset_verified=False,
            code="web_probe_failed",
        )
        value["turns"] = value["turns"][:1]
        result = self.verify(FakeDocker(website=value))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["readiness_verified"])
        self.assertFalse(result["two_turn_search_verified"])

    def test_rejects_invalid_identity_before_running(self):
        cases = [
            {"revision": REVISION[:12]},
            {"revision": "a" * 40 + ";"},
            {"bundle_id": True},
            {"manifest_sha256": "sha256:" + MANIFEST},
            {"container_ids": {**IDS, "extra": "4" * 64}},
            {"container_ids": {**IDS, "web": "2" * 64}},
            {"container_ids": {**IDS, "web": "short-name"}},
            {"search": 1},
        ]
        for kwargs in cases:
            docker = FakeDocker()
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaisesRegex(probe.ReleaseProbeError, "identity_invalid"),
            ):
                self.verify(docker, **kwargs)
            self.assertFalse(docker.calls)

    def test_rejects_extra_private_output_duplicate_keys_and_size(self):
        cases = [
            json.dumps({**api_ready(), "private": PRIVATE}).encode(),
            b'{"schema_version":1,"schema_version":1}',
            b"[1]",
            b"NaN",
            b"x" * (probe.MAX_OUTPUT_BYTES + 1),
            "not-bytes",
        ]
        for raw in cases:
            docker = FakeDocker()
            docker.outputs[0] = raw
            with (
                self.subTest(raw=type(raw)),
                self.assertRaises(probe.ReleaseProbeError) as caught,
            ):
                self.verify(docker)
            self.assertNotIn(PRIVATE, str(caught.exception))

    def test_rejects_api_type_and_readiness_inconsistency(self):
        cases = [
            {"schema_version": True},
            {"documents": True},
            {"documents": 0},
            {"documents": 10001},
            {"backend_ready": 1},
            {"api_version_verified": False, "code": "api_probe_failed"},
            {"pinned_backend_models_verified": False, "code": "api_probe_failed"},
            {"reranker_ready": False},
            {"code": PRIVATE},
            {"revision": "f" * 40},
        ]
        for changes in cases:
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(probe.ReleaseProbeError, "api_contract_invalid"),
            ):
                self.verify(FakeDocker(api={**api_ready(), **changes}))

    def test_rejects_web_quality_claim_and_invalid_metrics(self):
        values = []
        for changes in (
            {"search_quality_verified": True},
            {"session_reset_verified": 1},
            {"revision": "f" * 40},
            {"private": PRIVATE},
            {"turns": []},
            {"two_turn_search_verified": False},
        ):
            values.append({**web_ready(), **changes})
        for changes in (
            {"version": True},
            {"version": 2},
            {"cards": 0},
            {"cards": 4},
            {"elapsed_ms": -1},
            {"elapsed_ms": float("inf")},
            {"elapsed_ms": True},
            {"elapsed_ms": 286000},
            {"private": PRIVATE},
        ):
            value = web_ready()
            value["turns"][0].update(changes)
            values.append(value)
        for value in values:
            with self.subTest(value=value), self.assertRaises(probe.ReleaseProbeError):
                self.verify(FakeDocker(website=value))

    def test_command_failure_is_sanitized(self):
        docker = FakeDocker()
        docker.error = ValueError(PRIVATE)
        with self.assertRaisesRegex(
            probe.ReleaseProbeError, "release_probe_command_failed"
        ) as caught:
            self.verify(docker)
        self.assertNotIn(PRIVATE, str(caught.exception))

    def test_clock_and_total_deadline_fail_closed(self):
        for ticks, code in (
            ([float("nan")], "clock_invalid"),
            ([1, 0], "deadline_exceeded"),
            ([1, 800], "deadline_exceeded"),
            ([1, 2, float("inf")], "deadline_exceeded"),
            ([1, 3, 2], "deadline_exceeded"),
        ):
            iterator = iter(ticks)
            with (
                self.subTest(ticks=ticks),
                self.assertRaisesRegex(probe.ReleaseProbeError, code),
            ):
                self.verify(clock=lambda: next(iterator))


class ScriptFixture:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def send(self, request, *, timeout, max_bytes):
        self.calls.append((request, timeout, max_bytes))
        expected_path, status, body = self.responses.pop(0)
        if not request.url.endswith(expected_path):
            raise AssertionError("unexpected fixture endpoint")
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return HTTPResponse(status, (), raw, request.url)


def execute(script, fixture, *, modules=None):
    output = io.StringIO()
    with (
        mock.patch.object(sys, "argv", ["probe", REVISION, BUNDLE, MANIFEST]),
        mock.patch("search.transport.http.UrllibTransport", return_value=fixture),
        mock.patch.dict(sys.modules, {} if modules is None else modules),
        mock.patch("time.sleep"),
        redirect_stdout(output),
    ):
        exec(
            compile(script, "<private-release-probe>", "exec"), {"__name__": "__main__"}
        )
    raw = output.getvalue()
    if PRIVATE in raw:
        raise AssertionError("private body escaped")
    return json.loads(raw)


def helper_ready():
    return {
        "schema_version": 1,
        "status": "ready",
        "keep_alive": -1,
        "gpu_uuid_verification": "external_required",
        "search_quality_verified": False,
        "models": [
            {
                **row,
                "status": "ready",
                "code": "ready",
                "resident_before": False,
                "prompt_ms": 500,
                "decode_ms": 500,
                "prompt_eval_count": 100,
                "eval_count": 10,
            }
            for row in warmup_ready()["models"]
        ],
        "total_ms": 12000,
    }


class WarmupScriptTests(unittest.TestCase):
    def run_script(self, value=None, *, error=None):
        fixture = ScriptFixture([])
        with (
            mock.patch(
                "search.operations.shared.runtime_probe.probe_models",
                return_value=helper_ready() if value is None else value,
                side_effect=error,
            ) as helper,
            mock.patch("time.monotonic", side_effect=[1, 13]),
        ):
            result = execute(probe._WARMUP_SCRIPT, fixture)
        helper.assert_called_once_with(budget_seconds=300)
        self.assertFalse(fixture.calls)
        probe._warmup(result, REVISION, BUNDLE, MANIFEST)
        return result

    def test_fixed_helper_runs_once_and_only_closed_residency_summary_escapes(self):
        value = self.run_script()
        self.assertEqual(value, warmup_ready())
        for row in value["models"]:
            self.assertNotIn("prompt_eval_count", row)
            self.assertNotIn("resident_before", row)
            self.assertEqual(row["size_bytes"], row["vram_bytes"])
        self.assertNotIn("messages", probe._WARMUP_SCRIPT)
        self.assertNotIn("/api/chat", probe._WARMUP_SCRIPT)
        self.assertNotIn("http://", probe._WARMUP_SCRIPT)

    def test_incomplete_or_partial_gpu_helper_cannot_pass_and_is_not_retried(self):
        values = []
        for changes in (
            {"status": "blocked"},
            {"schema_version": True},
            {"keep_alive": True},
            {"gpu_uuid_verification": "verified"},
            {"search_quality_verified": True},
            {"models": []},
            {"private": PRIVATE},
        ):
            values.append({**helper_ready(), **changes})
        for changes in (
            {"status": "blocked"},
            {"code": "full_gpu_residency_required"},
            {"processor": "CPU+GPU"},
            {"vram_bytes": 512},
            {"model": PRIVATE},
            {"digest": "f" * 64},
            {"num_ctx": True},
            {"resident_before": 1},
            {"load_ms": PRIVATE},
            {"private": PRIVATE},
        ):
            value = copy.deepcopy(helper_ready())
            value["models"][0].update(changes)
            values.append(value)
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(self.run_script(value), warmup_failed())
        self.assertEqual(self.run_script(error=RuntimeError(PRIVATE)), warmup_failed())


class APIScriptTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            revision=REVISION,
            manifest_sha256=MANIFEST,
            rerank_enabled=True,
            bundle_path="/sealed",
            key_file="/secret",
        )
        self.version = {
            "revision": REVISION,
            "catalog_manifest_sha256": MANIFEST,
            "geography": False,
            "persistent_memory": False,
            "generated_answers": False,
            "filter_scope": "index_prefilter",
        }
        self.ready = {"ready": True, "documents": 7, "reranker_ready": True}

    def run_script(self, responses, *, bundle_id=BUNDLE):
        fixture = ScriptFixture(responses)
        with (
            mock.patch.object(Settings, "from_environment", return_value=self.settings),
            mock.patch.object(
                CatalogBundle,
                "load",
                return_value=SimpleNamespace(bundle_id=bundle_id, documents=range(7)),
            ),
            mock.patch("search.secrets.read_secret", return_value=PRIVATE),
        ):
            value = execute(probe._API_SCRIPT, fixture)
        return value, fixture

    def test_api_validates_sealed_identity_and_ready_contract(self):
        value, fixture = self.run_script(
            [
                ("/v1/version", 200, self.version),
                ("/readyz", 200, self.ready),
            ]
        )
        self.assertEqual(value, api_ready())
        for request, timeout, max_bytes in fixture.calls:
            self.assertEqual(
                dict(request.headers)["Authorization"], "Bearer " + PRIVATE
            )
            self.assertLessEqual(timeout, 30)
            self.assertEqual(max_bytes, 256 * 1024)
        self.assertNotIn(PRIVATE, json.dumps(value))

    def test_api_readiness_retries_are_bounded_without_turns(self):
        value, fixture = self.run_script(
            [
                ("/v1/version", 200, self.version),
                ("/readyz", 503, {"error": PRIVATE}),
                ("/readyz", 503, {"error": PRIVATE}),
                ("/readyz", 200, self.ready),
            ]
        )
        self.assertEqual(value, api_ready())
        self.assertEqual(len(fixture.calls), 4)

    def test_api_bad_readiness_cannot_become_ready(self):
        for ready in (
            {**self.ready, "documents": True},
            {**self.ready, "documents": 6},
            {**self.ready, "reranker_ready": False},
            {**self.ready, "ready": 1},
            {**self.ready, "private": PRIVATE},
        ):
            with self.subTest(ready=ready):
                value, fixture = self.run_script(
                    [
                        ("/v1/version", 200, self.version),
                        *[("/readyz", 200, ready)] * 3,
                    ]
                )
                self.assertFalse(value["backend_ready"])
                self.assertEqual(value["documents"], 0)
                self.assertEqual(len(fixture.calls), 4)
                probe._api(value, REVISION, BUNDLE, MANIFEST)

    def test_api_response_bounds_and_duplicate_json_fail_closed(self):
        for raw in (
            b"x" * (256 * 1024 + 1),
            b'{"revision":"a","revision":"b"}',
            b'{"private":"never-emit-private-http-body"}',
        ):
            with self.subTest(raw_size=len(raw)):
                value, fixture = self.run_script([("/v1/version", 200, raw)])
                self.assertFalse(value["api_version_verified"])
                self.assertFalse(value["backend_ready"])
                self.assertEqual(len(fixture.calls), 1)

    def test_api_version_boolean_integer_and_bundle_mismatch_fail(self):
        for changes in (
            {"geography": 0},
            {"generated_answers": 0},
            {"revision": "f" * 40},
        ):
            with self.subTest(changes=changes):
                value, fixture = self.run_script(
                    [("/v1/version", 200, {**self.version, **changes})]
                )
                self.assertFalse(value["api_version_verified"])
                self.assertEqual(len(fixture.calls), 1)
        value, fixture = self.run_script([], bundle_id="f" * 64)
        self.assertFalse(value["api_version_verified"])
        self.assertFalse(fixture.calls)


class WebControlFlowTests(unittest.TestCase):
    def setUp(self):
        self.context = SimpleNamespace(timezone="Asia/Taipei")
        self.results = [self.result([]), self.result(["CSV"])]
        contracts, schemas, upstream = (
            ModuleType(name)
            for name in (
                "search.api.conversation.contract",
                "search.api.retrieval.schemas",
                "search.web.upstream",
            )
        )
        contracts.TurnResponse = SimpleNamespace(
            model_validate=mock.Mock(side_effect=self.results)
        )
        schemas.ContextUsed = SimpleNamespace(
            model_validate=lambda _value: self.context
        )
        upstream.WebSettings = SimpleNamespace(
            from_environment=lambda: SimpleNamespace(
                revision=REVISION, origin="http://127.0.0.1:18083"
            )
        )
        self.modules = {
            module.__name__: module for module in (contracts, schemas, upstream)
        }
        self.token, self.replacement = "a" * 43, "b" * 43

    def result(self, formats):
        card = SimpleNamespace(
            rank=1,
            dataset_id="12345678-1234-1234-1234-123456789abc",
            url="https://data.depositar.io/dataset/public-rainfall",
            condition_status="match",
        )
        state = SimpleNamespace(
            topic="降雨", formats=formats, time=None, unapplied_time=None
        )
        search = SimpleNamespace(
            status="ok",
            revision=REVISION,
            catalog_manifest_sha256=MANIFEST,
            mode="ai",
            decision="search",
            planning_status="ready",
            filter_scope="index_prefilter" if formats else "none",
            ranking="nvidia-rerank",
            rerank_status="applied",
            answer=SimpleNamespace(status="not_requested"),
            context_used=self.context,
            cards=[card],
            candidate_count=2,
            applied_conditions=SimpleNamespace(formats=formats) if formats else None,
            parsed_conditions=SimpleNamespace(formats=formats),
            timing_ms={},
        )
        return SimpleNamespace(
            status="ok",
            reason="none",
            revision=REVISION,
            state=state,
            search=search,
            timing_ms={},
        )

    def responses(self, *, include_assets=False):
        session = {
            "version": 0,
            "expires_in": 1800,
            "max_turns": 20,
            "revision": REVISION,
        }
        checks = [("/healthz", 200, {"status": "ok"})]
        if include_assets:
            checks.append(
                ("/", 200, b'<title>depositar AI</title><div id="root"></div>')
            )
        return checks + [
            ("/api/session", 200, {**session, "token": self.token}),
            ("/api/turn", 200, {"version": 1, "result": {}, "context_used": {}}),
            ("/api/turn", 200, {"version": 2, "result": {}, "context_used": {}}),
            ("/api/session", 200, {"status": "cleared"}),
            ("/api/turn", 410, {"error": {"code": "session_expired"}}),
            ("/api/session", 200, {**session, "token": self.replacement}),
            ("/api/session", 200, {"status": "cleared"}),
            ("/api/session", 200, {"status": "cleared"}),
        ]

    def run_script(self, responses=None):
        fixture = ScriptFixture(self.responses() if responses is None else responses)
        value = execute(probe._WEB_SCRIPT, fixture, modules=self.modules)
        return value, fixture

    def test_two_http_turns_chain_versions_and_reset_without_exposing_tokens(self):
        value, fixture = self.run_script()
        self.assertEqual(value["code"], "ready")
        self.assertTrue(value["two_turn_search_verified"])
        self.assertTrue(value["session_reset_verified"])
        self.assertFalse(value["search_quality_verified"])
        self.assertNotIn(self.token, json.dumps(value))
        probe._web(value, REVISION, BUNDLE, MANIFEST)
        turns = [
            json.loads(request.body)
            for request, _, _ in fixture.calls
            if request.method == "POST" and request.url.endswith("/api/turn")
        ]
        self.assertEqual([turn["version"] for turn in turns], [0, 1, 2])
        self.assertEqual(turns[1]["query"], "CSV")
        self.assertEqual(len(fixture.calls), 9)
        for request, timeout, max_bytes in fixture.calls:
            self.assertEqual(dict(request.headers)["Origin"], "http://127.0.0.1:18083")
            self.assertNotEqual(request.url, "http://127.0.0.1:8000/")
            self.assertLessEqual(timeout, 130)
            self.assertEqual(max_bytes, 256 * 1024)

    def test_failed_second_turn_cleans_session_and_keeps_partial_evidence(self):
        responses = self.responses()[:4]
        responses[3] = ("/api/turn", 503, {"error": PRIVATE})
        responses.append(("/api/session", 200, {"status": "cleared"}))
        value, fixture = self.run_script(responses)
        self.assertTrue(value["website_ready"])
        self.assertFalse(value["two_turn_search_verified"])
        self.assertFalse(value["session_reset_verified"])
        self.assertEqual(len(value["turns"]), 1)
        self.assertEqual(fixture.calls[-1][0].method, "DELETE")
        probe._web(value, REVISION, BUNDLE, MANIFEST)

    def test_changed_subject_or_missing_csv_filter_cannot_pass_refinement(self):
        self.results[1].state.topic = "unrelated topic"
        responses = self.responses()[:4] + [
            ("/api/session", 200, {"status": "cleared"})
        ]
        value, _fixture = self.run_script(responses)
        self.assertEqual(value["code"], "web_probe_failed")
        self.assertFalse(value["two_turn_search_verified"])
        self.assertEqual(len(value["turns"]), 2)

    def test_old_token_must_fail_before_claiming_reset(self):
        responses = self.responses()[:6]
        responses[5] = ("/api/turn", 200, {"private": PRIVATE})
        responses.append(("/api/session", 200, {"status": "cleared"}))
        value, _fixture = self.run_script(responses)
        self.assertTrue(value["two_turn_search_verified"])
        self.assertFalse(value["session_reset_verified"])
        self.assertEqual(value["code"], "web_probe_failed")

    def test_zero_cards_or_reranker_fallback_blocks_functional_acceptance(self):
        self.results[0].search.cards = []
        responses = self.responses()[:3] + [
            ("/api/session", 200, {"status": "cleared"})
        ]
        value, _fixture = self.run_script(responses)
        self.assertFalse(value["two_turn_search_verified"])
        self.assertEqual(value["turns"], [])


if __name__ == "__main__":
    unittest.main()
