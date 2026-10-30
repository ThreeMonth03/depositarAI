"""Bind multi-batch observations without dropping failures or mixing baselines."""

import copy
import hashlib
import json
import unittest

from search.quality import batches, contract
from tests.unit.quality.fixtures import capture, plan_for, turn


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def fixture_identity():
    return {
        "operator_revision": "c" * 40,
        "candidate_revision": "a" * 40,
        "production_revision": "d" * 40,
        "catalog_manifest_sha256": "b" * 64,
        "bundle_id": "1" * 64,
        "request_policy_sha256": "e" * 64,
        "model_manifest_sha256": "f" * 64,
        "resident_models": [
            {
                "role": role,
                "model": "fixture",
                "digest": "5" * 64,
                "num_ctx": 4096,
                "size_bytes": 1000,
                "vram_bytes": 1000,
            }
            for role in ("scope", "planner")
        ],
        "gpu_compute_sha256": "2" * 64,
        "gpu_image_ids": {"scope": "sha256:" + "4" * 64},
        "candidate_runtime": {"start_identity_sha256": "3" * 64},
    }


def rewrite(record, *, run=None, captured=None, outcomes=None):
    files = record["files"]
    receipt = json.loads(files["run.json"]) if run is None else run
    for key, value in (("capture", captured), ("outcomes", outcomes)):
        if value is not None:
            files[key + ".json"] = encoded(value)
        raw = files[key + ".json"]
        receipt["artifacts"][key] = {
            "name": key + ".json",
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    files["run.json"] = encoded(receipt)
    return record


def make_record(captured, status="complete", identity=None):
    plan = plan_for(captured)
    count = sum(len(case["turns"]) for case in plan["cases"])
    receipt = {
        "schema_version": 1,
        "operator_revision": "c" * 40,
        "status": status,
        "error": None if status == "complete" else "shared_batch_failed",
        "identity_verified": status != "quarantined",
        "identity": fixture_identity() if identity is None else identity,
        "plan_sha256": contract.plan_digest(plan),
        "requested_cases": len(plan["cases"]),
        "requested_turns": count,
        "completed_cases": len(plan["cases"]),
        "completed_turns": count,
        "audit_timing": {"before_seconds": 1.0, "after_seconds": 1.0},
        "operator_seconds": 2.0,
        "quality_evaluation": "not_run",
        "unavailable_trace": [
            "branch_candidates_and_scores",
            "rrf_candidate_order_and_scores",
            "reranker_scores_and_complete_order",
        ],
        "models_restarted": False,
        "public_route_changed": False,
        "artifacts": {},
    }
    outcomes = {
        "schema_version": 1,
        "cases": [
            {
                "id": case["id"],
                "status": "complete",
                "error": None,
                "session_reset": True,
                "turns": [
                    {
                        "turn": number,
                        "status": "ok",
                        "wall_seconds": 0.4,
                        "request_seconds": 0.2,
                    }
                    for number, _ in enumerate(case["turns"], 1)
                ],
            }
            for case in plan["cases"]
        ],
    }
    record = {"plan": plan, "files": {"run.json": b""}}
    return rewrite(record, run=receipt, captured=captured, outcomes=outcomes)


def full_fixture():
    records = []
    for name in ("climate-a", "climate-b"):
        value = capture(
            turn({"start": "2020", "end": "2025"}),
            turn({"start": "2024", "end": "2024"}, version=2, retain_topic=True),
        )
        value["cases"][0]["id"] = name
        records.append(make_record(value))
    return {
        "schema_version": 1,
        "cases": [copy.deepcopy(record["plan"]["cases"][0]) for record in records],
    }, records


class BatchAggregationTests(unittest.TestCase):
    def setUp(self):
        self.plan, self.records = full_fixture()

    def rejected(self, records=None, *, plan=None, code=None, baseline=None):
        with self.assertRaises(batches.BatchEvidenceError) as caught:
            batches.aggregate(
                self.plan if plan is None else plan,
                self.records if records is None else records,
                baseline=baseline,
            )
        self.assertRegex(str(caught.exception), r"^quality_batches_[a-z_]+$")
        if code:
            self.assertEqual("quality_batches_" + code, str(caught.exception))

    def test_complete_batches_share_the_existing_evaluator_and_keep_provenance(self):
        before = copy.deepcopy(self.records)
        report = batches.aggregate(self.plan, self.records)
        self.assertEqual("passed", report["status"])
        self.assertEqual(contract.plan_digest(self.plan), report["plan_sha256"])
        self.assertEqual(4, report["summary"]["turn_count"])
        self.assertEqual(4, report["summary"]["evidence_complete_turn_count"])
        self.assertEqual(0, report["summary"]["evidence_missing_turn_count"])
        self.assertEqual(0, report["summary"]["quality_failed_turn_count"])
        self.assertEqual("not_evaluated", report["topical_relevance"])
        self.assertEqual(3, len(report["unavailable_trace"]))
        self.assertEqual(fixture_identity(), report["evidence"]["baseline"])
        self.assertEqual(
            [1, 2], [case["batch"] for case in report["evidence"]["cases"]]
        )
        self.assertEqual(
            hashlib.sha256(self.records[0]["files"]["run.json"]).hexdigest(),
            report["evidence"]["batches"][0]["run_sha256"],
        )
        self.assertEqual(before, self.records)

    def test_missing_batches_retain_the_full_denominator_without_model_failures(self):
        for records, missing in (([], 4), (self.records[:1], 2)):
            report = batches.aggregate(self.plan, records)
            self.assertEqual("incomplete", report["status"])
            self.assertEqual(4, report["summary"]["turn_count"])
            self.assertEqual(missing, report["summary"]["evidence_missing_turn_count"])
            self.assertEqual(0, report["summary"]["quality_failed_turn_count"])
            self.assertEqual("missing", report["evidence"]["cases"][-1]["status"])
            self.assertEqual(
                ["turn_not_captured"], report["cases"][-1]["turns"][-1]["issues"]
            )

    def test_quality_assertion_failure_is_distinct_from_incomplete_evidence(self):
        record = self.records[0]
        captured = json.loads(record["files"]["capture.json"])
        captured["cases"][0]["turns"][1]["response"]["result"]["state"]["topic"] = (
            "different"
        )
        rewrite(record, captured=captured)
        report = batches.aggregate(self.plan, self.records)
        self.assertEqual("failed", report["status"])
        self.assertEqual("complete", report["evidence"]["status"])
        self.assertEqual(0, report["summary"]["evidence_missing_turn_count"])
        self.assertEqual(1, report["summary"]["quality_failed_turn_count"])

    def test_failed_partial_batch_supplies_no_cards_including_successful_prefix(self):
        record = self.records[0]
        run, captured, outcomes = [
            json.loads(record["files"][name])
            for name in ("run.json", "capture.json", "outcomes.json")
        ]
        run.update(
            status="failed",
            error="shared_batch_busy",
            completed_cases=0,
            completed_turns=1,
        )
        captured["cases"][0]["turns"].pop()
        case = outcomes["cases"][0]
        case.update(status="failed", error="shared_batch_busy")
        case["turns"][1].update(status="failed", error="shared_batch_busy")
        rewrite(record, run=run, captured=captured, outcomes=outcomes)
        report = batches.aggregate(self.plan, self.records)
        self.assertEqual("incomplete", report["status"])
        self.assertEqual(2, report["summary"]["captured_turn_count"])
        self.assertEqual(2, report["summary"]["evidence_missing_turn_count"])
        self.assertEqual([], report["cases"][0]["turns"][0]["cards"])
        self.assertEqual("failed", report["evidence"]["batches"][0]["status"])

    def test_quarantine_after_successful_queries_cannot_be_scored(self):
        run = json.loads(self.records[0]["files"]["run.json"])
        run.update(
            status="quarantined",
            error="shared_backend_batch_failed",
            identity_verified=False,
        )
        rewrite(self.records[0], run=run)
        report = batches.aggregate(self.plan, self.records)
        self.assertEqual("incomplete", report["status"])
        self.assertEqual(2, report["summary"]["evidence_missing_turn_count"])
        self.assertEqual([], report["cases"][0]["turns"][0]["cards"])

    def test_initial_audit_failure_can_have_no_identity_or_observations(self):
        record = self.records[0]
        run = json.loads(record["files"]["run.json"])
        run.update(
            status="quarantined",
            error="shared_backend_batch_failed",
            identity_verified=False,
            identity=None,
            completed_cases=0,
            completed_turns=0,
        )
        case = record["plan"]["cases"][0]
        outcomes = {
            "schema_version": 1,
            "cases": [
                {
                    "id": case["id"],
                    "status": "not_run",
                    "error": None,
                    "session_reset": None,
                    "turns": [{"turn": n, "status": "not_run"} for n in range(1, 3)],
                }
            ],
        }
        rewrite(
            record,
            run=run,
            captured={"schema_version": 1, "cases": []},
            outcomes=outcomes,
        )
        report = batches.aggregate(self.plan, [record])
        self.assertIsNone(report["evidence"]["baseline"])
        self.assertEqual(4, report["summary"]["evidence_missing_turn_count"])

    def test_reset_failure_with_all_queries_captured_is_still_ineligible(self):
        record = self.records[0]
        run = json.loads(record["files"]["run.json"])
        outcomes = json.loads(record["files"]["outcomes.json"])
        run.update(
            status="failed", error="shared_batch_reset_failed", completed_cases=0
        )
        outcomes["cases"][0].update(
            status="failed", error="shared_batch_reset_failed", session_reset=False
        )
        rewrite(record, run=run, outcomes=outcomes)
        report = batches.aggregate(self.plan, self.records)
        self.assertEqual(2, report["summary"]["evidence_complete_turn_count"])

    def test_failed_middle_turn_and_unrun_tail_cases_remain_in_the_plan(self):
        captured = {
            "schema_version": 1,
            "cases": [
                json.loads(record["files"]["capture.json"])["cases"][0]
                for record in self.records
            ],
        }
        captured["cases"][0]["turns"].append(turn(None, version=3, retain_topic=True))
        record = make_record(captured)
        full_plan = copy.deepcopy(record["plan"])
        run = json.loads(record["files"]["run.json"])
        outcomes = json.loads(record["files"]["outcomes.json"])
        run.update(
            status="failed",
            error="shared_batch_busy",
            completed_cases=0,
            completed_turns=1,
        )
        first, later = outcomes["cases"]
        first.update(status="failed", error="shared_batch_busy")
        first["turns"][1].update(status="failed", error="shared_batch_busy")
        first["turns"][2] = {"turn": 3, "status": "not_run"}
        later.update(
            status="not_run",
            error=None,
            session_reset=None,
            turns=[{"turn": n, "status": "not_run"} for n in (1, 2)],
        )
        captured["cases"] = captured["cases"][:1]
        captured["cases"][0]["turns"] = captured["cases"][0]["turns"][:1]
        rewrite(record, run=run, captured=captured, outcomes=outcomes)
        report = batches.aggregate(full_plan, [record])
        self.assertEqual("incomplete", report["status"])
        self.assertEqual(5, report["summary"]["turn_count"])
        self.assertEqual(5, report["summary"]["evidence_missing_turn_count"])
        for case in report["cases"]:
            for row in case["turns"]:
                self.assertEqual(["turn_not_captured"], row["issues"])
                self.assertEqual([], row["cards"])
        # A forged later success must not turn a stopped run into usable evidence.
        later["status"] = "complete"
        rewrite(record, run=run, outcomes=outcomes)
        self.rejected([record], plan=full_plan, code="outcomes_invalid")

    def test_mixed_source_policy_models_runtime_or_future_fields_are_rejected(self):
        for key, change in (
            ("production_revision", "6" * 40),
            ("bundle_id", "6" * 64),
            ("request_policy_sha256", "6" * 64),
            ("model_manifest_sha256", "6" * 64),
            ("candidate_runtime", {"start_identity_sha256": "6" * 64}),
            ("resident_models", [{"role": "scope", "digest": "6" * 64}]),
            ("selected_catalog", {"manifest_sha256": "6" * 64}),
        ):
            with self.subTest(key=key):
                records = copy.deepcopy(self.records)
                run = json.loads(records[1]["files"]["run.json"])
                run["identity"][key] = change
                rewrite(records[1], run=run)
                self.rejected(records, code="mixed_identity")

    def test_supplied_baseline_uses_all_fields_and_validates_hash_shapes(self):
        self.assertEqual(
            "passed",
            batches.aggregate(self.plan, self.records, baseline=fixture_identity())[
                "status"
            ],
        )
        expected = fixture_identity()
        expected["selected_catalog"] = {"manifest_sha256": "7" * 64}
        self.rejected(baseline=expected, code="mixed_identity")
        for key in (
            "candidate_revision",
            "catalog_manifest_sha256",
            "request_policy_sha256",
        ):
            expected = fixture_identity()
            expected[key] = "not-a-hash"
            self.rejected(baseline=expected, code="identity_invalid")

    def test_nested_identity_hashes_types_and_sizes_are_bounded(self):
        for key, value in (
            (
                "resident_models",
                [{"role": "scope", "digest": "PRIVATE_INVALID_DIGEST"}],
            ),
            ("candidate_runtime", {"start_identity_sha256": "PRIVATE_INVALID_SHA"}),
            ("additional", {1: "integer key"}),
            ("additional", "x" * 4097),
            ("additional", {"value": float("nan")}),
            ("additional", tuple()),
        ):
            with self.subTest(key=key, value_type=type(value).__name__):
                expected = fixture_identity()
                expected[key] = value
                self.rejected(baseline=expected, code="identity_invalid")

    def test_unverified_batches_still_cannot_mix_known_identity_bindings(self):
        for index, record in enumerate(self.records):
            run = json.loads(record["files"]["run.json"])
            run.update(
                status="quarantined",
                error="shared_backend_batch_failed",
                identity_verified=False,
            )
            if index:
                run["identity"]["model_manifest_sha256"] = "7" * 64
            rewrite(record, run=run)
        self.rejected(code="mixed_identity")

    def test_response_revisions_and_catalog_must_match_receipt(self):
        for target, key in (
            ("result", "revision"),
            ("search", "revision"),
            ("search", "catalog_manifest_sha256"),
        ):
            records = copy.deepcopy(self.records)
            captured = json.loads(records[0]["files"]["capture.json"])
            result = captured["cases"][0]["turns"][0]["response"]["result"]
            (result if target == "result" else result["search"])[key] = "7" * (
                40 if key == "revision" else 64
            )
            rewrite(records[0], captured=captured)
            self.rejected(records, code="response_identity_mismatch")

    def test_missing_or_tampered_files_cannot_produce_a_report(self):
        for name in batches.NAMES:
            for mutation in ("missing", "changed"):
                records = copy.deepcopy(self.records)
                if mutation == "missing":
                    del records[0]["files"][name]
                else:
                    records[0]["files"][name] += b" "
                    if name == "run.json":
                        records[0]["files"][name] = (
                            b'{"schema_version":1,"schema_version":1}'
                        )
                self.rejected(records)

    def test_case_partition_disallows_duplicates_splits_or_changed_expectations(self):
        self.rejected(self.records + [self.records[0]], code="duplicate_case")
        for mutation in ("split", "query", "expected", "id"):
            records = copy.deepcopy(self.records)
            case = records[0]["plan"]["cases"][0]
            if mutation == "split":
                case["turns"].pop()
            elif mutation == "query":
                case["turns"][0]["query"] = "PRIVATE_CHANGED_QUERY"
            elif mutation == "expected":
                case["turns"][0]["expected"]["formats"] = ["CSV"]
            else:
                case["id"] = "unplanned"
            run = json.loads(records[0]["files"]["run.json"])
            run["plan_sha256"] = contract.plan_digest(records[0]["plan"])
            rewrite(records[0], run=run)
            self.rejected(records)

    def test_capture_cannot_rewrite_raw_query_or_expectation(self):
        records = copy.deepcopy(self.records)
        value = json.loads(records[0]["files"]["capture.json"])
        value["cases"][0]["turns"][0]["query"] = "PRIVATE_CHANGED_QUERY"
        rewrite(records[0], captured=value)
        self.rejected(records)

    def test_inconsistent_receipts_and_outcomes_are_rejected(self):
        for mutation in (
            "count",
            "bool_count",
            "verified",
            "operator",
            "hash",
            "reset",
            "turn_order",
            "capture_missing",
        ):
            records = copy.deepcopy(self.records)
            run, captured, outcomes = [
                json.loads(records[0]["files"][name])
                for name in ("run.json", "capture.json", "outcomes.json")
            ]
            if mutation == "count":
                run["completed_turns"] = 1
            elif mutation == "bool_count":
                run["requested_cases"] = True
            elif mutation == "verified":
                run["identity_verified"] = False
            elif mutation == "operator":
                run["operator_revision"] = "e" * 40
            elif mutation == "hash":
                run["plan_sha256"] = "0" * 64
            elif mutation == "reset":
                outcomes["cases"][0]["session_reset"] = False
            elif mutation == "turn_order":
                outcomes["cases"][0]["turns"].reverse()
            else:
                captured["cases"][0]["turns"].pop()
            rewrite(records[0], run=run, captured=captured, outcomes=outcomes)
            self.rejected(records)

    def test_private_input_values_never_appear_in_domain_diagnostics(self):
        for raw in (
            b'{"secret":"PRIVATE_TOKEN"}',
            b'{"schema_version":NaN}',
            b"\xff",
            b'{"schema_version":1,"schema_version":1}',
            b"x" * (batches.MAX_RECEIPT_BYTES + 1),
        ):
            records = copy.deepcopy(self.records)
            records[0]["files"]["run.json"] = raw
            self.rejected(records)


if __name__ == "__main__":
    unittest.main()
