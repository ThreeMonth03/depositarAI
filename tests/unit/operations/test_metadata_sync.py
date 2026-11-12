"""Production metadata cache tests: real bundles, fake transport, no
services."""

from __future__ import annotations

import fcntl
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

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
        self.temporary = tempfile.TemporaryDirectory(
            prefix="depositar-sync-test-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.cache.mkdir(mode=0o700)
        self.calls = []
        self.rows = [dataset(), dataset(2)]

    def fetch(self, start, rows, deadline):
        self.calls.append((start, rows, deadline))
        return {
            "count": len(self.rows),
            "results": self.rows[start : start + rows],
        }

    def sync(self, **options):
        return syncer.sync(self.cache, fetch=self.fetch, **options)

    def pointer(self):
        return self.cache / "latest.json"

    def baseline(self, report):
        return Path(report["bundle_path"]), report["manifest_sha256"]

    def test_first_sync_has_two_scans_and_a_sealed_cache_without_baseline(
        self,
    ):
        report = self.sync()
        self.assertEqual(2, len(self.calls))
        self.assertEqual(
            ("ready", 2, 2),
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
            report["snapshot_sha256"],
            hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        )
        manifest = json.loads(
            (Path(report["bundle_path"]) / "bundle-manifest.json").read_text()
        )
        self.assertEqual(
            report["snapshot_sha256"],
            manifest["artifacts"]["source.jsonl.gz"]["sha256"],
        )
        self.assertEqual(
            report, json.loads(Path(report["receipt_path"]).read_text())
        )
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
        with patch.object(
            CatalogBundle, "load", wraps=CatalogBundle.load
        ) as loader:
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
        with patch.object(
            syncer.time, "time", return_value=first["fetched_at"] + 7200
        ):
            self.assertFalse(self.sync(ttl_seconds=3600)["cache_hit"])
        self.assertEqual(6, len(self.calls))

    def test_offline_jsonl_is_validated_without_network_or_ttl(self):
        snapshot = self.root / "snapshot.jsonl"
        snapshot.write_bytes(
            b"".join(
                source.canonical(row) + b"\n" for row in reversed(self.rows)
            )
        )
        with patch.object(source, "fetch_page", side_effect=AssertionError):
            report = syncer.sync(
                self.cache, snapshot=snapshot, ttl_seconds=3600
            )
        self.assertEqual("offline_not_checked", report["source_consistency"])
        self.assertFalse(report["cache_hit"])
        self.assertEqual(2, report["document_count"])

    def test_failed_download_keeps_previous_pointer_and_failure_record(
        self,
    ):
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
        self.assertEqual(
            "quarantined", json.loads(failure.read_text())["status"]
        )

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
        with patch.object(
            CatalogBundle, "load", side_effect=change_after_load
        ):
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
            with (
                self.subTest(folder=name),
                self.assertRaises(storage.SyncError),
            ):
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

    def test_cache_lock_and_retention_refuse_overlap_without_deleting_evidence(
        self,
    ):
        self.sync()
        with (self.cache / ".sync.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(storage.SyncError):
                self.sync()
        with (
            patch.object(syncer, "MAX_RUNS", 1),
            self.assertRaises(storage.SyncError),
        ):
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
        with patch.object(
            syncer.time, "time", return_value=report["fetched_at"] - 60
        ):
            with self.assertRaises(storage.SyncError):
                self.sync(ttl_seconds=3600)
        pointer = json.loads(self.pointer().read_text())
        self.pointer().write_bytes(
            source.canonical({**pointer, "snapshot_sha256": "0" * 64})
        )
        with self.assertRaises(storage.SyncError):
            self.sync(ttl_seconds=3600)
        self.assertEqual(2, len(self.calls))

    def test_atomic_records_flush_parent_and_never_overwrite_receipts(
        self,
    ):
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
            with (
                self.subTest(options=options),
                self.assertRaises(storage.SyncError),
            ):
                self.sync(**options)
        snapshot = self.root / "private.jsonl"
        snapshot.write_bytes(source.canonical(dataset(private=True)) + b"\n")
        with self.assertRaises(BundleError):
            syncer.sync(self.cache, snapshot=snapshot)
        self.assertFalse(self.pointer().exists())
        self.assertEqual([], self.calls)

    def test_partial_layout_failure_closes_owned_fds_and_retains_evidence(
        self,
    ):
        opened, closed = [], []
        admission_failed = False
        original_directory = storage.private_directory
        original_open, original_close = os.open, os.close

        def open_file(name, *args, **options):
            descriptor = original_open(name, *args, **options)
            if name == ".sync.lock":
                opened.append(descriptor)
            return descriptor

        def open_child(parent, name, **options):
            nonlocal admission_failed
            if name == "bundles":
                admission_failed = True
                raise OSError("synthetic child admission failure")
            descriptor = original_directory(parent, name, **options)
            opened.append(descriptor)
            return descriptor

        def close(descriptor):
            if admission_failed:
                closed.append(descriptor)
            original_close(descriptor)

        with (
            patch.object(storage, "private_directory", side_effect=open_child),
            patch.object(os, "open", side_effect=open_file),
            patch.object(os, "close", side_effect=close),
            self.assertRaisesRegex(OSError, "synthetic child admission"),
        ):
            self.sync()
        self.assertEqual(opened[::-1], closed[:2])
        self.assertEqual(3, len(closed))
        for descriptor in closed:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertTrue((self.cache / "snapshots").is_dir())
        self.assertFalse((self.cache / "cache.json").exists())
        self.assertEqual([], self.calls)

    def test_invalid_marker_precedes_run_retention(self):
        self.sync()
        (self.cache / "cache.json").write_bytes(source.canonical({}))
        with (
            patch.object(syncer, "MAX_RUNS", 0),
            self.assertRaisesRegex(
                storage.SyncError, "^invalid public metadata cache marker$"
            ),
        ):
            self.sync()
        self.assertEqual(2, len(self.calls))

    def test_exact_ttl_boundary_rebuilds_one_baseline_without_fetching(self):
        first = self.sync()
        with (
            patch.object(
                syncer.time, "time", return_value=first["fetched_at"] + 60
            ),
            patch.object(
                syncer.builder,
                "build_bundle",
                wraps=syncer.builder.build_bundle,
            ) as build,
        ):
            second = self.sync(ttl_seconds=60)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["fetched_at"], second["fetched_at"])
        self.assertEqual(2, len(self.calls))
        self.assertEqual(1, build.call_count)
        self.assertEqual(Path(first["snapshot_path"]), build.call_args.args[0])
        self.assertEqual({}, build.call_args.kwargs)

    def test_declared_latest_with_missing_result_is_not_a_refresh_miss(self):
        first = self.sync()
        previous = self.pointer().read_bytes()
        Path(first["receipt_path"]).unlink()
        with self.assertRaisesRegex(
            storage.SyncError, "^latest metadata cache is incomplete$"
        ) as caught:
            self.sync(ttl_seconds=3600)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(previous, self.pointer().read_bytes())
        self.assertEqual(2, len(self.calls))

    def test_bad_baseline_precedes_pristine_cache_mutation(self):
        with self.assertRaisesRegex(storage.SyncError, "^baseline must be"):
            self.sync(baseline="invalid")
        self.assertEqual([], list(self.cache.iterdir()))
        self.assertEqual([], self.calls)

    def test_profile_baseline_combination_refuses_before_cache_mutation(self):
        for options in (
            {"profile_baseline_bundle": self.root / "captured"},
            {"profile_baseline_bundle": 1, "profile_set": self.root / "set"},
        ):
            with self.subTest(options=options):
                with self.assertRaises(storage.SyncError):
                    self.sync(**options)
                self.assertEqual([], list(self.cache.iterdir()))
                self.assertEqual([], self.calls)

    def test_profile_baseline_comparison_keeps_types_and_remaining_fields(
        self,
    ):
        self.rows = [dataset()]
        baseline = self.sync()
        manifest = json.loads(
            (
                Path(baseline["bundle_path"]) / "bundle-manifest.json"
            ).read_bytes()
        )
        content = syncer._profile_baseline_content(manifest)
        recipe = deepcopy(manifest)
        recipe["bundle_id"] = "a" * 64
        recipe["builder"]["source_sha256"] = "b" * 64
        self.assertEqual(content, syncer._profile_baseline_content(recipe))
        for value in (1.0, True):
            with self.subTest(dataset_count=value, type=type(value)):
                changed = deepcopy(manifest)
                changed["dataset_count"] = value
                self.assertNotEqual(
                    content, syncer._profile_baseline_content(changed)
                )
        changed = deepcopy(manifest)
        changed["normalization_counters"]["normalized_fields"] = 0
        self.assertNotEqual(content, syncer._profile_baseline_content(changed))

    def test_publication_faults_keep_result_latest_and_quarantine_order(self):
        original = storage.write_record
        for target, after in (
            ("result.json", False),
            ("latest.json", False),
            ("latest.json", True),
        ):
            with self.subTest(target=target, after=after):
                cache = self.root / (target + str(after))
                cache.mkdir(mode=0o700)
                first = syncer.sync(cache, fetch=self.fetch)
                pointer = cache / "latest.json"
                previous = pointer.read_bytes()
                written = []

                def publish(parent, name, value, **options):
                    written.append(name)
                    if name == target and not after:
                        raise OSError("synthetic publication fault")
                    original(parent, name, value, **options)
                    if name == target:
                        raise OSError("synthetic publication fault")

                with (
                    patch.object(storage, "write_record", side_effect=publish),
                    self.assertRaisesRegex(OSError, "synthetic publication"),
                ):
                    syncer.sync(cache, fetch=self.fetch)
                expected = ["started.json", "result.json"]
                if target == "latest.json":
                    expected.append("latest.json")
                self.assertEqual(expected + ["failure.json"], written)
                self.assertEqual(not after, pointer.read_bytes() == previous)
                failures = list((cache / "runs").glob("*/failure.json"))
                self.assertEqual(1, len(failures))
                self.assertEqual(
                    "OSError",
                    json.loads(failures[0].read_text())["error_type"],
                )
                self.assertTrue(Path(first["receipt_path"]).is_file())

    def test_failure_record_error_preserves_original_exception_context(self):
        original = storage.write_record
        written = []

        def publish(parent, name, value, **options):
            written.append(name)
            if name == "failure.json":
                raise OSError("synthetic quarantine fault")
            original(parent, name, value, **options)

        def fail_fetch(*_args):
            raise RuntimeError("synthetic source fault")

        with (
            patch.object(storage, "write_record", side_effect=publish),
            self.assertRaisesRegex(OSError, "synthetic quarantine") as caught,
        ):
            syncer.sync(self.cache, fetch=fail_fetch)
        self.assertIs(type(caught.exception.__context__), RuntimeError)
        self.assertEqual(
            ["cache.json", "started.json", "failure.json"], written
        )
        self.assertFalse(self.pointer().exists())

    def test_online_observation_precedes_build_and_survives_two_ttl_hits(self):
        clock = [1000]
        build = syncer.builder.build_bundle

        def fetch(*args):
            clock[0] += 1
            return self.fetch(*args)

        def build_later(*args, **kwargs):
            clock[0] += 5
            return build(*args, **kwargs)

        with (
            patch.object(source.time, "time", side_effect=lambda: clock[0]),
            patch.object(
                syncer.builder, "build_bundle", side_effect=build_later
            ),
        ):
            first = syncer.sync(self.cache, fetch=fetch)
        observation = first["source_observation"]
        self.assertEqual(
            {
                "schema_version": 1,
                "kind": "complete_online",
                "clock": "operator_utc",
                "started_at": 1000,
                "completed_at": 1002,
                "source_endpoint": source.ENDPOINT,
                "snapshot_sha256": first["snapshot_sha256"],
                "evidence_run_id": first["run_id"],
            },
            observation,
        )
        self.assertEqual(1007, first["fetched_at"])
        self.assertEqual(1007, first["decision_at"])
        self.assertIsNone(first["cache_used_at"])
        original = Path(first["receipt_path"]).read_bytes()
        for instant in (1010, 1020):
            with patch.object(source.time, "time", return_value=instant):
                reused = self.sync(ttl_seconds=60)
            self.assertTrue(reused["cache_hit"])
            self.assertNotEqual(first["run_id"], reused["run_id"])
            self.assertEqual(observation, reused["source_observation"])
            self.assertEqual(instant, reused["decision_at"])
            self.assertEqual(instant, reused["cache_used_at"])
            self.assertEqual(1007, reused["fetched_at"])
        self.assertEqual(original, Path(first["receipt_path"]).read_bytes())
        self.assertEqual(2, len(self.calls))

    def test_offline_observation_stays_unknown_when_reused(self):
        snapshot = self.root / "offline.jsonl"
        snapshot.write_bytes(
            b"".join(source.canonical(r) + b"\n" for r in self.rows)
        )
        with patch.object(source.time, "time", return_value=1000):
            first = syncer.sync(self.cache, snapshot=snapshot)
        original = Path(first["receipt_path"]).read_bytes()
        with patch.object(source.time, "time", return_value=1010):
            reused = self.sync(ttl_seconds=60)
        observation = reused["source_observation"]
        self.assertEqual("unknown_offline", observation["kind"])
        self.assertIsNone(observation["started_at"])
        self.assertIsNone(observation["completed_at"])
        self.assertEqual(first["source_observation"], observation)
        self.assertEqual(1010, reused["cache_used_at"])
        self.assertEqual([], self.calls)
        self.assertEqual(original, Path(first["receipt_path"]).read_bytes())

    def test_old_immutable_receipt_has_unknown_observation_without_today_fill(
        self,
    ):
        with patch.object(source.time, "time", return_value=1000):
            self.sync()
            first = self.sync(ttl_seconds=60)
        self.assertTrue(first["cache_hit"])
        receipt_path = Path(first["receipt_path"])
        old = json.loads(receipt_path.read_bytes())
        old["schema_version"] = 1
        for key in ("source_observation", "decision_at", "cache_used_at"):
            del old[key]
        receipt_path.write_bytes(source.canonical(old) + b"\n")
        pointer = json.loads(self.pointer().read_bytes())
        pointer["schema_version"] = 1
        del pointer["source_observation"]
        self.pointer().write_bytes(source.canonical(pointer) + b"\n")
        immutable = receipt_path.read_bytes()
        for instant in (1010, 1020):
            with patch.object(source.time, "time", return_value=instant):
                reused = self.sync(ttl_seconds=60)
            observation = reused["source_observation"]
            self.assertEqual("unknown_legacy", observation["kind"])
            self.assertEqual(first["run_id"], observation["evidence_run_id"])
            self.assertIsNone(observation["started_at"])
            self.assertIsNone(observation["completed_at"])
            self.assertEqual(1000, reused["fetched_at"])
            self.assertEqual(2, reused["schema_version"])
            self.assertEqual(immutable, receipt_path.read_bytes())
        self.assertEqual(2, len(self.calls))

    def test_observation_origin_identity_is_revalidated_on_cache_hit(self):
        first = self.sync()
        self.sync(ttl_seconds=60)
        origin_path = Path(first["receipt_path"])
        original = origin_path.read_bytes()
        previous = self.pointer().read_bytes()
        for key, value in (
            ("run_id", "a" * 32),
            ("source_endpoint", "https://example.test/secret"),
            ("status", "quarantined"),
            ("schema_version", 1),
            ("source_consistency", "offline_not_checked"),
            ("snapshot_sha256", "a" * 64),
            ("decision_at", first["fetched_at"] + 3600),
            ("cache_hit", True),
            ("cache_used_at", first["fetched_at"]),
        ):
            with self.subTest(field=key):
                changed = json.loads(original)
                changed[key] = value
                origin_path.write_bytes(source.canonical(changed) + b"\n")
                with self.assertRaises(syncer.ObservationError):
                    self.sync(ttl_seconds=60)
                self.assertEqual(previous, self.pointer().read_bytes())
                origin_path.write_bytes(original)
        self.assertEqual(2, len(self.calls))

    def test_v2_cannot_lose_observation_or_disagree_with_original(self):
        first = self.sync()
        self.sync(ttl_seconds=60)
        original_pointer = self.pointer().read_bytes()
        origin_path = Path(first["receipt_path"])
        original_receipt = origin_path.read_bytes()
        pointer = json.loads(original_pointer)
        del pointer["source_observation"]
        self.pointer().write_bytes(source.canonical(pointer) + b"\n")
        tampered = self.pointer().read_bytes()
        with self.assertRaises(storage.SyncError):
            self.sync(ttl_seconds=60)
        self.assertEqual(tampered, self.pointer().read_bytes())
        self.pointer().write_bytes(original_pointer)
        receipt = json.loads(original_receipt)
        receipt["source_observation"]["started_at"] -= 1
        origin_path.write_bytes(source.canonical(receipt) + b"\n")
        with self.assertRaises(syncer.ObservationError):
            self.sync(ttl_seconds=60)
        self.assertEqual(original_pointer, self.pointer().read_bytes())
        self.assertEqual(2, len(self.calls))

    def test_origin_observation_is_validated_before_numeric_equality(self):
        with patch.object(source.time, "time", return_value=1):
            first = self.sync()
            self.sync(ttl_seconds=60)
        previous = self.pointer().read_bytes()
        origin_path = Path(first["receipt_path"])
        original = origin_path.read_bytes()
        for field, value in (
            ("schema_version", True),
            ("started_at", True),
            ("completed_at", 1.0),
        ):
            with self.subTest(field=field):
                changed = json.loads(original)
                changed["source_observation"][field] = value
                origin_path.write_bytes(source.canonical(changed) + b"\n")
                with (
                    patch.object(source.time, "time", return_value=2),
                    self.assertRaises(syncer.ObservationError),
                ):
                    self.sync(ttl_seconds=60)
                self.assertEqual(previous, self.pointer().read_bytes())
        self.assertEqual(2, len(self.calls))

    def test_linked_origin_cache_fields_cannot_claim_a_new_observation(self):
        snapshot = self.root / "original.jsonl"
        snapshot.write_bytes(
            b"".join(source.canonical(row) + b"\n" for row in self.rows)
        )
        for kind in ("online", "offline"):
            with self.subTest(kind=kind):
                cache = self.root / kind
                cache.mkdir(mode=0o700)
                options = (
                    {"fetch": self.fetch}
                    if kind == "online"
                    else {"snapshot": snapshot}
                )
                with patch.object(source.time, "time", return_value=1000):
                    first = syncer.sync(cache, **options)
                with patch.object(source.time, "time", return_value=1010):
                    syncer.sync(cache, fetch=self.fetch, ttl_seconds=60)
                previous = (cache / "latest.json").read_bytes()
                origin_path = Path(first["receipt_path"])
                origin = json.loads(origin_path.read_bytes())
                origin.update(
                    cache_hit=True, cache_used_at=origin["fetched_at"]
                )
                origin_path.write_bytes(source.canonical(origin) + b"\n")
                with (
                    patch.object(source.time, "time", return_value=1020),
                    self.assertRaisesRegex(
                        syncer.ObservationError, "lineage mismatch"
                    ),
                ):
                    syncer.sync(cache, fetch=self.fetch, ttl_seconds=60)
                self.assertEqual(
                    previous, (cache / "latest.json").read_bytes()
                )
                failure = next((cache / "runs").glob("*/failure.json"))
                self.assertEqual(
                    "evidence_invalid",
                    json.loads(failure.read_bytes())["failure_code"],
                )

    def test_current_cache_fields_cannot_hide_reuse_or_omit_nullable_time(
        self,
    ):
        with patch.object(source.time, "time", return_value=1000):
            self.sync()
        with patch.object(source.time, "time", return_value=1010):
            current = self.sync(ttl_seconds=60)
        previous = self.pointer().read_bytes()
        receipt_path = Path(current["receipt_path"])
        original = receipt_path.read_bytes()
        for missing in (False, True):
            with self.subTest(missing_time=missing):
                changed = json.loads(original)
                if missing:
                    del changed["cache_used_at"]
                else:
                    changed.update(cache_hit=False, cache_used_at=None)
                receipt_path.write_bytes(source.canonical(changed) + b"\n")
                with (
                    patch.object(source.time, "time", return_value=1020),
                    self.assertRaises(syncer.ObservationError),
                ):
                    self.sync(ttl_seconds=60)
                self.assertEqual(previous, self.pointer().read_bytes())
        self.assertEqual(2, len(self.calls))

    def test_clock_failures_preserve_latest_and_have_closed_codes(self):
        self.sync()
        previous = self.pointer().read_bytes()
        for times, code in (
            ([float("nan")], "clock_invalid"),
            ([True], "clock_invalid"),
            ([-1], "clock_invalid"),
            ([1002, 1000], "clock_reversed"),
            ([1000, 1002, 1001], "clock_future"),
            ([1000, 1002, 1003, 1002], "clock_future"),
        ):
            with self.subTest(code=code, times=times):
                before = set((self.cache / "runs").glob("*/failure.json"))
                with (
                    patch.object(source.time, "time", side_effect=times),
                    self.assertRaises(ValueError),
                ):
                    self.sync()
                failures = (
                    set((self.cache / "runs").glob("*/failure.json")) - before
                )
                self.assertEqual(1, len(failures))
                failure = json.loads(failures.pop().read_bytes())
                self.assertEqual(code, failure["failure_code"])
                self.assertEqual(previous, self.pointer().read_bytes())

    def test_future_and_invalid_retained_observation_cannot_be_reused(self):
        first = self.sync()
        pointer = json.loads(self.pointer().read_bytes())
        for field, value in (
            ("started_at", first["fetched_at"] + 1),
            ("completed_at", first["fetched_at"] + 1),
            ("kind", []),
            ("schema_version", True),
        ):
            with self.subTest(field=field):
                changed = deepcopy(pointer)
                changed["source_observation"][field] = value
                self.pointer().write_bytes(source.canonical(changed) + b"\n")
                previous = self.pointer().read_bytes()
                with self.assertRaises(storage.SyncError):
                    self.sync(ttl_seconds=60)
                self.assertEqual(previous, self.pointer().read_bytes())
        self.assertEqual(2, len(self.calls))

    def test_source_failures_have_closed_codes_and_never_replace_latest(self):
        self.sync()
        previous = self.pointer().read_bytes()
        page = {"count": 1, "results": [dataset()]}
        for pages, code in (
            ([TimeoutError("secret-url")], "timeout"),
            ([page, TimeoutError("secret-body")], "timeout"),
            ([URLError(TimeoutError("secret-timeout"))], "timeout"),
            ([OSError("secret-url")], "io_failure"),
            ([{"count": 2, "results": [dataset()]}], "source_incomplete"),
            ([{"count": 0, "results": []}], "source_incomplete"),
            (
                [page, {"count": 2, "results": [dataset()]}],
                "source_incomplete",
            ),
            (
                [page, {"count": 2, "results": self.rows}],
                "source_count_changed",
            ),
            (
                [page, {"count": 1, "results": [dataset(title="changed")]}],
                "source_content_changed",
            ),
            (
                [page, {"count": 1, "results": [dataset(unindexed="new")]}],
                "source_content_changed",
            ),
        ):
            with self.subTest(code=code):
                before = set((self.cache / "runs").glob("*/failure.json"))
                pages = iter(pages)

                def fetch(*_args):
                    result = next(pages)
                    if isinstance(result, Exception):
                        raise result
                    return result

                with self.assertRaises(
                    ValueError if code.startswith("source_") else OSError
                ):
                    syncer.sync(self.cache, fetch=fetch)
                failures = (
                    set((self.cache / "runs").glob("*/failure.json")) - before
                )
                failure = failures.pop().read_bytes()
                self.assertEqual(code, json.loads(failure)["failure_code"])
                self.assertNotIn(b"secret-", failure)
                self.assertEqual(previous, self.pointer().read_bytes())

    def test_unknown_exception_names_and_messages_are_not_receipt_fields(self):
        self.sync()
        previous = self.pointer().read_bytes()
        secret_type = type("PRIVATE_CLASS_SENTINEL", (RuntimeError,), {})

        def fetch(*_args):
            raise secret_type("https://example.test/PRIVATE_URL_SENTINEL")

        with self.assertRaises(secret_type):
            syncer.sync(self.cache, fetch=fetch)
        failure = next(
            (self.cache / "runs").glob("*/failure.json")
        ).read_bytes()
        self.assertNotIn(b"PRIVATE_", failure)
        self.assertEqual("Exception", json.loads(failure)["error_type"])
        self.assertEqual(
            "unexpected_failure", json.loads(failure)["failure_code"]
        )
        self.assertEqual(previous, self.pointer().read_bytes())

    def test_missing_metadata_remains_review_not_confirmed_withdrawal(self):
        first = self.sync()
        self.rows.pop()
        report = self.sync(baseline=self.baseline(first))
        self.assertEqual("review_required", report["diff"]["status"])
        self.assertEqual([dataset(2)["id"]], report["diff"]["missing"])
        self.assertFalse(report["promotion_allowed"])
        self.assertEqual(
            "complete_online", report["source_observation"]["kind"]
        )


if __name__ == "__main__":
    unittest.main()
