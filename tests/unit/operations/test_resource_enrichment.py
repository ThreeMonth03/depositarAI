"""Grounded resource context and explicit selection across metadata cache hits."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.catalog.bundle import CatalogBundle
from search.operations.documents import enrichment
from search.operations.metadata import sync as metadata_sync
from search.operations.shared import private_io


DATASET = "00000000-0000-4000-8000-000000000001"
RESOURCE = "11111111-1111-4111-8111-111111111111"


class ResourceEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.cache.mkdir(mode=0o700)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir(mode=0o700)
        self.rows = [
            {
                "id": DATASET,
                "name": "water",
                "title": "Water observations",
                "private": False,
                "state": "active",
                "type": "dataset",
                "resources": [
                    {
                        "id": RESOURCE,
                        "name": "Water samples",
                        "format": "CSV",
                        "url": "https://data.depositar.io/water.csv",
                    }
                ],
            }
        ]
        self.fetches = 0
        self.base = self.sync()
        self.profile = self.make_profile()

    def fetch(self, start, count, _deadline):
        self.fetches += 1
        return {"count": len(self.rows), "results": self.rows[start : start + count]}

    def sync(self, **options):
        return metadata_sync.sync(self.cache, fetch=self.fetch, **options)

    def make_profile(self):
        content = hashlib.sha256(b"uniqueColumn42\nsecretValue\n").hexdigest()
        return {
            "schema_version": 1,
            "source": {
                "catalog_manifest_sha256": self.base["manifest_sha256"],
                "dataset_id": DATASET,
                "resource_id": RESOURCE,
                "url_sha256": hashlib.sha256(
                    self.rows[0]["resources"][0]["url"].encode()
                ).hexdigest(),
                "content_sha256": content,
                "bytes": 26,
                "acquired_at": 100,
                "complete": True,
            },
            "extractor": {"image_id": "sha256:" + "a" * 64, "source_sha256": "b" * 64},
            "policy": {"encoding": "utf-8", "header_policy": "first_record"},
            "result": {
                "schema_version": 1,
                "status": "ok",
                "detected_mime": "text/csv",
                "input_sha256": content,
                "columns": ["uniqueColumn42"],
                "text": "uniqueColumn42\nsecretValue",
                "text_truncated": False,
                "rows_observed": 1,
                "rows_complete": False,
                "may_require_ocr": False,
            },
        }

    def publish(self, profile=None):
        raw = private_io.canonical(profile or self.profile)
        path = self.profiles / (hashlib.sha256(raw).hexdigest() + ".json")
        path.write_bytes(raw)
        path.chmod(0o600)
        return path

    def load(self, paths):
        return enrichment.load_profiles(
            paths,
            self.base["bundle_path"],
            self.base["manifest_sha256"],
            self.base["snapshot_path"],
        )

    def test_import_binds_provenance_and_uses_headers_without_row_values(self):
        path = self.publish()
        with (
            patch(
                "search.operations.documents.source.fetch",
                side_effect=AssertionError("network"),
            ),
            patch(
                "search.operations.documents.extractor.extract",
                side_effect=AssertionError("parser"),
            ),
        ):
            contexts, report = self.load([path])
        entry = contexts[DATASET][0]
        self.assertEqual("uniqueColumn42", entry["index_text"])
        self.assertNotIn("secretValue", json.dumps(contexts))
        self.assertEqual(["uniqueColumn42"], entry["context"]["columns"])
        self.assertEqual(
            "https://data.depositar.io/dataset/water/resource/" + RESOURCE,
            entry["context"]["url"],
        )
        self.assertEqual(path.stem, entry["profile_sha256"])
        self.assertEqual(1, report["included_count"])

    def test_cache_hit_does_not_inherit_or_lose_an_explicit_profile_selection(self):
        path = self.publish()
        enriched = self.sync(ttl_seconds=3600, profile_files=[path])
        plain = self.sync(ttl_seconds=3600)
        again = self.sync(ttl_seconds=3600, profile_files=[path])
        self.assertEqual(2, self.fetches)
        self.assertTrue(all(item["cache_hit"] for item in (enriched, plain, again)))
        self.assertEqual(self.base["bundle_id"], plain["bundle_id"])
        self.assertNotEqual(enriched["bundle_id"], plain["bundle_id"])
        self.assertEqual(enriched["manifest_sha256"], again["manifest_sha256"])
        bundle = CatalogBundle.load(
            Path(again["bundle_path"]),
            expected_manifest_sha256=again["manifest_sha256"],
        )
        self.assertEqual(
            "uniqueColumn42",
            next(row for row in bundle.documents if row["dataset_id"] == DATASET)[
                "resource_context"
            ][0]["columns"][0],
        )

    def test_profile_source_and_result_mismatches_fail_closed(self):
        for key, value in (
            ("catalog_manifest_sha256", "c" * 64),
            ("dataset_id", RESOURCE),
            ("url_sha256", "c" * 64),
            ("content_sha256", "c" * 64),
            ("complete", 1),
            ("bytes", True),
            ("acquired_at", True),
        ):
            with self.subTest(key=key):
                profile = deepcopy(self.profile)
                profile["source"][key] = value
                with self.assertRaises(ValueError):
                    self.load([self.publish(profile)])

    def test_digest_modes_and_symlinks_are_rejected(self):
        path = self.publish()
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(enrichment.EnrichmentError):
            self.load([path])
        path = self.publish()
        path.chmod(0o644)
        with self.assertRaises(enrichment.EnrichmentError):
            self.load([path])
        path.chmod(0o600)
        link = self.profiles / ("f" * 64 + ".json")
        link.symlink_to(path)
        with self.assertRaises(enrichment.EnrichmentError):
            self.load([link])

    def test_changed_metadata_requires_a_new_profile_receipt_not_relabeling(self):
        self.rows[0]["title"] = "New catalog source"
        with self.assertRaises(enrichment.EnrichmentError):
            self.sync(profile_files=[self.publish()])

    def test_absent_header_empty_or_unusable_text_is_explicitly_skipped(self):
        for policy, columns, text in (
            ("absent", [], "secretValue"),
            ("first_record", ["<script>hidden()</script>"], "x"),
        ):
            with self.subTest(policy=policy, columns=columns):
                profile = deepcopy(self.profile)
                profile["policy"]["header_policy"] = policy
                profile["result"].update(columns=columns, text=text)
                contexts, report = self.load([self.publish(profile)])
                self.assertEqual({}, contexts)
                self.assertEqual(1, report["skipped_count"])

    def test_profile_cannot_be_applied_to_an_independent_snapshot(self):
        changed = deepcopy(self.rows)
        changed[0]["name"] = "different-catalog"
        changed[0]["resources"][0]["url"] = "https://data.depositar.io/different.csv"
        path = self.root / "different.jsonl"
        path.write_bytes(b"".join(private_io.canonical(row) for row in changed))
        path.chmod(0o600)
        with self.assertRaisesRegex(enrichment.EnrichmentError, "source_mismatch"):
            enrichment.load_profiles(
                [self.publish()],
                self.base["bundle_path"],
                self.base["manifest_sha256"],
                path,
            )

    def test_snapshot_replacement_after_import_cannot_change_the_staged_candidate(self):
        original = enrichment.load_profiles
        before = (self.cache / "latest.json").read_bytes()

        def mutate(*args):
            result = original(*args)
            snapshot_path = Path(args[3])
            changed = deepcopy(self.rows)
            changed[0]["resources"][0]["url"] = (
                "https://data.depositar.io/different.csv"
            )
            import gzip

            snapshot_path.chmod(0o600)
            snapshot_path.write_bytes(
                gzip.compress(b"".join(private_io.canonical(row) for row in changed))
            )
            snapshot_path.chmod(0o400)
            return result

        with patch.object(enrichment, "load_profiles", side_effect=mutate):
            with self.assertRaisesRegex(enrichment.EnrichmentError, "source_mismatch"):
                self.sync(ttl_seconds=3600, profile_files=[self.publish()])
        self.assertEqual(before, (self.cache / "latest.json").read_bytes())

    def test_duplicate_selection_and_catalog_mismatch_are_rejected(self):
        path = self.publish()
        for paths in ([path, path], [path] * 21):
            with self.assertRaises(enrichment.EnrichmentError):
                self.load(paths)

    def test_column_budget_redaction_and_truncation_are_bound_to_the_index(self):
        self.profile["result"].update(
            columns=["abc@example.test"] + ["x" * 128] * 31 + ["last"], text="rows"
        )
        contexts, _ = self.load([self.publish()])
        entry = contexts[DATASET][0]
        self.assertNotIn("@", json.dumps(entry["context"]))
        self.assertLessEqual(sum(map(len, entry["context"]["columns"])), 2048)
        self.assertTrue(entry["truncated"])
        self.assertEqual(" ".join(entry["context"]["columns"]), entry["index_text"])

    def test_empty_pdf_is_skipped_without_inferring_a_time_range(self):
        self.rows[0]["resources"][0]["format"] = "PDF"
        self.base = self.sync()
        self.profile = self.make_profile()
        self.profile["result"].update(
            status="empty_text",
            detected_mime="application/pdf",
            columns=[],
            text="",
            rows_observed=None,
            may_require_ocr=True,
        )
        contexts, report = self.load([self.publish()])
        self.assertEqual({}, contexts)
        self.assertEqual("empty_text", report["resources"][0]["reason"])

    def test_pdf_excerpt_is_bounded_and_grounded_in_normalized_index_text(self):
        self.rows[0]["resources"][0]["format"] = "PDF"
        self.base = self.sync()
        self.profile = self.make_profile()
        self.profile["result"].update(
            detected_mime="application/pdf",
            columns=[],
            text="<p>Water pdfToken42</p> " + "description " * 800,
            rows_observed=None,
        )
        contexts, report = self.load([self.publish()])
        entry = contexts[DATASET][0]
        self.assertEqual([], entry["context"]["columns"])
        self.assertEqual(entry["index_text"][:320], entry["context"]["excerpt"])
        self.assertNotIn("<p>", entry["index_text"])
        self.assertEqual(1, report["included_count"])
