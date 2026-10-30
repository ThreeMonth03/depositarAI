"""Native read boundary; no live server, models or Haystack required."""

import copy
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import patch

from search.api.retrieval import index
from search.api.retrieval.results import ValidatedSearch
from search.catalog.build import builder
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
            "resource_context": [],
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
            ("resource_context", None),
            (index_filters.FIELD, {}),
        ):
            changed = copy.deepcopy(self.response)
            changed["hits"]["hits"][0]["_source"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check_hits(changed)

    def test_resource_context_projection_must_be_present_and_match_sealed_document(
        self,
    ):
        context = {
            "resource_id": IDS[1],
            "name": "Water measurements",
            "format": "CSV",
            "url": self.doc["url"] + "/resource/" + IDS[1],
            "columns": ["station", "temperature"],
            "excerpt": "",
        }
        self.doc["resource_context"] = [context]
        for value in ([], [{**context, "columns": ["invented"]}], None):
            changed = copy.deepcopy(self.response)
            changed["hits"]["hits"][0]["_source"]["resource_context"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.check_hits(changed)
        changed = copy.deepcopy(self.response)
        del changed["hits"]["hits"][0]["_source"]["resource_context"]
        with self.assertRaises(ValueError):
            self.check_hits(changed)
        self.source["resource_context"] = [copy.deepcopy(context)]
        self.assertEqual(self.check_hits(self.response), [(IDS[0], 1.0)])

    def test_native_query_requests_resource_context_for_sealed_projection_check(self):
        client = index.IndexClient.__new__(index.IndexClient)
        client.bundle = self.bundle
        client.binding = {"index": "fess.20260917120000"}
        client.origin = "https://opensearch-search:9200"
        client.transport = object()
        with (
            patch.object(index, "read_secret", return_value="fixture"),
            patch.object(client, "json_request", return_value=self.response) as send,
        ):
            self.assertEqual(
                client.retrieve("氣候", CONDITIONS, "lexical", None), [(IDS[0], 1.0)]
            )
        self.assertIn("resource_context", send.call_args.args[2]["_source"])

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

    def test_cards_use_sealed_coverage_without_index_or_query_date_inference(self):
        coverage = (
            ("2020", "2025", "complete"),
            ("2024-2", "2024-02-29", "complete"),
            ("2024", None, "partial"),
            (None, None, "absent"),
            ("2024-02-30", "2024", "invalid"),
        )

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        source = base / "datasets.jsonl"
        records = [
            {
                "id": IDS[number],
                "name": f"coverage-{number}",
                "private": False,
                "state": "active",
                "type": "dataset",
                "title": "2030 年氣候資料",
                "start_time": start,
                "end_time": end,
                "created_time": "2010",
                "metadata_created": "2026-01-01T00:00:00",
                "metadata_modified": "2026-01-02T00:00:00",
            }
            for number, (start, end, _status) in enumerate(coverage)
        ]
        source.write_text("".join(json.dumps(record) + "\n" for record in records))
        source.chmod(0o600)
        output = base / "bundles"
        output.mkdir(mode=0o700)
        built = builder.build_bundle(source, output)
        bundle_path = Path(built["bundle_path"])
        for artifact in bundle_path.iterdir():
            self.addCleanup(artifact.chmod, 0o600)
        self.addCleanup(bundle_path.chmod, 0o700)
        bundle = index.CatalogBundle.load(
            bundle_path, expected_manifest_sha256=built["manifest_sha256"]
        )
        by_id = {row["dataset_id"]: row for row in bundle.documents}
        self.assertTrue(all("temporal_coverage" not in row for row in by_id.values()))
        fused = [
            SimpleNamespace(id=identifier, score=1 / rank)
            for rank, identifier in enumerate(by_id, 1)
        ]
        branches = {name: set(by_id) for name in ("lexical", "vector")}
        pipeline = SimpleNamespace(retrieve=lambda *_args: (fused, branches))
        client = SimpleNamespace(by_id=by_id, embed=lambda _query: [0.0] * 768)
        with (
            patch.object(index, "IndexClient", return_value=client),
            patch.dict(sys.modules, {"search.api.retrieval.pipeline": pipeline}),
        ):
            result = index.search(
                {"query": "找 2040 年氣候", "top_k": len(coverage), "conditions": {}},
                SimpleNamespace(
                    bundle_path=bundle_path,
                    manifest_sha256=built["manifest_sha256"],
                ),
            )
        cards = ValidatedSearch.from_report(
            "找 2040 年氣候",
            result,
            conditions={},
            manifest_sha256=built["manifest_sha256"],
        ).cards()
        for card, (start, end, status) in zip(cards, coverage):
            self.assertEqual(
                card["temporal_coverage"],
                {"start": start, "end": end, "status": status},
            )
            self.assertEqual(card["metadata_modified"], "2026-01-02T00:00:00")


@unittest.skipUnless(importlib.util.find_spec("haystack"), "Pinned pipeline dependency")
class IndexPipelineTests(unittest.TestCase):
    def test_empty_time_format_and_combined_searches_use_the_same_pipeline(self):
        row = report("氣候", count=1)["results"][0]
        context = {
            "resource_id": IDS[1],
            "name": "Observations",
            "format": "CSV",
            "url": row["url"] + "/resource/" + IDS[1],
            "columns": ["temperature", "precipitation"],
            "excerpt": "",
        }
        row["resource_context"] = (
            MappingProxyType({**context, "columns": tuple(context["columns"])}),
        )
        for field in ("tags", "groups", "resource_formats"):
            row[field] = tuple(row[field])
        row = MappingProxyType(row)
        bundle = SimpleNamespace(
            manifest_sha256="b" * 64,
            condition_metadata={row["dataset_id"]: condition_document("2020", "2025")},
        )
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
                self.assertEqual(found.cards()[0]["resource_context"], [context])
                observed = result["results"][0]
                for field in ("tags", "groups", "resource_formats"):
                    self.assertIs(type(observed[field]), list)
                    self.assertEqual(observed[field], list(row[field]))
                    observed[field].append("changed after retrieval")
                    self.assertEqual(tuple(observed[field][:-1]), row[field])
                observed["resource_context"][0]["columns"].clear()
                self.assertEqual(
                    row["resource_context"][0]["columns"], tuple(context["columns"])
                )
                self.assertEqual(found.cards()[0]["resource_context"], [context])
