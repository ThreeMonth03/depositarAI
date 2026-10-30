"""Real private-file and descriptor-fenced capture tests; no model or Docker."""

import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from search.quality.contract import plan_digest
from search.operations.deployment import shared_batch_capture as capture


def cases():
    return {
        "schema_version": 1,
        "cases": [
            {
                "id": "氣候資料",
                "turns": [
                    {
                        "query": "找氣候資料",
                        "expected": {
                            "time": {"start": "2020", "end": "2025"},
                            "formats": [],
                            "retain_topic": False,
                        },
                    },
                    {
                        "query": "我要2024年的",
                        "expected": {
                            "time": {"start": "2024", "end": "2024"},
                            "formats": ["CSV"],
                            "retain_topic": True,
                        },
                    },
                ],
            }
        ],
    }


class CaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="shared-capture-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.input = self.root / "cases.json"
        self.destination = self.root / "run"
        self.write_cases(cases())

    def write_cases(self, value):
        self.input.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        self.input.chmod(0o600)

    def rejected(self, function, *args, **kwargs):
        with self.assertRaises(capture.SharedBatchCaptureError) as caught:
            function(*args, **kwargs)
        self.assertRegex(str(caught.exception), r"^shared_batch_capture_[a-z_]+$")
        return caught.exception

    def fail_capture(self, mutation):
        with self.assertRaises(capture.SharedBatchCaptureError):
            with capture.capture_run(self.destination) as sink:
                sink.write_json("capture.json", {"query": "private"})
                sink.write_json("run.json", {"status": "completed"})
                mutation(sink)

    def test_unicode_ids_raw_query_digest_and_detached_json(self):
        value = cases()
        value["cases"][0]["turns"][0]["query"] = "  找　ＣＳＶ\n 資料  "
        self.write_cases(value)
        actual = capture.load_cases(self.input)
        self.assertEqual(actual["cases"][0]["id"], "氣候資料")
        self.assertEqual(
            actual["cases"][0]["turns"][0]["query"], "  找　ＣＳＶ\n 資料  "
        )
        self.assertEqual(plan_digest(actual), plan_digest(value))
        normalized = copy.deepcopy(value)
        normalized["cases"][0]["turns"][0]["query"] = "找 CSV 資料"
        self.assertNotEqual(plan_digest(actual), plan_digest(normalized))
        actual["cases"][0]["turns"][1]["expected"]["formats"].append("PDF")
        self.assertEqual(
            capture.load_cases(self.input)["cases"][0]["turns"][1]["expected"][
                "formats"
            ],
            ["CSV"],
        )

    def test_optional_outcome_and_topic_change_plans_preserve_exact_semantics(self):
        for status, reason in (
            ("clarification_required", "ambiguous_time"),
            ("clarification_required", "missing_topic"),
            ("rejected", "unsafe"),
            ("rejected", "off_topic"),
        ):
            with self.subTest(status=status, reason=reason):
                value = cases()
                expected = value["cases"][0]["turns"][0]["expected"]
                expected.update(status=status, reason=reason, topic_changed=False)
                self.write_cases(value)
                self.assertEqual(capture.load_cases(self.input), value)
        value = cases()
        value["cases"][0]["turns"][1]["expected"].update(
            status="ok", reason="none", retain_topic=False, topic_changed=True
        )
        self.write_cases(value)
        self.assertEqual(capture.load_cases(self.input), value)
        self.assertEqual(
            plan_digest(capture.load_cases(self.input)), plan_digest(value)
        )

    def test_malformed_outcomes_and_topic_changes_use_shared_validator(self):
        invalid = (
            {"status": "clarification_required"},
            {"status": "rejected", "reason": "ambiguous_time"},
            {"status": "ok", "reason": "unsafe"},
            {"status": "refused", "reason": "unsafe"},
            {"reason": "private-malformed-reason"},
            {"reason": ["unsafe"]},
            {"topic_changed": 1},
            {"topic_changed": True},
        )
        for fields in invalid:
            with self.subTest(fields=fields):
                value = cases()
                value["cases"][0]["turns"][0]["expected"].update(fields)
                self.write_cases(value)
                error = self.rejected(capture.load_cases, self.input)
                self.assertNotIn("private-malformed-reason", str(error))

    def test_predicates_preserve_date_precision_and_absent_constraints(self):
        for time in (
            None,
            {"start": None, "end": "2024-2"},
            {"start": "2024-2-29", "end": "2024-3"},
        ):
            with self.subTest(time=time):
                value = cases()
                value["cases"][0]["turns"][0]["expected"]["time"] = time
                self.write_cases(value)
                actual = capture.load_cases(self.input)
                self.assertEqual(
                    actual["cases"][0]["turns"][0]["expected"]["time"], time
                )

    def test_spec_structure_and_case_bounds(self):
        invalid = []
        for schema in (True, 2):
            value = cases()
            value["schema_version"] = schema
            invalid.append(value)
        for identifier in ("", "ＣＳＶ", "bad\x00id", "x" * 129, 1):
            value = cases()
            value["cases"][0]["id"] = identifier
            invalid.append(value)
        for count in (0, 4):
            value = cases()
            value["cases"] *= count
            invalid.append(value)
        value = cases()
        value["cases"] *= 2
        invalid.append(value)
        for count in (0, 9):
            value = cases()
            value["cases"][0]["turns"] = value["cases"][0]["turns"][:1] * count
            invalid.append(value)
        value = cases()
        value["cases"] = [copy.deepcopy(value["cases"][0]) for _ in range(3)]
        for number, case in enumerate(value["cases"]):
            case["id"] = str(number)
            case["turns"] = case["turns"][:1] * 3
        invalid.append(value)
        for value in invalid:
            with self.subTest(value=value):
                self.write_cases(value)
                self.rejected(capture.load_cases, self.input)

    def test_expected_uses_existing_query_date_format_semantics(self):
        for key, invalid in (
            ("query", ["", "x" * 513, "bad\x00query", "bad\u200bquery", 1]),
            (
                "time",
                [
                    {"start": "2025", "end": "2024"},
                    {"start": "2024-2-30", "end": None},
                    {"start": None, "end": None},
                    {},
                ],
            ),
            ("formats", [["csv"], ["CSV", "CSV"], ["EXE"], "CSV"]),
            ("retain_topic", [True, 1, None]),
        ):
            for item in invalid:
                with self.subTest(key=key, item=item):
                    value = cases()
                    turn = value["cases"][0]["turns"][0]
                    target = turn if key == "query" else turn["expected"]
                    target[key] = item
                    self.write_cases(value)
                    self.rejected(capture.load_cases, self.input)
        value = cases()
        value["cases"][0]["turns"][0]["expected"]["extra"] = 1
        self.write_cases(value)
        self.rejected(capture.load_cases, self.input)

    def test_case_file_is_bounded_strict_json_private_single_link_regular(self):
        for raw in (
            b"",
            b" " * (capture.MAX_CASE_BYTES + 1),
            b'{"schema_version":1,"schema_version":1,"cases":[]}',
            b'{"x":NaN}',
        ):
            with self.subTest(raw=raw[:70]):
                self.input.write_bytes(raw)
                self.rejected(capture.load_cases, self.input)
        self.write_cases(cases())
        self.input.chmod(0o644)
        self.rejected(capture.load_cases, self.input)
        self.input.chmod(0o600)
        os.link(self.input, self.root / "second-link")
        self.rejected(capture.load_cases, self.input)
        (self.root / "second-link").unlink()
        self.input.unlink()
        os.mkfifo(self.input, mode=0o600)
        self.rejected(capture.load_cases, self.input)

    def test_input_parent_and_symlinks_rejected(self):
        target = self.root / "target.json"
        self.input.rename(target)
        self.input.symlink_to(target)
        self.rejected(capture.load_cases, self.input)
        self.input.unlink()
        target.rename(self.input)
        self.root.chmod(0o755)
        self.rejected(capture.load_cases, self.input)
        self.root.chmod(0o700)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.rejected(capture.load_cases, alias / "cases.json")

    def test_read_rejects_ancestor_rename_even_symlink_points_to_same_file(self):
        parent = self.root / "private"
        parent.mkdir(mode=0o700)
        self.input.rename(parent / self.input.name)
        real_read = capture.private_io.read_file

        def replace_parent(*args, **kwargs):
            result = real_read(*args, **kwargs)
            parent.rename(self.root / "moved")
            parent.symlink_to(self.root / "moved", target_is_directory=True)
            return result

        with mock.patch.object(
            capture.private_io, "read_file", side_effect=replace_parent
        ):
            self.rejected(capture.load_cases, parent / "cases.json")

    def test_staged_records_publish_in_order_only_after_success(self):
        real_link = os.link
        published = []

        def record_link(source, target, **kwargs):
            if target in {"capture.json", "outcomes.json", "run.json"}:
                published.append(target)
            return real_link(source, target, **kwargs)

        with mock.patch.object(capture.os, "link", side_effect=record_link):
            with capture.capture_run(self.destination, excluded=[self.input]) as sink:
                for name in ("capture.json", "outcomes.json", "run.json"):
                    metadata = sink.write_json(name, {"status": "completed"})
                    self.assertFalse((self.destination / name).exists())
                self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o700)
                self.assertTrue(
                    all(
                        stat.S_IMODE(p.stat().st_mode) == 0o600
                        for p in self.destination.iterdir()
                    )
                )
        self.assertEqual(published, ["capture.json", "outcomes.json", "run.json"])
        raw = (self.destination / "run.json").read_bytes()
        self.assertEqual(
            metadata,
            {
                "name": "run.json",
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
        )
        self.assertEqual(
            stat.S_IMODE((self.destination / "run.json").stat().st_mode), 0o600
        )
        self.rejected(sink.verify)

    def test_existing_destination_never_reused_or_changed(self):
        for kind in ("directory", "file", "symlink", "dangling"):
            with self.subTest(kind=kind):
                destination = self.root / kind
                if kind == "directory":
                    destination.mkdir(mode=0o700)
                elif kind == "file":
                    destination.write_text("foreign")
                else:
                    destination.symlink_to(
                        self.root if kind == "symlink" else self.root / "missing"
                    )
                with self.assertRaises(capture.SharedBatchCaptureError):
                    with capture.capture_run(destination):
                        self.fail("must not enter")
                self.assertTrue(destination.exists() or destination.is_symlink())

    def test_output_requires_private_parent_and_canonical_path(self):
        self.root.chmod(0o755)
        with self.assertRaises(capture.SharedBatchCaptureError):
            with capture.capture_run(self.destination):
                self.fail("unsafe parent")
        self.root.chmod(0o700)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(capture.SharedBatchCaptureError):
            with capture.capture_run(alias / "run"):
                self.fail("symlink ancestor")

    def test_both_inputs_and_output_outside_git_and_bare_git(self):
        for marker in ("directory", "file", "bare"):
            with self.subTest(marker=marker):
                repo = self.root / marker
                repo.mkdir(mode=0o700)
                if marker == "bare":
                    (repo / "HEAD").write_text("ref: refs/heads/main")
                    (repo / "objects").mkdir()
                    (repo / "refs").mkdir()
                elif marker == "file":
                    (repo / ".git").write_text("gitdir: /elsewhere")
                else:
                    (repo / ".git").mkdir()
                sample = repo / "cases.json"
                sample.write_bytes(self.input.read_bytes())
                sample.chmod(0o600)
                self.rejected(capture.load_cases, sample)
                with self.assertRaises(capture.SharedBatchCaptureError):
                    with capture.capture_run(repo / "run"):
                        self.fail("inside Git")

    def test_excluded_paths_reject_equal_ancestor_descendant_and_relative(self):
        for excluded in (
            [self.destination],
            [self.root],
            [self.destination / "models"],
            [Path("relative")],
            [self.input, self.root],
        ):
            with self.subTest(excluded=excluded):
                with self.assertRaises(capture.SharedBatchCaptureError):
                    with capture.capture_run(self.destination, excluded=excluded):
                        self.fail("excluded overlap")
                self.assertFalse(self.destination.exists())

    def test_names_sizes_nonfinite_and_duplicates_no_leak(self):
        with capture.capture_run(self.destination) as sink:
            for name in (
                "../run.json",
                "/run.json",
                "sub/run.json",
                "",
                ".json",
                "x" * 100 + ".json",
            ):
                self.rejected(sink.write_json, name, {})
            for maximum in (0, True, capture.MAX_OUTPUT_BYTES + 1):
                self.rejected(sink.write_json, "run.json", {}, max_bytes=maximum)
            self.rejected(
                sink.write_json, "run.json", {"secret-query": "x" * 100}, max_bytes=10
            )
            self.rejected(sink.write_json, "run.json", {"value": float("nan")})
            sink.write_json("run.json", {"first": True})
            self.rejected(sink.write_json, "run.json", {"overwrite": True})
        self.assertEqual(
            json.loads((self.destination / "run.json").read_text()), {"first": True}
        )

    def test_unexpected_body_failure_removes_own_files_and_redacts_exception(self):
        with self.assertRaises(capture.SharedBatchCaptureError) as caught:
            with capture.capture_run(self.destination) as sink:
                sink.write_json("run.json", {"status": "completed"})
                raise RuntimeError("private-query-and-response")
        self.assertNotIn("private-query", str(caught.exception))
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_output_rename_preserves_replacement_foreign_files(self):
        moved = self.root / "moved"

        def rename(sink):
            self.destination.rename(moved)
            self.destination.mkdir(mode=0o700)
            (self.destination / "run.json").write_text("foreign")

        self.fail_capture(rename)
        self.assertEqual(list(moved.iterdir()), [])
        self.assertEqual((self.destination / "run.json").read_text(), "foreign")

    def test_output_permission_tamper_fails_and_revokes(self):
        self.fail_capture(lambda sink: self.destination.chmod(0o000))
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_foreign_record_collision_is_preserved(self):
        self.fail_capture(
            lambda sink: (self.destination / "run.json").write_text("foreign")
        )
        self.assertEqual([p.name for p in self.destination.iterdir()], ["run.json"])
        self.assertEqual((self.destination / "run.json").read_text(), "foreign")

    def test_staged_file_permissions_content_and_hardlinks_are_fenced(self):
        for kind in ("mode", "content", "hardlink"):
            with self.subTest(kind=kind):
                self.destination = self.root / kind

                def tamper(sink):
                    pending = next(self.destination.iterdir())
                    if kind == "mode":
                        pending.chmod(0o644)
                    elif kind == "content":
                        pending.write_text("different")
                    else:
                        os.link(pending, self.root / "foreign-hardlink")

                self.fail_capture(tamper)
                self.assertFalse((self.destination / "run.json").exists())

    def test_late_ancestor_change_revokes_already_published_records(self):
        parent = self.root / "private"
        parent.mkdir(mode=0o700)
        self.destination = parent / "run"
        moved = self.root / "moved"
        real_link = os.link

        def replace_after_link(source, target, **kwargs):
            result = real_link(source, target, **kwargs)
            if target == "run.json":
                parent.rename(moved)
                parent.symlink_to(moved, target_is_directory=True)
            return result

        with mock.patch.object(capture.os, "link", side_effect=replace_after_link):
            self.fail_capture(lambda sink: None)
        self.assertEqual(list((moved / "run").iterdir()), [])

    def test_primary_fd_closed_after_final_link_still_revokes_success(self):
        real_link = os.link

        def close_after_link(source, target, **kwargs):
            result = real_link(source, target, **kwargs)
            if target == "run.json":
                os.close(kwargs["dst_dir_fd"])
            return result

        with mock.patch.object(capture.os, "link", side_effect=close_after_link):
            self.fail_capture(lambda sink: None)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_late_output_content_change_cannot_be_adopted_as_new_identity(self):
        real_read = capture.private_io.read_file

        def replace_after_read(fd, name, **kwargs):
            raw = real_read(fd, name, **kwargs)
            if name == "run.json":
                (self.destination / name).write_bytes(
                    raw.replace(b"completed", b"corrupted")
                )
            return raw

        with mock.patch.object(
            capture.private_io, "read_file", side_effect=replace_after_read
        ):
            self.fail_capture(lambda sink: None)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_content_verification_rechecks_ancestors_after_read(self):
        parent = self.root / "private"
        parent.mkdir(mode=0o700)
        self.destination = parent / "run"
        moved = self.root / "moved"
        real_read = capture.private_io.read_file

        def replace_after_read(*args, **kwargs):
            result = real_read(*args, **kwargs)
            parent.rename(moved)
            parent.symlink_to(moved, target_is_directory=True)
            return result

        with capture.capture_run(self.destination) as sink:
            sink.write_json("run.json", {"status": "completed"})
            sink.publish()
            try:
                with mock.patch.object(
                    capture.private_io, "read_file", side_effect=replace_after_read
                ):
                    self.rejected(sink.verify)
            finally:
                parent.unlink()
                moved.rename(parent)
        self.assertEqual(
            json.loads((self.destination / "run.json").read_text()),
            {"status": "completed"},
        )

    def test_late_foreign_replacement_is_preserved_while_own_outputs_revoke(self):
        real_read = capture.private_io.read_file
        replaced = []

        def replace_after_read(fd, name, **kwargs):
            raw = real_read(fd, name, **kwargs)
            if name == "run.json" and not replaced:
                path = self.destination / "capture.json"
                foreign = self.root / "foreign.json"
                foreign.write_text("foreign", encoding="utf-8")
                foreign.chmod(0o600)
                self.assertNotEqual(path.stat().st_ino, foreign.stat().st_ino)
                foreign.replace(path)
                replaced.append(path)
            return raw

        with mock.patch.object(
            capture.private_io, "read_file", side_effect=replace_after_read
        ):
            self.fail_capture(lambda sink: None)
        self.assertEqual(replaced, [self.destination / "capture.json"])
        self.assertEqual([p.name for p in self.destination.iterdir()], ["capture.json"])
        self.assertEqual((self.destination / "capture.json").read_text(), "foreign")

    def test_final_payload_digest_rejects_changes_hidden_by_timestamp_resolution(self):
        real_read = capture.private_io.read_file
        real_identity = capture.private_io.identity

        def coarse_identity(info):
            return (*real_identity(info)[:-2], 0, 0)

        for target in ("capture.json", "run.json"):
            with self.subTest(target=target):
                self.destination = self.root / target.removesuffix(".json")
                observations = []

                def replace_after_read(fd, name, **kwargs):
                    raw = real_read(fd, name, **kwargs)
                    if name == "run.json" and not observations:
                        path = self.destination / target
                        original = path.read_bytes()
                        changed = original.replace(
                            b"private" if target == "capture.json" else b"completed",
                            b"altered" if target == "capture.json" else b"corrupted",
                        )
                        before = coarse_identity(path.stat())
                        path.write_bytes(changed)
                        observations.append(
                            (before, coarse_identity(path.stat()), original, changed)
                        )
                    return raw

                with (
                    mock.patch.object(
                        capture.private_io, "identity", side_effect=coarse_identity
                    ),
                    mock.patch.object(
                        capture.private_io, "read_file", side_effect=replace_after_read
                    ),
                    self.assertRaises(capture.SharedBatchCaptureError),
                ):
                    with capture.capture_run(self.destination) as sink:
                        artifact = sink.write_json(
                            "capture.json", {"query": "x" * (2 * 65536) + "private"}
                        )
                        sink.write_json(
                            "run.json",
                            {
                                "status": "completed",
                                "artifacts": {"capture.json": artifact},
                            },
                        )
                self.assertEqual(1, len(observations))
                before, after, original, changed = observations[0]
                self.assertEqual(before, after)
                self.assertEqual(len(original), len(changed))
                self.assertNotEqual(
                    hashlib.sha256(original).hexdigest(),
                    hashlib.sha256(changed).hexdigest(),
                )
                self.assertEqual([], list(self.destination.iterdir()))


if __name__ == "__main__":
    unittest.main()
