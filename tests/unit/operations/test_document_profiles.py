"""Protect the single profile processor's reusable extraction and provenance."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.operations.documents import controller, extractor, source


IMAGE = {"image_id": "sha256:" + "a" * 64, "source_sha256": "b" * 64}
DATASET = "00000000-0000-4000-8000-000000000001"
RESOURCE = {"id": "11111111-1111-4111-8111-111111111111", "format": "CSV"}


class DocumentProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.raw = b"pH\n7.2\n"
        self.material = self.root / "input.csv"
        self.material.write_bytes(self.raw)
        self.material.chmod(0o600)
        self.sha = hashlib.sha256(self.raw).hexdigest()
        self.acquired = {
            "local_path": self.material,
            "sha256": self.sha,
            "bytes": len(self.raw),
            "acquired_at": 100,
            "source_url_sha256": "c" * 64,
            "cache_hit": True,
        }
        self.result = {
            "schema_version": 1,
            "input_sha256": self.sha,
            "status": "ok",
            "detected_mime": "text/csv",
            "columns": ["pH"],
            "text": "pH\n7.2",
            "text_truncated": False,
            "rows_observed": 1,
            "rows_complete": False,
            "may_require_ocr": False,
        }

    def process(self, *, manifest="d" * 64, header_policy="first_record"):
        with controller._state(self.root / "state") as parent:
            extractions = controller._child(parent, "extractions")
            profiles = controller._child(parent, "profiles")
            try:
                return controller.process(
                    DATASET,
                    RESOURCE,
                    manifest,
                    self.root / "state",
                    IMAGE,
                    "utf-8",
                    header_policy,
                    extractions,
                    profiles,
                    container_name="depositarai-extract-" + "e" * 32,
                )
            finally:
                import os

                os.close(profiles)
                os.close(extractions)

    def test_extraction_reuse_preserves_bound_profile_and_private_payload(self):
        with (
            patch.object(source, "acquire", return_value=self.acquired),
            patch.object(extractor, "extract", return_value=self.result) as parse,
        ):
            first = self.process()
            second = self.process()
            changed_manifest = self.process(manifest="f" * 64)
        self.assertEqual(parse.call_count, 1)
        self.assertTrue(second["extraction_cache_hit"])
        self.assertEqual(first["profile_sha256"], second["profile_sha256"])
        self.assertNotEqual(first["profile_sha256"], changed_manifest["profile_sha256"])
        self.assertNotIn("7.2", json.dumps(first))
        path = self.root / "state/profiles" / (first["profile_sha256"] + ".json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads(path.read_text())["source"]["content_sha256"], self.sha
        )

    def test_policy_change_reextracts_cached_material(self):
        with (
            patch.object(source, "acquire", return_value=self.acquired),
            patch.object(extractor, "extract", return_value=self.result) as parse,
        ):
            self.process()
            parse.return_value = dict(self.result, columns=[])
            self.process(header_policy="absent")
        self.assertEqual(parse.call_count, 2)
        self.assertEqual(parse.call_args.args[3], "absent")

    def test_corrupt_extraction_cache_is_not_silently_rebuilt(self):
        with (
            patch.object(source, "acquire", return_value=self.acquired),
            patch.object(extractor, "extract", return_value=self.result) as parse,
        ):
            self.process()
            path = next((self.root / "state/extractions").glob("*.json"))
            record = json.loads(path.read_text())
            record["identity"]["encoding"] = "cp950"
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(controller.ProfileError, "cache_invalid"):
                self.process()
        self.assertEqual(parse.call_count, 1)

    def test_wrong_content_digest_does_not_publish_profile(self):
        with (
            patch.object(source, "acquire", return_value=self.acquired),
            patch.object(
                extractor,
                "extract",
                return_value=dict(self.result, input_sha256="0" * 64),
            ),
        ):
            with self.assertRaisesRegex(controller.ProfileError, "content_mismatch"):
                self.process()
        self.assertEqual(list((self.root / "state/profiles").iterdir()), [])

    def test_helper_cleanup_error_is_preserved(self):
        with (
            patch.object(source, "acquire", return_value=self.acquired),
            patch.object(
                extractor,
                "extract",
                side_effect=extractor.ExtractorError(
                    "document_extractor_cleanup_failed"
                ),
            ),
        ):
            with self.assertRaisesRegex(extractor.ExtractorError, "cleanup_failed"):
                self.process()


if __name__ == "__main__":
    unittest.main()
