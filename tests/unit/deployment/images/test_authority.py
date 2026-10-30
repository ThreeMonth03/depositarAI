"""Prove completed image authority against real private staging receipts."""

import copy
import hashlib
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from search.operations.deployment import inference, runtime_docker, source_bundle
from search.operations.deployment.images import authority as image_authority
from search.operations.deployment.images import build as image_build
from search.operations.shared import private_io
from search.operations.state import locked_state
from tests.unit.deployment.images.fixtures import ImageStagingFixture, fixture_tree


ROOT = Path(__file__).resolve().parents[4]


class ImageAuthorityBoundaryTests(unittest.TestCase):
    def test_image_authority_uses_the_shared_runtime_error(self):
        self.assertIs(image_authority.InferenceError, runtime_docker.InferenceError)
        self.assertIs(inference.InferenceError, image_authority.InferenceError)

    def test_fresh_import_does_not_load_controllers_or_model_dependencies(self):
        script = """
import subprocess
import sys
sys.path.insert(0, sys.argv[1])
def forbidden_process(*args, **kwargs):
    raise AssertionError("image authority import started a process")
subprocess.Popen = forbidden_process
from search.operations.deployment.images import authority as image_authority
from search.operations.deployment.runtime_docker import InferenceError
assert image_authority.InferenceError is InferenceError
for name in (
    "search.operations.deployment.inference",
    "search.operations.deployment.release",
    "search.operations.deployment.gateway",
    "search.operations.deployment.gateway.activation",
    "search.operations.deployment.packaging",
    "search.operations.deployment.inference_runtime",
    "search.operations.deployment.gpu_ownership",
    "search.operations.doctor", "search.reranker.model",
    "scripts.prepare_search_models", "search.api",
    "torch", "fastapi", "django", "rest_framework", "adrf", "pydantic",
):
    assert not any(
        loaded == name or loaded.startswith(name + ".") for loaded in sys.modules
    ), name
"""
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script, str(ROOT)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)


class CompletedImageAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ImageStagingFixture(self)
        self.fixture.invoke()
        self.fixture.docker.calls.clear()

    def read(self):
        with locked_state(self.fixture.config) as store:
            return image_authority.read_completed_images(
                self.fixture.config, store, self.fixture.plan
            )

    def current_plan(self, *, changed="web/src/main.tsx"):
        sources = dict(self.fixture.sources)
        sources[changed] += b"\n# new source\n"
        tree = fixture_tree(sources)
        tree["search/catalog/query.py"]["mode"] = "100755"
        return source_bundle.plan_source_bundle("f" * 40, tree, sources)

    def read_origin(self, current):
        with locked_state(self.fixture.config) as store:
            return image_authority.read_completed_images_at_revision(
                self.fixture.config, store, current, self.fixture.revision
            )

    def test_original_authority_preserves_build_revision_across_ui_source_change(self):
        current = self.current_plan()
        paths = list(self.fixture.config.state_root.rglob("*.json"))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        original, authority = self.read_origin(current)
        self.assertEqual(self.fixture.plan, original)
        self.assertEqual(self.fixture.revision, authority["revision"])
        self.assertEqual(self.fixture.read("image-complete"), authority)
        self.assertEqual(
            before,
            {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths},
        )
        image = self.fixture.docker.images["reranker"]
        proof = source_bundle.validate_compatible_built_image(
            current, "reranker", self.fixture.revision, image["Id"], image
        )
        self.assertEqual(authority["images"]["reranker"], proof)
        with self.assertRaises(source_bundle.SourceBundleError):
            source_bundle.validate_built_image(current, "reranker", image["Id"], image)

    def test_unrelated_new_website_recipe_does_not_reinterpret_original_manifest(self):
        path = source_bundle.RECIPES["web"]["dockerfile"]
        sources = dict(self.fixture.sources)
        sources[path] += b"\n# changed UI recipe\n"
        recipes = copy.deepcopy(source_bundle.RECIPES)
        recipes["web"]["dockerfile_sha256"] = hashlib.sha256(sources[path]).hexdigest()
        tree = fixture_tree(sources)
        tree["search/catalog/query.py"]["mode"] = "100755"
        with mock.patch.object(source_bundle, "RECIPES", recipes):
            current = source_bundle.plan_source_bundle("f" * 40, tree, sources)
            with self.assertRaises(source_bundle.SourceBundleError):
                source_bundle._verified_plan(self.fixture.plan)
            original, authority = self.read_origin(current)
        self.assertEqual(self.fixture.plan, original)
        self.assertEqual(self.fixture.revision, authority["revision"])

    def test_changed_reranker_implementation_or_dependencies_cannot_reuse_origin(self):
        for path in ("search/reranker/server.py", "search/reranker/requirements.lock"):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(
                    image_authority.InferenceError,
                    "inference_origin_reranker_source_changed",
                ),
            ):
                self.read_origin(self.current_plan(changed=path))

    def test_origin_build_receipts_and_context_are_freshly_authenticated(self):
        current = self.current_plan()
        record = self.fixture.read("image-complete")
        record["source_manifest_sha256"] = "f" * 64
        self.fixture.change_record("image-complete", record)
        with self.assertRaisesRegex(
            image_authority.InferenceError, "inference_source_receipt_changed"
        ):
            self.read_origin(current)

    def test_origin_revision_labels_cannot_be_replaced_by_new_application_revision(
        self,
    ):
        current = self.current_plan()
        image = copy.deepcopy(self.fixture.docker.images["reranker"])
        image["Config"]["Labels"]["org.opencontainers.image.revision"] = current[
            "revision"
        ]
        with self.assertRaisesRegex(
            source_bundle.SourceBundleError, "source_bundle_image_labels_changed"
        ):
            source_bundle.validate_compatible_built_image(
                current, "reranker", self.fixture.revision, image["Id"], image
            )

    def test_completed_authority_reads_real_receipts_without_writes_or_docker(self):
        paths = list(self.fixture.config.state_root.rglob("*.json"))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        with mock.patch.object(
            subprocess, "Popen", side_effect=AssertionError("unexpected process")
        ):
            authority = self.read()
        self.assertEqual(self.fixture.read("image-complete"), authority)
        self.assertEqual(set(image_build.SERVICES), set(authority["images"]))
        self.assertEqual(
            before,
            {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths},
        )
        self.assertEqual([], self.fixture.docker.calls)

    def test_candidate_and_stage_completion_cannot_attest_changed_identity(self):
        with locked_state(self.fixture.config) as store:
            candidate = store.read("image-candidate.json")
            store.write(
                "image-candidate.json", {**candidate, "extra": True}, replace=True
            )
        with self.assertRaisesRegex(
            image_authority.InferenceError, "^inference_image_candidate_changed$"
        ):
            self.read()
        with locked_state(self.fixture.config) as store:
            store.write("image-candidate.json", candidate, replace=True)
        record = self.fixture.read("image-complete")
        record["images_sha256"] = "f" * 64
        self.fixture.change_record("image-complete", record)
        with self.assertRaisesRegex(
            image_authority.InferenceError, "^inference_source_receipt_changed$"
        ):
            self.read()

    def test_per_image_receipt_must_match_the_completed_authority(self):
        path = self.fixture.slot / "images/reranker/complete.json"
        record = image_build._json(path.read_bytes())
        record["image"]["image_environment_sha256"] = "f" * 64
        path.write_bytes(private_io.canonical(record))
        with self.assertRaisesRegex(
            image_authority.InferenceError, "^inference_built_image_receipt_changed$"
        ):
            self.read()

    def test_staged_source_tamper_keeps_source_bundle_error_classification(self):
        path = self.fixture.slot / "context/search/reranker/server.py"
        path.write_bytes(b"changed staged source\n")
        with self.assertRaises(source_bundle.SourceBundleError):
            self.read()

    def test_missing_and_malformed_receipts_keep_underlying_exception_types(self):
        phase = self.fixture.record("stage-complete")
        raw = phase.read_bytes()
        phase.unlink()
        with self.assertRaises(FileNotFoundError):
            self.read()
        phase.write_bytes(raw)
        phase.chmod(0o600)
        path = self.fixture.slot / "images/reranker/complete.json"
        path.write_bytes(b'{"identity":1,"identity":2}')
        with self.assertRaisesRegex(
            image_build.ImageBuildError, "^image_build_json_invalid$"
        ) as caught:
            self.read()
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual([], self.fixture.docker.calls)
