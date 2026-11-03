"""Keep offline wrappers source-bound without renewing frozen acquisition."""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.catalog.build import builder, identity
from search.operations.documents import (
    batches,
    cache,
    controller,
    derivation,
    enrichment,
    extractor,
    source,
)
from search.operations.shared import private_io


IMAGE = {
    "image_id": "sha256:" + "a" * 64,
    "source_sha256": "b" * 64,
    "base_image": extractor.FESS_IMAGE,
}
REVISION = "f" * 40
DATASET = "00000000-0000-4000-8000-000000000001"


class DerivationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.origin = self.root / "origin"
        self.target = self.root / "target"
        self.ids = [
            f"{index:08x}-1111-4111-8111-111111111111" for index in range(1, 4)
        ]
        self.csv = b"station,pH\nfixtureValue,7.2\n"
        self.pdf = b"%PDF-empty-fixture"
        self.rows = [
            {
                "id": DATASET,
                "name": "water",
                "title": "Original water",
                "private": False,
                "state": "active",
                "type": "dataset",
                "resources": [self.resource(i) for i in range(3)],
            }
        ]
        self.selection = self.root / "selection.json"
        self.selection.write_bytes(private_io.canonical(self.ids))
        self.selection.chmod(0o600)
        self.a = self.build("a")
        self.b = self.build("c")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(
                extractor,
                "source_identity",
                return_value=IMAGE["source_sha256"],
            )
        )
        self.aplan = self.prepare(self.a, self.origin)
        self.bplan = self.prepare(self.b, self.target)
        with (
            patch.object(source.time, "time", return_value=100),
            patch.object(source, "fetch", side_effect=self.fetch),
            patch.object(extractor, "build_image", return_value=IMAGE),
            patch.object(extractor, "extract", side_effect=self.parse),
        ):
            self.original_report = batches.run(
                self.a["bundle_path"], self.origin, self.aplan
            )

    def resource(self, index):
        format_ = "PDF" if index == 2 else "CSV"
        return {
            "id": self.ids[index],
            "name": "Water samples",
            "format": format_,
            "url": (
                "https://data.depositar.io/fixture/"
                + self.ids[index]
                + "."
                + format_.lower()
            ),
            "size": len(self.pdf if format_ == "PDF" else self.csv),
            "last_modified": "2026-10-01",
        }

    def build(self, source_hash, *, rows=None):
        path = self.root / ("snapshot-" + source_hash + ".jsonl")
        path.write_bytes(
            b"".join(private_io.canonical(x) for x in (rows or self.rows))
        )
        path.chmod(0o600)
        output = self.root / "bundles"
        output.mkdir(mode=0o700, exist_ok=True)
        with patch.object(
            identity, "source_digest", return_value=source_hash * 64
        ):
            result = builder.build_bundle(path, output)
        return dict(result, snapshot_path=path)

    def prepare(self, bundle, state):
        return batches.prepare(
            bundle["bundle_path"],
            bundle["manifest_sha256"],
            state,
            self.selection,
        )["plan_sha256"]

    def fetch(self, resource):
        body = self.pdf if resource["format"] == "PDF" else self.csv
        mime = "application/pdf" if resource["format"] == "PDF" else "text/csv"
        return body, {
            "content_type": mime,
            "etag": None,
            "last_modified": None,
        }

    def parse(self, path, expected_format, *_args, **_kwargs):
        is_pdf = expected_format == "PDF"
        return {
            "schema_version": 1,
            "input_sha256": hashlib.sha256(
                Path(path).read_bytes()
            ).hexdigest(),
            "status": "empty_text" if is_pdf else "ok",
            "detected_mime": "application/pdf" if is_pdf else "text/csv",
            "columns": [] if is_pdf else ["station", "pH"],
            "text": "" if is_pdf else "station: fixtureValue; pH: 7.2",
            "text_truncated": False,
            "rows_observed": None if is_pdf else 1,
            "rows_complete": False,
            "may_require_ocr": is_pdf,
        }

    def snapshot(self, root):
        return {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def profile(self, root, identifier):
        outcome = json.loads(
            (root / "outcomes" / (identifier + ".json")).read_bytes()
        )
        return json.loads(
            (
                root / "profiles" / (outcome["profile_sha256"] + ".json")
            ).read_bytes()
        )

    @contextmanager
    def offline(self):
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    derivation, "require_clean_revision", return_value=REVISION
                )
            )
            for owner, name in (
                (source, "fetch"),
                (source, "acquire"),
                (extractor, "extract"),
                (extractor, "build_image"),
                (extractor, "helper_exists"),
                (extractor, "_call"),
            ):
                stack.enter_context(
                    patch.object(owner, name, side_effect=AssertionError(name))
                )
            yield

    def derive(self, **options):
        arguments = {
            "source_bundle": self.a["bundle_path"],
            "source_state_root": self.origin,
            "expected_source_plan_sha256": self.aplan,
            "bundle": self.b["bundle_path"],
            "state_root": self.target,
            "expected_plan_sha256": self.bplan,
        }
        arguments.update(options)
        with self.offline():
            return derivation.derive(**arguments)

    def write(self, path, value):
        path.write_bytes(private_io.canonical(value))
        path.chmod(0o600)

    def replace_profile(self, identifier, profile):
        path = self.origin / "outcomes" / (identifier + ".json")
        outcome = json.loads(path.read_bytes())
        sha = controller.digest(profile)
        self.write(self.origin / "profiles" / (sha + ".json"), profile)
        outcome["profile_sha256"] = sha
        self.write(path, outcome)

    @contextmanager
    def replacement_lock(self, root, name, events):
        path = root / name
        original_inode = path.stat().st_ino
        path.rename(root / (name + "-original"))
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertNotEqual(original_inode, os.fstat(descriptor).st_ino)
            events.append("replacement exclusively held")
            yield
        finally:
            os.close(descriptor)

    def assert_lock_replacement_refused(self, plane):
        owner, name = (
            (cache, "locked") if plane == "cache" else (batches, "_state")
        )
        selected = self.origin if plane == "origin" else self.target
        selected = (
            self.origin / "acquisition" if plane == "cache" else selected
        )
        lock = ".lock" if plane == "cache" else ".profiles.lock"
        original = getattr(owner, name)
        events = []
        lock_names = {
            ".profiles.lock",
            ".lock",
            ".profiles.lock-original",
            ".lock-original",
        }
        before = {
            p: data
            for p, data in self.snapshot(self.origin).items()
            if p.name not in lock_names
        }

        @contextmanager
        def swapped(root, **options):
            with original(root, **options) as value:
                if Path(root) == selected:
                    with self.replacement_lock(Path(root), lock, events):
                        yield value
                else:
                    yield value

        with patch.object(owner, name, side_effect=swapped):
            with self.assertRaises(derivation.DerivationError):
                self.derive()
        self.assertEqual(events, ["replacement exclusively held"])
        self.assertEqual(list((self.target / "profiles").iterdir()), [])
        self.assertEqual(list((self.target / "outcomes").iterdir()), [])
        self.assertEqual(
            batches.status(self.target, self.bplan)["pending_count"], 3
        )
        self.assertEqual(
            before,
            {
                p: data
                for p, data in self.snapshot(self.origin).items()
                if p.name not in lock_names
            },
        )

    def test_replacement_cache_lock_never_becomes_held_proof(self):
        self.assert_lock_replacement_refused("cache")

    def test_replacement_origin_lock_never_becomes_held_proof(self):
        self.assert_lock_replacement_refused("origin")

    def test_replacement_target_lock_never_becomes_held_proof(self):
        self.assert_lock_replacement_refused("target")

    def test_absent_state_proof_never_falls_back_to_named_identity(self):
        original = batches._state

        @contextmanager
        def without_guard(root, **options):
            options.pop("on_lock", None)
            with original(root, **options) as value:
                yield value

        with patch.object(batches, "_state", side_effect=without_guard):
            with self.assertRaises(derivation.DerivationError):
                self.derive()
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_absent_cache_proof_never_falls_back_to_named_identity(self):
        original = cache.locked

        @contextmanager
        def without_guard(root, **options):
            options.pop("on_lock", None)
            with original(root, **options) as value:
                yield value

        with patch.object(cache, "locked", side_effect=without_guard):
            with self.assertRaises(derivation.DerivationError):
                self.derive()
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_unsupported_profile_fields_cannot_supply_lineage(self):
        profile = self.profile(self.origin, self.ids[0])
        profile["lineage"] = {"schema_version": 2}
        self.replace_profile(self.ids[0], profile)
        with self.assertRaises(derivation.DerivationError):
            self.derive()
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_derive_preserves_origin_and_records_real_new_provenance(self):
        before = self.snapshot(self.origin)
        report = self.derive()
        self.assertEqual(report["derived_count"], 3)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["fresh_acquisitions"], 0)
        self.assertEqual(
            report["verified_body_bytes"], 2 * len(self.csv) + len(self.pdf)
        )
        self.assertEqual(self.snapshot(self.origin), before)
        self.assertEqual(
            batches.status(self.target, self.bplan)["status"], "completed"
        )
        proofs = list((self.target / "derivations").iterdir())
        self.assertEqual(len(proofs), 3)
        for identifier in self.ids:
            old, new = (
                self.profile(self.origin, identifier),
                self.profile(self.target, identifier),
            )
            self.assertEqual(old["result"], new["result"])
            self.assertEqual(
                old["source"]["acquired_at"], new["source"]["acquired_at"]
            )
            self.assertEqual(
                new["source"]["catalog_manifest_sha256"],
                self.b["manifest_sha256"],
            )
        proof = json.loads(proofs[0].read_bytes())
        self.assertEqual(proof["operator_revision"], REVISION)
        self.assertEqual(proof["original_acquired_at"], 100)
        self.assertFalse(proof["fresh_acquisition"])
        self.assertFalse(proof["parser_executed"])
        self.assertNotIn("https://", json.dumps(report))
        self.assertNotIn("fixtureValue", json.dumps(report))

    def test_checkpoint_and_resume_never_double_count(self):
        first = self.derive(max_resources=1)
        self.assertEqual(
            (first["derived_count"], first["pending_count"]), (1, 2)
        )
        self.assertEqual(first["unverified_candidate_count"], 2)
        proofs = self.snapshot(self.target / "derivations")
        self.assertEqual(self.derive(max_resources=1)["derived_count"], 1)
        self.assertEqual(self.derive()["status"], "completed")
        self.assertEqual(self.derive()["derived_count"], 0)
        for path, data in proofs.items():
            self.assertEqual(
                (self.target / "derivations" / path).read_bytes(), data
            )

    def test_target_metadata_is_not_taken_from_origin(self):
        rows = deepcopy(self.rows)
        rows[0]["title"] = "Target water"
        self.b = self.build("d", rows=rows)
        self.target = self.root / "changed-target"
        self.bplan = self.prepare(self.b, self.target)
        self.derive()
        paths = list((self.target / "profiles").iterdir())
        contexts, report = enrichment.load_profiles(
            paths,
            self.b["bundle_path"],
            self.b["manifest_sha256"],
            self.b["snapshot_path"],
        )
        self.assertEqual(report["selected_count"], 3)
        self.assertEqual(report["included_count"], 2)
        self.assertEqual(report["skipped_count"], 1)
        self.assertNotIn("Original water", json.dumps(contexts))
        self.assertNotIn("fixtureValue", json.dumps(contexts))

    def test_wrong_hashes_and_same_manifest_publish_nothing(self):
        variants = (
            {"expected_source_plan_sha256": "e" * 64},
            {"expected_plan_sha256": "e" * 64},
            {"bundle": self.a["bundle_path"]},
        )
        before = self.snapshot(self.target)
        for options in variants:
            with (
                self.subTest(options=options),
                self.assertRaises(derivation.DerivationError),
            ):
                self.derive(**options)
            self.assertEqual(before, self.snapshot(self.target))

    def test_alias_nested_and_symlink_roots_refuse_without_repair(self):
        alias = self.root / "alias"
        alias.symlink_to(self.origin, target_is_directory=True)
        for path in (self.origin, self.origin / "nested", alias):
            with (
                self.subTest(path=path),
                self.assertRaises(derivation.DerivationError),
            ):
                self.derive(state_root=path)
        self.assertFalse((self.origin / "nested").exists())

    def test_source_cache_missing_lock_is_never_created(self):
        lock = self.origin / "acquisition/.lock"
        lock.unlink()
        before = self.snapshot(self.origin)
        with self.assertRaisesRegex(
            derivation.DerivationError, "cache_invalid"
        ):
            self.derive()
        self.assertFalse(lock.exists())
        self.assertEqual(before, self.snapshot(self.origin))
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_source_exclusive_state_lock_blocks_derivation(self):
        with controller._state(self.origin, create=False):
            with self.assertRaises(derivation.DerivationError):
                self.derive()
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_source_exclusive_cache_lock_blocks_derivation(self):
        with cache.locked(self.origin / "acquisition"):
            with self.assertRaises(derivation.DerivationError):
                self.derive()
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_target_exclusive_lock_blocks_derivation(self):
        with controller._state(self.target, create=False):
            with self.assertRaises(derivation.DerivationError):
                self.derive()
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_alternate_source_checkout_cannot_label_the_operator(self):
        with patch.object(batches, "_state") as state:
            with self.assertRaisesRegex(
                derivation.DerivationError, "operator_changed"
            ):
                self.derive(source_root=self.root)
            state.assert_not_called()

    def test_root_identity_refuses_before_state_or_source_validation(self):
        for name in ("getuid", "getgid", "geteuid", "getegid"):
            with (
                self.subTest(name=name),
                patch.object(extractor.os, name, return_value=0),
            ):
                with patch.object(batches, "_state") as state:
                    with self.assertRaisesRegex(
                        extractor.ExtractorError, "must_be_nonroot"
                    ):
                        self.derive()
                    state.assert_not_called()

    def test_source_profile_corruption_is_not_repaired(self):
        path = next((self.origin / "profiles").iterdir())
        original = json.loads(path.read_bytes())
        original["source"]["acquired_at"] += 1
        self.write(path, original)
        before = self.snapshot(self.origin)
        with self.assertRaises(derivation.DerivationError):
            self.derive()
        self.assertEqual(before, self.snapshot(self.origin))

    def test_body_corruption_and_absence_have_no_fallback(self):
        first = self.profile(self.origin, self.ids[0])
        path = (
            self.origin
            / "acquisition"
            / cache.body_name(first["source"]["content_sha256"])
        )
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(
            derivation.DerivationError, "cache_invalid"
        ):
            self.derive()
        path.unlink()
        with self.assertRaisesRegex(
            derivation.DerivationError, "cache_invalid"
        ):
            self.derive()
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_missing_extraction_refuses_without_parser_or_write(self):
        for path in (self.origin / "extractions").iterdir():
            path.unlink()
        with self.assertRaisesRegex(
            derivation.DerivationError, "cache_missing"
        ):
            self.derive()
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_invalid_extraction_is_not_recomputed(self):
        for path in (self.origin / "extractions").iterdir():
            value = json.loads(path.read_bytes())
            value["identity"]["encoding"] = "cp950"
            self.write(path, value)
        with self.assertRaisesRegex(
            derivation.DerivationError, "cache_invalid"
        ):
            self.derive()
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_unsafe_source_body_is_refused_without_publication(self):
        first = self.profile(self.origin, self.ids[0])
        path = (
            self.origin
            / "acquisition"
            / cache.body_name(first["source"]["content_sha256"])
        )
        path.chmod(0o644)
        with self.assertRaisesRegex(
            derivation.DerivationError, "cache_invalid"
        ):
            self.derive()
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_missing_receipt_remains_pending_without_download(self):
        for path in (self.origin / "acquisition").glob("resource-*.json"):
            path.unlink()
        with self.assertRaisesRegex(
            derivation.DerivationError, "cache_missing"
        ):
            self.derive()
        self.assertEqual(
            batches.status(self.target, self.bplan)["pending_count"], 3
        )

    def test_byte_limit_refuses_before_reading_a_body(self):
        original = private_io.read_file
        bodies = []

        def read(parent, name, **options):
            if name.endswith(".bin"):
                bodies.append(name)
            return original(parent, name, **options)

        with patch.object(private_io, "read_file", side_effect=read):
            with self.assertRaisesRegex(
                derivation.DerivationError, "budget_exhausted"
            ):
                self.derive(max_bytes=1)
        self.assertEqual(bodies, [])
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_deadline_refuses_before_target_publication(self):
        with patch.object(derivation.time, "monotonic", side_effect=[0, 1]):
            with self.assertRaisesRegex(
                derivation.DerivationError, "deadline_exceeded"
            ):
                self.derive(wall_seconds=1)
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_invalid_numeric_limits_refuse_before_state_io(self):
        variants = (
            {"max_resources": True},
            {"max_resources": 21},
            {"max_bytes": 0},
            {"max_bytes": derivation.MAX_BYTES + 1},
            {"wall_seconds": True},
            {"wall_seconds": 301},
        )
        for options in variants:
            with (
                self.subTest(options=options),
                patch.object(batches, "_state") as state,
            ):
                with self.assertRaisesRegex(
                    derivation.DerivationError, "options_invalid"
                ):
                    self.derive(**options)
                state.assert_not_called()

    def test_resource_key_drift_refuses_before_profile_publication(self):
        changes = (
            ("url", "https://data.depositar.io/fixture/changed.csv"),
            ("format", "PDF"),
            ("size", 999),
            ("last_modified", "2026-10-02"),
        )
        for index, (key, value) in enumerate(changes):
            rows = deepcopy(self.rows)
            rows[0]["resources"][0][key] = value
            self.b = self.build(str(index + 4), rows=rows)
            self.target = self.root / ("key-target-" + str(index))
            self.bplan = self.prepare(self.b, self.target)
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(
                    derivation.DerivationError, "identity_changed"
                ),
            ):
                self.derive()
            self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_parser_identity_and_policy_drift_refuse(self):
        self.write(
            self.target / "tool.json",
            dict(IMAGE, image_id="sha256:" + "c" * 64),
        )
        with self.assertRaisesRegex(
            derivation.DerivationError, "identity_changed"
        ):
            self.derive()
        (self.target / "tool.json").unlink()
        with patch.object(extractor, "source_identity", return_value="d" * 64):
            with self.assertRaisesRegex(
                derivation.DerivationError, "identity_changed"
            ):
                self.derive()
        self.target = self.root / "different-policy"
        self.bplan = batches.prepare(
            self.b["bundle_path"],
            self.b["manifest_sha256"],
            self.target,
            self.selection,
            header_policy="absent",
        )["plan_sha256"]
        with self.assertRaisesRegex(
            derivation.DerivationError, "identity_changed"
        ):
            self.derive()

    def test_missing_and_failed_source_rows_stay_in_target_denominator(self):
        missing = self.origin / "outcomes" / (self.ids[0] + ".json")
        missing.unlink()
        failed_path = self.origin / "outcomes" / (self.ids[1] + ".json")
        failed = json.loads(failed_path.read_bytes())
        for field in (
            "profile_sha256",
            "acquisition_cache_hit",
            "extraction_cache_hit",
        ):
            failed.pop(field)
        failed.update(status="failed", code="document_source_http_failed")
        self.write(failed_path, failed)
        report = self.derive()
        self.assertEqual(
            (
                report["selected_count"],
                report["profile_count"],
                report["pending_count"],
            ),
            (3, 1, 2),
        )
        self.assertEqual(
            report["unavailable_counts"],
            {
                "document_derivation_source_missing": 1,
                "document_derivation_source_ineligible": 1,
            },
        )
        self.assertEqual(report["status"], "checkpoint")
        self.assertEqual(
            self.profile(self.target, self.ids[-1])["result"]["status"],
            "empty_text",
        )

    def test_uncertain_source_outcome_refuses_without_helper_probe(self):
        path = self.origin / "outcomes" / (self.ids[0] + ".json")
        value = json.loads(path.read_bytes())
        for field in (
            "profile_sha256",
            "acquisition_cache_hit",
            "extraction_cache_hit",
        ):
            value.pop(field)
        value["status"] = "running"
        self.write(path, value)
        with self.assertRaisesRegex(
            derivation.DerivationError, "source_incomplete"
        ):
            self.derive()
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_existing_target_failure_is_not_implicitly_retried(self):
        source_path = self.origin / "outcomes" / (self.ids[0] + ".json")
        failed = json.loads(source_path.read_bytes())
        for field in (
            "profile_sha256",
            "acquisition_cache_hit",
            "extraction_cache_hit",
        ):
            failed.pop(field)
        failed.update(
            plan_sha256=self.bplan,
            status="failed",
            code="document_source_timeout",
        )
        path = self.target / "outcomes" / source_path.name
        self.write(path, failed)
        before = path.read_bytes()
        report = self.derive()
        self.assertEqual(report["derived_count"], 2)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            batches.status(self.target, self.bplan)["failed_count"], 1
        )

    def test_lineage_is_durable_before_profile_or_outcome(self):
        actual = controller.publish_profile
        observed = []

        def publish(profiles, profile):
            proofs = [
                json.loads(p.read_bytes())
                for p in (self.target / "derivations").iterdir()
            ]
            sha = controller.digest(profile)
            self.assertTrue(
                any(p["target_profile_sha256"] == sha for p in proofs)
            )
            self.assertFalse(
                (
                    self.target
                    / "outcomes"
                    / (profile["source"]["resource_id"] + ".json")
                ).exists()
            )
            observed.append(sha)
            return actual(profiles, profile)

        with patch.object(controller, "publish_profile", side_effect=publish):
            self.derive()
        self.assertEqual(len(observed), 3)

    def test_interruption_preserves_lineage_and_resumes_without_overwrite(
        self,
    ):
        actual = controller.publish_profile

        def interrupted(*args):
            actual(*args)
            raise KeyboardInterrupt

        with patch.object(
            controller, "publish_profile", side_effect=interrupted
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.derive()
        self.assertEqual(list((self.target / "outcomes").iterdir()), [])
        evidence = self.snapshot(self.target / "derivations")
        self.assertEqual(len(evidence), 1)
        self.assertEqual(self.derive()["status"], "completed")
        for path, data in evidence.items():
            self.assertEqual(
                (self.target / "derivations" / path).read_bytes(), data
            )

    def test_replaced_target_profile_directory_cannot_publish(self):
        actual = derivation._lineage

        def replaced(*args):
            (self.target / "profiles").rename(
                self.target / "displaced-profiles"
            )
            (self.target / "profiles").mkdir(mode=0o700)
            return actual(*args)

        with patch.object(derivation, "_lineage", side_effect=replaced):
            with self.assertRaises(derivation.DerivationError):
                self.derive()
        self.assertEqual(list((self.target / "outcomes").iterdir()), [])
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_replaced_evidence_directory_cannot_complete_outcome(self):
        actual = controller.publish_profile

        def replaced(*args):
            result = actual(*args)
            (self.target / "derivations").rename(
                self.target / "displaced-proofs"
            )
            (self.target / "derivations").mkdir(mode=0o700)
            return result

        with patch.object(controller, "publish_profile", side_effect=replaced):
            with self.assertRaises(derivation.DerivationError):
                self.derive()
        self.assertEqual(list((self.target / "outcomes").iterdir()), [])

    def test_replaced_source_state_lock_refuses_before_publication(self):
        actual = derivation._lineage

        def replaced(*args):
            (self.origin / ".profiles.lock").rename(
                self.origin / "original-lock"
            )
            path = self.origin / ".profiles.lock"
            path.write_bytes(b"")
            path.chmod(0o600)
            return actual(*args)

        with patch.object(derivation, "_lineage", side_effect=replaced):
            with self.assertRaises(derivation.DerivationError):
                self.derive()
        self.assertEqual(list((self.target / "profiles").iterdir()), [])
        self.assertEqual(list((self.target / "outcomes").iterdir()), [])

    def test_future_acquisition_time_is_not_renewed_or_accepted(self):
        identifier = self.ids[0]
        old = self.profile(self.origin, identifier)
        old["source"]["acquired_at"] = 9999999999
        self.replace_profile(identifier, old)
        record = next(
            p
            for p in (self.origin / "acquisition").glob("resource-*.json")
            if json.loads(p.read_bytes())["source"]["resource_id"]
            == identifier
        )
        value = json.loads(record.read_bytes())
        value["acquired_at"] = old["source"]["acquired_at"]
        self.write(record, value)
        before = self.snapshot(self.origin)
        with self.assertRaisesRegex(
            derivation.DerivationError, "identity_changed"
        ):
            self.derive()
        self.assertEqual(self.snapshot(self.origin), before)
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_twenty_resource_limit_uses_fixed_target_order(self):
        self.ids = [
            f"{i:08x}-1111-4111-8111-111111111111" for i in range(1, 22)
        ]
        base = self.rows[0]
        self.rows = [
            dict(
                base,
                id=f"{n + 1:08x}-0000-4000-8000-000000000000",
                name="water-" + str(n),
                resources=[
                    self.resource(i)
                    for i in range(start, min(start + 3, len(self.ids)))
                ],
            )
            for n, start in enumerate(range(0, len(self.ids), 3))
        ]
        self.write(self.selection, self.ids)
        self.a, self.b = self.build("8"), self.build("9")
        self.origin, self.target = (
            self.root / "large-origin",
            self.root / "large-target",
        )
        self.aplan, self.bplan = (
            self.prepare(self.a, self.origin),
            self.prepare(self.b, self.target),
        )
        with (
            patch.object(source.time, "time", return_value=100),
            patch.object(source, "fetch", side_effect=self.fetch),
            patch.object(extractor, "build_image", return_value=IMAGE),
            patch.object(extractor, "extract", side_effect=self.parse),
        ):
            batches.run(self.a["bundle_path"], self.origin, self.aplan)
            batches.run(self.a["bundle_path"], self.origin, self.aplan)
        report = self.derive()
        self.assertEqual(
            (report["derived_count"], report["pending_count"]), (20, 1)
        )
        self.assertFalse(
            (self.target / "outcomes" / (self.ids[-1] + ".json")).exists()
        )
        self.assertEqual(self.derive()["status"], "completed")

    def test_linked_source_body_cannot_be_reused(self):
        first = self.profile(self.origin, self.ids[0])
        path = (
            self.origin
            / "acquisition"
            / cache.body_name(first["source"]["content_sha256"])
        )
        os.link(path, self.root / "body-alias")
        with self.assertRaisesRegex(
            derivation.DerivationError, "cache_invalid"
        ):
            self.derive()
        self.assertEqual(list((self.target / "profiles").iterdir()), [])

    def test_corrupt_orphan_lineage_is_not_overwritten_on_resume(self):
        with patch.object(
            controller, "publish_profile", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.derive(max_resources=1)
        path = next((self.target / "derivations").iterdir())
        path.write_bytes(b"corrupt")
        with self.assertRaises(derivation.DerivationError):
            self.derive(max_resources=1)
        self.assertEqual(path.read_bytes(), b"corrupt")
        self.assertEqual(list((self.target / "outcomes").iterdir()), [])

    def test_profile_publisher_cannot_break_the_lineage_link(self):
        with patch.object(
            controller, "publish_profile", return_value="e" * 64
        ):
            with self.assertRaises(derivation.DerivationError):
                self.derive(max_resources=1)
        self.assertEqual(list((self.target / "outcomes").iterdir()), [])

    def test_frozen_fixture_has_duplicate_content_and_valid_empty_text(self):
        self.assertNotEqual(
            self.a["manifest_sha256"], self.b["manifest_sha256"]
        )
        self.assertEqual(len(list((self.origin / "extractions").iterdir())), 2)
        self.assertEqual(
            len(list((self.origin / "acquisition").glob("sha256-*.bin"))), 2
        )
        self.assertEqual(self.original_report["profile_count"], 3)
        self.assertEqual(
            self.profile(self.origin, self.ids[-1])["result"]["status"],
            "empty_text",
        )

    def test_old_profile_rejects_new_builder_manifest(self):
        path = next((self.origin / "profiles").iterdir())
        with self.assertRaisesRegex(
            enrichment.EnrichmentError, "provenance_invalid"
        ):
            enrichment.load_profiles(
                [path],
                self.b["bundle_path"],
                self.b["manifest_sha256"],
                self.b["snapshot_path"],
            )


if __name__ == "__main__":
    unittest.main()
