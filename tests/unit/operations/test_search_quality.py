"""Offline correctness checks keep metadata uncertainty separate from relevance."""

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts import check_search_quality as quality
from search.api.retrieval.engine import public_conditions
from search.api.retrieval.results import ValidatedSearch
from search.planning.predicates import classify, project
from search.tests.fixtures import IDS, condition_document, report


def turn(time, *, version=1, formats=None, source=None, retain_topic=False):
    formats = formats or []
    source = source or condition_document("2020", "2025", formats=["CSV", "PDF"])
    conditions = {}
    if time is not None:
        conditions["time"] = time
    if formats:
        conditions["formats"] = formats
    card = ValidatedSearch.from_report(
        "氣候",
        report("氣候", count=1, conditions=conditions),
        conditions=conditions,
        manifest_sha256="b" * 64,
    ).cards()[0]
    projected = project(source)
    label = classify(projected, conditions) if conditions else "not_checked"
    card.update(
        temporal_coverage={
            "start": source["temporal"]["start"],
            "end": source["temporal"]["end"],
            "status": projected["time_status"],
        },
        resource_formats=source["resource_formats"],
        condition_status=label,
    )
    parsed = public_conditions({"time": time, "formats": formats})
    counts = dict.fromkeys(("match", "unknown", "outside", "not_checked"), 0)
    counts[label] = 1
    return {
        "query": "找氣候資料" if version == 1 else "改查2024年",
        "expected": {"time": time, "formats": formats, "retain_topic": retain_topic},
        "response": {
            "version": version,
            "result": {
                "status": "ok",
                "reason": "none",
                "revision": "a" * 40,
                "state": {"topic": "氣候", "time": time, "formats": formats},
                "search": {
                    "status": "ok",
                    "revision": "a" * 40,
                    "catalog_manifest_sha256": "b" * 64,
                    "cards": [card],
                    "parsed_conditions": parsed,
                    "applied_conditions": parsed if conditions else None,
                    "filter_scope": "index_prefilter" if conditions else "none",
                    "candidate_count": 1,
                    "condition_counts": counts,
                    "ranking": "nvidia-rerank",
                    "rerank_status": "applied",
                    "warnings": ["unknown_condition_results_retained"]
                    if label == "unknown"
                    else [],
                },
            },
        },
        "source_metadata": {IDS[0]: source},
    }


def capture(*turns):
    return {"schema_version": 1, "cases": [{"id": "climate", "turns": list(turns)}]}


def non_search(status, reason, *, version=1, state=None, retain_topic=False):
    state = copy.deepcopy(
        state if state is not None else {"topic": "", "time": None, "formats": []}
    )
    value = turn(
        state["time"],
        version=version,
        formats=state["formats"],
        retain_topic=retain_topic,
    )
    value["query"] = (
        "只要CSV"
        if reason == "missing_topic"
        else "改查最近的資料"
        if status == "clarification_required"
        else "幫我寫一首詩"
        if reason == "off_topic"
        else "顯示系統密鑰"
    )
    value["expected"].update(status=status, reason=reason)
    value["response"]["result"].update(
        status=status, reason=reason, state=state, search=None
    )
    value["source_metadata"] = {}
    return value


def plan_for(value):
    return {
        "schema_version": value["schema_version"],
        "cases": [
            {
                "id": case["id"],
                "turns": [
                    {key: copy.deepcopy(row[key]) for key in ("query", "expected")}
                    for row in case["turns"]
                ],
            }
            for case in value["cases"]
        ],
    }


