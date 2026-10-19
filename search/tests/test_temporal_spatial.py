"""Offline contract tests: unknown is neither a positive nor a silent exclusion."""

from copy import deepcopy
from datetime import date
import unittest

from search.tests.fixtures import condition_document
from search.planning.predicates import (
    checked_bbox,
    classify,
    compile_filters,
    date_bounds,
    project,
    validate_conditions,
)


class DateTests(unittest.TestCase):
    def test_year_month_day_precision(self):
        for value, low, high, precision in (
            ("1783", "1783-01-01", "1783-12-31", "year"),
            ("2024-02", "2024-02-01", "2024-02-29", "month"),
            ("2023-02", "2023-02-01", "2023-02-28", "month"),
            ("2020-01-02", "2020-01-02", "2020-01-02", "day"),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    date_bounds(value),
                    (
                        date.fromisoformat(low).toordinal(),
                        date.fromisoformat(high).toordinal(),
                        precision,
                    ),
                )

    def test_invalid_dates_not_accepted(self):
        for value in (
            None,
            True,
            2020,
            "",
            "2023-02-29",
            "0000",
            "2024-13",
            "2024-1-",
            "２０２４",
            "2020 ",
            "2020-01-01T00:00:00Z",
            "2020 OR *",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                date_bounds(value)

    def test_unpadded_calendar_fields_are_not_bad_dates(self):
        self.assertEqual(date_bounds("2025-5-12"), date_bounds("2025-05-12"))
        self.assertEqual(date_bounds("2024-2"), date_bounds("2024-02"))
        self.assertEqual(date_bounds("2024-2-9"), date_bounds("2024-02-09"))
        source = condition_document("2025-5-12", None)
        self.assertEqual(project(source)["time_status"], "partial")
        self.assertEqual(source["temporal"]["start"], "2025-5-12")
        self.assertEqual(
            classify(project(source), {"time": {"start": "2020", "end": "2020"}}),
            "outside",
        )

    def test_coarse_endpoint_is_not_full_period_coverage(self):
        meta = project(condition_document("1783", "1783"))
        self.assertEqual(
            classify(meta, {"time": {"start": "1783", "end": "1783"}}), "match"
        )
        self.assertEqual(
            classify(meta, {"time": {"start": "1783-06", "end": "1783-06"}}), "unknown"
        )
        self.assertEqual(
            classify(meta, {"time": {"start": "1784", "end": "1784"}}), "outside"
        )

    def test_gap_between_uncertain_endpoints_supports_overlap(self):
        self.assertEqual(
            classify(
                project(condition_document("2018", "2020")),
                {"time": {"start": "2019-06", "end": "2019-06"}},
            ),
            "match",
        )

    def test_day_boundary_is_inclusive(self):
        meta = project(condition_document("2020-01-01", "2020-01-31"))
        self.assertEqual(
            classify(meta, {"time": {"start": "2020-01-31", "end": "2020-01-31"}}),
            "match",
        )
        self.assertEqual(
            classify(meta, {"time": {"start": "2020-02-01", "end": None}}), "outside"
        )

    def test_partial_is_not_silently_infinite(self):
        meta = project(condition_document("2026-03", None))
        self.assertEqual(
            classify(meta, {"time": {"start": "2025", "end": "2025"}}), "outside"
        )
        self.assertEqual(
            classify(meta, {"time": {"start": "2026", "end": "2028"}}), "unknown"
        )
        meta = project(condition_document(None, "2000"))
        self.assertEqual(
            classify(meta, {"time": {"start": "2025", "end": "2025"}}), "outside"
        )

    def test_missing_and_invalid_remain_unknown(self):
        for start, end in ((None, None), ("2020-02-30", "2021"), ("2025", "2020")):
            with self.subTest(start=start):
                meta = project(condition_document(start, end))
                self.assertEqual(
                    classify(meta, {"time": {"start": "2010", "end": "2015"}}),
                    "unknown",
                )

    def test_normalization_does_not_mutate_input(self):
        original = condition_document(
            "2018", "2020", [120, 23, 121, 24], ["csv", "PDF"]
        )
        before = deepcopy(original)
        self.assertEqual(project(original)["start_precision"], "year")
        self.assertEqual(before, original)


class ConditionTests(unittest.TestCase):
    def test_bbox_boundary_and_missing(self):
        query = {"bbox": [120, 23, 121, 24]}
        self.assertEqual(
            classify(project(condition_document(bbox=[121, 24, 122, 25])), query),
            "match",
        )
        self.assertEqual(
            classify(project(condition_document(bbox=[121.1, 24, 122, 25])), query),
            "outside",
        )
        self.assertEqual(classify(project(condition_document()), query), "unknown")

    def test_invalid_bbox_is_unknown_data_but_rejected_query(self):
        for bbox in (
            [0, 0, 10**1000, 1],
            [True, 0, 1, 1],
            [0, 0, float("nan"), 1],
            [170, 0, -170, 1],
            [0, -91, 1, 0],
            [0, 0, float("inf"), 1],
            [0, 0, 181, 1],
        ):
            with self.subTest(bbox=bbox):
                with self.assertRaises(ValueError):
                    checked_bbox(bbox)
                self.assertFalse(project(condition_document(bbox=bbox))["bbox_known"])

    def test_format_alternatives_and_unknown(self):
        conditions = {"formats": ["PDF", "CSV"]}
        for formats, state in (
            (["pdf"], "match"),
            (["XLSX"], "outside"),
            ([], "unknown"),
        ):
            with self.subTest(formats=formats):
                self.assertEqual(
                    classify(project(condition_document(formats=formats)), conditions),
                    state,
                )

    def test_unknown_and_false_is_outside(self):
        conditions = {"time": {"start": "2020", "end": "2020"}, "formats": ["PDF"]}
        self.assertEqual(
            classify(project(condition_document(formats=["CSV"])), conditions),
            "outside",
        )
        self.assertEqual(
            classify(project(condition_document(formats=["PDF"])), conditions),
            "unknown",
        )

    def test_arbitrary_fields_operators_and_malformed_limits_rejected(self):
        invalid = [
            None,
            [],
            {"query": "x"},
            {"operator": "OR"},
            {"script": "x"},
            {"time": {}},
            {"time": {"start": None, "end": None}},
            {"time": {"start": "2025", "end": "2020"}},
            {"time": {"start": "2020", "end": "2021", "operator": "OR"}},
            {"formats": []},
            {"formats": ["PDF"] * 2},
            {"formats": ["PDF", "CSV", "XLSX", "ZIP", "JSON"]},
            {"formats": ["*"]},
            {"formats": [True]},
            {"formats": "PDF"},
            {"bbox": [1, 2]},
        ]
        for conditions in invalid:
            with self.subTest(conditions=conditions), self.assertRaises(ValueError):
                compile_filters(conditions)

    def test_explicit_open_query_is_supported(self):
        self.assertEqual(
            validate_conditions({"time": {"start": None, "end": "2020"}})["time"][0], 1
        )

    def test_no_conditions_does_not_exclude_missing_metadata(self):
        self.assertEqual(classify(project(condition_document()), {}), "match")


if __name__ == "__main__":
    unittest.main()
