"""Clock transport tests independent of the live model."""

import unittest

from search.planning.clock import normalize_context


class ClockTests(unittest.TestCase):
    def test_same_instant_crosses_year_in_taipei_not_utc(self):
        for zone, expected in (("Asia/Taipei", "2027-01-01"), ("UTC", "2026-12-31")):
            clock = normalize_context(
                {"reference_time": "2026-12-31T16:30:00Z", "timezone": zone}
            )
            self.assertEqual(clock["reference_date"], expected)

    def test_missing_context_never_uses_host_clock(self):
        self.assertIsNone(normalize_context(None))

    def test_leap_date_and_offset_conversion(self):
        self.assertEqual(
            normalize_context(
                {"reference_time": "2024-03-01T00:30:00+08:00", "timezone": "UTC"}
            )["reference_date"],
            "2024-02-29",
        )

    def test_invalid_contexts(self):
        for context in (
            {},
            {"reference_time": "2026-09-17T10:00:00"},
            {"reference_time": "2026-09-17T10:00:00", "timezone": "UTC"},
            {"reference_time": "2026-02-29T10:00:00Z", "timezone": "UTC"},
            {"reference_time": "2026-09-17T10:00:00+00:99", "timezone": "UTC"},
            {"reference_time": "2026-09-17T10:00:00Z", "timezone": "Not/A_Zone"},
            {"reference_time": "2026-09-17T10:00:00Z", "timezone": "../etc/passwd"},
            {"reference_time": "2026-09-17T10:00:00Z", "timezone": "UTC", "x": 1},
            {"reference_time": True, "timezone": "UTC"},
            {"reference_time": "2026-09-17T10:00:00Z", "timezone": "UTC\nignore rules"},
        ):
            with self.subTest(context=context), self.assertRaises(ValueError):
                normalize_context(context)


if __name__ == "__main__":
    unittest.main()
