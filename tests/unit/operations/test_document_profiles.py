"""Protect resource provenance, reusable extraction and the independent CLI."""

from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.catalog.build.builder import build_bundle
from search.operations import cli
from search.operations.documents import controller


RESOURCE = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"
IMAGE = {"image_id": "sha256:" + "a" * 64, "source_sha256": "b" * 64}


class DocumentProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.material = self.root / "input.csv"
        self.raw = b"pH\n7.2\n"
        self.material.write_bytes(self.raw)
        self.material.chmod(0o600)
        self.content_sha = hashlib.sha256(self.raw).hexdigest()
        self.bundle, self.manifest = self.build()
        self.state = self.root / "state"

    def build(self, *, second=False, duplicate=False, notes="Water"):
        resources = [
            {
                "id": RESOURCE,
                "format": "CSV",
                "name": "Water",
                "url": "https://data.depositar.io/water.csv",
            }
        ]
        if second:
            resources.append(
                {
                    "id": SECOND,
                    "format": "CSV",
                    "url": "https://data.depositar.io/second.csv",
                }
            )
        record = {
            "id": "00000000-0000-4000-8000-000000000001",
            "name": "water",
            "private": False,
            "state": "active",
            "type": "dataset",
            "title": "Water",
            "notes": notes,
            "resources": resources,
        }
        records = [record]
        if duplicate:
            records.append(
                dict(record, id="00000000-0000-4000-8000-000000000002", name="other")
            )
        snapshot = self.root / "snapshot.jsonl"
        snapshot.write_bytes(
            b"".join(json.dumps(row).encode() + b"\n" for row in records)
        )
        snapshot.chmod(0o600)
        output = self.root / "bundles"
        output.mkdir(mode=0o700, exist_ok=True)
        result = build_bundle(snapshot, output)
        return Path(result["bundle_path"]), result["manifest_sha256"]

    def acquisition(self):
        return {
            "local_path": self.material,
            "sha256": self.content_sha,
            "bytes": len(self.raw),
            "acquired_at": 100,
            "source_url_sha256": "c" * 64,
            "cache_hit": True,
        }

    def extracted(self, *, status="ok", columns=None):
        return {
            "schema_version": 1,
            "input_sha256": self.content_sha,
            "status": status,
            "detected_mime": "text/csv",
            "columns": ["pH"] if columns is None else columns,
            "text": "pH\n7.2" if status == "ok" else "",
            "text_truncated": False,
            "rows_observed": 1,
            "rows_complete": False,
            "may_require_ocr": False,
        }

    def run_profiles(self, resources=None, **kwargs):
        return controller.run(
            self.bundle, self.manifest, self.state, resources or [RESOURCE], **kwargs
        )

    def mocks(self):
        from search.operations.documents import extractor, source

        return (
            patch.object(source, "acquire", return_value=self.acquisition()),
            patch.object(extractor, "build_image", return_value=IMAGE),
            patch.object(extractor, "extract", return_value=self.extracted()),
        )

    def test_replay_reuses_extraction_and_keeps_payload_private(self):
        acquisition, build, extraction = self.mocks()
        with acquisition as fetch, build, extraction as parse:
            first = self.run_profiles()
            second = self.run_profiles()
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(second["extraction_cache_hits"], 1)
        self.assertEqual(
            first["resources"][0]["profile_sha256"],
            second["resources"][0]["profile_sha256"],
        )
        text = json.dumps(second)
        self.assertNotIn("https://", text)
        self.assertNotIn("7.2", text)
        profile_path = self.state / second["resources"][0]["profile_file"]
        self.assertEqual(profile_path.stat().st_mode & 0o777, 0o600)
        profile = json.loads(profile_path.read_text())
        self.assertEqual(profile["source"]["content_sha256"], self.content_sha)
        self.assertEqual(profile["source"]["catalog_manifest_sha256"], self.manifest)
        self.assertEqual(profile["policy"]["header_policy"], "first_record")

    def test_unrelated_metadata_update_reuses_tool_result_with_new_provenance(self):
        acquisition, build, extraction = self.mocks()
        with acquisition, build, extraction as parse:
            first = self.run_profiles()
            self.bundle, self.manifest = self.build(notes="Updated metadata")
            second = self.run_profiles()
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(second["extraction_cache_hits"], 1)
        self.assertNotEqual(
            first["resources"][0]["profile_sha256"],
            second["resources"][0]["profile_sha256"],
        )

    def test_header_policy_change_reextracts_cached_material(self):
        acquisition, build, extraction = self.mocks()
        with acquisition, build, extraction as parse:
            self.run_profiles()
            parse.return_value = self.extracted(columns=[])
            self.run_profiles(header_policy="absent")
        self.assertEqual(parse.call_count, 2)
        self.assertEqual(parse.call_args.args[3], "absent")

    def test_missing_duplicate_or_invalid_selection_stops_before_io(self):
        from search.operations.documents import extractor, source

        for resources in ([SECOND], [RESOURCE, RESOURCE], ["not-an-id"]):
            with (
                self.subTest(resources=resources),
                patch.object(source, "acquire") as fetch,
                patch.object(extractor, "build_image") as build,
            ):
                with self.assertRaises(controller.ProfileError):
                    self.run_profiles(resources)
                fetch.assert_not_called()
                build.assert_not_called()
        self.bundle, self.manifest = self.build(duplicate=True)
        with self.assertRaisesRegex(controller.ProfileError, "ambiguous"):
            self.run_profiles()

    def test_corrupt_extraction_cache_is_not_silently_rebuilt(self):
        acquisition, build, extraction = self.mocks()
        with acquisition, build, extraction as parse:
            self.run_profiles()
            cache = next((self.state / "extractions").glob("*.json"))
            record = json.loads(cache.read_text())
            record["identity"]["encoding"] = "cp950"
            cache.write_text(json.dumps(record))
            with self.assertRaisesRegex(controller.ProfileError, "cache_invalid"):
                self.run_profiles()
        self.assertEqual(parse.call_count, 1)

    def test_output_digest_must_identify_the_acquired_bytes(self):
        acquisition, build, extraction = self.mocks()
        with acquisition, build, extraction as parse:
            parse.return_value = dict(self.extracted(), input_sha256="d" * 64)
            with self.assertRaisesRegex(controller.ProfileError, "content_mismatch"):
                self.run_profiles()
        self.assertEqual(list((self.state / "profiles").iterdir()), [])

    def test_cached_result_must_match_the_requested_format_and_header_policy(self):
        for header_policy in ("first_record", "absent"):
            with self.subTest(header_policy=header_policy):
                self.state = self.root / ("state-" + header_policy)
                acquisition, build, extraction = self.mocks()
                with acquisition, build, extraction as parse:
                    parse.return_value = self.extracted(
                        columns=[] if header_policy == "absent" else ["pH"]
                    )
                    self.run_profiles(header_policy=header_policy)
                    cached = next((self.state / "extractions").glob("*.json"))
                    record = json.loads(cached.read_text())
                    if header_policy == "absent":
                        record["result"]["columns"] = ["fabricated-header"]
                    else:
                        record["result"].update(
                            detected_mime="application/pdf",
                            columns=[],
                            rows_observed=None,
                        )
                    cached.write_text(json.dumps(record))
                    report = self.run_profiles(header_policy=header_policy)
                self.assertEqual(parse.call_count, 1)
                self.assertEqual(report["status"], "partial")
                self.assertEqual(report["resources"][0]["status"], "failed")

    def test_replaced_state_directory_cannot_claim_a_profile_was_published(self):
        acquisition, build, extraction = self.mocks()

        def replace_state(_source_root):
            self.state.rename(self.root / "retained-state")
            self.state.mkdir(mode=0o700)
            return IMAGE

        with acquisition, build as image, extraction:
            image.side_effect = replace_state
            with self.assertRaisesRegex(controller.ProfileError, "state_changed"):
                self.run_profiles()
        self.assertFalse((self.state / "profiles").exists())

    def test_source_failure_does_not_discard_other_resources(self):
        from search.operations.documents import source

        self.bundle, self.manifest = self.build(second=True)
        acquisition, build, extraction = self.mocks()
        with acquisition as fetch, build, extraction:
            fetch.side_effect = [
                source.SourceError("document_source_failed"),
                self.acquisition(),
            ]
            report = self.run_profiles([RESOURCE, SECOND])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(
            [row["status"] for row in report["resources"]], ["failed", "ok"]
        )
        self.assertNotIn("https://", json.dumps(report))

    def test_empty_text_keeps_its_status(self):
        acquisition, build, extraction = self.mocks()
        with acquisition, build, extraction as parse:
            parse.return_value = self.extracted(status="empty_text", columns=[])
            report = self.run_profiles()
        self.assertEqual(report["resources"][0]["status"], "empty_text")

    def test_cleanup_failure_stops_the_batch(self):
        from search.operations.documents import extractor

        self.bundle, self.manifest = self.build(second=True)
        acquisition, build, extraction = self.mocks()
        with acquisition as fetch, build, extraction as parse:
            parse.side_effect = extractor.ExtractorError(
                "document_extractor_cleanup_failed"
            )
            with self.assertRaisesRegex(extractor.ExtractorError, "cleanup_failed"):
                self.run_profiles([RESOURCE, SECOND])
        self.assertEqual(fetch.call_count, 1)

    def test_replaced_profile_directory_cannot_claim_a_published_path(self):
        acquisition, build, extraction = self.mocks()

        def replace_profiles(*_args, **_kwargs):
            profiles = self.state / "profiles"
            profiles.rename(self.state / "retained-profiles")
            profiles.mkdir(mode=0o700)
            return self.extracted()

        with acquisition, build, extraction as parse:
            parse.side_effect = replace_profiles
            with self.assertRaisesRegex(controller.ProfileError, "state_changed"):
                self.run_profiles()
        self.assertEqual(list((self.state / "profiles").iterdir()), [])

    def test_cli_has_no_runtime_config_or_authority(self):
        args = [
            "profile-resources",
            "--bundle",
            str(self.bundle),
            "--expected-manifest-sha256",
            self.manifest,
            "--state-root",
            str(self.state),
            "--resource-id",
            RESOURCE,
        ]
        output = io.StringIO()
        with (
            patch.object(
                controller, "run", return_value={"status": "completed"}
            ) as run,
            patch.object(cli, "_runtime_report") as runtime,
            redirect_stdout(output),
        ):
            self.assertEqual(cli.main(args), 0)
        runtime.assert_not_called()
        self.assertEqual(run.call_args.kwargs["header_policy"], "first_record")
        self.assertEqual(json.loads(output.getvalue())["status"], "completed")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["--config", "/private/product.json", *args])


if __name__ == "__main__":
    unittest.main()