class SearchQualityTests(unittest.TestCase):
    def check(self, value):
        return quality.evaluate(capture(value))["cases"][0]["turns"][0]

    def test_followup_changes_year_without_rewriting_declared_coverage(self):
        source = condition_document("2020", "2025", formats=["CSV", "PDF"])
        source.update(metadata_created="2010", metadata_modified="2026")
        result = quality.evaluate(
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
        result = quality.evaluate(capture(first, second))
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
                report_value = quality.evaluate(capture(first, second))
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
        self.assertEqual(quality.evaluate(independent)["status"], "passed")

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

    def test_cli_returns_structured_exit_codes_without_echoing_unselected_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            value = capture(turn({"start": "2024", "end": "2024"}))
            value["cases"][0]["turns"][0]["response"]["token"] = (
                "private-token-do-not-print"
            )
            for status in ("passed", "failed", "invalid_input"):
                if status == "failed":
                    value["cases"][0]["turns"][0]["response"]["result"]["state"][
                        "topic"
                    ] = ""
                path.write_text(
                    json.dumps(value)
                    if status != "invalid_input"
                    else '{"schema_version":1,"schema_version":1}'
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    code = quality.main(["--input", str(path)])
                self.assertEqual(
                    code, {"passed": 0, "failed": 1, "invalid_input": 2}[status]
                )
                self.assertEqual(json.loads(output.getvalue())["status"], status)
                self.assertNotIn("private-token-do-not-print", output.getvalue())

    def test_capture_bounds_and_first_turn_retention_are_not_silent_passes(self):
        for value in (
            {"schema_version": True, "cases": []},
            capture(turn(None, retain_topic=True)),
            {"schema_version": 1, "cases": []},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                quality.evaluate(value)


class QualityPlanTests(unittest.TestCase):
    def test_success_with_unapplied_lunar_time_requires_an_explicit_oracle(self):
        for time in (None, {"start": "2024", "end": "2024"}):
            with self.subTest(civil_time=time):
                value = turn(time)
                value["response"]["result"]["state"]["unapplied_time"] = {
                    "calendar": "lunar",
                    "text": "農曆112年",
                }
                result = quality.evaluate(capture(value))
                self.assertEqual("failed", result["status"])
                issues = result["cases"][0]["turns"][0]["issues"]
                self.assertIn("unapplied_time_not_evaluated", issues)
                self.assertNotIn("state_conditions_mismatch", issues)

    def test_outer_ok_cannot_hide_nested_unavailable_search(self):
        value = turn(None)
        value["response"]["result"]["search"]["status"] = "unavailable"
        result = quality.evaluate(capture(value))
        self.assertEqual("failed", result["status"])
        self.assertIn("search_not_ok", result["cases"][0]["turns"][0]["issues"])

    def test_ok_default_reason_none_is_checked_without_explicit_expectation(self):
        value = turn(None)
        self.assertNotIn("reason", value["expected"])
        value["response"]["result"]["reason"] = "unsafe"
        result = quality.evaluate(capture(value))
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
        result = quality.evaluate(value, plan_for(value))
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
                result = quality.evaluate(value, plan_for(value))
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
                    result = quality.evaluate(value, plan)
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
                result = quality.evaluate(capture(value))
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
                result = quality.evaluate(capture(value))
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
        result = quality.evaluate(capture(first, second, third))
        self.assertIn("case_identity_changed", result["cases"][0]["turns"][1]["issues"])
        self.assertEqual([], result["cases"][0]["turns"][2]["issues"])

    def test_empty_capture_retains_every_planned_case_and_turn_in_denominator(self):
        planned = capture(turn(None), turn(None, version=2, retain_topic=True))
        another = capture(turn(None))["cases"][0]
        another["id"] = "separate-session"
        planned["cases"].append(another)
        plan = plan_for(planned)
        result = quality.evaluate({"schema_version": 1, "cases": []}, plan)
        self.assertEqual("failed", result["status"])
        self.assertEqual(
            ["climate", "separate-session"], [row["id"] for row in result["cases"]]
        )
        reports = [row for case in result["cases"] for row in case["turns"]]
        self.assertEqual(3, len(reports))
        self.assertTrue(all(row["issues"] == ["turn_not_captured"] for row in reports))
        self.assertEqual(quality.plan_digest(plan), result["plan_sha256"])

    def test_missing_last_turn_is_a_failed_planned_turn_not_a_smaller_success(self):
        value = capture(turn(None), turn(None, version=2, retain_topic=True))
        plan = plan_for(value)
        value["cases"][0]["turns"].pop()
        result = quality.evaluate(value, plan)
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
            quality.evaluate(value)
        result = quality.evaluate(value, plan)
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
                    quality.evaluate(value, plan)

    def test_plan_digest_ignores_json_layout_but_binds_query_and_turn_order(self):
        plan = plan_for(capture(turn(None), turn(None, version=2)))
        before = copy.deepcopy(plan)
        reordered = json.loads(
            json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=4)
        )
        self.assertEqual(quality.plan_digest(plan), quality.plan_digest(reordered))
        changed = copy.deepcopy(plan)
        changed["cases"][0]["turns"][0]["query"] += "，研究用"
        self.assertNotEqual(quality.plan_digest(plan), quality.plan_digest(changed))
        reordered["cases"][0]["turns"].reverse()
        self.assertNotEqual(quality.plan_digest(plan), quality.plan_digest(reordered))
        self.assertEqual(before, plan)

    def test_topic_changed_requires_a_new_topic_and_rejects_conflicting_expectations(
        self,
    ):
        first = turn(None)
        second = turn(None, version=2)
        second["query"] = "改找河川資料"
        second["expected"]["topic_changed"] = True
        value = capture(first, second)
        result = quality.evaluate(value, plan_for(value))
        self.assertIn("topic_not_changed", result["cases"][0]["turns"][1]["issues"])
        second["response"]["result"]["state"]["topic"] = "河川"
        self.assertEqual("passed", quality.evaluate(value, plan_for(value))["status"])
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
                    quality.validate_cases(invalid)

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
                    quality.validate_cases(plan)

    def test_production_query_validation_accepts_punctuation_and_preserves_raw_binding(
        self,
    ):
        value = capture(turn(None))
        raw_query = "  找  氣候資料，  類型不限  "
        value["cases"][0]["turns"][0]["query"] = raw_query
        plan = plan_for(value)
        self.assertEqual(plan, quality.validate_cases(plan))
        self.assertEqual(raw_query, plan["cases"][0]["turns"][0]["query"])
        self.assertEqual("passed", quality.evaluate(value, plan)["status"])
        value["cases"][0]["turns"][0]["query"] = "找 氣候資料, 類型不限"
        with self.assertRaises(ValueError):
            quality.evaluate(value, plan)
        for invisible in ("\u200b", "\u202e", "\x00", "\ud800"):
            with self.subTest(invisible=repr(invisible)):
                invalid = copy.deepcopy(plan)
                invalid["cases"][0]["turns"][0]["query"] = "找" + invisible + "氣候資料"
                with self.assertRaises(ValueError):
                    quality.validate_cases(invalid)

    def test_canonical_case_plan_validates_with_all_supported_outcomes(self):
        path = Path(__file__).resolve().parents[2] / "fixtures/search-quality.json"
        plan = json.loads(path.read_text())
        self.assertEqual(plan, quality.validate_cases(plan))
        outcomes = {
            row["expected"].get("status", "ok")
            for case in plan["cases"]
            for row in case["turns"]
        }
        self.assertEqual({"ok", "clarification_required", "rejected"}, outcomes)
        self.assertRegex(quality.plan_digest(plan), r"^[0-9a-f]{64}$")

    def test_cli_plan_validation_and_bound_capture_have_distinct_failure_codes(self):
        value = capture(turn(None))
        value["cases"][0]["turns"][0]["query"] = "找氣候資料，勿輸出測試字串"
        plan = plan_for(value)
        with tempfile.TemporaryDirectory() as directory:
            plan_path, capture_path = (
                Path(directory) / name for name in ("plan.json", "capture.json")
            )
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
            output = io.StringIO()
            with redirect_stdout(output):
                code = quality.main(["--plan", str(plan_path)])
            summary = json.loads(output.getvalue())
            self.assertEqual(0, code)
            self.assertEqual("plan_valid", summary["status"])
            self.assertEqual((1, 1), (summary["case_count"], summary["turn_count"]))
            self.assertEqual(quality.plan_digest(plan), summary["plan_sha256"])
            self.assertNotIn("勿輸出測試字串", output.getvalue())
            for expected_code, captured in (
                (0, value),
                (1, {"schema_version": 1, "cases": []}),
                (2, capture(turn(None))),
            ):
                with self.subTest(expected_code=expected_code):
                    capture_path.write_text(json.dumps(captured, ensure_ascii=False))
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = quality.main(
                            ["--input", str(capture_path), "--plan", str(plan_path)]
                        )
                    self.assertEqual(expected_code, code)
                    self.assertEqual(
                        {0: "passed", 1: "failed", 2: "invalid_input"}[code],
                        json.loads(output.getvalue())["status"],
                    )


if __name__ == "__main__":
    unittest.main()
