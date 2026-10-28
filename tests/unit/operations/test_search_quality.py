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
                "revision": "a" * 40,
                "state": {"topic": "氣候", "time": time, "formats": formats},
                "search": {
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


if __name__ == "__main__":
    unittest.main()
