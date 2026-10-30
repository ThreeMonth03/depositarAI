"""Batch lifecycle, audit publication and offline quality interoperability."""

from contextlib import contextmanager
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import check_search_quality as quality
from search.operations.deployment import shared_backend as shared
from search.operations.deployment import shared_batch as batch
from tests.unit.operations.test_search_quality import turn


REVISION = "a" * 40
MANIFEST = "b" * 64


class BatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="shared-batch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.destination = self.root / "capture"
        self.config = self.root / "config.json"
        self.specfile = self.root / "cases.json"
        self.fixture = [
            turn({"start": "2020", "end": "2025"}),
            turn({"start": "2024", "end": "2024"}, version=2, retain_topic=True),
            turn(
                {"start": "2024", "end": "2024"},
                version=3,
                retain_topic=True,
                formats=["CSV"],
            ),
        ]
        self.specification = {
            "schema_version": 1,
            "cases": [
                {
                    "id": "PRIVATE_CASE",
                    "turns": [
                        {key: item[key] for key in ("query", "expected")}
                        for item in self.fixture
                    ],
                }
            ],
        }
        self.specification["cases"][0]["turns"][0]["query"] = "PRIVATE_QUERY"
        self.write_spec()
        self.profile = {
            "state_root": self.state,
            "production_config": self.root / "production.json",
            "production_source": self.root / "native-source",
        }
        self.product = SimpleNamespace(
            **{
                name: self.root / name
                for name in (
                    "state_root",
                    "api_model_root",
                    "embedding_model_root",
                    "reranker_model_root",
                )
            }
        )
        self.record = {
            "phase": "running",
            "revision": REVISION,
            "policy": {"policy": {}, "request_policy_sha256": "c" * 64},
            "compute_sha256": "d" * 64,
            "runtime": {"start_identity_sha256": "e" * 64},
        }
        metadata = self.fixture[0]["source_metadata"]
        self.session = SimpleNamespace(
            context={
                "source_revision": "f" * 40,
                "plan": {"manifest_sha256": MANIFEST},
                "bundle": SimpleNamespace(
                    bundle_id="1" * 64, condition_metadata=metadata
                ),
            },
            inputs=SimpleNamespace(identity={"model_manifest_sha256": "2" * 64}),
            native=SimpleNamespace(
                inference_images={"reranker": {"Id": "sha256:" + "3" * 64}}
            ),
            docker=SimpleNamespace(deadline=time.monotonic() + 1800),
            candidate_endpoint="172.20.0.4",
            attach_record=lambda _record: None,
        )
        self.events = []
        self.audit_failure = None
        self.exit_failure = False
        self.session.verify = self.verify
        self.session.observe = lambda: self.events.append("observe")
        self.session.check_models = self.check_models
        self.session.remaining = self.remaining
        self.report_error = None
        self.probe_calls = 0
        self.source_checks = 0
        self.operator_changed = False

    def source_revision(self, _root):
        self.source_checks += 1
        return (
            "8" * 40 if self.operator_changed and self.source_checks >= 3 else "9" * 40
        )

    def write_spec(self):
        self.specfile.write_text(json.dumps(self.specification), encoding="utf-8")
        self.specfile.chmod(0o600)

    def verify(self, *_args):
        self.events.append("audit")
        if self.audit_failure == self.events.count("audit"):
            raise shared.SharedBackendError(
                "shared_backend_baseline_query_audit_failed"
            )
        return {"models": [{"role": "scope", "digest": "4" * 64}]}

    def check_models(self, *_args, **_kwargs):
        self.events.append("model_checkpoint")

    def remaining(self, maximum, *, deadline=None):
        shared.require(
            deadline is None or time.monotonic() < deadline, "batch_deadline_exceeded"
        )
        return maximum

    @contextmanager
    def live(self, *_args):
        yield self.session
        self.events.append("live_exit")
        if self.exit_failure:
            raise ValueError("PRIVATE_FAILED_PATH")

    def probe(self, _endpoint, queries, revision, manifest, *, checkpoint, deadline):
        self.probe_calls += 1
        self.assertEqual(revision, REVISION)
        self.assertEqual(manifest, MANIFEST)
        self.assertLess(deadline, self.session.docker.deadline)
        rows = []
        for index, _query in enumerate(queries):
            checkpoint()
            response = copy.deepcopy(self.fixture[index]["response"])
            response["context_used"] = {"actual_reference_clock": "preserved"}
            response["result"]["search"]["cards"][0]["title"] = "PRIVATE_TITLE"
            if self.report_error is not None and index == 1:
                rows.append(
                    {
                        "status": "failed",
                        "wall_seconds": 0.3,
                        "request_seconds": 0.2,
                        "error": self.report_error,
                    }
                )
                break
            rows.append(
                {
                    "status": "ok",
                    "response": response,
                    "wall_seconds": 0.4,
                    "request_seconds": 0.2,
                }
            )
        return {
            "status": "complete" if self.report_error is None else "failed",
            "turns": rows,
            "error": self.report_error,
            "session_reset": True,
        }

    def run_batch(self, *, expected=REVISION):
        with (
            patch.object(
                shared, "require_clean_revision", side_effect=self.source_revision
            ),
            patch.object(shared, "load_config", return_value=self.profile),
            patch.object(shared, "current", return_value=self.record),
            patch.object(shared, "live", side_effect=self.live),
            patch.object(
                batch.product_config, "load_config", return_value=self.product
            ),
            patch.object(batch.probe, "conversation", side_effect=self.probe),
        ):
            return batch.run_batch(
                self.config,
                cases=self.specfile,
                destination=self.destination,
                expected_revision=expected,
            )

    def read(self, name):
        return json.loads((self.destination / name).read_text())

    def test_multiturn_capture_can_be_scored_without_reimplementing_quality(self):
        result = self.run_batch()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["completed_turns"], 3)
        self.assertEqual(result["plan_sha256"], quality.plan_digest(self.specification))
        self.assertEqual(self.events.count("audit"), 2)
        self.assertEqual(self.events.count("model_checkpoint"), 3)
        self.assertEqual(self.events[-1], "live_exit")
        captured = self.read("capture.json")
        self.assertEqual(quality.evaluate(captured)["status"], "passed")
        replies = captured["cases"][0]["turns"]
        self.assertEqual([row["response"]["version"] for row in replies], [1, 2, 3])
        self.assertEqual(
            replies[1]["response"]["context_used"],
            {"actual_reference_clock": "preserved"},
        )
        serialized = json.dumps(result) + json.dumps(self.read("run.json"))
        for private in ("PRIVATE_QUERY", "PRIVATE_CASE", "PRIVATE_TITLE"):
            self.assertNotIn(private, serialized)
        self.assertIn("PRIVATE_QUERY", json.dumps(captured))

    def test_busy_stops_remaining_turns_and_cases_without_retry(self):
        self.specification["cases"].append(
            {
                "id": "later",
                "turns": [copy.deepcopy(self.specification["cases"][0]["turns"][0])],
            }
        )
        self.write_spec()
        self.report_error = "shared_batch_busy"
        result = self.run_batch()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["identity_verified"])
        self.assertEqual(self.probe_calls, 1)
        self.assertEqual(result["completed_turns"], 1)
        outcomes = self.read("outcomes.json")["cases"]
        self.assertEqual(
            [row["status"] for row in outcomes[0]["turns"]], ["ok", "failed", "not_run"]
        )
        self.assertEqual(outcomes[1]["status"], "not_run")
        self.assertEqual(self.events.count("audit"), 2)

    def test_post_audit_or_late_fd_failure_quarantines_results(self):
        for failure in ("audit", "exit"):
            with self.subTest(failure=failure):
                self.destination = self.root / ("capture-" + failure)
                self.events.clear()
                self.audit_failure = 2 if failure == "audit" else None
                self.exit_failure = failure == "exit"
                result = self.run_batch()
                self.assertEqual(result["status"], "quarantined")
                self.assertFalse(self.read("run.json")["identity_verified"])
                self.assertEqual(len(self.read("capture.json")["cases"]), 1)
                self.assertNotIn("PRIVATE_FAILED_PATH", json.dumps(result))

    def test_wrong_candidate_or_occupied_slot_never_creates_capture(self):
        with self.assertRaisesRegex(
            shared.SharedBackendError, "batch_candidate_changed"
        ):
            self.run_batch(expected="f" * 40)
        self.assertFalse(self.destination.exists())
        with shared.journal(self.state):
            with self.assertRaisesRegex(
                shared.SharedBackendError, "shared_backend_busy"
            ):
                self.run_batch()
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.probe_calls, 0)

    def test_audit_failure_before_query_records_unexecuted_suite(self):
        self.audit_failure = 1
        result = self.run_batch()
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["completed_turns"], 0)
        self.assertEqual(self.probe_calls, 0)
        self.assertEqual(self.read("outcomes.json")["cases"][0]["status"], "not_run")

    def test_batch_reserves_final_audit_before_starting_queries(self):
        self.session.docker.deadline = time.monotonic() + 100
        result = self.run_batch()
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["completed_turns"], 0)
        self.assertEqual(self.probe_calls, 0)
        self.assertEqual(self.events.count("audit"), 2)

    def test_changed_operator_after_execution_cannot_publish_complete_receipt(self):
        self.operator_changed = True
        result = self.run_batch()
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["error"], "shared_backend_batch_operator_changed")
        self.assertEqual(result["operator_revision"], "9" * 40)
        self.assertFalse(self.read("run.json")["identity_verified"])

    def test_changed_bff_endpoint_prevents_query_and_retains_final_audit(self):
        self.session.observe = lambda: setattr(
            self.session, "candidate_endpoint", "172.20.0.5"
        )
        result = self.run_batch()
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["error"], "shared_backend_batch_endpoint_changed")
        self.assertEqual(self.probe_calls, 0)
        self.assertEqual(self.events.count("audit"), 2)

    def test_incomplete_probe_cannot_claim_a_completed_case(self):
        report = {
            "status": "complete",
            "turns": [],
            "error": None,
            "session_reset": True,
        }
        with self.assertRaisesRegex(shared.SharedBackendError, "batch_probe_invalid"):
            batch._observations(
                self.specification["cases"][0], report, self.session.context["bundle"]
            )


if __name__ == "__main__":
    unittest.main()
