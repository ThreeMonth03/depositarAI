"""Production metadata cache tests: real bundles, fake transport, no services."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.catalog.build.policy import BundleError
from search.catalog.bundle import CatalogBundle
from search.catalog.errors import CatalogError
from search.operations.metadata import sync as syncer
from search.operations.metadata import source, storage


def dataset(number=1, **changes):
    return {
        "id": f"00000000-0000-4000-8000-{number:012d}",
        "name": f"dataset-{number}",
        "title": f"Dataset {number}",
        "private": False,
        "state": "active",
        "type": "dataset",
        "notes": "Public dataset metadata",
        "resources": [],
        **changes,
    }


class CatalogSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="depositar-sync-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.cache.mkdir(mode=0o700)
        self.calls = []
        self.rows = [dataset(), dataset(2)]

    def fetch(self, start, rows, deadline):
        self.calls.append((start, rows, deadline))
        return {"count": len(self.rows), "results": self.rows[start : start + rows]}

    def sync(self, **options):
        return syncer.sync(self.cache, fetch=self.fetch, **options)

    def pointer(self):
        return self.cache / "latest.json"

    def baseline(self, report):
        return Path(report["bundle_path"]), report["manifest_sha256"]

    def test_first_sync_has_two_scans_and_a_sealed_cache_without_baseline(self):
        report = self.sync()
        self.assertEqual(2, len(self.calls))
        self.assertEqual(
            ("ready", 1, 2),
            (
                report["status"],
                report["schema_version"],
                report["document_count"],
            ),
        )
        self.assertEqual("initial_snapshot", report["diff"]["status"])
        self.assertFalse(report["unchanged"])
        self.assertFalse(report["cache_hit"])
        self.assertFalse(report["promotion_allowed"])
        self.assertFalse(report["live_index_modified"])
        self.assertFalse(report["models_called"])
        snapshot = Path(report["snapshot_path"])
        self.assertEqual(0o400, snapshot.stat().st_mode & 0o777)
        self.assertEqual(1, snapshot.stat().st_nlink)
        self.assertEqual(
            report["snapshot_sha256"], hashlib.sha256(snapshot.read_bytes()).hexdigest()
        )
        manifest = json.loads(
            (Path(report["bundle_path"]) / "bundle-manifest.json").read_text()
        )
        self.assertEqual(
            report["snapshot_sha256"],
            manifest["artifacts"]["source.jsonl.gz"]["sha256"],
        )
        self.assertEqual(report, json.loads(Path(report["receipt_path"]).read_text()))
        self.assertEqual(0o600, self.pointer().stat().st_mode & 0o777)

    def test_default_always_fetches_and_deduplicates_immutable_objects(self):
        first, second = self.sync(), self.sync()
        self.assertEqual(4, len(self.calls))
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["snapshot_path"], second["snapshot_path"])
        self.assertEqual(first["bundle_path"], second["bundle_path"])
        self.assertFalse(second["cache_hit"])
        self.assertFalse(second["bundle_created"])
        self.assertEqual(2, len(list((self.cache / "runs").iterdir())))
        self.assertEqual(1, len(list((self.cache / "snapshots").iterdir())))

    def test_unchanged_requires_complete_bundle_identity(self):
        first = self.sync()
        same = self.sync(baseline=self.baseline(first))
        self.assertTrue(same["unchanged"])
        self.rows[0] = dataset(unindexed_source_field="Changed upstream")
        changed = self.sync(baseline=self.baseline(first))
        self.assertEqual("no_search_change", changed["diff"]["status"])
        self.assertFalse(changed["unchanged"])
        self.assertNotEqual(first["bundle_id"], changed["bundle_id"])

    def test_explicit_ttl_revalidates_snapshot_and_bundle(self):
        first = self.sync()
        with patch.object(CatalogBundle, "load", wraps=CatalogBundle.load) as loader:
            second = self.sync(ttl_seconds=3600)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(2, len(self.calls))
        self.assertGreaterEqual(loader.call_count, 1)
        self.assertEqual(first["fetched_at"], second["fetched_at"])
        self.assertEqual(first["bundle_id"], second["bundle_id"])

    def test_force_and_expired_ttl_download_again(self):
        first = self.sync()
        self.sync(ttl_seconds=3600, force=True)
        self.assertEqual(4, len(self.calls))
        with patch.object(syncer.time, "time", return_value=first["fetched_at"] + 7200):
            self.assertFalse(self.sync(ttl_seconds=3600)["cache_hit"])
        self.assertEqual(6, len(self.calls))

    def test_offline_jsonl_is_validated_without_network_or_ttl(self):
        snapshot = self.root / "snapshot.jsonl"
        snapshot.write_bytes(
            b"".join(source.canonical(row) + b"\n" for row in reversed(self.rows))
        )
        with patch.object(source, "fetch_page", side_effect=AssertionError):
            report = syncer.sync(self.cache, snapshot=snapshot, ttl_seconds=3600)
        self.assertEqual("offline_not_checked", report["source_consistency"])
        self.assertFalse(report["cache_hit"])
        self.assertEqual(2, report["document_count"])

    def test_failed_download_preserves_previous_pointer_and_safe_failure_record(self):
        self.sync()
        previous = self.pointer().read_bytes()
        with self.assertRaises(OSError):
            syncer.sync(
                self.cache,
                fetch=lambda *args: (_ for _ in ()).throw(
                    OSError("remote-sensitive-text")
                ),
            )
        self.assertEqual(previous, self.pointer().read_bytes())
        failure = next((self.cache / "runs").glob("*/failure.json"))
        self.assertNotIn("remote-sensitive-text", failure.read_text())
        self.assertEqual("quarantined", json.loads(failure.read_text())["status"])

    def test_unstable_download_never_changes_latest(self):
        self.sync()
        previous = self.pointer().read_bytes()
        count = 0

        def unstable(start, rows, deadline):
            nonlocal count
            count += 1
            return {"count": 1, "results": [dataset(title=str(count))]}

        with self.assertRaises(source.MetadataSourceError):
            syncer.sync(self.cache, fetch=unstable)
        self.assertEqual(previous, self.pointer().read_bytes())

    def test_directory_replacement_during_download_cannot_publish(self):
        self.sync()
        previous = self.pointer().read_bytes()
        saved = self.cache / "snapshots-saved"
        replaced = False

        def replacing(start, rows, deadline):
            nonlocal replaced
            if not replaced:
                (self.cache / "snapshots").rename(saved)
                (self.cache / "snapshots").mkdir(mode=0o700)
                replaced = True
            return self.fetch(start, rows, deadline)

        with self.assertRaisesRegex(storage.SyncError, "directory changed"):
            syncer.sync(self.cache, fetch=replacing)
        self.assertEqual(previous, self.pointer().read_bytes())
        self.assertEqual([], list((self.cache / "snapshots").iterdir()))
        self.assertTrue(saved.exists())

    def test_ttl_refuses_changed_raw_snapshot(self):
        report = self.sync()
        snapshot = Path(report["snapshot_path"])
        snapshot.chmod(0o600)
        snapshot.write_bytes(b"corrupt")
        snapshot.chmod(0o400)
        previous = self.pointer().read_bytes()
        with self.assertRaises(storage.SyncError):
            self.sync(ttl_seconds=3600)
        self.assertEqual(2, len(self.calls))
        self.assertEqual(previous, self.pointer().read_bytes())

    def test_ttl_refuses_changed_bundle_manifest(self):
        report = self.sync()
        manifest = Path(report["bundle_path"]) / "bundle-manifest.json"
        manifest.chmod(0o600)
        manifest.write_bytes(b"{}\n")
        manifest.chmod(0o400)
        with self.assertRaises(CatalogError):
            self.sync(ttl_seconds=3600)
        self.assertEqual(2, len(self.calls))

    def test_ttl_rechecks_manifest_hash_after_catalog_validation(self):
        self.sync()
        original = CatalogBundle.load

        def change_after_load(path, **options):
            loaded = original(path, **options)
            manifest = Path(path) / "bundle-manifest.json"
            manifest.chmod(0o600)
            manifest.write_bytes(b"{}\n")
            manifest.chmod(0o400)
            return loaded

        previous = self.pointer().read_bytes()
        with patch.object(CatalogBundle, "load", side_effect=change_after_load):
            with self.assertRaisesRegex(storage.SyncError, "manifest changed"):
                self.sync(ttl_seconds=3600)
        self.assertEqual(2, len(self.calls))
        self.assertEqual(previous, self.pointer().read_bytes())

    def test_bad_baseline_fails_before_download(self):
        report = self.sync()
        for baseline in (
            (Path(report["bundle_path"]), "0" * 64),
            ("relative", "0" * 64),
            "bad",
        ):
            with self.subTest(baseline=baseline):
                with self.assertRaises((ValueError, CatalogError)):
                    self.sync(baseline=baseline)
        self.assertEqual(2, len(self.calls))

    def test_marker_requires_fixed_endpoint_and_exact_integer_schema(self):
        self.sync()
        marker = self.cache / "cache.json"
        original = json.loads(marker.read_text())
        for values in (
            {**original, "endpoint": "http://127.0.0.1"},
            {**original, "schema_version": True},
        ):
            marker.write_bytes(source.canonical(values))
            with self.assertRaises(storage.SyncError):
                self.sync()
        self.assertEqual(2, len(self.calls))

    def test_existing_managed_directories_cannot_be_symlinks_or_public(self):
        self.sync()
        for name in ("snapshots", "bundles", "runs"):
            folder = self.cache / name
            folder.chmod(0o755)
            with self.subTest(folder=name), self.assertRaises(storage.SyncError):
                self.sync()
            folder.chmod(0o700)
            saved = self.cache / (name + "-saved")
            folder.rename(saved)
            folder.symlink_to(saved, target_is_directory=True)
            with self.subTest(symlink=name), self.assertRaises(OSError):
                self.sync()
            folder.unlink()
            saved.rename(folder)
        self.assertEqual(2, len(self.calls))

    def test_cache_lock_and_retention_refuse_overlap_without_deleting_evidence(self):
        self.sync()
        with (self.cache / ".sync.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(storage.SyncError):
                self.sync()
        with patch.object(syncer, "MAX_RUNS", 1), self.assertRaises(storage.SyncError):
            self.sync()
        self.assertEqual(1, len(list((self.cache / "runs").iterdir())))
        self.assertEqual(2, len(self.calls))

    def test_pointer_cannot_escape_cache_or_reference_a_quarantined_run(self):
        self.sync()
        previous = json.loads(self.pointer().read_text())
        self.pointer().write_bytes(
            source.canonical({**previous, "bundle_id": "../outside"})
        )
        with self.assertRaises(storage.SyncError):
            self.sync(ttl_seconds=3600)
        self.pointer().write_bytes(source.canonical(previous))
        failed = self.cache / "runs" / previous["run_id"] / "failure.json"
        failed.write_bytes(source.canonical({"status": "quarantined"}))
        failed.chmod(0o600)
        with self.assertRaises(storage.SyncError):
            self.sync(ttl_seconds=3600)
        self.assertEqual(2, len(self.calls))

    def test_future_clock_and_wrong_snapshot_bundle_pairing_fail_closed(self):
        report = self.sync()
        with patch.object(syncer.time, "time", return_value=report["fetched_at"] - 60):
            with self.assertRaises(storage.SyncError):
                self.sync(ttl_seconds=3600)
        pointer = json.loads(self.pointer().read_text())
        self.pointer().write_bytes(
            source.canonical({**pointer, "snapshot_sha256": "0" * 64})
        )
        with self.assertRaises(storage.SyncError):
            self.sync(ttl_seconds=3600)
        self.assertEqual(2, len(self.calls))

    def test_atomic_records_flush_parent_directory_and_never_overwrite_receipts(self):
        report = self.sync()
        folder = self.cache / "runs" / report["run_id"]
        parent_fd = os.open(str(folder.parent), os.O_RDONLY)
        try:
            fd = storage.private_directory(parent_fd, folder.name)
        finally:
            os.close(parent_fd)
        try:
            with patch.object(os, "fsync", wraps=os.fsync) as flushed:
                storage.write_record(fd, "extra.json", {"safe": True})
                self.assertEqual(2, flushed.call_count)
            with self.assertRaises(FileExistsError):
                storage.write_record(fd, "extra.json", {"safe": False})
            self.assertEqual(
                {"safe": True}, json.loads((folder / "extra.json").read_text())
            )
        finally:
            os.close(fd)

    def test_unsafe_inputs_and_nonpublic_snapshots_never_publish(self):
        for options in (
            {"ttl_seconds": True},
            {"ttl_seconds": -1},
            {"ttl_seconds": float("nan")},
            {"ttl_seconds": 10**10},
            {"force": "yes"},
        ):
            with self.subTest(options=options), self.assertRaises(storage.SyncError):
                self.sync(**options)
        snapshot = self.root / "private.jsonl"
        snapshot.write_bytes(source.canonical(dataset(private=True)) + b"\n")
        with self.assertRaises(BundleError):
            syncer.sync(self.cache, snapshot=snapshot)
        self.assertFalse(self.pointer().exists())
        self.assertEqual([], self.calls)


if __name__ == "__main__":
    unittest.main()
