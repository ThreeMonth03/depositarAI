"""Production native projection: sealed bundles, paginated fake transport only."""

from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from search.catalog.build import builder
from search.catalog.bundle import CatalogBundle
from search.catalog.inventory import InventorySnapshot
from search.catalog.vectors import VectorProgressSnapshot
from search.operations.catalog import projection
from search.operations.shared import private_io
from search.planning import index_filters as filters
from search.vector_journal import state_sha256
from tests.unit.operations.test_metadata_sync import dataset


INDEX = "fess.20261008120000"


def make_bundle(root, count=101):
    source = root / "snapshot.jsonl"
    source.write_text(
        "".join(
            json.dumps(
                dataset(
                    number,
                    extras=[
                        {"key": "temporal_start", "value": "2020"},
                        {"key": "temporal_end", "value": "2022"},
                    ],
                )
            )
            + "\n"
            for number in range(1, count + 1)
        )
    )
    destination = root / "bundles"
    destination.mkdir(mode=0o700)
    report = builder.build_bundle(source, destination)
    return CatalogBundle.load(
        Path(report["bundle_path"]),
        expected_manifest_sha256=report["manifest_sha256"],
    )


def shards():
    return {"total": 1, "successful": 1, "failed": 0}


class ProjectionClient:
    """Faithful enough to catch missing paging/CAS/item validation, not a server."""

    def __init__(self, bundle):
        self.bundle = bundle
        self.mapping = {
            INDEX: {
                "mappings": {
                    "properties": {
                        "url": {"type": "keyword"},
                        filters.VECTOR_PATH: {
                            "type": "nested",
                            "properties": {
                                "vector": {
                                    "type": "knn_vector",
                                    "dimension": 768,
                                    "method": {"engine": "lucene"},
                                }
                            },
                        },
                    }
                }
            }
        }
        self.rows = []
        hashes = {url: digest for url, _, digest in bundle.expected_inventory_entries}
        for number, row in enumerate(bundle.documents):
            source = {key: row[key] for key in ("dataset_id", "url")}
            source.update(
                {key: list(row[key]) for key in ("role", "label", "virtual_host")}
            )
            source.update(
                {
                    "depositar_bundle_id": bundle.bundle_id,
                    "depositar_document_sha256": hashes[row["url"]],
                    "content_chunk_status": "done",
                    filters.VECTOR_PATH: [{"vector": [1.0] + [0.0] * 767}],
                }
            )
            self.rows.append(
                {
                    "_index": INDEX,
                    "_id": "native-" + str(number),
                    "_seq_no": number,
                    "_primary_term": 1,
                    "_source": source,
                }
            )
        self.calls = []
        self.scans = 0
        self.search_fault = None
        self.bulk_fault = None
        self.after_bulk = None
        self.mapping_fault = None
        self.refresh_fault = None
        self.mapping_ack = True
        self.reorder_between_pages = False

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(payload), kwargs))
        if method == "GET" and path in (
            "/fess.search/_mapping",
            "/" + INDEX + "/_mapping",
        ):
            response = copy.deepcopy(self.mapping)
            if self.mapping_fault:
                self.mapping_fault(response)
            return 200, response
        if method == "POST" and path == "/" + INDEX + "/_search":
            start, size = payload["from"], payload["size"]
            if start == 0:
                self.scans += 1
            if payload["sort"] == [{"url": "asc"}]:
                ordered = sorted(self.rows, key=lambda row: row["_source"]["url"])
                rows = copy.deepcopy(ordered[start : start + size])
                for row in rows:
                    row["sort"] = [row["_source"]["url"]]
            elif payload["sort"] == ["_doc"]:
                rows = copy.deepcopy(self.rows[start : start + size])
                for offset, row in enumerate(rows, start=start):
                    row["sort"] = [offset]
            else:
                raise AssertionError("unexpected inventory sort")
            response = {
                "timed_out": False,
                "_shards": shards(),
                "hits": {
                    "total": {"value": len(self.rows), "relation": "eq"},
                    "hits": rows,
                },
            }
            if self.reorder_between_pages:
                self.rows.reverse()
            if self.search_fault:
                self.search_fault(response, start, self.scans)
            return 200, response
        if method == "PUT" and path == "/" + INDEX + "/_mapping":
            self.mapping[INDEX]["mappings"]["properties"].update(
                copy.deepcopy(payload["properties"])
            )
            return 200, {"acknowledged": self.mapping_ack}
        if method == "POST" and path == "/" + INDEX + "/_bulk":
            assert kwargs == {"ndjson": True}
            lines = payload.splitlines()
            # Preserve first-match behavior and rebuild after any fixture mutation.
            rows_by_id = {}
            for row in self.rows:
                rows_by_id.setdefault(row["_id"], row)
            response = {"errors": False, "items": []}
            for offset in range(0, len(lines), 2):
                action = json.loads(lines[offset])["update"]
                row = rows_by_id.get(action["_id"])
                if row is None:
                    raise StopIteration
                assert action["_index"] == INDEX
                assert action["if_seq_no"] == row["_seq_no"]
                assert action["if_primary_term"] == row["_primary_term"]
                row["_source"].update(json.loads(lines[offset + 1])["doc"])
                row["_seq_no"] += 1
                response["items"].append(
                    {
                        "update": {
                            "_index": INDEX,
                            "_id": row["_id"],
                            "status": 200,
                            "result": "updated",
                            "_shards": shards(),
                        }
                    }
                )
            if self.after_bulk:
                self.after_bulk(self.rows)
            if self.bulk_fault:
                self.bulk_fault(response)
            return 200, response
        if method == "POST" and path == "/" + INDEX + "/_refresh":
            response = {"_shards": shards()}
            if self.refresh_fault:
                self.refresh_fault(response)
            return 200, response
        raise AssertionError("unexpected fixed operation")


