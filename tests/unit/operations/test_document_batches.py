"""Protect durable, manifest-bound resource progress and explicit retry policy."""

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.catalog.build.builder import build_bundle
from search.operations.documents import batches, controller, extractor, sets, source


IMAGE = {
    "image_id": "sha256:" + "a" * 64,
    "source_sha256": "b" * 64,
    "base_image": extractor.FESS_IMAGE,
}


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.material = self.root / "input.csv"
        self.raw = b"pH\n7.2\n"
        self.material.write_bytes(self.raw)
        self.material.chmod(0o600)
        self.sha = hashlib.sha256(self.raw).hexdigest()
        self.result = {
            "schema_version": 1,
            "input_sha256": self.sha,
            "status": "ok",
            "detected_mime": "text/csv",
            "columns": ["pH"],
            "text": "pH: 7.2",
            "text_truncated": False,
            "rows_observed": 1,
            "rows_complete": False,
            "may_require_ocr": False,
        }
        self.bundle, self.manifest, self.ids = self.build(2)
        self.selection = self.root / "selection.json"
        self.selection.write_text(json.dumps(self.ids))
        self.selection.chmod(0o600)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(
                extractor, "source_identity", return_value=IMAGE["source_sha256"]
            )
        )
        self.image = self.stack.enter_context(
            patch.object(extractor, "build_image", return_value=IMAGE)
        )
        self.fetch = self.stack.enter_context(
            patch.object(source, "acquire", side_effect=self.acquired)
        )
        self.parse = self.stack.enter_context(
            patch.object(
                extractor, "extract", side_effect=lambda *a, **kw: dict(self.result)
            )
        )

    def build(self, count, *, same_dataset=False, title="Water"):
        rows = []
        identifiers = [
            f"{index + 1:08x}-1111-4111-8111-111111111111" for index in range(count)
        ]
        for index, identifier in enumerate(identifiers):
            if not rows or not same_dataset and index % 3 == 0:
                rows.append(
                    {
                        "id": f"{len(rows) + 1:08x}-0000-4000-8000-000000000000",
                        "name": "water-" + str(len(rows)),
                        "title": title,
                        "notes": "Water",
                        "private": False,
                        "state": "active",
                        "type": "dataset",
                        "resources": [],
                    }
                )
            rows[-1]["resources"].append(
                {
                    "id": identifier,
                    "format": "CSV",
                    "name": "Water",
                    "url": "https://data.depositar.io/" + identifier + ".csv",
                }
            )
        snapshot = self.root / "snapshot.jsonl"
        snapshot.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
        snapshot.chmod(0o600)
        output = self.root / "bundles"
        output.mkdir(mode=0o700, exist_ok=True)
        result = build_bundle(snapshot, output)
        return Path(result["bundle_path"]), result["manifest_sha256"], identifiers

    def acquired(self, _root, resource):
        return {
            "local_path": self.material,
            "sha256": self.sha,
            "bytes": len(self.raw),
            "acquired_at": 100,
            "cache_hit": True,
            "source_url_sha256": hashlib.sha256(resource["url"].encode()).hexdigest(),
        }

    def prepare(self):
        result = batches.prepare(self.bundle, self.manifest, self.state, self.selection)
        self.plan_sha = result["plan_sha256"]
        return result

    def run_batch(self, **options):
        return batches.run(self.bundle, self.state, self.plan_sha, **options)

    def test_prepare_has_no_download_parser_or_docker_calls(self):
        report = self.prepare()
        self.assertEqual(report["status"], "prepared")
        self.assertEqual(report["selected_count"], 2)
        self.fetch.assert_not_called()
        self.parse.assert_not_called()
        self.image.assert_not_called()
        self.assertEqual((self.state / "plan.json").stat().st_mode & 0o777, 0o600)

    def test_checkpoint_then_resume_skips_completed_sources_and_exports_exact_set(self):
        self.prepare()
        first = self.run_batch(max_resources=1)
        self.assertEqual(
            (first["status"], first["profile_count"], first["pending_count"]),
            ("checkpoint", 1, 1),
        )
        with patch("time.time", return_value=100 + 3 * source.CACHE_TTL_SECONDS):
            final = self.run_batch()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(self.parse.call_count, 1)
        record, digest = sets.read(self.state / final["profile_set_file"])
        self.assertEqual(digest, final["profile_set_sha256"])
        self.assertEqual([row["resource_id"] for row in record["profiles"]], self.ids)
        self.assertEqual(record["failures"], [])
        self.assertNotIn("https://", json.dumps(final))
        self.assertNotIn("7.2", json.dumps(final))
        self.assertEqual(self.run_batch()["attempted_count"], 0)
        self.assertEqual(self.fetch.call_count, 2)

    def test_interrupt_keeps_first_success_and_exact_unfinished_helper(self):
        self.prepare()
        actual = controller.process
        calls = []

        def interrupt(*args, **kwargs):
            calls.append(kwargs["container_name"])
            if len(calls) == 2:
                raise KeyboardInterrupt
            return actual(*args, **kwargs)

        with (
            patch.object(controller, "process", side_effect=interrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_batch()
        first = batches.status(self.state, self.plan_sha)
        self.assertEqual((first["profile_count"], first["pending_count"]), (1, 1))
        with patch.object(extractor, "helper_exists", return_value=True) as active:
            with self.assertRaisesRegex(batches.BatchError, "helper_active"):
                self.run_batch()
        active.assert_called_once_with(calls[-1])
        self.assertEqual(self.fetch.call_count, 1)
        with patch.object(extractor, "helper_exists", return_value=False):
            self.assertEqual(self.run_batch()["status"], "completed")
        self.assertEqual(self.fetch.call_count, 2)

    def test_failures_do_not_starve_pending_and_require_explicit_bounded_retry(self):
        self.prepare()
        self.fetch.side_effect = source.SourceError("document_source_timeout")
        first = self.run_batch(max_resources=1)
        self.assertEqual(first["status"], "checkpoint")
        self.fetch.side_effect = self.acquired
        second = self.run_batch(max_resources=1, retry_failed=True)
        self.assertEqual((second["profile_count"], second["failed_count"]), (1, 1))
        self.assertEqual(self.run_batch()["attempted_count"], 0)
        self.fetch.side_effect = source.SourceError("document_source_timeout")
        self.assertEqual(self.run_batch(retry_failed=True)["retryable_count"], 1)
        final = self.run_batch(retry_failed=True)
        self.assertEqual((final["status"], final["retryable_count"]), ("partial", 0))
        self.assertEqual(self.run_batch(retry_failed=True)["attempted_count"], 0)
        record, _ = sets.read(self.state / final["profile_set_file"])
        self.assertEqual(
            record["failures"],
            [
                {
                    "dataset_id": "00000001-0000-4000-8000-000000000000",
                    "resource_id": self.ids[0],
                    "code": "document_source_timeout",
                }
            ],
        )

    def test_parse_error_profile_is_terminal_and_explicitly_exported(self):
        self.prepare()
        self.result.update(
            status="parse_error", columns=[], text="", rows_observed=None
        )
        first = self.run_batch()
        self.assertEqual((first["status"], first["profile_count"]), ("partial", 2))
        self.assertEqual(self.run_batch(retry_failed=True)["attempted_count"], 0)
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(self.parse.call_count, 1)

    def test_selection_bounds_are_rejected_before_state_or_download(self):
        self.bundle, self.manifest, self.ids = self.build(4, same_dataset=True)
        self.selection.write_text(json.dumps(self.ids))
        with self.assertRaisesRegex(batches.BatchError, "plan_invalid"):
            self.prepare()
        self.assertFalse(self.state.exists())
        self.selection.write_text(json.dumps([self.ids[0]] * (sets.MAX_SELECTED + 1)))
        with self.assertRaisesRegex(batches.BatchError, "selection_invalid"):
            self.prepare()
        self.fetch.assert_not_called()

    def test_twenty_attempt_limit_leaves_durable_pending_items(self):
        self.bundle, self.manifest, self.ids = self.build(23)
        self.selection.write_text(json.dumps(self.ids))
        self.prepare()
        first = self.run_batch()
        self.assertEqual(
            (first["attempted_count"], first["profile_count"], first["pending_count"]),
            (20, 20, 3),
        )
        final = self.run_batch()
        self.assertEqual((final["status"], final["attempted_count"]), ("completed", 3))
        self.assertEqual(self.fetch.call_count, 23)
        with self.assertRaisesRegex(batches.BatchError, "options_invalid"):
            self.run_batch(max_resources=21)

    def test_empty_and_unsupported_profiles_are_not_retried(self):
        for status in ("empty_text", "unsupported_format"):
            with self.subTest(status=status):
                self.state = self.root / status
                self.prepare()
                self.result.update(
                    status=status,
                    columns=[],
                    text="",
                    rows_observed=None if status == "unsupported_format" else 0,
                )
                final = self.run_batch()
                self.assertEqual(
                    (final["status"], final["profile_count"]), ("completed", 2)
                )
                before = self.fetch.call_count
                self.assertEqual(
                    self.run_batch(retry_failed=True)["attempted_count"], 0
                )
                self.assertEqual(self.fetch.call_count, before)

    def test_outcome_commit_failure_cannot_claim_completion(self):
        self.prepare()
        write = batches.private_io.write_json

        def fail_outcome(parent, name, value, **kwargs):
            if name == self.ids[0] + ".json" and value["status"] == "ok":
                raise OSError("disk unavailable")
            return write(parent, name, value, **kwargs)

        with (
            patch.object(batches.private_io, "write_json", side_effect=fail_outcome),
            self.assertRaisesRegex(batches.BatchError, "state_invalid"),
        ):
            self.run_batch()
        report = batches.status(self.state, self.plan_sha)
        self.assertEqual((report["profile_count"], report["pending_count"]), (0, 2))
        self.assertEqual(len(list((self.state / "profiles").glob("*.json"))), 1)
        with patch.object(extractor, "helper_exists", return_value=False):
            self.assertEqual(self.run_batch()["status"], "completed")
        self.assertEqual(self.parse.call_count, 1)

    def test_corrupt_outcome_does_not_trigger_download(self):
        self.prepare()
        self.run_batch(max_resources=1)
        path = self.state / "outcomes" / (self.ids[0] + ".json")
        value = json.loads(path.read_text())
        value["attempt"] = True
        path.write_bytes(batches.private_io.canonical(value))
        with self.assertRaisesRegex(batches.BatchError, "outcome_invalid"):
            self.run_batch()
        self.assertEqual(self.fetch.call_count, 1)

    def test_changed_manifest_plan_and_recipe_stop_before_acquisition(self):
        self.prepare()
        original = self.bundle
        self.bundle, _, _ = self.build(2, title="Changed")
        with self.assertRaisesRegex(batches.BatchError, "plan_changed"):
            self.run_batch()
        self.bundle = original
        with patch.object(extractor, "source_identity", return_value="c" * 64):
            with self.assertRaisesRegex(batches.BatchError, "tool_changed"):
                self.run_batch()
        with self.assertRaisesRegex(batches.BatchError, "plan_changed"):
            batches.run(self.bundle, self.state, "d" * 64)
        self.fetch.assert_not_called()

    def test_reprepare_preserves_verified_existing_progress(self):
        self.prepare()
        self.run_batch(max_resources=1)
        report = self.prepare()
        self.assertEqual(
            (report["status"], report["profile_count"], report["pending_count"]),
            ("checkpoint", 1, 1),
        )
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.run_batch()["status"], "completed")

    def test_terminal_run_still_requires_matching_bundle_and_recipe(self):
        self.prepare()
        self.run_batch()
        self.image.reset_mock()
        before = self.fetch.call_count
        original = self.bundle
        self.bundle, _, _ = self.build(2, title="Changed")
        with self.assertRaisesRegex(batches.BatchError, "plan_changed"):
            self.run_batch()
        self.bundle = self.root / "missing-bundle"
        with self.assertRaisesRegex(batches.BatchError, "plan_changed"):
            self.run_batch()
        self.bundle = original
        with patch.object(extractor, "source_identity", return_value="c" * 64):
            with self.assertRaisesRegex(batches.BatchError, "tool_changed"):
                self.run_batch()
        self.assertEqual(self.run_batch()["attempted_count"], 0)
        self.assertEqual(self.fetch.call_count, before)
        self.image.assert_not_called()

    def test_space_preflight_fails_before_build_or_download(self):
        self.prepare()
        with patch.object(
            batches.shutil, "disk_usage", return_value=SimpleNamespace(free=1)
        ):
            with self.assertRaisesRegex(batches.BatchError, "insufficient_space"):
                self.run_batch()
        self.image.assert_not_called()
        self.fetch.assert_not_called()

    def test_tampered_success_profile_fails_closed_without_retry(self):
        self.prepare()
        self.run_batch(max_resources=1)
        profile = next((self.state / "profiles").glob("*.json"))
        value = json.loads(profile.read_text())
        value["result"]["text"] = "tampered"
        profile.write_text(json.dumps(value))
        with self.assertRaisesRegex(batches.BatchError, "profile_invalid"):
            self.run_batch()
        self.assertEqual(self.fetch.call_count, 1)

    def test_status_does_not_write_or_repair_and_final_export_can_resume(self):
        self.prepare()
        self.run_batch()
        before = {path: path.stat().st_mtime_ns for path in self.state.rglob("*")}
        self.assertEqual(
            batches.status(self.state, self.plan_sha)["status"], "completed"
        )
        self.assertEqual(
            before, {path: path.stat().st_mtime_ns for path in self.state.rglob("*")}
        )
        for path in self.state.glob("profile-set-*.json"):
            path.unlink()
        self.assertTrue(batches.status(self.state, self.plan_sha)["export_pending"])
        self.assertEqual(self.run_batch()["status"], "completed")
        self.assertEqual(self.fetch.call_count, 2)

    def test_replaced_root_during_build_stops_before_acquisition(self):
        self.prepare()

        def replace(_source_root):
            self.state.rename(self.root / "retained-state")
            self.state.mkdir(mode=0o700)
            return IMAGE

        self.image.side_effect = replace
        with self.assertRaises(ValueError):
            self.run_batch()
        self.fetch.assert_not_called()

    def test_replaced_profile_directory_cannot_claim_completed_item(self):
        self.prepare()

        def replace(*args, **kwargs):
            (self.state / "profiles").rename(self.state / "retained-profiles")
            (self.state / "profiles").mkdir(mode=0o700)
            return self.result

        self.parse.side_effect = replace
        with self.assertRaisesRegex(batches.BatchError, "state_invalid"):
            self.run_batch()
        self.assertEqual(list((self.state / "profiles").iterdir()), [])
        self.assertEqual(batches.status(self.state, self.plan_sha)["profile_count"], 0)

    def test_replaced_profile_directory_during_status_is_rejected(self):
        self.prepare()
        self.run_batch()
        verify = batches._verify_profile
        changed = []

        def replace(*args):
            verify(*args)
            if not changed:
                (self.state / "profiles").rename(self.state / "retained-profiles")
                (self.state / "profiles").mkdir(mode=0o700)
                changed.append(True)

        with (
            patch.object(batches, "_verify_profile", side_effect=replace),
            self.assertRaisesRegex(batches.BatchError, "state_invalid"),
        ):
            batches.status(self.state, self.plan_sha)

    def test_existing_batch_runners_hold_exclusive_lock(self):
        self.prepare()
        with batches._state(self.state, create=False):
            with self.assertRaisesRegex(controller.ProfileError, "state_busy"):
                self.run_batch()
            with self.assertRaisesRegex(controller.ProfileError, "state_busy"):
                batches.status(self.state, self.plan_sha)
        with batches._state(self.state, create=False, read_only=True):
            self.assertEqual(
                batches.status(self.state, self.plan_sha)["status"], "checkpoint"
            )
            with self.assertRaisesRegex(controller.ProfileError, "state_busy"):
                self.run_batch()

    def test_concurrent_writer_lock_and_cleanup_failure_stop_progress(self):
        self.prepare()
        with (
            controller._state(self.state),
            self.assertRaisesRegex(controller.ProfileError, "state_busy"),
        ):
            self.run_batch()
        self.parse.side_effect = extractor.ExtractorError(
            "document_extractor_cleanup_failed"
        )
        with self.assertRaisesRegex(extractor.ExtractorError, "cleanup_failed"):
            self.run_batch()
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(batches.status(self.state, self.plan_sha)["pending_count"], 2)


if __name__ == "__main__":
    unittest.main()
