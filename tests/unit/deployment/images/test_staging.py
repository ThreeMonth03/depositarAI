"""Real clean Git/state and fake Docker coverage for the private image adapter."""

from __future__ import annotations

import unittest
from unittest import mock

from search.operations.deployment import source_bundle
from search.operations.deployment.images import build as image_build
from search.operations.deployment.images import staging as image_staging
from search.operations.shared import private_io
from search.operations.state import locked_state
from tests.unit.deployment.images.fixtures import (
    FakeDocker as FakeDocker,
    ImageStagingFixture,
    fixture_sources as fixture_sources,
    fixture_tree as fixture_tree,
)


class ProductImageStagingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ImageStagingFixture(self)

    def test_fresh_private_stage_build_receipts_and_product_pointer_bind_full_source(
        self,
    ):
        real_stage = source_bundle.stage_source_bundle

        def check_intent(*args, **kwargs):
            self.assertTrue(self.fixture.record("stage-started").exists())
            self.assertFalse(self.fixture.record("stage-complete").exists())
            self.assertFalse(self.fixture.slot.exists())
            return real_stage(*args, **kwargs)

        with mock.patch.object(
            source_bundle, "stage_source_bundle", side_effect=check_intent
        ):
            result = self.fixture.invoke()
        self.assertEqual(self.fixture.revision, result["revision"])
        self.assertEqual(str(self.fixture.slot), result["staged_root"])
        self.assertFalse(result["source_staging_reused"])
        self.assertFalse(result["runtime_verified"])
        self.assertFalse(result["active_changed"])
        self.assertFalse(result["services_started"])
        self.assertFalse(result["registry_published"])
        self.assertEqual(
            image_build._digest(self.fixture.plan), result["source_manifest_sha256"]
        )
        self.assertEqual(
            self.fixture.plan["source_map_sha256"], result["source_map_sha256"]
        )
        self.assertEqual(set(image_build.SERVICES), set(result["images"]))
        self.assertEqual(
            4, sum(call[0][5] == "build" for call in self.fixture.docker.calls)
        )
        candidate = dict(result)
        del candidate["source_staging_reused"], candidate["runtime_verified"]
        self.assertEqual(candidate, self.fixture.read("image-complete"))
        with locked_state(self.fixture.config) as store:
            self.assertEqual(candidate, store.read("image-candidate.json"))
        for phase in ("stage-started", "stage-complete", "image-complete"):
            self.assertEqual(0o600, self.fixture.record(phase).stat().st_mode & 0o777)
            self.assertNotIn(
                "never-save-this-value", self.fixture.record(phase).read_text()
            )
            self.assertNotIn("PRIVATE_SENTINEL", self.fixture.record(phase).read_text())

    def test_completed_reuse_authenticates_git_and_reinspects_images_without_build(
        self,
    ):
        first = self.fixture.invoke()
        before = {
            phase: self.fixture.record(phase).stat().st_mtime_ns
            for phase in (
                "stage-started",
                "stage-complete",
                "image-complete",
            )
        }
        self.fixture.docker.calls.clear()
        with mock.patch.object(source_bundle, "stage_source_bundle") as stage:
            second = self.fixture.invoke()
        stage.assert_not_called()
        self.assertTrue(second["source_staging_reused"])
        self.assertEqual(first["images"], second["images"])
        self.assertEqual(
            0, sum(call[0][5] == "build" for call in self.fixture.docker.calls)
        )
        self.assertEqual(
            8, sum(call[0][5] == "image" for call in self.fixture.docker.calls)
        )
        self.assertEqual(
            before,
            {phase: self.fixture.record(phase).stat().st_mtime_ns for phase in before},
        )

    def test_dirty_checkout_or_different_source_sha_fails_before_staging(self):
        with self.assertRaises(image_staging.ImageStagingError):
            self.fixture.invoke(source_revision="b" * 40)
        self.assertFalse(self.fixture.parent.exists())
        (self.fixture.repo / "search/api/conversation/app.py").write_text(
            "dirty source\n"
        )
        with self.assertRaises(image_staging.ImageStagingError):
            self.fixture.invoke()
        self.assertFalse(self.fixture.parent.exists())
        self.assertEqual([], self.fixture.docker.calls)

    def test_source_failure_leaves_intent_and_does_not_adopt_or_retry_partial(self):
        with mock.patch.object(
            source_bundle, "stage_source_bundle", side_effect=ValueError("sentinel")
        ) as stage:
            with self.assertRaises(image_staging.ImageStagingError):
                self.fixture.invoke()
            self.assertTrue(self.fixture.record("stage-started").exists())
            with self.assertRaisesRegex(
                image_staging.ImageStagingError, "partial_source_state"
            ):
                self.fixture.invoke()
        self.assertEqual(1, stage.call_count)
        self.assertFalse(self.fixture.record("stage-complete").exists())
        self.assertFalse(
            (self.fixture.config.state_root / "image-candidate.json").exists()
        )
        self.assertEqual([], self.fixture.docker.calls)

    def test_unmarked_existing_source_root_is_not_adopted(self):
        with locked_state(self.fixture.config, create=True):
            self.fixture.parent.mkdir(mode=0o700)
            self.fixture.slot.mkdir(mode=0o700)
            marker = self.fixture.slot / "preserve-legacy"
            marker.write_bytes(b"preserve evidence")
        with self.assertRaisesRegex(
            image_staging.ImageStagingError, "partial_source_state"
        ):
            self.fixture.invoke()
        self.assertEqual(b"preserve evidence", marker.read_bytes())
        self.assertEqual([], self.fixture.docker.calls)

    def test_independent_git_plan_rejects_consistent_tampered_manifest_and_receipts(
        self,
    ):
        self.fixture.invoke()
        expected_pointer = (
            self.fixture.config.state_root / "image-candidate.json"
        ).read_bytes()
        changed_sources = dict(self.fixture.sources)
        changed_sources["web/src/styles.css"] += b"changed css\n"
        tree = fixture_tree(changed_sources)
        tree["search/catalog/query.py"]["mode"] = "100755"
        fake_plan = source_bundle.plan_source_bundle(
            self.fixture.revision, tree, changed_sources
        )
        (self.fixture.slot / "manifest.json").write_bytes(
            private_io.canonical(fake_plan)
        )
        (self.fixture.slot / "context/web/src/styles.css").write_bytes(
            changed_sources["web/src/styles.css"]
        )
        for phase in ("stage-started", "stage-complete"):
            changed = self.fixture.read(phase)
            changed["source_manifest_sha256"] = image_build._digest(fake_plan)
            changed["source_map_sha256"] = fake_plan["source_map_sha256"]
            self.fixture.change_record(phase, changed)
        self.fixture.docker.calls.clear()
        with self.assertRaisesRegex(
            image_staging.ImageStagingError, "source_intent_changed"
        ):
            self.fixture.invoke()
        self.assertEqual([], self.fixture.docker.calls)
        self.assertEqual(
            expected_pointer,
            (self.fixture.config.state_root / "image-candidate.json").read_bytes(),
        )

    def test_build_failure_cannot_publish_and_ambiguous_image_intent_is_not_retried(
        self,
    ):
        self.fixture.docker.return_failure = True
        with self.assertRaises(image_build.ImageBuildError):
            self.fixture.invoke()
        self.assertTrue(self.fixture.record("stage-complete").exists())
        self.assertTrue(
            (self.fixture.slot / "images/conversation-api/started.json").exists()
        )
        self.assertFalse(self.fixture.record("image-complete").exists())
        self.assertFalse(
            (self.fixture.config.state_root / "image-candidate.json").exists()
        )
        self.fixture.docker.return_failure = False
        self.fixture.docker.calls.clear()
        with self.assertRaisesRegex(image_build.ImageBuildError, "partial_state"):
            self.fixture.invoke()
        self.assertEqual([], self.fixture.docker.calls)

    def test_image_proof_tampering_is_rejected_before_pointer_update(self):
        self.fixture.invoke()
        expected_pointer = (
            self.fixture.config.state_root / "image-candidate.json"
        ).read_bytes()
        changed = self.fixture.read("image-complete")
        changed["images"]["web"]["image_environment_sha256"] = "f" * 64
        self.fixture.change_record("image-complete", changed)
        with self.assertRaisesRegex(
            image_staging.ImageStagingError, "completed_images_changed"
        ):
            self.fixture.invoke()
        self.assertEqual(
            expected_pointer,
            (self.fixture.config.state_root / "image-candidate.json").read_bytes(),
        )

    def test_checkout_mutation_during_build_cannot_publish_image_candidate(self):
        def change_source(service, iid):
            if service == "web-nginx":
                (self.fixture.repo / "search/api/conversation/app.py").write_text(
                    "changed checkout during build\n"
                )

        self.fixture.docker.on_build = change_source
        with self.assertRaises(image_staging.ImageStagingError):
            self.fixture.invoke()
        self.assertFalse(self.fixture.record("image-complete").exists())
        self.assertFalse(
            (self.fixture.config.state_root / "image-candidate.json").exists()
        )

    def test_late_stage_receipt_change_is_rejected_before_pointer_publication(self):
        def change_receipt(service, iid):
            if service == "web-nginx":
                changed = self.fixture.read("stage-complete")
                changed["schema_version"] = True
                self.fixture.change_record("stage-complete", changed)

        self.fixture.docker.on_build = change_receipt
        with self.assertRaisesRegex(
            image_staging.ImageStagingError, "source_receipt_changed"
        ):
            self.fixture.invoke()
        self.assertFalse(
            (self.fixture.config.state_root / "image-candidate.json").exists()
        )

    def test_unknown_state_symlinks_and_retention_are_refused_without_deletion(self):
        with locked_state(self.fixture.config, create=True):
            self.fixture.parent.mkdir(mode=0o700)
        unknown = self.fixture.parent / "unknown"
        unknown.write_bytes(b"preserve")
        with self.assertRaisesRegex(image_staging.ImageStagingError, "unknown_state"):
            self.fixture.invoke()
        self.assertEqual(b"preserve", unknown.read_bytes())
        unknown.unlink()
        self.fixture.slot.symlink_to(self.fixture.repo, target_is_directory=True)
        with self.assertRaisesRegex(
            image_staging.ImageStagingError, "partial_source_state"
        ):
            self.fixture.invoke()
        self.assertTrue(self.fixture.slot.is_symlink())
        self.fixture.slot.unlink()
        with mock.patch.object(image_staging, "MAX_SOURCE_SLOTS", 0):
            with self.assertRaisesRegex(
                image_staging.ImageStagingError, "retention_limit"
            ):
                self.fixture.invoke()
        self.assertEqual([], self.fixture.docker.calls)

    def test_builder_result_requires_closed_exact_types_source_and_image_proofs(self):
        def result():
            return {
                "schema_version": 1,
                "revision": self.fixture.revision,
                "images": {
                    service: source_bundle.validate_built_image(
                        self.fixture.plan, service, raw["Id"], raw
                    )
                    for service, raw in self.fixture.docker.images.items()
                },
                "services_started": False,
                "registry_published": False,
            }

        for mutate in (
            lambda r: r.update(schema_version=True),
            lambda r: r.update(services_started=0),
            lambda r: r.update(extra="unknown"),
            lambda r: r["images"]["web"].update(revision="b" * 40),
            lambda r: r["images"]["web"].update(source_sha256="f" * 64),
            lambda r: r["images"]["web"].update(image_environment_sha256="F" * 64),
            lambda r: r["images"]["web"].update(
                image_id=r["images"]["reranker"]["image_id"]
            ),
        ):
            value = result()
            mutate(value)
            with self.assertRaises(image_staging.ImageStagingError):
                image_staging._image_result(self.fixture.plan, value)
        self.assertEqual(
            result()["images"], image_staging._image_result(self.fixture.plan, result())
        )


if __name__ == "__main__":
    unittest.main()