class CatalogConditionMappingTests(unittest.TestCase):
    def test_only_top_level_absent_object_type_is_equivalent_without_mutation(self):
        expected = filters.mapping()["properties"][filters.FIELD]
        for omitted in (False, True):
            with self.subTest(omitted=omitted):
                actual = copy.deepcopy(expected)
                if omitted:
                    actual.pop("type")
                before = copy.deepcopy(actual)
                self.assertTrue(projection._condition_mapping(actual))
                self.assertEqual(before, actual)

    def test_explicit_wrong_types_and_nonobject_values_are_not_normalized(self):
        for value in (None, False, True, 1, [], "object"):
            with self.subTest(value=value):
                self.assertFalse(projection._condition_mapping(value))
        for value in (None, False, True, 1, [], {}, "", "nested", "keyword", "OBJECT"):
            with self.subTest(explicit_type=value):
                actual = filters.mapping()["properties"][filters.FIELD]
                actual["type"] = value
                self.assertFalse(projection._condition_mapping(actual))

    def test_every_other_schema_key_and_nested_field_remains_exact(self):
        changes = (
            lambda actual: actual.pop("dynamic"),
            lambda actual: actual.update(dynamic=True),
            lambda actual: actual.update(dynamic=False),
            lambda actual: actual.update(dynamic=None),
            lambda actual: actual.update(dynamic="true"),
            lambda actual: actual.update(dynamic="false"),
            lambda actual: actual.update(enabled=False),
            lambda actual: actual.pop("properties"),
            lambda actual: actual.update(properties=None),
            lambda actual: actual.update(properties=[]),
            lambda actual: actual["properties"].pop("catalog"),
            lambda actual: actual["properties"].update(extra={"type": "keyword"}),
            lambda actual: actual["properties"]["record"].pop("type"),
            lambda actual: actual["properties"]["record"].update(type="integer"),
            lambda actual: actual["properties"]["record"].update(type=None),
            lambda actual: actual["properties"]["record"].update(type=False),
            lambda actual: actual["properties"]["record"].update(type=[]),
            lambda actual: actual["properties"]["record"].update(type={}),
            lambda actual: actual["properties"]["schema"].update(doc_values=False),
        )
        for omitted in (False, True):
            for change in changes:
                with self.subTest(omitted=omitted, change=change):
                    actual = filters.mapping()["properties"][filters.FIELD]
                    if omitted:
                        actual.pop("type")
                    change(actual)
                    self.assertFalse(projection._condition_mapping(actual))


class CatalogProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="depositar-projection-")
        cls.bundle = make_bundle(Path(cls.temporary.name))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.client = ProjectionClient(self.bundle)

    def provision(self):
        return projection.provision_projection(self.client, self.bundle)

    def writes(self):
        return [
            (method, path)
            for method, path, _, _ in self.client.calls
            if method == "PUT" or path.endswith("/_bulk")
        ]

    def test_complete_paging_cas_projection_and_postscan(self):
        report = self.provision()
        self.assertEqual("ready", report["status"])
        self.assertEqual(INDEX, report["index"])
        self.assertEqual(101, report["document_count"])
        self.assertEqual(101, report["projection_updated_count"])
        self.assertEqual(101, report["vector_chunk_count"])
        for name in (
            "inventory_verified",
            "projection_verified",
            "vector_state_verified",
        ):
            self.assertIs(report[name], True)
        searches = [
            payload
            for _, path, payload, _ in self.client.calls
            if path.endswith("/_search")
        ]
        self.assertEqual([0, 50, 100, 0, 50, 100], [p["from"] for p in searches])
        self.assertTrue(all(p["seq_no_primary_term"] is True for p in searches))
        self.assertTrue(all(path.startswith("/" + INDEX) for _, path in self.writes()))
        for row in self.client.rows:
            identifier = row["_source"]["dataset_id"]
            self.assertEqual(
                filters.metadata(
                    self.bundle.condition_metadata[identifier], self.bundle.bundle_id
                ),
                row["_source"][filters.FIELD],
            )

    def test_verified_repeat_never_rewrites_mapping_or_projection(self):
        first = self.provision()
        self.client.calls.clear()
        second = self.provision()
        self.assertEqual([], self.writes())
        self.assertEqual(0, second["projection_updated_count"])
        self.assertEqual(first["vectors_sha256"], second["vectors_sha256"])

    def test_doc_order_changes_between_pages_do_not_change_complete_scans(self):
        self.assertGreater(len(self.bundle.documents), projection.PAGE_SIZE)
        self.client.reorder_between_pages = True
        before = projection.verify_vectors(self.client, self.bundle)
        projected = self.provision()
        final = projection.verify_projection(self.client, self.bundle, INDEX)
        after = projection.verify_vectors(self.client, self.bundle, INDEX)
        self.assertEqual(before, after)
        self.assertEqual(
            {**projected, "projection_updated_count": 0},
            final,
        )
        self.assertEqual(
            len(self.bundle.documents), projected["projection_updated_count"]
        )
        searches = [
            payload
            for _, path, payload, _ in self.client.calls
            if path.endswith("/_search")
        ]
        self.assertTrue(
            all(payload["sort"] == [{"url": "asc"}] for payload in searches)
        )

    def test_url_keyword_defaults_and_nonclipping_limits_support_all_gates(self):
        maximum = max(len(row["url"]) for row in self.bundle.documents)
        for options in (
            {},
            {"doc_values": True},
            {"normalizer": None},
            {"ignore_above": maximum},
            {"doc_values": True, "normalizer": None, "ignore_above": maximum + 1},
        ):
            with self.subTest(options=options):
                self.client = ProjectionClient(self.bundle)
                self.client.mapping[INDEX]["mappings"]["properties"]["url"].update(
                    options
                )
                before = projection.verify_vectors(self.client, self.bundle)
                first = self.provision()
                self.client.calls.clear()
                final = projection.verify_projection(self.client, self.bundle, INDEX)
                after = projection.verify_vectors(self.client, self.bundle, INDEX)
                self.assertEqual(before, after)
                self.assertEqual({**first, "projection_updated_count": 0}, final)
                self.assertEqual([], self.writes())

    def test_unsortable_normalized_or_clipped_url_mapping_fails_before_writes(self):
        maximum = max(len(row["url"]) for row in self.bundle.documents)
        mappings = [None, False, [], "keyword", {}, {"type": "text"}]
        mappings.extend(
            {"type": "keyword", "doc_values": value}
            for value in (False, None, 0, 1, "true", [], {})
        )
        mappings.extend(
            {"type": "keyword", "normalizer": value}
            for value in ("lowercase", "", False, 0, [], {})
        )
        mappings.extend(
            {"type": "keyword", "ignore_above": value}
            for value in (maximum - 1, 0, -1, True, False, 1.0, "1000", None, 2**31)
        )
        for mapping in mappings:
            with self.subTest(mapping=mapping):
                self.client = ProjectionClient(self.bundle)
                properties = self.client.mapping[INDEX]["mappings"]["properties"]
                properties.update(filters.mapping()["properties"])
                if mapping is None:
                    properties.pop("url")
                else:
                    properties["url"] = mapping
                for operation in (
                    lambda: projection.verify_vectors(self.client, self.bundle),
                    self.provision,
                    lambda: projection.verify_projection(
                        self.client, self.bundle, INDEX
                    ),
                ):
                    with self.assertRaisesRegex(
                        projection.ProjectionError, "catalog_projection_mapping_changed"
                    ):
                        operation()
                self.assertEqual([], self.writes())
                self.assertTrue(
                    all(method == "GET" for method, _, _, _ in self.client.calls)
                )

    def test_missing_or_forged_hit_sort_values_fail_before_writes(self):
        missing = object()
        url = self.client.rows[0]["_source"]["url"]
        for value in (
            missing,
            None,
            [],
            [None],
            [1],
            [True],
            [url + "-forged"],
            [url, url],
            (url,),
            url,
            {},
        ):
            with self.subTest(sort=value):
                self.client = ProjectionClient(self.bundle)

                def fault(response, _start, _scan):
                    row = response["hits"]["hits"][0]
                    if value is missing:
                        row.pop("sort")
                    else:
                        row["sort"] = value

                self.client.search_fault = fault
                with self.assertRaisesRegex(
                    projection.ProjectionError, "catalog_projection_inventory_changed"
                ):
                    self.provision()
                self.assertEqual([], self.writes())

    def test_exact_unique_hits_in_wrong_page_order_fail_before_writes(self):
        ordered = copy.deepcopy(
            sorted(self.client.rows, key=lambda row: row["_source"]["url"])
        )
        for row in ordered:
            row["sort"] = [row["_source"]["url"]]

        def swap_first_two_pages(response, start, _scan):
            if start in (0, projection.PAGE_SIZE):
                other = projection.PAGE_SIZE - start
                response["hits"]["hits"] = copy.deepcopy(
                    ordered[other : other + projection.PAGE_SIZE]
                )

        self.client.search_fault = swap_first_two_pages
        with self.assertRaisesRegex(
            projection.ProjectionError, "catalog_projection_inventory_changed"
        ):
            self.provision()
        self.assertEqual([], self.writes())

    def test_server_object_type_omission_passes_provision_repeat_and_readonly_gates(
        self,
    ):
        for omitted in (False, True):
            with self.subTest(omitted=omitted):
                self.client = ProjectionClient(self.bundle)

                def server_mapping(value):
                    field = value[INDEX]["mappings"]["properties"].get(filters.FIELD)
                    if omitted and field is not None:
                        field.pop("type", None)

                self.client.mapping_fault = server_mapping
                first = self.provision()
                self.assertEqual("ready", first["status"])
                self.assertEqual(101, first["projection_updated_count"])
                self.client.calls.clear()
                repeated = self.provision()
                verified = projection.verify_projection(self.client, self.bundle, INDEX)
                vectors = projection.verify_vectors(self.client, self.bundle, INDEX)
                self.assertEqual(0, repeated["projection_updated_count"])
                self.assertEqual(0, verified["projection_updated_count"])
                self.assertEqual([], self.writes())
                for report in (repeated, verified, vectors):
                    self.assertEqual("ready", report["status"])
                    self.assertEqual(first["vectors_sha256"], report["vectors_sha256"])

    def test_existing_null_or_nonobject_condition_mapping_fails_before_writes(self):
        for value in (None, False, True, 1, [], "object"):
            with self.subTest(value=value):
                self.client = ProjectionClient(self.bundle)
                self.client.mapping[INDEX]["mappings"]["properties"][filters.FIELD] = (
                    value
                )
                with self.assertRaisesRegex(
                    projection.ProjectionError, "catalog_projection_mapping_changed"
                ):
                    self.provision()
                self.assertEqual([], self.writes())

    def test_partial_known_projection_can_resume_without_overwriting(self):
        self.client.mapping[INDEX]["mappings"]["properties"].update(
            filters.mapping()["properties"]
        )
        row = self.client.rows[0]
        row["_source"][filters.FIELD] = filters.metadata(
            self.bundle.condition_metadata[row["_source"]["dataset_id"]],
            self.bundle.bundle_id,
        )
        report = self.provision()
        self.assertEqual(100, report["projection_updated_count"])
        updates = "".join(
            p for _, path, p, _ in self.client.calls if path.endswith("/_bulk")
        )
        self.assertNotIn('"_id": "native-0"', updates)

    def test_readonly_verify_requires_projection_and_issues_no_writes(self):
        with self.assertRaises(projection.ProjectionError):
            projection.verify_projection(self.client, self.bundle, INDEX)
        self.provision()
        self.client.calls.clear()
        projection.verify_projection(self.client, self.bundle, INDEX)
        self.assertEqual([], self.writes())
        self.assertTrue(
            all(
                path.endswith(("/_mapping", "/_search"))
                for _, path, _, _ in self.client.calls
            )
        )

    def test_mapping_rejects_alias_fanout_wrong_dimensions_and_unknown_engine(self):
        for fault in (
            lambda value: value.update(
                {"fess.20261008120001": copy.deepcopy(value[INDEX])}
            ),
            lambda value: value[INDEX]["mappings"]["properties"][filters.VECTOR_PATH][
                "properties"
            ]["vector"].update(dimension=767),
            lambda value: value[INDEX]["mappings"]["properties"][filters.VECTOR_PATH][
                "properties"
            ]["vector"].update(dimension=True),
            lambda value: value[INDEX]["mappings"]["properties"][filters.VECTOR_PATH][
                "properties"
            ]["vector"]["method"].update(engine="unknown"),
        ):
            self.client = ProjectionClient(self.bundle)
            self.client.mapping_fault = fault
            with (
                self.subTest(fault=fault),
                self.assertRaises(projection.ProjectionError),
            ):
                self.provision()
            self.assertEqual([], self.writes())

    def test_missing_bad_done_vectors_fail_before_any_write(self):
        for change in (
            {"content_chunk_status": "pending"},
            {filters.VECTOR_PATH: []},
            {filters.VECTOR_PATH: [{"vector": [0.0] * 768}]},
            {filters.VECTOR_PATH: [{"vector": [1.0] * 767}]},
            {filters.VECTOR_PATH: [{"vector": [True] * 768}]},
            {filters.VECTOR_PATH: [{"vector": [float("nan")] * 768}]},
            {filters.VECTOR_PATH: [{"vector": [float("inf")] * 768}]},
            {filters.VECTOR_PATH: [{"vector": [10**1000] * 768}]},
        ):
            self.client = ProjectionClient(self.bundle)
            self.client.rows[-1]["_source"].update(change)
            with (
                self.subTest(change=next(iter(change))),
                self.assertRaises(projection.ProjectionError),
            ):
                self.provision()
            self.assertEqual([], self.writes())

    def test_url_hash_bundle_acl_foreign_ids_and_duplicates_fail_before_write(self):
        for change in (
            {"url": "https://evil.invalid/secret"},
            {"depositar_document_sha256": "0" * 64},
            {"depositar_bundle_id": "0" * 64},
            {"role": ["admin"]},
            {"dataset_id": "foreign"},
            {"dataset_id": self.client.rows[0]["_source"]["dataset_id"]},
        ):
            self.client = ProjectionClient(self.bundle)
            self.client.rows[-1]["_source"].update(change)
            with (
                self.subTest(change=next(iter(change))),
                self.assertRaises(projection.ProjectionError),
            ):
                self.provision()
            self.assertEqual([], self.writes())

    def test_native_duplicate_and_absent_concurrency_fields_fail_before_write(self):
        for mutate in (
            lambda row: row.update(_id="native-0"),
            lambda row: row.pop("_seq_no"),
            lambda row: row.update(_primary_term=True),
        ):
            self.client = ProjectionClient(self.bundle)
            mutate(self.client.rows[-1])
            with self.assertRaises(projection.ProjectionError):
                self.provision()
            self.assertEqual([], self.writes())

    def test_partial_pages_totals_shards_and_timeouts_cannot_be_success(self):
        for fault in (
            lambda value: value["hits"]["total"].update(relation="gte"),
            lambda value: value["hits"]["total"].update(value=102),
            lambda value: value["hits"]["hits"].pop(),
            lambda value: value.update(timed_out=True),
            lambda value: value.update(terminated_early=True),
            lambda value: value["_shards"].update(failed=1),
            lambda value: value["_shards"].update(failed=False),
        ):
            self.client = ProjectionClient(self.bundle)
            self.client.search_fault = lambda value, _start, _scan: fault(value)
            with (
                self.subTest(fault=fault),
                self.assertRaises(projection.ProjectionError),
            ):
                self.provision()
            self.assertEqual([], self.writes())

    def test_bulk_every_item_must_confirm_the_expected_update(self):
        for fault in (
            lambda value: value.update(errors=True),
            lambda value: value["items"].pop(),
            lambda value: value["items"][0]["update"].update(status=409),
            lambda value: value["items"][0]["update"].update(_id="foreign"),
            lambda value: value["items"][0]["update"].update(
                error={"reason": "sensitive"}
            ),
            lambda value: value["items"][0]["update"]["_shards"].update(failed=1),
        ):
            self.client = ProjectionClient(self.bundle)
            self.client.bulk_fault = fault
            with (
                self.subTest(fault=fault),
                self.assertRaises(projection.ProjectionError),
            ):
                self.provision()

    def test_postscan_detects_omitted_projection_and_vector_changes(self):
        for mutate in (
            lambda rows: rows[0]["_source"].pop(filters.FIELD, None),
            lambda rows: rows[0]["_source"][filters.VECTOR_PATH][0][
                "vector"
            ].__setitem__(0, 2.0),
        ):
            self.client = ProjectionClient(self.bundle)
            self.client.after_bulk = mutate
            with self.assertRaises(projection.ProjectionError):
                self.provision()

    def test_mapping_ack_and_refresh_must_be_confirmed(self):
        self.client.mapping_ack = False
        with self.assertRaises(projection.ProjectionError):
            self.provision()
        self.client = ProjectionClient(self.bundle)
        self.client.refresh_fault = lambda value: value["_shards"].update(failed=1)
        with self.assertRaises(projection.ProjectionError):
            self.provision()

    def test_existing_wrong_projection_is_not_silently_replaced(self):
        row = self.client.rows[0]
        expected = filters.metadata(
            self.bundle.condition_metadata[row["_source"]["dataset_id"]],
            self.bundle.bundle_id,
        )
        row["_source"][filters.FIELD] = {**expected, "record": 1}
        with self.assertRaises(projection.ProjectionError):
            self.provision()
        self.assertEqual([], self.writes())

    def test_transport_output_and_credentials_do_not_reach_exception(self):
        class Broken:
            def request(self, *_args, **_kwargs):
                raise OSError("administrator-password-and-remote-body")

        with self.assertRaises(projection.ProjectionError) as error:
            projection.provision_projection(Broken(), self.bundle)
        self.assertNotIn("administrator-password", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_document_limit_is_checked_before_transport(self):
        bundle = replace(self.bundle, documents=self.bundle.documents * 100)
        with self.assertRaises(projection.ProjectionError):
            projection.provision_projection(self.client, bundle)
        self.assertEqual([], self.client.calls)

    def test_no_legacy_4000_result_cap(self):
        with tempfile.TemporaryDirectory(
            prefix="depositar-projection-large-"
        ) as temporary:
            bundle = make_bundle(Path(temporary), count=4001)
            client = ProjectionClient(bundle)
            report = projection.provision_projection(client, bundle)
        self.assertEqual(4001, report["document_count"])
        self.assertEqual(4001, report["projection_updated_count"])
        self.assertTrue(
            any(
                p.get("from") == 4000
                for _, path, p, _ in client.calls
                if path.endswith("/_search")
            )
        )


class CatalogVectorVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="depositar-vectors-readonly-"
        )
        cls.bundle = make_bundle(Path(cls.temporary.name))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.client = ProjectionClient(self.bundle)

    def verify(self, index=None):
        return projection.verify_vectors(self.client, self.bundle, index)

    def assert_readonly(self):
        self.assertTrue(
            all(
                method == "GET"
                and path.endswith("/_mapping")
                or method == "POST"
                and path.endswith("/_search")
                for method, path, _, _ in self.client.calls
            )
        )

    def reject(self, error=None):
        with self.assertRaises(projection.ProjectionError) as caught:
            self.verify()
        if error is not None:
            self.assertEqual(error, str(caught.exception))
        self.assert_readonly()

    def test_complete_unprojected_vectors_return_one_closed_sanitized_report(self):
        before = copy.deepcopy((self.client.mapping, self.client.rows))
        report = self.verify()
        self.assertEqual(
            {
                "schema_version",
                "status",
                "index",
                "bundle_id",
                "manifest_sha256",
                "document_count",
                "inventory_verified",
                "acl_verified",
                "vector_state_verified",
                "vector_dimension",
                "vector_chunk_count",
                "vectors_sha256",
                "inventory_sha256",
                "done_count",
                "pending_count",
                "after_state_sha256",
            },
            set(report),
        )
        self.assertEqual("ready", report["status"])
        self.assertEqual(INDEX, report["index"])
        self.assertEqual(self.bundle.bundle_id, report["bundle_id"])
        self.assertEqual(self.bundle.manifest_sha256, report["manifest_sha256"])
        self.assertEqual(101, report["document_count"])
        self.assertEqual(101, report["done_count"])
        self.assertEqual(0, report["pending_count"])
        self.assertEqual(768, report["vector_dimension"])
        self.assertEqual(101, report["vector_chunk_count"])
        for name in ("inventory_verified", "acl_verified", "vector_state_verified"):
            self.assertIs(report[name], True)
        encoded = private_io.canonical(report).decode()
        for forbidden in ("native-", "_source", "role", "projection_verified"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(before, (self.client.mapping, self.client.rows))
        self.assert_readonly()
        self.assertEqual(
            ["/fess.search/_mapping", "/fess.search/_mapping"],
            [path for method, path, _, _ in self.client.calls if method == "GET"],
        )
        self.assertEqual(
            [0, 50, 100],
            [
                payload["from"]
                for _, path, payload, _ in self.client.calls
                if path.endswith("/_search")
            ],
        )

    def test_complete_partition_digest_matches_fess_pass_and_durable_journal(self):
        report = self.verify()
        entries = self.bundle.expected_inventory_entries
        snapshot = VectorProgressSnapshot(
            InventorySnapshot(entries), done_entries=entries, pending_entries=()
        )
        self.assertEqual(snapshot.sha256, report["after_state_sha256"])
        self.assertEqual(state_sha256(snapshot.as_dict()), report["after_state_sha256"])
        self.assertEqual(
            hashlib.sha256(private_io.canonical(entries)).hexdigest(),
            report["inventory_sha256"],
        )

    def test_physical_index_is_bound_and_invalid_index_fails_before_transport(self):
        report = self.verify(INDEX)
        self.assertEqual(INDEX, report["index"])
        self.assertTrue(
            all(path.startswith("/" + INDEX) for _, path, _, _ in self.client.calls)
        )
        for index in ("fess.search", "fess.*", "", True, [], "fess.123"):
            with self.subTest(index=index):
                self.client.calls.clear()
                with self.assertRaisesRegex(
                    projection.ProjectionError, "catalog_projection_index_invalid"
                ):
                    self.verify(index)
                self.assertEqual([], self.client.calls)

    def test_alias_cannot_move_to_a_different_index_during_verification(self):
        reads = 0

        def move(value):
            nonlocal reads
            reads += 1
            if reads == 2:
                value["fess.20261008120001"] = value.pop(INDEX)

        self.client.mapping_fault = move
        self.reject("catalog_projection_index_changed")

    def test_mapping_fanout_wrong_dimension_or_unknown_engine_remains_rejected(self):
        for fault in (
            lambda value: value.update(
                {"fess.20261008120001": copy.deepcopy(value[INDEX])}
            ),
            lambda value: value[INDEX]["mappings"]["properties"][filters.VECTOR_PATH][
                "properties"
            ]["vector"].update(dimension=767),
            lambda value: value[INDEX]["mappings"]["properties"][filters.VECTOR_PATH][
                "properties"
            ]["vector"].update(dimension=True),
            lambda value: value[INDEX]["mappings"]["properties"][filters.VECTOR_PATH][
                "properties"
            ]["vector"]["method"].update(engine="unknown"),
        ):
            with self.subTest(fault=fault):
                self.client = ProjectionClient(self.bundle)
                self.client.mapping_fault = fault
                self.reject()

    def test_transport_exceptions_never_echo_private_response_or_credential(self):
        class Broken:
            def request(self, *_args, **_kwargs):
                raise OSError("private-administrator-password")

        with self.assertRaises(projection.ProjectionError) as caught:
            projection.verify_vectors(Broken(), self.bundle)
        self.assertEqual("catalog_projection_transport_failed", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_missing_pending_or_unknown_status_never_returns_complete(self):
        for status in (None, "pending", "failed", "skipped", True, "DONE"):
            with self.subTest(status=status):
                self.client = ProjectionClient(self.bundle)
                self.client.rows[-1]["_source"]["content_chunk_status"] = status
                self.reject("catalog_projection_vectors_incomplete")
        self.client = ProjectionClient(self.bundle)
        self.client.rows[-1]["_source"].pop("content_chunk_status")
        self.reject("catalog_projection_vectors_incomplete")

    def test_missing_empty_nonlist_or_over_limit_chunks_are_rejected(self):
        for chunks in (None, [], {}, [{"vector": [1.0] * 768}] * 1001):
            with self.subTest(type=type(chunks)):
                self.client = ProjectionClient(self.bundle)
                self.client.rows[-1]["_source"][filters.VECTOR_PATH] = chunks
                self.reject("catalog_projection_vectors_incomplete")
        self.client = ProjectionClient(self.bundle)
        self.client.rows[-1]["_source"].pop(filters.VECTOR_PATH)
        self.reject("catalog_projection_vectors_incomplete")

    def test_invalid_nonfinite_or_zero_vectors_are_rejected(self):
        for chunk in (
            None,
            {},
            {"vector": [1.0] * 767},
            {"vector": [1.0] * 769},
            {"vector": [0.0] * 768},
            {"vector": [True] * 768},
            {"vector": ["1"] * 768},
            {"vector": [float("nan")] * 768},
            {"vector": [float("inf")] * 768},
            {"vector": [-float("inf")] * 768},
            {"vector": [10**1000] * 768},
        ):
            with self.subTest(type=type(chunk)):
                self.client = ProjectionClient(self.bundle)
                self.client.rows[-1]["_source"][filters.VECTOR_PATH] = [chunk]
                self.reject("catalog_projection_vector_invalid")

    def test_partial_shards_missing_hits_and_nonexact_totals_are_rejected(self):
        for fault in (
            lambda value: value.pop("_shards"),
            lambda value: value["_shards"].update(successful=0),
            lambda value: value["_shards"].update(failed=1),
            lambda value: value["_shards"].update(failed=False),
            lambda value: value["hits"]["hits"].pop(),
            lambda value: value["hits"]["total"].update(value=100),
            lambda value: value["hits"]["total"].update(relation="gte"),
            lambda value: value.update(timed_out=True),
            lambda value: value.update(terminated_early=True),
        ):
            with self.subTest(fault=fault):
                self.client = ProjectionClient(self.bundle)
                self.client.search_fault = lambda value, _start, _scan: fault(value)
                self.reject()

    def test_foreign_duplicate_changed_hash_bundle_or_acl_documents_are_rejected(self):
        for change in (
            {"dataset_id": "foreign"},
            {"dataset_id": self.client.rows[0]["_source"]["dataset_id"]},
            {"url": "https://evil.invalid/private"},
            {"depositar_bundle_id": "0" * 64},
            {"depositar_document_sha256": "0" * 64},
            {"role": ["admin"]},
            {"label": []},
            {"virtual_host": []},
        ):
            with self.subTest(field=next(iter(change))):
                self.client = ProjectionClient(self.bundle)
                self.client.rows[-1]["_source"].update(change)
                self.reject("catalog_projection_inventory_changed")

    def test_existing_projection_must_still_match_without_being_required(self):
        self.client.mapping[INDEX]["mappings"]["properties"].update(
            filters.mapping()["properties"]
        )
        row = self.client.rows[0]
        row["_source"][filters.FIELD] = filters.metadata(
            self.bundle.condition_metadata[row["_source"]["dataset_id"]],
            self.bundle.bundle_id,
        )
        self.verify()
        row["_source"][filters.FIELD]["record"] = 1
        self.reject("catalog_projection_document_changed")

    def test_vector_digest_binds_all_chunks_but_not_transport_row_order(self):
        first = self.verify()
        self.client.rows.reverse()
        self.assertEqual(first, self.verify())
        self.client.rows[0]["_source"][filters.VECTOR_PATH].append(
            {"vector": [-1.0] + [0.0] * 767}
        )
        changed = self.verify()
        self.assertEqual(102, changed["vector_chunk_count"])
        self.assertNotEqual(first["vectors_sha256"], changed["vectors_sha256"])
        self.assertEqual(first["after_state_sha256"], changed["after_state_sha256"])
        self.assert_readonly()

    def test_invalid_bundle_is_rejected_before_transport(self):
        for bundle in (
            None,
            replace(self.bundle, documents=self.bundle.documents * 100),
        ):
            with (
                self.subTest(type=type(bundle)),
                self.assertRaises(projection.ProjectionError),
            ):
                projection.verify_vectors(self.client, bundle)
        self.assertEqual([], self.client.calls)


if __name__ == "__main__":
    unittest.main()
