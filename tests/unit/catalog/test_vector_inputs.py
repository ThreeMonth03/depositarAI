"""Queued vector inputs use static authority, never progress or model work."""

from contextlib import ExitStack
import copy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.catalog import administration, execution, vectors
from tests.unit.catalog.test_vectors import host_context


SOURCE = "a" * 40
LOAD_CONTEXT = execution.load_context


class VectorInputTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.context = host_context(
            self.root, SimpleNamespace(bundle_id="f" * 64), "c" * 64, 2
        )
        self.context["source_revision"] = SOURCE
        state = self.root / "state"
        state.mkdir(mode=0o700)
        execution.write_record(state, "operations.lock", {})
        self.images = {
            "schema_version": 1,
            "project": self.context["plan"]["project"],
            "revision": self.context["plan"]["revision"],
            "images": {"fess-search": {"image_id": "sha256:" + "1" * 64}},
        }
        self.runtime = {
            **execution.catalog_identity(self.context),
            "container_ids": {"fess-search": "2" * 64},
            "network_id": "3" * 64,
        }
        self.save_images()
        sources = {
            name: hashlib.sha256(b"sealed fixture\n").hexdigest()
            for name in administration.ADMIN_FILES
        }
        self.admin_image = {
            "image_id": "sha256:" + "4" * 64,
            "reference": self.context["plan"]["project"] + "-catalog-admin:1",
            "labels": execution.image_labels(
                self.context, "catalog-admin", sources, administration.ADMIN_BASE
            ),
            "sources": sources,
        }
        execution.write_record(self.root, "admin-image.json", self.admin_image)
        self.load = self.patch(execution, "load_context", return_value=self.context)
        self.runtime_check = self.patch(
            execution, "require_runtime", side_effect=self.require_runtime
        )
        self.patch(execution, "sealed_source", return_value=b"sealed fixture\n")
        self.patch(execution, "verify_image", return_value={})
        self.image_build = self.patch(
            execution, "build_image", side_effect=AssertionError("image build")
        )
        self.deep_model = self.stack.enter_context(
            patch("search.operations.doctor._embedding_model")
        )
        self.history = self.patch(vectors, "_history")
        self.secret = self.patch(administration, "_secret")
        self.seed = self.patch(vectors.reuse, "seed", return_value={})
        self.pass_work = self.patch(vectors, "_one_pass")
        self.patch(administration, "_admin_ids", return_value=[])
        self.patch(administration, "_admin_records", return_value=({}, [], {}))

    def patch(self, target, name, **options):
        return self.stack.enter_context(patch.object(target, name, **options))

    def save_images(self):
        execution.write_record(self.root, "images.json", self.images, replace=True)
        self.runtime["images_sha256"] = execution.digest_record(self.images)

    def require_runtime(self, context):
        self.assertIs(context, self.context)
        with self.assertRaises(BlockingIOError):
            with execution.operation_lock(context):
                self.fail("catalog lock was not retained")
        return copy.deepcopy(self.runtime), copy.deepcopy(self.images["images"])

    def lookup(self):
        return vectors.inputs(None, self.context["plan"], repo_root=self.root)

    def build(self, expected):
        return vectors.build(
            None,
            self.context["plan"],
            repo_root=self.root,
            max_passes=1,
            wall_seconds=3600,
            expected_inputs_sha256=expected,
            clock=lambda: 100,
        )

    def assert_no_work(self):
        for operation in (
            self.deep_model,
            self.history,
            self.secret,
            self.seed,
            self.pass_work,
            self.image_build,
        ):
            operation.assert_not_called()

    def test_lookup_is_locked_read_only_and_does_not_require_a_vector_pass(self):
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        recipe = vectors._recipe(self.context)
        plan = copy.deepcopy(self.context["plan"])
        report = self.lookup()
        self.assertEqual(
            {
                "inputs_sha256",
                "source_revision",
                "catalog_revision",
                "bundle_id",
                "manifest_sha256",
                "read_only",
            },
            set(report),
        )
        self.assertRegex(report["inputs_sha256"], r"\A[0-9a-f]{64}\Z")
        self.assertEqual(SOURCE, report["source_revision"])
        self.assertTrue(report["read_only"])
        self.assertEqual(recipe, vectors._recipe(self.context))
        self.assertEqual(plan, self.context["plan"])
        self.assertEqual(
            before,
            {
                path: path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            },
        )
        self.assertFalse((self.root / "vector-operations").exists())
        self.assert_no_work()

    def test_matching_inputs_proceed_and_reuse_read_only_admin_proof(self):
        expected = self.lookup()["inputs_sha256"]
        self.history.return_value = (self.root, "5" * 64, 0, None)
        self.pass_work.return_value = {
            "status": "complete-clean",
            "done_count": 2,
            "pending_count": 0,
        }
        with patch.object(
            administration, "_admin_image", wraps=administration._admin_image
        ) as admin:
            report = self.build(expected)
        self.assertEqual("vectors-complete", report["status"])
        self.assertEqual(1, report["passes_this_invocation"])
        admin.assert_called_once_with(self.context, read_only=True)
        self.deep_model.assert_called_once()
        self.history.assert_called_once()
        self.pass_work.assert_called_once()
        self.image_build.assert_not_called()

    def test_full_static_identity_rejects_catalog_source_model_and_recipe_changes(self):
        for kind in ("catalog", "source", "model", "recipe"):
            with self.subTest(kind=kind), ExitStack() as changes:
                expected = self.lookup()["inputs_sha256"]
                originals = copy.deepcopy(
                    (self.context, self.images, self.runtime, self.admin_image)
                )
                try:
                    if kind == "catalog":
                        self.context["plan"]["project"] = self.context["plan"][
                            "project"
                        ].replace("a" * 12, "9" * 12)
                        self.images["project"] = self.context["plan"]["project"]
                        self.runtime.update(execution.catalog_identity(self.context))
                        self.save_images()
                        self.admin_image["reference"] = (
                            self.context["plan"]["project"] + "-catalog-admin:1"
                        )
                        self.admin_image["labels"] = execution.image_labels(
                            self.context,
                            "catalog-admin",
                            self.admin_image["sources"],
                            administration.ADMIN_BASE,
                        )
                        execution.write_record(
                            self.root,
                            "admin-image.json",
                            self.admin_image,
                            replace=True,
                        )
                    elif kind == "source":
                        self.context["source_revision"] = "b" * 40
                    elif kind == "model":
                        changes.enter_context(
                            patch("search.catalog.embedding.MODEL_DIGEST", "6" * 64)
                        )
                    else:
                        recipe = vectors._recipe(self.context)
                        changes.enter_context(
                            patch.object(
                                vectors,
                                "_recipe",
                                return_value=recipe | {"concurrency": 3},
                            )
                        )
                    with self.assertRaisesRegex(
                        ValueError, "catalog_vector_inputs_changed"
                    ):
                        self.build(expected)
                finally:
                    for current, original in zip(
                        (self.context, self.images, self.runtime, self.admin_image),
                        originals,
                    ):
                        current.clear()
                        current.update(original)
                    self.save_images()
                    execution.write_record(
                        self.root, "admin-image.json", self.admin_image, replace=True
                    )
                self.assert_no_work()

    def test_same_plan_valid_image_admin_and_runtime_resealing_changes_identity(self):
        for kind in ("images", "admin", "runtime"):
            with self.subTest(kind=kind):
                expected = self.lookup()["inputs_sha256"]
                plan = copy.deepcopy(self.context["plan"])
                if kind == "images":
                    self.images["images"]["fess-search"]["image_id"] = (
                        "sha256:" + "7" * 64
                    )
                    self.save_images()
                elif kind == "admin":
                    self.admin_image["image_id"] = "sha256:" + "8" * 64
                    execution.write_record(
                        self.root, "admin-image.json", self.admin_image, replace=True
                    )
                else:
                    self.runtime["network_id"] = "9" * 64
                with self.assertRaisesRegex(
                    ValueError, "catalog_vector_inputs_changed"
                ):
                    self.build(expected)
                self.assertEqual(plan, self.context["plan"])
                self.assert_no_work()

    def test_valid_future_record_fields_participate_without_projection(self):
        for kind in ("plan", "images", "runtime", "admin"):
            with self.subTest(kind=kind), ExitStack() as changes:
                expected = self.lookup()["inputs_sha256"]
                if kind == "plan":
                    self.context["plan"]["future"] = {"version": 2}
                elif kind == "images":
                    self.images["future"] = {"version": 2}
                    self.save_images()
                elif kind == "runtime":
                    self.runtime["future"] = {"version": 2}
                else:
                    changes.enter_context(
                        patch.object(
                            administration,
                            "_admin_image",
                            return_value=self.admin_image | {"future": {"version": 2}},
                        )
                    )
                self.assertNotEqual(expected, self.lookup()["inputs_sha256"])
                self.assert_no_work()

    def test_advanced_checkpoint_progress_does_not_change_expected_inputs(self):
        expected = self.lookup()["inputs_sha256"]
        self.history.return_value = (
            self.root,
            "5" * 64,
            1,
            {
                "status": "partial-clean",
                "done_count": 1,
                "pending_count": 1,
                "after_state_sha256": "6" * 64,
            },
        )
        self.pass_work.return_value = {
            "status": "complete-clean",
            "done_count": 2,
            "pending_count": 0,
        }
        result = self.build(expected)
        self.assertEqual(2, result["completed_passes"])
        self.assertEqual(expected, self.lookup()["inputs_sha256"])

    def test_missing_admin_prerequisite_never_builds_on_lookup_or_expected_run(self):
        expected = self.lookup()["inputs_sha256"]
        (self.root / "admin-image.json").unlink()
        for action in (self.lookup, lambda: self.build(expected)):
            with (
                self.subTest(action=action),
                self.assertRaisesRegex(
                    ValueError, "catalog_admin_image_evidence_missing"
                ),
            ):
                action()
        self.assert_no_work()

    def test_expectation_does_not_override_the_existing_plan_schema_guard(self):
        expected = self.lookup()["inputs_sha256"]
        self.context["plan"]["schema_version"] = True
        with patch.object(execution, "load_context", side_effect=LOAD_CONTEXT):
            with self.assertRaisesRegex(ValueError, "catalog_plan_invalid"):
                self.build(expected)
        self.assert_no_work()

    def test_expectation_does_not_override_runtime_or_recipe_validation(self):
        expected = self.lookup()["inputs_sha256"]
        with patch.object(
            execution,
            "require_runtime",
            side_effect=vectors.RuntimeError("catalog_runtime_receipt_changed"),
        ):
            with self.assertRaisesRegex(ValueError, "catalog_runtime_receipt_changed"):
                self.build(expected)
        self.context["compose"]["services"]["fess-search"]["environment"][
            "FESS_JAVA_OPTS"
        ] = "unreviewed profile"
        with self.assertRaisesRegex(ValueError, "catalog_vector_profile_changed"):
            self.build(expected)
        self.assert_no_work()

    def test_expectation_keeps_closed_admin_record_validation(self):
        expected = self.lookup()["inputs_sha256"]
        execution.write_record(
            self.root,
            "admin-image.json",
            self.admin_image | {"unrecognized": True},
            replace=True,
        )
        with self.assertRaisesRegex(ValueError, "catalog_admin_image_receipt_changed"):
            self.build(expected)
        self.assert_no_work()

    def test_matching_expectation_still_checks_model_bytes_and_clean_history(self):
        expected = self.lookup()["inputs_sha256"]
        self.deep_model.side_effect = ValueError("synthetic bad model bytes")
        with self.assertRaisesRegex(ValueError, "synthetic bad model bytes"):
            self.build(expected)
        self.history.assert_not_called()
        self.deep_model.side_effect = None
        self.history.side_effect = vectors.RuntimeError(
            "catalog_vector_pass_requires_explicit_recovery"
        )
        with self.assertRaisesRegex(
            ValueError, "catalog_vector_pass_requires_explicit_recovery"
        ):
            self.build(expected)
        self.secret.assert_not_called()
        self.seed.assert_not_called()
        self.pass_work.assert_not_called()
        self.image_build.assert_not_called()

    def test_runtime_or_images_record_drift_is_refused_before_model_work(self):
        expected = self.lookup()["inputs_sha256"]
        self.images["future"] = 1
        execution.write_record(self.root, "images.json", self.images, replace=True)
        with self.assertRaisesRegex(ValueError, "catalog_vector_inputs_changed"):
            self.build(expected)
        self.assert_no_work()

    def test_second_images_read_with_a_nondict_has_a_closed_diagnostic(self):
        expected = self.lookup()["inputs_sha256"]
        for value in (None, [], 1, True):
            with (
                self.subTest(value=value),
                patch.object(execution, "read_record", return_value=value),
            ):
                for action in (self.lookup, lambda: self.build(expected)):
                    with self.assertRaisesRegex(
                        ValueError, "catalog_vector_inputs_changed"
                    ):
                        action()
        self.assert_no_work()

    def test_malformed_expected_digest_is_refused_before_loading_catalog(self):
        self.load.reset_mock()
        for value in ("", "a" * 63, "A" * 64, "g" * 64, "a" * 64 + "\n", True, 1, {}):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    ValueError, "catalog_vector_expected_inputs_invalid"
                ),
            ):
                self.build(value)
        self.load.assert_not_called()
        self.assert_no_work()
