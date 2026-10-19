"""Prove completed image authority against real private staging receipts."""

from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from search.operations.deployment import (
    image_authority,
    image_build,
    inference,
    runtime_docker,
    source_bundle,
)
from search.operations.shared import private_io
from search.operations.state import locked_state
from tests.unit.deployment.image_fixtures import ImageStagingFixture


ROOT = Path(__file__).resolve().parents[3]


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
from search.operations.deployment import image_authority
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
