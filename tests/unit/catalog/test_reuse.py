"""Cross-catalog reuse with real sealed bundles, private records and fake HTTP."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.catalog import embedding
from search.catalog.build import builder
from search.catalog.bundle import CatalogBundle
from search.operations.catalog import chunks, connection, execution, projection, reuse
from search.operations.shared import private_io
from tests.unit.catalog.test_projection import INDEX, ProjectionClient
from tests.unit.catalog.test_vectors import host_context
from tests.unit.operations.test_metadata_sync import dataset


PROFILE = {
    "schema_version": 1,
    "model": embedding.MODEL,
    "model_manifest_sha256": embedding.MODEL_DIGEST,
    "dimensions": embedding.DIMENSIONS,
    "native_chunk_profile": chunks.PROFILE,
    "fess_build_arguments": {"FESS_BASE_IMAGE": "fixture-fess"},
    "fess_environment": {"FESS_JAVA_OPTS": " ".join(chunks.JAVA_OPTIONS)},
    "fess_sources": {name: "3" * 64 for name in reuse.PROFILE_FILES},
    "embedding_image": "fixture-embedding-image",
    "embedding_environment": {"CUDA_VISIBLE_DEVICES": "-1"},
}
SOURCE_REVISION = "a" * 40
TARGET_REVISION = "b" * 40
NATIVE_FIELDS = {"content", "content_chunk_vector", "content_chunk_status"}


def write_record(root, name, value):
    descriptor = private_io.open_directory(root)
    try:
        private_io.write_json(
            descriptor, name, value, replace=True, max_bytes=8 * 1024**2
        )
    finally:
        os.close(descriptor)


class VectorReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-vector-reuse-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalogs = self.root / "catalogs"
        self.catalogs.mkdir(mode=0o700)
        self.profiles = {}
        self.addCleanup(patch.stopall)
        patch.object(
            reuse,
            "_profile",
            side_effect=lambda context: self.profiles.get(
                context["plan"]["revision"], PROFILE
            ),
        ).start()
        patch.object(connection, "materials", return_value=("a" * 64, b"ca")).start()
        patch.object(
            connection,
            "peers",
            return_value={"opensearch-search": "172.19.0.3"},
        ).start()
        self.source_rows = [dataset(1), dataset(2)]
        self.source = self.context("source", self.source_rows, SOURCE_REVISION)
        self.source_client = self.client(self.source, done=True)

    def context(self, label, rows, revision):
        inputs = self.root / label
        inputs.mkdir(mode=0o700)
        snapshot = inputs / "snapshot.jsonl"
        snapshot.write_text("".join(json.dumps(row) + "\n" for row in rows))
        bundles = inputs / "bundles"
        bundles.mkdir(mode=0o700)
        report = builder.build_bundle(snapshot, bundles)
        bundle = CatalogBundle.load(
            Path(report["bundle_path"]),
            expected_manifest_sha256=report["manifest_sha256"],
        )
        slot = self.catalogs / (bundle.bundle_id[:12] + "-" + revision[:12])
        slot.mkdir(mode=0o700)
        context = host_context(slot, bundle, bundle.manifest_sha256, len(rows))
        context["plan"].update(
            revision=revision,
            project="depositar-ai-catalog-" + slot.name,
            bundle_path=report["bundle_path"],
            work_root=str(slot),
            data_root=str(slot),
            source_sha256=PROFILE["fess_sources"],
        )
        context.update(repo=self.root, source_revision=revision)
        profile = self.profiles.get(revision, PROFILE)
        context["compose"] = {
            "services": {
                "fess-search": {
                    "build": {"args": profile["fess_build_arguments"]},
                    "environment": profile["fess_environment"],
                },
                "ollama-embedding": {
                    "image": profile["embedding_image"],
                    "environment": profile["embedding_environment"],
                },
            }
        }
        context["plan"]["compose_sha256"] = execution.digest_record(context["compose"])
        write_record(slot, "compose.json", context["compose"])
        write_record(slot, "plan.json", context["plan"])
        return context

    def client(self, context, *, done):
        client = ProjectionClient(context["bundle"])
        documents = {row["dataset_id"]: row for row in context["bundle"].documents}
        for row in client.rows:
            source = row["_source"]
            document = documents[source["dataset_id"]]
            source.update(
                content=[document["content"]] if done else document["content"],
                metadata_modified=document["metadata_modified"],
                target_marker=context["plan"]["revision"],
            )
            if not done:
                source.pop("content_chunk_status", None)
                source.pop("content_chunk_vector", None)
        return client

    def ready(self, context, scan, *, run_id="1" * 32):
        root = context["root"]
        query = root / "query-access"
        query.mkdir(mode=0o700, exist_ok=True)
        history = root / "query-operations"
        history.mkdir(mode=0o700, exist_ok=True)
        run = history / run_id
        run.mkdir(mode=0o700, exist_ok=True)
        profile = self.profiles.get(context["plan"]["revision"], PROFILE)
        vectors = root / "vector-operations"
        vectors.mkdir(mode=0o700, exist_ok=True)
        recipe = {
            **execution.catalog_identity(context),
            "embedding_model": profile["model"],
            "embedding_manifest_sha256": profile["model_manifest_sha256"],
            "dimensions": profile["dimensions"],
        }
        write_record(vectors, "recipe.json", recipe)
        identity = {
            **execution.catalog_identity(context),
            "index": scan["index"],
            "vectors_sha256": scan["vectors_sha256"],
            "inventory_sha256": scan["inventory_sha256"],
            "recipe_sha256": execution.digest_record(recipe),
        }
        started = {"identity": identity, "run_id": run_id}
        complete = {
            "started_sha256": execution.digest_record(started),
            "reports": {
                "projection": {"vectors_sha256": scan["vectors_sha256"]},
                "access": {},
            },
        }
        write_record(history, "identity.json", identity)
        write_record(run, "started.json", started)
        write_record(run, "complete.json", complete)
        ready = {
            "identity": identity,
            "run_id": run_id,
            "complete_sha256": execution.digest_record(complete),
        }
        write_record(query, "ready.json", ready)
        return ready

    def export(self, context=None, client=None, *, run_id="1" * 32):
        context = self.source if context is None else context
        client = self.source_client if client is None else client
        scan = projection.verify_vectors(client, context["bundle"])
        ready = self.ready(context, scan, run_id=run_id)
        return reuse.export(context, client, scan, ready)

    def seed(self, context, client):
        def factory(peer, ca, admin):
            self.assertEqual(("172.19.0.3", b"ca", "a" * 64), (peer, ca, admin))
            return client

        return reuse.seed(
            context,
            {},
            {},
            opensearch_factory=factory,
        )

    def target(self, *, changed=False, all_hits=False):
        rows = [dataset(1, metadata_modified="2026-10-29T00:00:00")]
        rows.append(
            dataset(2, notes="Changed discovery text") if changed else dataset(2)
        )
        if not all_hits:
            rows.append(dataset(3))
        context = self.context("target", rows, TARGET_REVISION)
        return context, self.client(context, done=False)

    def bulk(self, client):
        return [
            call
            for call in client.calls
            if call[:2] == ("POST", "/" + INDEX + "/_bulk")
        ]

    def test_mixed_reuse_keeps_new_metadata_and_only_writes_native_vector_fields(self):
        self.export()
        context, client = self.target(changed=True)
        before = copy.deepcopy(client.rows)
        source_before = copy.deepcopy(self.source_client.rows)
        source_calls = len(self.source_client.calls)
        self.seed(context, client)
        self.assertEqual("done", client.rows[0]["_source"]["content_chunk_status"])
        self.assertNotIn("content_chunk_status", client.rows[1]["_source"])
        self.assertNotIn("content_chunk_status", client.rows[2]["_source"])
        for old, new in zip(before, client.rows):
            self.assertEqual(
                {k: v for k, v in old["_source"].items() if k not in NATIVE_FIELDS},
                {k: v for k, v in new["_source"].items() if k not in NATIVE_FIELDS},
            )
        for call in self.bulk(client):
            lines = call[2].splitlines()
            for offset in range(1, len(lines), 2):
                self.assertEqual(NATIVE_FIELDS, set(json.loads(lines[offset])["doc"]))
        self.assertEqual(source_before, self.source_client.rows)
        self.assertEqual(source_calls, len(self.source_client.calls))

    def test_new_bundle_project_and_revision_do_not_prevent_all_unchanged_content_hits(
        self,
    ):
        self.export()
        context, client = self.target(all_hits=True)
        self.assertNotEqual(
            self.source["bundle"].bundle_id, context["bundle"].bundle_id
        )
        self.seed(context, client)
        self.assertTrue(
            all(row["_source"]["content_chunk_status"] == "done" for row in client.rows)
        )

    def assert_profile_miss(self, field, changed):
        self.export()
        self.profiles[TARGET_REVISION] = {**PROFILE, field: changed}
        context, client = self.target(all_hits=True)
        before = copy.deepcopy(client.rows)
        self.seed(context, client)
        self.assertEqual(before, client.rows)
        self.assertEqual([], self.bulk(client))

    def test_changed_logical_model_is_a_cache_miss(self):
        self.assert_profile_miss("model_manifest_sha256", "9" * 64)

    def test_changed_chunk_profile_is_a_cache_miss(self):
        self.assert_profile_miss(
            "native_chunk_profile", {**chunks.PROFILE, "length.chunk_size": "400"}
        )

    def test_completed_seed_replay_does_not_write_again(self):
        self.export()
        context, client = self.target(all_hits=True)
        self.seed(context, client)
        before = len(self.bulk(client))
        calls = len(client.calls)
        self.seed(context, client)
        self.assertEqual(before, len(self.bulk(client)))
        self.assertTrue(
            all(
                call[1].endswith(("/_mapping", "/_search"))
                for call in client.calls[calls:]
            )
        )

    def test_export_publishes_readonly_records_and_manifest(self):
        self.export()
        files = list((self.source["root"] / "vector-cache").iterdir())
        self.assertTrue(any(path.name == "manifest.json" for path in files))
        for path in files:
            self.assertEqual(
                0o600 if "started" in path.name else 0o400, path.stat().st_mode & 0o777
            )

    def test_later_query_ready_receipt_does_not_rewrite_original_cache_provenance(self):
        self.export()
        manifest = self.source["root"] / "vector-cache/manifest.json"
        before = manifest.read_bytes()
        self.export(run_id="2" * 32)
        self.assertEqual(before, manifest.read_bytes())

    def test_corrupt_published_record_fails_before_target_writes(self):
        self.export()
        root = self.source["root"] / "vector-cache"
        record = next(path for path in root.iterdir() if len(path.stem) == 64)
        raw = record.read_bytes()
        record.chmod(0o600)
        record.write_bytes(raw + b" ")
        record.chmod(0o400)
        context, client = self.target(all_hits=True)
        with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
            self.seed(context, client)
        self.assertEqual([], self.bulk(client))

    def test_source_query_history_tampering_cannot_authorize_cache(self):
        self.export()
        complete = (
            self.source["root"] / "query-operations" / ("1" * 32) / "complete.json"
        )
        value = json.loads(complete.read_bytes())
        value["reports"]["projection"]["vectors_sha256"] = "0" * 64
        write_record(complete.parent, complete.name, value)
        context, client = self.target(all_hits=True)
        with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
            self.seed(context, client)
        self.assertEqual([], self.bulk(client))

    def test_partial_bulk_response_keeps_intent_and_retry_observes_applied_values(self):
        self.export()
        context, client = self.target(all_hits=True)
        client.bulk_fault = lambda response: response.update(errors=True)
        with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
            self.seed(context, client)
        first_writes = len(self.bulk(client))
        client.bulk_fault = None
        self.seed(context, client)
        self.assertEqual(1, first_writes)
        written_ids = [
            json.loads(call[2].splitlines()[0])["update"]["_id"]
            for call in self.bulk(client)
        ]
        self.assertEqual(len(client.rows), len(written_ids))
        self.assertEqual(len(written_ids), len(set(written_ids)))
        self.assertTrue(
            all(row["_source"]["content_chunk_status"] == "done" for row in client.rows)
        )

    def test_source_manifest_hash_covers_canonical_record_bytes(self):
        self.export()
        root = self.source["root"] / "vector-cache"
        manifest = json.loads((root / "manifest.json").read_bytes())
        for entry in manifest["entries"]:
            digest = entry["record_sha256"]
            if digest is not None:
                self.assertEqual(
                    digest,
                    hashlib.sha256(
                        (root / (digest + ".json")).read_bytes()
                    ).hexdigest(),
                )

    def test_oversized_cache_records_are_skipped_without_truncation(self):
        with patch.object(reuse, "MAX_ENTRY_BYTES", 500):
            result = self.export()
            root = self.source["root"] / "vector-cache"
            manifest = json.loads((root / "manifest.json").read_bytes())
            self.assertEqual(0, result["cached_documents"])
            self.assertEqual(2, result["skipped_size_limit_documents"])
            self.assertTrue(
                all(entry["record_sha256"] is None for entry in manifest["entries"])
            )
            self.assertEqual(
                {"manifest.json", "export-started.json"},
                {path.name for path in root.iterdir()},
            )
            context, client = self.target(all_hits=True)
            self.seed(context, client)
            self.assertEqual([], self.bulk(client))

    def test_total_export_budget_skips_remaining_records_without_partial_content(self):
        self.export()
        source_root = self.source["root"] / "vector-cache"
        sizes = sorted(
            path.stat().st_size
            for path in source_root.iterdir()
            if len(path.stem) == 64
        )
        self.assertEqual(2, len(sizes))
        budget = sum(sizes) - 1
        context = self.context("limited", self.source_rows, "c" * 40)
        client = self.client(context, done=True)
        with patch.object(reuse, "MAX_EXPORT_BYTES", budget):
            result = self.export(context, client)
            root = context["root"] / "vector-cache"
            manifest = json.loads((root / "manifest.json").read_bytes())
            self.assertEqual(1, result["cached_documents"])
            self.assertEqual(1, result["skipped_size_limit_documents"])
            self.assertLessEqual(manifest["record_bytes"], budget)
            self.assertEqual(
                manifest["record_bytes"],
                sum(
                    path.stat().st_size
                    for path in root.iterdir()
                    if len(path.stem) == 64
                ),
            )

    def test_duplicate_content_records_share_one_export_budget_allocation(self):
        rows = [
            dataset(10, name="a", title="b c", notes="shared"),
            dataset(11, name="a b", title="c", notes="shared"),
        ]
        control = self.context("single-control", rows[:1], "d" * 40)
        self.export(control, self.client(control, done=True))
        control_root = control["root"] / "vector-cache"
        record = next(path for path in control_root.iterdir() if len(path.stem) == 64)
        budget = record.stat().st_size
        context = self.context("same-content", rows, "c" * 40)
        self.assertEqual(
            1, len({row["content"] for row in context["bundle"].documents})
        )
        with patch.object(reuse, "MAX_EXPORT_BYTES", budget):
            result = self.export(context, self.client(context, done=True))
            root = context["root"] / "vector-cache"
            manifest = json.loads((root / "manifest.json").read_bytes())
            self.assertEqual(2, result["cached_documents"])
            self.assertEqual(0, result["skipped_size_limit_documents"])
            self.assertEqual(budget, manifest["record_bytes"])
            self.assertEqual(
                1, len({entry["record_sha256"] for entry in manifest["entries"]})
            )

    def test_failed_export_scan_never_publishes_a_manifest(self):
        scan = projection.verify_vectors(self.source_client, self.source["bundle"])
        ready = self.ready(self.source, scan)
        self.source_client.search_fault = (
            lambda response, _start, _scan: response.update(timed_out=True)
        )
        with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
            reuse.export(self.source, self.source_client, scan, ready)
        self.assertFalse((self.source["root"] / "vector-cache/manifest.json").exists())

    def test_export_count_bound_is_checked_before_manifest_publication(self):
        with patch.object(reuse, "MAX_RECORDS", 1):
            with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
                self.export()
        self.assertFalse((self.source["root"] / "vector-cache/manifest.json").exists())

    def test_interrupted_export_reuses_immutable_records_before_publishing_manifest(
        self,
    ):
        scan = projection.verify_vectors(self.source_client, self.source["bundle"])
        ready = self.ready(self.source, scan)
        mapping_reads = []

        def change_after_capture(response):
            mapping_reads.append(True)
            if len(mapping_reads) == 2:
                response.clear()

        self.source_client.mapping_fault = change_after_capture
        with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
            reuse.export(self.source, self.source_client, scan, ready)
        root = self.source["root"] / "vector-cache"
        before = {
            path.name: (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
            for path in root.iterdir()
            if len(path.stem) == 64
        }
        self.assertEqual(2, len(before))
        self.assertFalse((root / "manifest.json").exists())
        self.source_client.mapping_fault = None
        self.export()
        self.assertTrue((root / "manifest.json").exists())
        self.assertEqual(
            before,
            {
                name: (
                    (root / name).stat().st_ino,
                    (root / name).stat().st_mtime_ns,
                    (root / name).read_bytes(),
                )
                for name in before
            },
        )

    def test_existing_vector_history_prevents_new_seeding(self):
        self.export()
        context, client = self.target(all_hits=True)
        history = context["root"] / "vector-operations"
        history.mkdir(mode=0o700)
        (history / ("3" * 32)).mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
            self.seed(context, client)
        self.assertEqual([], self.bulk(client))

    def test_manifest_numbers_require_exact_integer_types(self):
        self.export()
        path = self.source["root"] / "vector-cache/manifest.json"
        original = json.loads(path.read_bytes())
        context, client = self.target(all_hits=True)
        for field in (
            "schema_version",
            "cached_count",
            "skipped_size_limit_count",
            "record_bytes",
        ):
            value = copy.deepcopy(original)
            value[field] = True if field == "schema_version" else float(value[field])
            path.chmod(0o600)
            path.write_bytes(private_io.canonical(value))
            path.chmod(0o400)
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"),
            ):
                self.seed(context, client)
        self.assertEqual([], self.bulk(client))

    def test_rehashed_logical_profile_cannot_relabel_the_source_model(self):
        self.export()
        root = self.source["root"] / "vector-cache"
        manifest = json.loads((root / "manifest.json").read_bytes())
        intent = json.loads((root / "export-started.json").read_bytes())
        profile = {**manifest["profile"], "model_manifest_sha256": "f" * 64}
        for value in (manifest, intent):
            value.update(
                profile=profile, profile_sha256=execution.digest_record(profile)
            )
        path = root / "manifest.json"
        path.chmod(0o600)
        path.write_bytes(private_io.canonical(manifest))
        path.chmod(0o400)
        write_record(root, "export-started.json", intent)
        context, client = self.target(all_hits=True)
        with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
            self.seed(context, client)
        self.assertEqual([], self.bulk(client))

    def test_record_boolean_version_is_rejected_even_with_updated_record_hash(self):
        self.export()
        root = self.source["root"] / "vector-cache"
        manifest = json.loads((root / "manifest.json").read_bytes())
        entry = manifest["entries"][0]
        old_path = root / (entry["record_sha256"] + ".json")
        old_size = old_path.stat().st_size
        value = json.loads(old_path.read_bytes())
        value["schema_version"] = True
        raw = private_io.canonical(value)
        digest = hashlib.sha256(raw).hexdigest()
        new_path = root / (digest + ".json")
        new_path.write_bytes(raw)
        new_path.chmod(0o400)
        old_path.unlink()
        entry["record_sha256"] = digest
        manifest["record_bytes"] += len(raw) - old_size
        path = root / "manifest.json"
        path.chmod(0o600)
        path.write_bytes(private_io.canonical(manifest))
        path.chmod(0o400)
        context, client = self.target(all_hits=True)
        with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
            self.seed(context, client)
        self.assertEqual([], self.bulk(client))

    def test_corrupt_partial_seed_intent_does_not_authorize_remaining_writes(self):
        self.export()
        context, client = self.target(all_hits=True)
        client.bulk_fault = lambda response: response.update(errors=True)
        with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
            self.seed(context, client)
        before = len(self.bulk(client))
        root = context["root"] / "vector-reuse"
        intent = json.loads((root / "started.json").read_bytes())
        intent["schema_version"] = True
        write_record(root, "started.json", intent)
        client.bulk_fault = None
        with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
            self.seed(context, client)
        self.assertEqual(before, len(self.bulk(client)))

    def test_missing_manifest_entry_cannot_hide_source_catalog_documents(self):
        self.export()
        path = self.source["root"] / "vector-cache/manifest.json"
        value = json.loads(path.read_bytes())
        value["entries"].pop()
        path.chmod(0o600)
        path.write_bytes(private_io.canonical(value))
        path.chmod(0o400)
        context, client = self.target(all_hits=True)
        with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
            self.seed(context, client)
        self.assertEqual([], self.bulk(client))

    def test_changed_target_acl_is_rejected_before_cache_writes(self):
        self.export()
        context, client = self.target(all_hits=True)
        client.rows[0]["_source"]["role"] = ["foreign-role"]
        with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
            self.seed(context, client)
        self.assertEqual([], self.bulk(client))

    def test_quarantined_source_catalog_is_a_cache_miss(self):
        self.export()
        manifest = self.source["root"] / "vector-cache/manifest.json"
        original = manifest.read_bytes()
        write_record(
            self.source["root"],
            "failure.json",
            {"schema_version": 1, "reason": "catalog_index_identity_changed"},
        )
        context, client = self.target(all_hits=True)
        before = copy.deepcopy(client.rows)
        result = self.seed(context, client)
        self.assertEqual(0, result["reused_documents"])
        self.assertEqual(before, client.rows)
        self.assertEqual([], self.bulk(client))
        self.assertEqual(original, manifest.read_bytes())

    def test_completed_zero_hit_plan_does_not_add_later_available_donors(self):
        context, client = self.target(all_hits=True)
        first = self.seed(context, client)
        self.assertEqual(0, first["reused_documents"])
        before = copy.deepcopy(client.rows)
        self.export()
        second = self.seed(context, client)
        self.assertEqual(0, second["reused_documents"])
        self.assertEqual(before, client.rows)
        self.assertEqual([], self.bulk(client))

    def test_concurrent_target_metadata_drift_prevents_completion(self):
        self.export()
        context, client = self.target(all_hits=True)
        client.after_bulk = lambda rows: rows[0]["_source"].update(
            metadata_modified="changed-during-write"
        )
        with self.assertRaisesRegex(ValueError, "catalog_vector_reuse_"):
            self.seed(context, client)
        self.assertFalse((context["root"] / "vector-reuse/complete.json").exists())


if __name__ == "__main__":
    unittest.main()
