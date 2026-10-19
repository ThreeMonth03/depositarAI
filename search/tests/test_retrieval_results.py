"""Strict catalog metadata, query identity and server-owned result cards."""

import copy
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import unittest
import urllib.parse

from search.api.retrieval.results import ResultError, TOP_K, ValidatedSearch
from search.catalog.query import normalize_query
from search.tests.fixtures import IDS, report, result


class RetrievalResultTests(unittest.TestCase):
    def test_shared_fixture_import_needs_no_test_suites_or_http_dependencies(self):
        from search.tests import fixtures

        self.assertIs(IDS, fixtures.IDS)
        self.assertIs(report, fixtures.report)
        self.assertIs(result, fixtures.result)
        root = Path(__file__).resolve().parents[2]
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(root)!r})\n"
            "import search.tests.fixtures\n"
            "assert not any(name.startswith('search.tests.test_') "
            "for name in sys.modules), 'test suite imported by fixtures'\n"
            "for package in ('fastapi', 'django', 'rest_framework', 'adrf', 'starlette', 'httpx', 'pydantic'):\n"
            "    assert not any(name.partition('.')[0] == package "
            "for name in sys.modules), 'HTTP dependency imported by fixtures'\n"
            "print('shared fixtures import independently')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(), "shared fixtures import independently"
        )

    def test_cards_expose_only_server_validated_fields_and_bound_snippets(self):
        query = "ＥＲＡ５／WRF？"
        source = report(query)
        source["results"][0]["notes"] = "資料" * 2000
        found = ValidatedSearch.from_report(
            query, source, conditions={}, manifest_sha256="b" * 64
        )
        cards = found.cards()
        self.assertEqual(len(cards), TOP_K)
        self.assertEqual(
            found.query_sha256,
            hashlib.sha256(normalize_query(query).encode()).hexdigest(),
        )
        expected = {
            "rank",
            "dataset_id",
            "title",
            "url",
            "snippet",
            "organization_title",
            "resource_formats",
            "metadata_modified",
        }
        for rank, card in enumerate(cards, 1):
            self.assertEqual(set(card), expected)
            self.assertEqual(card["rank"], rank)
        self.assertEqual(len(cards[0]["snippet"]), 2000)
        self.assertEqual(cards[0]["url"], source["results"][0]["url"])
        json.dumps(cards, ensure_ascii=False, allow_nan=False).encode()

    def test_malformed_retrieval_shapes_fail_closed(self):
        query = "臺灣氣候資料"
        baseline = report(query)
        mutations = []
        for field, value in (
            ("manifest_sha256", "c" * 64),
            ("manifest_sha256", True),
            ("conditions", {"formats": ["CSV"]}),
            ("conditions", None),
            ("ranking", "fess-rrf"),
        ):
            invalid_identity = copy.deepcopy(baseline)
            invalid_identity[field] = value
            mutations.append(invalid_identity)
        extra_envelope = copy.deepcopy(baseline)
        extra_envelope["query"] = query
        mutations.append(extra_envelope)
        wrong_hash = copy.deepcopy(baseline)
        wrong_hash["query_sha256"] = "0" * 64
        mutations.append(wrong_hash)
        mutations.append(report(query, count=6))
        extra_result = copy.deepcopy(baseline)
        extra_result["results"][0]["model_url"] = "https://evil.example"
        mutations.append(extra_result)
        non_finite = copy.deepcopy(baseline)
        non_finite["results"][0]["rank_fusion_score"] = math.nan
        mutations.append(non_finite)
        duplicate = copy.deepcopy(baseline)
        duplicate["results"][1]["dataset_id"] = duplicate["results"][0]["dataset_id"]
        mutations.append(duplicate)
        unordered = copy.deepcopy(baseline)
        unordered["results"][1]["rank_fusion_score"] = 2.0
        mutations.append(unordered)
        for unsafe_url in (
            "https://user@data.depositar.io/dataset/dataset-1",
            "https://data.depositar.io:443/dataset/dataset-1",
            "https://data.depositar.io/dataset/dataset-1?q=secret",
            "https://data.depositar.io/dataset/dataset-1#fragment",
            "https://data.depositar.io/dataset/",
            "https://data.depositar.io/dataset/%64ataset-1",
        ):
            unsafe_origin = copy.deepcopy(baseline)
            unsafe_origin["results"][0]["url"] = unsafe_url
            mutations.append(unsafe_origin)
        for malformed in mutations:
            with self.subTest(keys=tuple(malformed)), self.assertRaises(ResultError):
                ValidatedSearch.from_report(
                    query, malformed, conditions={}, manifest_sha256="b" * 64
                )

    def test_canonical_percent_encoded_slug_and_resource_count_boundary(self):
        query = "臺灣資料"
        boundary = report(query)
        slug = "臺灣 資料"
        boundary["results"][0]["name"] = slug
        boundary["results"][0]["url"] = (
            "https://data.depositar.io/dataset/" + urllib.parse.quote(slug, safe="")
        )
        boundary["results"][0]["resource_count"] = 100_000
        found = ValidatedSearch.from_report(
            query, boundary, conditions={}, manifest_sha256="b" * 64
        )
        self.assertEqual(found.cards()[0]["url"], boundary["results"][0]["url"])
        too_large = copy.deepcopy(boundary)
        too_large["results"][0]["resource_count"] = 100_001
        with self.assertRaises(ResultError):
            ValidatedSearch.from_report(
                query, too_large, conditions={}, manifest_sha256="b" * 64
            )
        for dot_slug in (".", ".."):
            invalid = copy.deepcopy(boundary)
            invalid["results"][0]["name"] = dot_slug
            invalid["results"][0]["url"] = (
                "https://data.depositar.io/dataset/" + dot_slug
            )
            with self.subTest(slug=dot_slug), self.assertRaises(ResultError):
                ValidatedSearch.from_report(
                    query, invalid, conditions={}, manifest_sha256="b" * 64
                )

    def test_builtin_boundary_rejects_container_and_scalar_subclasses(self):
        class LyingList(list):
            def __len__(self):
                return 5

        class EvilString(str):
            def __getitem__(self, key):
                raise RuntimeError("private scalar detail")

        query = "臺灣資料"
        six_rows = report(query)
        six_rows["results"] = LyingList([result(i, 1.0 / i) for i in range(1, 7)])
        with self.assertRaises(ResultError):
            ValidatedSearch.from_report(
                query, six_rows, conditions={}, manifest_sha256="b" * 64
            )
        evil_scalar = report(query)
        evil_scalar["results"][0]["notes"] = EvilString("private scalar detail")
        with self.assertRaises(ResultError) as caught:
            ValidatedSearch.from_report(
                query, evil_scalar, conditions={}, manifest_sha256="b" * 64
            )
        self.assertNotIn("private scalar detail", str(caught.exception))

    def test_retrieval_text_must_be_safe_utf8_nfkc_without_controls(self):
        query = "臺灣資料"
        for text in ("bad\ud800", "fullwidth，comma", "left\u202eright"):
            malformed = report(query)
            malformed["results"][0]["title"] = text
            with self.subTest(text=repr(text)), self.assertRaises(ResultError):
                ValidatedSearch.from_report(
                    query, malformed, conditions={}, manifest_sha256="b" * 64
                )

    def test_empty_results_and_actual_maximum_remain_supported(self):
        query = "資料"
        self.assertEqual(
            ValidatedSearch.from_report(
                query, report(query, count=0), conditions={}, manifest_sha256="b" * 64
            ).cards(),
            [],
        )
        # The API may request twenty; the shared default remains five.
        self.assertEqual(
            len(
                ValidatedSearch.from_report(
                    query,
                    report(query, count=6),
                    maximum=20,
                    conditions={},
                    manifest_sha256="b" * 64,
                ).cards()
            ),
            6,
        )
        for maximum in (False, 0, 21):
            with self.subTest(maximum=maximum), self.assertRaises(ResultError):
                ValidatedSearch.from_report(
                    query,
                    report(query),
                    maximum=maximum,
                    conditions={},
                    manifest_sha256="b" * 64,
                )

    def test_validated_nested_collections_do_not_alias_the_report_or_cards(self):
        query = "氣候資料"
        source = report(query)
        found = ValidatedSearch.from_report(
            query, source, conditions={}, manifest_sha256="b" * 64
        )
        source["results"][0]["resource_formats"].clear()
        self.assertEqual(found.cards()[0]["resource_formats"], ["CSV", "PDF"])
        cards = found.cards()
        cards[0]["resource_formats"].clear()
        self.assertEqual(found.cards()[0]["resource_formats"], ["CSV", "PDF"])

    def test_empty_time_and_format_requests_bind_the_same_exact_report_contract(self):
        for conditions in (
            {},
            {"time": {"start": "2025-01-01", "end": None}},
            {"formats": ["CSV", "PDF"]},
        ):
            with self.subTest(conditions=conditions):
                source = report("水質", conditions=conditions)
                found = ValidatedSearch.from_report(
                    "水質", source, conditions=conditions, manifest_sha256="b" * 64
                )
                self.assertEqual(len(found.cards()), TOP_K)
                different = {"formats": ["ZIP"]}
                with self.assertRaises(ResultError):
                    ValidatedSearch.from_report(
                        "水質", source, conditions=different, manifest_sha256="b" * 64
                    )


if __name__ == "__main__":
    unittest.main()
