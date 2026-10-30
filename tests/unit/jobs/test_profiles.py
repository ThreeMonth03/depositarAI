"""Keep typed job inputs strict and independent of mutable caller files."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from search.operations.jobs import profiles
from search.operations.jobs.pueue import JobError
from search.operations.shared import private_io
from search.quality.contract import plan_digest


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.inputs = self.root / "inputs"
        self.job = self.root / "job"
        self.state = self.root / "resource-state"
        self.queue_root = self.root / "queue"
        for directory in (self.inputs, self.job, self.state, self.queue_root):
            directory.mkdir(mode=0o700)
        self.config = self.inputs / "native.json"
        self.config_bytes = (
            '{\n  "state_root": "/absolute/private/native-state",\n'
            '  "schema_version": 1\n}\n'
        ).encode()
        self.write(self.config, self.config_bytes)
        self.cases = self.inputs / "cases.json"
        self.plan = self.make_plan((1,))
        self.save_cases(self.plan)

    def write(self, filename, raw):
        filename.write_bytes(raw)
        filename.chmod(0o600)

    def make_plan(self, lengths):
        return {
            "schema_version": 1,
            "cases": [
                {
                    "id": "case-" + str(index),
                    "turns": [
                        {
                            "query": "  找２０２４年的\n氣候資料  ",
                            "expected": {
                                "time": {"start": "2024", "end": "2024"},
                                "formats": [],
                                "retain_topic": number > 0,
                            },
                        }
                        for number in range(length)
                    ],
                }
                for index, length in enumerate(lengths)
            ],
        }

    def save_cases(self, plan):
        raw = (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode()
        self.write(self.cases, raw)
        return raw

    def quality(self, plan=None):
        plan = self.plan if plan is None else plan
        return {
            "type": "quality-batch",
            "config": str(self.config),
            "config_sha256": hashlib.sha256(self.config.read_bytes()).hexdigest(),
            "source_revision": "a" * 40,
            "candidate_revision": "b" * 40,
            "cases": str(self.cases),
            "cases_sha256": hashlib.sha256(self.cases.read_bytes()).hexdigest(),
            "plan_sha256": plan_digest(plan),
            "expected_identity_sha256": "c" * 64,
        }

    def vector(self):
        return {
            "type": "vector-pass",
            "config": str(self.config),
            "config_sha256": hashlib.sha256(self.config.read_bytes()).hexdigest(),
            "source_revision": "a" * 40,
            "catalog_revision": "d" * 40,
            "bundle_id": "e" * 64,
            "manifest_sha256": "f" * 64,
            "expected_inputs_sha256": "1" * 64,
        }

    def resource(self):
        plan = {"catalog_manifest_sha256": "f" * 64, "resources": []}
        self.write(self.state / "plan.json", private_io.canonical(plan))
        return {
            "type": "resource-pass",
            "bundle": str(self.root / "bundle"),
            "state_root": str(self.state),
            "plan_sha256": hashlib.sha256(private_io.canonical(plan)).hexdigest(),
            "manifest_sha256": "f" * 64,
        }

    def test_three_explicit_schemas_reject_command_and_environment_passthrough(self):
        for profile in (self.resource(), self.quality(), self.vector()):
            with self.subTest(kind=profile["type"]):
                profiles.validate(profile, self.queue_root)
            for key, value in (
                ("command", "arbitrary"),
                ("argv", ["arbitrary"]),
                ("env", {"ARBITRARY": "fixture"}),
                ("timeout", 10**9),
            ):
                with (
                    self.subTest(kind=profile["type"], key=key),
                    self.assertRaisesRegex(JobError, "profile_invalid"),
                ):
                    profiles.validate({**profile, key: value}, self.queue_root)
            missing = dict(profile)
            missing.pop(next(key for key in profile if key.endswith("_sha256")))
            with self.assertRaisesRegex(JobError, "profile_invalid"):
                profiles.validate(missing, self.queue_root)

    def test_identity_hashes_and_revisions_require_exact_lowercase_hex(self):
        for profile in (self.quality(), self.vector()):
            for key in profile:
                if key.endswith("_sha256") or key.endswith("_revision"):
                    for value in (
                        True,
                        None,
                        "A" * len(profile[key]),
                        "0" * (len(profile[key]) - 1),
                    ):
                        with (
                            self.subTest(kind=profile["type"], key=key, value=value),
                            self.assertRaisesRegex(JobError, "profile_invalid"),
                        ):
                            profiles.validate({**profile, key: value}, self.queue_root)

    def test_executing_source_binding_is_distinct_from_catalog_or_candidate(self):
        for profile in (self.quality(), self.vector()):
            profiles.verify_source(profile, "a" * 40)
            with self.assertRaisesRegex(JobError, "profile_source_changed"):
                profiles.verify_source(profile, "d" * 40)
        profiles.verify_source(self.resource(), "d" * 40)

    def test_paths_are_absolute_lexical_and_outside_the_queue(self):
        for value in (
            "relative.json",
            str(self.inputs / ".." / "native.json"),
            str(self.config) + "\n",
            str(self.queue_root / "native.json"),
            str(self.root),
        ):
            with self.subTest(value=value), self.assertRaises(JobError):
                profiles.validate({**self.quality(), "config": value}, self.queue_root)

    def test_quality_freeze_preserves_raw_query_config_and_existing_absolute_paths(
        self,
    ):
        profile = self.quality()
        files = profiles.load_inputs(profile)
        profiles.freeze(self.job, profile, files)
        frozen = profiles.read_frozen(self.job, profile)
        self.assertEqual(files, frozen)
        self.assertEqual(self.config_bytes, frozen["native-config.json"])
        query = json.loads(frozen["quality-cases.json"])["cases"][0]["turns"][0][
            "query"
        ]
        self.assertEqual("  找２０２４年的\n氣候資料  ", query)
        self.assertEqual(
            "/absolute/private/native-state",
            json.loads(frozen["native-config.json"])["state_root"],
        )
        for name in frozen:
            self.assertEqual(0o600, (self.job / name).stat().st_mode & 0o777)

    def test_vector_freezes_only_private_config(self):
        profile = self.vector()
        files = profiles.load_inputs(profile)
        self.assertEqual({"native-config.json"}, set(files))
        profiles.freeze(self.job, profile, files)
        self.assertEqual(files, profiles.read_frozen(self.job, profile))

    def test_original_changes_deletion_and_symlinks_do_not_rebind_frozen_quality(self):
        profile = self.quality()
        files = profiles.load_inputs(profile)
        profiles.freeze(self.job, profile, files)
        self.write(self.config, b'{"changed":true}\n')
        self.cases.unlink()
        self.cases.symlink_to(self.config)
        profiles.validate(profile, self.queue_root)
        self.assertEqual(files, profiles.read_frozen(self.job, profile))
        self.config.unlink()
        self.assertEqual(files, profiles.read_frozen(self.job, profile))

    def test_caller_changes_before_freeze_are_rejected_by_byte_hashes(self):
        profile = self.quality()
        original = self.config.read_bytes()
        self.write(self.config, b'{"changed":true}\n')
        with self.assertRaisesRegex(JobError, "profile_config_changed"):
            profiles.load_inputs(profile)
        self.write(self.config, original)
        equivalent_json = json.dumps(self.plan, ensure_ascii=False).encode()
        self.write(self.cases, equivalent_json)
        with self.assertRaisesRegex(JobError, "profile_cases_changed"):
            profiles.load_inputs(profile)

    def test_native_plan_digest_does_not_use_the_receipt_newline_canonicalization(self):
        profile = self.quality()
        profile["plan_sha256"] = hashlib.sha256(
            private_io.canonical(self.plan)
        ).hexdigest()
        self.assertNotEqual(plan_digest(self.plan), profile["plan_sha256"])
        with self.assertRaisesRegex(JobError, "profile_plan_changed"):
            profiles.load_inputs(profile)

    def test_quality_batch_accepts_eight_turns_but_refuses_more_cases_or_turns(self):
        accepted = self.make_plan((3, 3, 2))
        self.save_cases(accepted)
        profiles.load_inputs(self.quality(accepted))
        for lengths in ((1, 1, 1, 1), (3, 3, 3)):
            plan = self.make_plan(lengths)
            self.save_cases(plan)
            with (
                self.subTest(lengths=lengths),
                self.assertRaisesRegex(JobError, "profile_plan_changed"),
            ):
                profiles.load_inputs(self.quality(plan))

    def test_oversized_inputs_are_refused_even_with_matching_hashes(self):
        config = b'{"padding":"' + b"x" * profiles.MAX_CONFIG_BYTES + b'"}'
        self.write(self.config, config)
        with self.assertRaisesRegex(ValueError, "file_size_invalid"):
            profiles.load_inputs(self.vector())
        self.write(self.config, self.config_bytes)
        cases = self.cases.read_bytes() + b" " * profiles.MAX_CASE_BYTES
        self.write(self.cases, cases)
        with self.assertRaisesRegex(ValueError, "file_size_invalid"):
            profiles.load_inputs(self.quality())

    def test_private_file_guards_refuse_modes_symlinks_and_hardlinks(self):
        profile = self.vector()
        self.config.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "file_unsafe"):
            profiles.load_inputs(profile)
        self.config.chmod(0o600)
        outside = self.root / "outside.json"
        self.write(outside, self.config_bytes)
        self.config.unlink()
        self.config.symlink_to(outside)
        with self.assertRaises(OSError):
            profiles.load_inputs(profile)
        self.config.unlink()
        os.link(outside, self.config)
        with self.assertRaisesRegex(ValueError, "file_unsafe"):
            profiles.load_inputs(profile)
        self.assertEqual(self.config_bytes, outside.read_bytes())

    def test_nonprivate_input_parent_is_refused(self):
        profile = self.vector()
        self.inputs.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "directory_unsafe"):
            profiles.load_inputs(profile)

    def test_frozen_corruption_is_refused_without_rereading_caller(self):
        profile = self.quality()
        files = profiles.load_inputs(profile)
        profiles.freeze(self.job, profile, files)
        self.write(self.job / "quality-cases.json", b'{"changed":true}\n')
        with self.assertRaisesRegex(JobError, "profile_cases_changed"):
            profiles.read_frozen(self.job, profile)

    def test_missing_input_or_second_publish_does_not_overwrite_frozen_files(self):
        profile = self.quality()
        files = profiles.load_inputs(profile)
        with self.assertRaisesRegex(JobError, "profile_inputs_invalid"):
            profiles.freeze(
                self.job, profile, {"native-config.json": files["native-config.json"]}
            )
        self.assertEqual([], list(self.job.iterdir()))
        profiles.freeze(self.job, profile, files)
        with self.assertRaises(FileExistsError):
            profiles.freeze(self.job, profile, files)
        self.assertEqual(files, profiles.read_frozen(self.job, profile))

    def test_resource_plan_contract_remains_digest_bound_without_new_sidecars(self):
        profile = self.resource()
        profiles.validate(profile, self.queue_root)
        self.assertEqual({}, profiles.load_inputs(profile))
        profiles.freeze(self.job, profile, {})
        self.assertEqual({}, profiles.read_frozen(self.job, profile))
        changed = json.loads((self.state / "plan.json").read_bytes())
        changed["resources"] = ["changed"]
        self.write(self.state / "plan.json", private_io.canonical(changed))
        with self.assertRaisesRegex(JobError, "profile_plan_changed"):
            profiles.verify_resource_plan(profile)


if __name__ == "__main__":
    unittest.main()
