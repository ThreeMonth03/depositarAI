"""Native read boundary; no live server, models or Haystack required."""

import copy
import importlib.util
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.api.retrieval import index
from search.api.retrieval.results import ValidatedSearch
from search.planning import index_filters
from search.tests.fixtures import IDS, CONDITIONS, report
from search.tests.fixtures import condition_document


class IndexBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.doc = {
            "dataset_id": IDS[0],
            "url": "https://data.depositar.io/dataset/a",
            "role": ["Rdepositar_search"],
            "label": ["catalog"],
            "virtual_host": ["catalog"],
        }
        self.bundle = SimpleNamespace(
            documents=[self.doc],
            bundle_id="a" * 64,
            manifest_sha256="b" * 64,
            expected_inventory_entries=[(self.doc["url"], "a" * 64, "c" * 64)],
            condition_metadata={IDS[0]: condition_document("2025", "2025")},
        )
        self.source = {
            **self.doc,
            "depositar_document_sha256": "c" * 64,
            index_filters.FIELD: index_filters.metadata(
                self.bundle.condition_metadata[IDS[0]], "a" * 64
            ),
        }
        self.response = {
            "timed_out": False,
            "_shards": {"total": 5, "successful": 5, "failed": 0},
            "hits": {
                "hits": [
                    {
                        "_index": "fess.20260917120000",
                        "_score": 1.0,
                        "_source": self.source,
                    }
                ]
            },
        }

    def check_hits(self, response):
        return index.validate_hits(
            response, self.bundle, "fess.20260917120000", CONDITIONS
        )

    def test_valid_native_hit_and_unknown_are_retained(self):
        self.assertEqual(self.check_hits(self.response), [(IDS[0], 1.0)])
        self.bundle.condition_metadata[IDS[0]] = condition_document(None, None)
        self.source[index_filters.FIELD] = index_filters.metadata(
            self.bundle.condition_metadata[IDS[0]], "a" * 64
        )
        self.assertEqual(len(self.check_hits(self.response)), 1)

    def test_partial_results_are_errors_even_with_valid_hits(self):
        for key, value in (
            ("timed_out", True),
            ("terminated_early", True),
            ("_shards", {"total": 5, "successful": 4, "failed": 1}),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.check_hits({**self.response, key: value})

    def test_hit_identity_projection_and_acl_drift_are_rejected(self):
        for field, value in (
            ("url", "https://evil.invalid"),
            ("role", ["admin"]),
            ("depositar_document_sha256", "0" * 64),
            (index_filters.FIELD, {}),
        ):
            changed = copy.deepcopy(self.response)
            changed["hits"]["hits"][0]["_source"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check_hits(changed)

    def test_wrong_physical_index_duplicates_and_nonfinite_scores_rejected(self):
        for field, value in (("_index", "fess.other"), ("_score", float("nan"))):
            changed = copy.deepcopy(self.response)
            changed["hits"]["hits"][0][field] = value
            with self.assertRaises(ValueError):
                self.check_hits(changed)
        changed = copy.deepcopy(self.response)
        changed["hits"]["hits"] *= 2
        with self.assertRaises(ValueError):
            self.check_hits(changed)

    def test_known_outside_fails_instead_of_being_silently_dropped(self):
        self.bundle.condition_metadata[IDS[0]] = condition_document("2000", "2000")
        self.source[index_filters.FIELD] = index_filters.metadata(
            self.bundle.condition_metadata[IDS[0]], "a" * 64
        )
        with self.assertRaises(ValueError):
            self.check_hits(self.response)

    def test_report_binds_query_manifest_conditions_and_backend(self):
        value = report("氣候", conditions=CONDITIONS)
        self.assertEqual(
            len(
                ValidatedSearch.from_report(
                    "氣候",
                    value,
                    conditions=CONDITIONS,
                    manifest_sha256="b" * 64,
                    maximum=20,
                ).rows
            ),
            5,
        )
        for field, replacement in (
            ("conditions", {}),
            ("manifest_sha256", "a" * 64),
            ("ranking", "fess-rrf"),
            ("query_sha256", "0" * 64),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                ValidatedSearch.from_report(
                    "氣候",
                    {**value, field: replacement},
                    conditions=CONDITIONS,
                    manifest_sha256="b" * 64,
                    maximum=20,
                )

    def test_report_count_duplicates_and_provenance_rejected(self):
        value = report("氣候", conditions=CONDITIONS)
        with self.assertRaises(ValueError):
            ValidatedSearch.from_report(
                "氣候",
                value,
                conditions=CONDITIONS,
                manifest_sha256="b" * 64,
                maximum=1,
            )
        for rows in (
            [value["results"][0]] * 2,
            [{**row, "searchers": ["default"]} for row in report("氣候")["results"]],
            list(reversed(value["results"])),
        ):
            with self.assertRaises(ValueError):
                ValidatedSearch.from_report(
                    "氣候",
                    {**value, "results": rows},
                    conditions=CONDITIONS,
                    manifest_sha256="b" * 64,
                    maximum=20,
                )

    def test_binding_is_bounded_owned_file_not_user_chosen_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binding"
            value = {
                "index": "fess.20260917120000",
                "manifest_sha256": "b" * 64,
                "bundle_id": "a" * 64,
                "projection_schema": index_filters.VERSION,
            }
            path.write_text(json.dumps(value))
            path.chmod(0o600)
            self.assertEqual(index.load_binding(path, self.bundle), value)
            for physical in ("fess.search", "*", "fess.20260917120000/_bulk"):
                path.write_text(json.dumps({**value, "index": physical}))
                with self.assertRaises(ValueError):
                    index.load_binding(path, self.bundle)
            path.write_text("x" * 4097)
            with self.assertRaises(ValueError):
                index.load_binding(path, self.bundle)

    def test_invalid_payload_cannot_reach_network(self):
        with patch.object(index, "IndexClient") as client:
            for payload in (
                {},
                {"query": "氣候", "top_k": True, "conditions": CONDITIONS},
                {"query": "氣候", "top_k": 5, "conditions": {"bbox": [0, 0, 1, 1]}},
            ):
                with self.assertRaises(ValueError):
                    index.search(payload, None)
            client.assert_not_called()

    def test_embedding_identity_cpu_and_no_truncation(self):
        client = index.IndexClient.__new__(index.IndexClient)
        client.embedding_transport = object()
        client.embedding_origin = "http://172.18.0.2:11434"
        tags = {"models": [{"name": index.MODEL, "digest": index.MODEL_DIGEST}]}
        with patch.object(
            client,
            "json_request",
            side_effect=[tags, {"model": index.MODEL, "embeddings": [[1.0] * 768]}],
        ) as send:
            self.assertEqual(len(client.embed("水質")), 768)
            payload = send.call_args.args[2]
            self.assertIs(payload["truncate"], False)
            self.assertEqual(payload["options"]["num_gpu"], 0)
        with patch.object(client, "json_request", return_value={"models": []}) as send:
            with self.assertRaises(ValueError):
                client.embed("水質")
            self.assertEqual(send.call_count, 1)


@unittest.skipUnless(importlib.util.find_spec("haystack"), "Pinned pipeline dependency")
class IndexPipelineTests(unittest.TestCase):
    def test_empty_time_format_and_combined_searches_use_the_same_pipeline(self):
        row = report("氣候", count=1)["results"][0]
        bundle = SimpleNamespace(manifest_sha256="b" * 64)
        for conditions in (
            {},
            CONDITIONS,
            {"formats": ["CSV"]},
            {**CONDITIONS, "formats": ["CSV"]},
        ):
            calls = []

            class Client:
                by_id = {row["dataset_id"]: row}

                def embed(self, query):
                    self.query = query
                    return [1.0] * 768

                def retrieve(self, query, received_conditions, branch, vector):
                    calls.append((query, received_conditions, branch, vector))
                    return [(row["dataset_id"], 1.0)]

            with (
                self.subTest(conditions=conditions),
                patch.object(index.CatalogBundle, "load", return_value=bundle),
                patch.object(index, "IndexClient", return_value=Client()),
            ):
                result = index.search(
                    {"query": "氣候", "top_k": 1, "conditions": conditions},
                    SimpleNamespace(
                        bundle_path=Path("/catalog"), manifest_sha256="b" * 64
                    ),
                )
                found = ValidatedSearch.from_report(
                    "氣候",
                    result,
                    conditions=conditions,
                    manifest_sha256="b" * 64,
                    maximum=1,
                )
                self.assertEqual(
                    [row["dataset_id"]], [r["dataset_id"] for r in found.rows]
                )
                self.assertEqual({"lexical", "vector"}, {call[2] for call in calls})
                self.assertTrue(
                    all(call[0] == "氣候" and call[1] == conditions for call in calls)
                )
                self.assertEqual(
                    ["opensearch-bm25", "opensearch-knn"], found.rows[0]["searchers"]
                )
