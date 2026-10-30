"""Keep source uncertainty, conversation state and plan identity distinct."""

import copy
import json
from pathlib import Path
import unittest

from search.quality import contract, evaluator
from search.tests.fixtures import IDS, condition_document
from tests.unit.quality.fixtures import capture, non_search, plan_for, turn


class SearchQualityTests(unittest.TestCase):
    def check(self, value):
        return evaluator.evaluate(capture(value))["cases"][0]["turns"][0]

    def test_followup_changes_year_without_rewriting_declared_coverage(self):
        source = condition_document("2020", "2025", formats=["CSV", "PDF"])
        source.update(metadata_created="2010", metadata_modified="2026")
        result = evaluator.evaluate(
            capture(
                turn({"start": "2020", "end": "2025"}, source=source),
                turn(
                    {"start": "2024", "end": "2024"},
                    version=2,
                    source=source,
                    retain_topic=True,
                ),
                turn(
                    {"start": "2024", "end": "2024"},
                    version=3,
                    source=source,
                    retain_topic=True,
                    formats=["CSV"],
                ),
            )
        )
        self.assertEqual(result["status"], "passed")
        last = result["cases"][0]["turns"][-1]
        self.assertEqual(last["cards"][0]["temporal_coverage"]["start"], "2020")
        self.assertEqual(last["revision"], "a" * 40)
        self.assertEqual(last["search_revision"], "a" * 40)
        self.assertEqual(last["catalog_manifest_sha256"], "b" * 64)
        self.assertEqual(result["topical_relevance"], "not_evaluated")
        self.assertIn("rrf_candidate_order_and_scores", result["unavailable_trace"])

    def test_broken_refinement_is_separate_from_correct_source_metadata(self):
        first = turn({"start": "2020", "end": "2025"})
        second = turn({"start": "2024", "end": "2024"}, version=2, retain_topic=True)
        second["response"]["result"]["state"].update(
            topic="水質", time=first["expected"]["time"]
        )
        result = evaluator.evaluate(capture(first, second))
        issues = result["cases"][0]["turns"][1]["issues"]
        self.assertIn("topic_not_retained", issues)
        self.assertIn("state_conditions_mismatch", issues)
        self.assertNotIn("coverage_source_mismatch", issues)

    def test_known_outside_cannot_be_hidden_behind_a_match_label(self):
        value = turn(
            {"start": "2024", "end": "2024"},
            source=condition_document("2020", "2020", formats=["CSV"]),
        )
        search = value["response"]["result"]["search"]
        search["cards"][0]["condition_status"] = "match"
        search["condition_counts"].update(outside=0, match=1)
        issues = self.check(value)["issues"]
        self.assertIn("known_outside_result", issues)
        self.assertIn("condition_label_mismatch", issues)

    def test_missing_partial_and_unreadable_dates_remain_visible_unknowns(self):
        for start, end in ((None, None), ("2020", None), ("invalid date", "2025")):
            with self.subTest(start=start, end=end):
                value = turn(
                    {"start": "2024", "end": "2024"},
                    source=condition_document(start, end, formats=["CSV"]),
                )
                result = self.check(value)
                self.assertEqual(result["issues"], [])
                self.assertEqual(
                    result["information_availability"]["unknown_condition_fraction"], 1
                )

    def test_dates_and_formats_must_come_from_same_source_document(self):
        value = turn({"start": "2024", "end": "2024"})
        card = value["response"]["result"]["search"]["cards"][0]
        card["temporal_coverage"].update(start="2024", end="2024")
        card["resource_formats"] = ["ZIP"]
        issues = self.check(value)["issues"]
        self.assertIn("coverage_source_mismatch", issues)
        self.assertIn("formats_source_mismatch", issues)

    def test_candidate_counts_describe_pool_not_just_final_cards(self):
        value = turn({"start": "2024", "end": "2024"})
        search = value["response"]["result"]["search"]
        search["candidate_count"] = 20
        search["condition_counts"].update(match=18, unknown=2)
        search["warnings"] = ["unknown_condition_results_retained"]
        result = self.check(value)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["information_availability"]["returned_count"], 1)
        self.assertEqual(
            result["information_availability"]["unknown_condition_fraction"], 0
        )

    def test_candidate_counts_cannot_underreport_source_verified_returned_statuses(
        self,
    ):
        for source, counts in (
            (
                condition_document(None, None, formats=["CSV"]),
                {"match": 20, "unknown": 0},
            ),
            (
                condition_document("2020", "2025", formats=["CSV"]),
                {"match": 0, "unknown": 20},
            ),
        ):
            with self.subTest(counts=counts):
                value = turn({"start": "2024", "end": "2024"}, source=source)
                search = value["response"]["result"]["search"]
                search["candidate_count"] = 20
                search["condition_counts"].update(counts)
                search["warnings"] = []
                result = self.check(value)
                self.assertIn(
                    "candidate_condition_counts_underreported", result["issues"]
                )
                self.assertIn("unknown_warning_missing", result["issues"])

    def test_zero_candidates_can_be_empty_but_positive_candidates_cannot(self):
        value = turn({"start": "2024", "end": "2024"})
        search = value["response"]["result"]["search"]
        search.update(
            cards=[],
            candidate_count=0,
            rerank_status="not_run",
            ranking="opensearch-haystack-rrf",
        )
        search["condition_counts"]["match"] = 0
        self.assertEqual(self.check(value)["issues"], [])
        search["candidate_count"] = 1
        search["condition_counts"]["match"] = 1
        self.assertIn("candidate_count_invalid", self.check(value)["issues"])

    def test_reply_identity_fields_are_required_valid_and_consistent(self):
        for owner, field, replacement, issue in (
            ("result", "revision", None, "revision_invalid"),
            ("search", "revision", "z" * 40, "search_revision_invalid"),
            ("search", "revision", "c" * 40, "revision_mismatch"),
            (
                "search",
                "catalog_manifest_sha256",
                "b" * 63,
                "catalog_manifest_sha256_invalid",
            ),
        ):
            with self.subTest(field=field, replacement=replacement):
                value = turn({"start": "2024", "end": "2024"})
                result = value["response"]["result"]
                target = result if owner == "result" else result["search"]
                if replacement is None:
                    target.pop(field)
                else:
                    target[field] = replacement
                self.assertIn(issue, self.check(value)["issues"])

    def test_one_case_cannot_mix_api_revisions_or_catalog_manifests(self):
        for change in ("revision", "catalog_manifest_sha256"):
            with self.subTest(change=change):
                first = turn({"start": "2020", "end": "2025"})
                second = turn(
                    {"start": "2024", "end": "2024"}, version=2, retain_topic=True
                )
                result = second["response"]["result"]
                if change == "revision":
                    result["revision"] = result["search"]["revision"] = "c" * 40
                else:
                    result["search"]["catalog_manifest_sha256"] = "d" * 64
                report_value = evaluator.evaluate(capture(first, second))
                self.assertIn(
                    "case_identity_changed",
                    report_value["cases"][0]["turns"][1]["issues"],
                )
        independent = capture(turn(None))
        another = capture(turn(None))["cases"][0]
        another["id"] = "another-snapshot"
        another["turns"][0]["response"]["result"]["search"][
            "catalog_manifest_sha256"
        ] = "c" * 64
        independent["cases"].append(another)
        self.assertEqual(evaluator.evaluate(independent)["status"], "passed")

    def test_unconditioned_search_and_equivalent_date_precision(self):
        self.assertEqual(self.check(turn(None))["issues"], [])
        value = turn({"start": "2024", "end": "2024"})
        value["response"]["result"]["state"]["time"] = {
            "start": "2024-01-01",
            "end": "2024-12-31",
        }
        self.assertEqual(self.check(value)["issues"], [])

    def test_missing_fields_duplicate_ids_and_rank_errors_fail(self):
        value = turn({"start": "2024", "end": "2024"})
        search = value["response"]["result"]["search"]
        search.pop("parsed_conditions")
        search["cards"].append(copy.deepcopy(search["cards"][0]))
        search.update(
            candidate_count=2,
            condition_counts={"match": 2, "unknown": 0, "outside": 0, "not_checked": 0},
        )
        issues = self.check(value)["issues"]
        for issue in (
            "parsed_conditions_invalid",
            "duplicate_dataset_id",
            "card_rank_invalid",
        ):
            self.assertIn(issue, issues)

    def test_malformed_source_format_container_is_not_valid_metadata(self):
        value = turn({"start": "2024", "end": "2024"})
        value["source_metadata"][IDS[0]]["resource_formats"] = "CSV"
        self.assertIn("source_metadata_invalid", self.check(value)["issues"])

    def test_capture_bounds_and_first_turn_retention_are_not_silent_passes(self):
        for value in (
            {"schema_version": True, "cases": []},
            capture(turn(None, retain_topic=True)),
            {"schema_version": 1, "cases": []},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                evaluator.evaluate(value)


class QualityPlanTests(unittest.TestCase):
    def test_success_with_unapplied_lunar_time_requires_an_explicit_oracle(self):
        for time in (None, {"start": "2024", "end": "2024"}):
            with self.subTest(civil_time=time):
                value = turn(time)
                value["response"]["result"]["state"]["unapplied_time"] = {
                    "calendar": "lunar",
                    "text": "農曆112年",
                }
                result = evaluator.evaluate(capture(value))
                self.assertEqual("failed", result["status"])
                issues = result["cases"][0]["turns"][0]["issues"]
                self.assertIn("unapplied_time_not_evaluated", issues)
                self.assertNotIn("state_conditions_mismatch", issues)

    def test_outer_ok_cannot_hide_nested_unavailable_search(self):
        value = turn(None)
        value["response"]["result"]["search"]["status"] = "unavailable"
        result = evaluator.evaluate(capture(value))
        self.assertEqual("failed", result["status"])
        self.assertIn("search_not_ok", result["cases"][0]["turns"][0]["issues"])

    def test_ok_default_reason_none_is_checked_without_explicit_expectation(self):
        value = turn(None)
        self.assertNotIn("reason", value["expected"])
        value["response"]["result"]["reason"] = "unsafe"
        result = evaluator.evaluate(capture(value))
        self.assertEqual("failed", result["status"])
        self.assertIn("reason_mismatch", result["cases"][0]["turns"][0]["issues"])

    def test_accepted_clarification_and_rejection_consume_versions_without_committing_state(
        self,
    ):
        first = turn({"start": "2020", "end": "2025"}, formats=["CSV"])
        state = first["response"]["result"]["state"]
        value = capture(
            first,
            non_search(
                "clarification_required",
                "ambiguous_time",
                version=2,
                state=state,
                retain_topic=True,
            ),
            non_search("rejected", "unsafe", version=3, state=state, retain_topic=True),
            turn(
                {"start": "2024", "end": "2024"},
                version=4,
                formats=["CSV"],
                retain_topic=True,
            ),
        )
        result = evaluator.evaluate(value, plan_for(value))
        self.assertEqual("passed", result["status"])
        self.assertEqual(
            ["ok", "clarification_required", "rejected", "ok"],
            [row["status"] for row in result["cases"][0]["turns"]],
        )

    def test_first_non_search_can_keep_empty_topic_before_first_success(self):
        for status, reason in (
            ("clarification_required", "missing_topic"),
            ("rejected", "off_topic"),
        ):
            with self.subTest(status=status):
                value = capture(non_search(status, reason), turn(None, version=2))
                result = evaluator.evaluate(value, plan_for(value))
                self.assertEqual("passed", result["status"])
                self.assertEqual([], result["cases"][0]["turns"][0]["cards"])
                self.assertNotIn(
                    "catalog_manifest_sha256", result["cases"][0]["turns"][0]
                )

    def test_non_search_state_pollution_is_reported_without_poisoning_next_refinement(
        self,
    ):
        for status, reason in (
            ("clarification_required", "ambiguous_time"),
            ("rejected", "unsafe"),
        ):
            for field, wrong in (
                ("topic", "另一個主題"),
                ("time", {"start": "2022", "end": "2022"}),
                ("formats", ["PDF"]),
                ("unapplied_time", {"calendar": "lunar", "text": "農曆112年"}),
            ):
                with self.subTest(status=status, field=field):
                    first = turn({"start": "2020", "end": "2025"}, formats=["CSV"])
                    second = non_search(
                        status,
                        reason,
                        version=2,
                        state=first["response"]["result"]["state"],
                        retain_topic=True,
                    )
                    third = turn(
                        {"start": "2024", "end": "2024"},
                        version=3,
                        formats=["CSV"],
                        retain_topic=True,
                    )
                    value = capture(first, second, third)
                    plan = plan_for(value)
                    second["response"]["result"]["state"][field] = wrong
                    result = evaluator.evaluate(value, plan)
                    reports = result["cases"][0]["turns"]
                    self.assertIn("non_search_state_changed", reports[1]["issues"])
                    self.assertEqual([], reports[2]["issues"])

    def test_non_search_cannot_smuggle_search_or_source_metadata(self):
        for field in ("search", "source_metadata"):
            with self.subTest(field=field):
                value = non_search("rejected", "unsafe")
                positive = turn(None)
                if field == "search":
                    value["response"]["result"]["search"] = positive["response"][
                        "result"
                    ]["search"]
                else:
                    value[field] = positive[field]
                result = evaluator.evaluate(capture(value))
                self.assertIn(
                    "unexpected_" + field, result["cases"][0]["turns"][0]["issues"]
                )

    def test_non_search_still_checks_reason_version_and_revision(self):
        mutations = (
            ("reason", "off_topic", "reason_mismatch"),
            ("revision", "z" * 40, "revision_invalid"),
            ("version", True, "session_version_mismatch"),
            ("version", 1.0, "session_version_mismatch"),
            ("version", 0, "session_version_mismatch"),
        )
        for field, wrong, issue in mutations:
            with self.subTest(field=field, wrong=wrong):
                value = non_search("rejected", "unsafe")
                target = (
                    value["response"]
                    if field == "version"
                    else value["response"]["result"]
                )
                target[field] = wrong
                result = evaluator.evaluate(capture(value))
                self.assertIn(issue, result["cases"][0]["turns"][0]["issues"])

    def test_non_search_revision_change_is_detected_between_successful_turns(self):
        first = turn(None)
        second = non_search(
            "rejected",
            "unsafe",
            version=2,
            state=first["response"]["result"]["state"],
            retain_topic=True,
        )
        second["response"]["result"]["revision"] = "c" * 40
        third = turn(None, version=3, retain_topic=True)
        result = evaluator.evaluate(capture(first, second, third))
        self.assertIn("case_identity_changed", result["cases"][0]["turns"][1]["issues"])
        self.assertEqual([], result["cases"][0]["turns"][2]["issues"])

    def test_empty_capture_retains_every_planned_case_and_turn_in_denominator(self):
        planned = capture(turn(None), turn(None, version=2, retain_topic=True))
        another = capture(turn(None))["cases"][0]
        another["id"] = "separate-session"
        planned["cases"].append(another)
        plan = plan_for(planned)
        result = evaluator.evaluate({"schema_version": 1, "cases": []}, plan)
        self.assertEqual("failed", result["status"])
        self.assertEqual(
            ["climate", "separate-session"], [row["id"] for row in result["cases"]]
        )
        reports = [row for case in result["cases"] for row in case["turns"]]
        self.assertEqual(3, len(reports))
        self.assertTrue(all(row["issues"] == ["turn_not_captured"] for row in reports))
        self.assertEqual(contract.plan_digest(plan), result["plan_sha256"])

    def test_missing_last_turn_is_a_failed_planned_turn_not_a_smaller_success(self):
        value = capture(turn(None), turn(None, version=2, retain_topic=True))
        plan = plan_for(value)
        value["cases"][0]["turns"].pop()
        result = evaluator.evaluate(value, plan)
        self.assertEqual("failed", result["status"])
        self.assertEqual([], result["cases"][0]["turns"][0]["issues"])
        self.assertEqual(
            ["turn_not_captured"], result["cases"][0]["turns"][1]["issues"]
        )

    def test_empty_captured_case_requires_a_plan_and_retains_its_turns(self):
        value = capture(turn(None))
        plan = plan_for(value)
        value["cases"][0]["turns"].clear()
        with self.assertRaises(ValueError):
            evaluator.evaluate(value)
        result = evaluator.evaluate(value, plan)
        self.assertEqual("failed", result["status"])
        self.assertEqual(
            ["turn_not_captured"], result["cases"][0]["turns"][0]["issues"]
        )

    def test_plan_rejects_changed_queries_expectations_unplanned_cases_and_turns(self):
        baseline = capture(turn({"start": "2024", "end": "2024"}))
        plan = plan_for(baseline)
        for mutation in ("query", "expected", "case", "turn"):
            with self.subTest(mutation=mutation):
                value = copy.deepcopy(baseline)
                if mutation == "query":
                    value["cases"][0]["turns"][0]["query"] += "。"
                elif mutation == "expected":
                    value["cases"][0]["turns"][0]["expected"]["time"]["start"] = (
                        "2024-01-01"
                    )
                elif mutation == "case":
                    case = copy.deepcopy(value["cases"][0])
                    case["id"] = "unplanned-session"
                    value["cases"].append(case)
                else:
                    value["cases"][0]["turns"].append(turn(None, version=2))
                with self.assertRaises(ValueError):
                    evaluator.evaluate(value, plan)

    def test_plan_digest_ignores_json_layout_but_binds_query_and_turn_order(self):
        plan = plan_for(capture(turn(None), turn(None, version=2)))
        before = copy.deepcopy(plan)
        reordered = json.loads(
            json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=4)
        )
        self.assertEqual(contract.plan_digest(plan), contract.plan_digest(reordered))
        changed = copy.deepcopy(plan)
        changed["cases"][0]["turns"][0]["query"] += "，研究用"
        self.assertNotEqual(contract.plan_digest(plan), contract.plan_digest(changed))
        reordered["cases"][0]["turns"].reverse()
        self.assertNotEqual(contract.plan_digest(plan), contract.plan_digest(reordered))
        self.assertEqual(before, plan)

    def test_topic_changed_requires_a_new_topic_and_rejects_conflicting_expectations(
        self,
    ):
        first = turn(None)
        second = turn(None, version=2)
        second["query"] = "改找河川資料"
        second["expected"]["topic_changed"] = True
        value = capture(first, second)
        result = evaluator.evaluate(value, plan_for(value))
        self.assertIn("topic_not_changed", result["cases"][0]["turns"][1]["issues"])
        second["response"]["result"]["state"]["topic"] = "河川"
        self.assertEqual("passed", evaluator.evaluate(value, plan_for(value))["status"])
        for conflict in ("first_turn", "retain_topic", "non_search"):
            with self.subTest(conflict=conflict):
                if conflict == "first_turn":
                    invalid = plan_for(capture(turn(None)))
                    invalid["cases"][0]["turns"][0]["expected"]["topic_changed"] = True
                else:
                    invalid = plan_for(copy.deepcopy(value))
                    expected = invalid["cases"][0]["turns"][1]["expected"]
                    if conflict == "retain_topic":
                        expected["retain_topic"] = True
                    else:
                        expected.update(status="rejected", reason="unsafe")
                with self.assertRaises(ValueError):
                    contract.validate_cases(invalid)

    def test_invalid_outcome_expectations_and_boolean_flags_are_rejected(self):
        for fields in (
            {"status": "unavailable"},
            {"status": True},
            {"reason": "unsafe"},
            {"status": "clarification_required"},
            {"status": "clarification_required", "reason": "unsafe"},
            {"status": "rejected", "reason": "ambiguous_time"},
            {"status": "rejected", "reason": 1},
            {"retain_topic": 1},
            {"topic_changed": 0},
            {"unexpected": "field"},
        ):
            with self.subTest(fields=fields):
                plan = plan_for(capture(turn(None)))
                plan["cases"][0]["turns"][0]["expected"].update(fields)
                with self.assertRaises(ValueError):
                    contract.validate_cases(plan)

    def test_production_query_validation_accepts_punctuation_and_preserves_raw_binding(
        self,
    ):
        value = capture(turn(None))
        raw_query = "  找  氣候資料，  類型不限  "
        value["cases"][0]["turns"][0]["query"] = raw_query
        plan = plan_for(value)
        self.assertEqual(plan, contract.validate_cases(plan))
        self.assertEqual(raw_query, plan["cases"][0]["turns"][0]["query"])
        self.assertEqual("passed", evaluator.evaluate(value, plan)["status"])
        value["cases"][0]["turns"][0]["query"] = "找 氣候資料, 類型不限"
        with self.assertRaises(ValueError):
            evaluator.evaluate(value, plan)
        for invisible in ("\u200b", "\u202e", "\x00", "\ud800"):
            with self.subTest(invisible=repr(invisible)):
                invalid = copy.deepcopy(plan)
                invalid["cases"][0]["turns"][0]["query"] = "找" + invisible + "氣候資料"
                with self.assertRaises(ValueError):
                    contract.validate_cases(invalid)

    def test_canonical_case_plan_validates_with_all_supported_outcomes(self):
        path = Path(__file__).resolve().parents[2] / "fixtures/search-quality.json"
        plan = json.loads(path.read_text())
        self.assertEqual(plan, contract.validate_cases(plan))
        outcomes = {
            row["expected"].get("status", "ok")
            for case in plan["cases"]
            for row in case["turns"]
        }
        self.assertEqual({"ok", "clarification_required", "rejected"}, outcomes)
        self.assertRegex(contract.plan_digest(plan), r"^[0-9a-f]{64}$")
        self.assertEqual(
            "0e2438ada5c5f72a8dbb49bab1faa96cc4cb74ccd8b29303a8ffa8907a01435b",
            contract.plan_digest(plan),
        )


if __name__ == "__main__":
    unittest.main()
