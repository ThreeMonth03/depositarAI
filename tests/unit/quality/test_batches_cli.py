"""Keep private multi-batch IO bounded and ordinary diagnostics sanitized."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_search_batches as cli
from tests.unit.quality.test_batches import full_fixture, make_record


class BatchCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="quality-batch-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.plan, self.records = full_fixture()
        self.planfile = self.root / "full.json"
        self.write(self.planfile, self.plan)
        self.batchargs = []
        self.batchdirs = []
        self.subplans = []
        for number, record in enumerate(self.records, 1):
            directory = self.root / f"batch-{number}"
            directory.mkdir(mode=0o700)
            subplan = self.root / f"subplan-{number}.json"
            self.write(subplan, record["plan"])
            for name, raw in record["files"].items():
                self.write(directory / name, raw)
            self.batchdirs.append(directory)
            self.subplans.append(subplan)
            self.batchargs += ["--batch", str(subplan), str(directory)]
        self.output = self.root / "report.json"

    def write(self, path, value):
        raw = (
            value
            if type(value) is bytes
            else json.dumps(value, ensure_ascii=False).encode("utf-8")
        )
        path.write_bytes(raw)
        path.chmod(0o600)

    def run_cli(self, *, batchargs=None, extra=()):
        output = io.StringIO()
        args = ["--plan", str(self.planfile)]
        args += self.batchargs if batchargs is None else batchargs
        args += ["--output", str(self.output), *extra]
        with redirect_stdout(output):
            code = cli.main(args)
        stdout = output.getvalue()
        self.assertNotIn(str(self.root), stdout)
        return code, json.loads(stdout), stdout

    def rejected(self, **kwargs):
        code, summary, stdout = self.run_cli(**kwargs)
        self.assertEqual(2, code)
        self.assertEqual(
            {
                "status": "invalid_input",
                "error": "quality_batches_input_invalid",
            },
            summary,
        )
        self.assertNotIn("PRIVATE_", stdout)
        return stdout

    def test_complete_private_batches_write_fresh_private_report(self):
        code, summary, _ = self.run_cli()
        report = json.loads(self.output.read_bytes())
        self.assertEqual(0, code)
        self.assertEqual("passed", summary["status"])
        self.assertEqual(2, summary["batch_count"])
        self.assertEqual(4, summary["summary"]["turn_count"])
        self.assertEqual("not_evaluated", summary["topical_relevance"])
        self.assertEqual(report["summary"], summary["summary"])
        self.assertNotIn("cases", summary)
        self.assertNotIn("evidence", summary)
        self.assertEqual(0o600, stat.S_IMODE(self.output.stat().st_mode))

    def test_missing_batches_keep_full_denominator_and_return_one(self):
        for number in (0, 1):
            with self.subTest(batch_count=number):
                self.output = self.root / f"missing-{number}.json"
                code, summary, _ = self.run_cli(batchargs=self.batchargs[: 3 * number])
                self.assertEqual(1, code)
                self.assertEqual("incomplete", summary["status"])
                self.assertEqual(4, summary["summary"]["turn_count"])
                self.assertEqual(
                    4 - 2 * number,
                    summary["summary"]["evidence_missing_turn_count"],
                )
                self.assertTrue(self.output.is_file())

    def test_failed_quality_assertions_return_one_with_complete_evidence(self):
        captured = json.loads(self.records[0]["files"]["capture.json"])
        captured["cases"][0]["turns"][1]["response"]["result"]["state"]["topic"] = (
            "different"
        )
        record = make_record(captured)
        for name, raw in record["files"].items():
            self.write(self.batchdirs[0] / name, raw)
        code, summary, _ = self.run_cli()
        self.assertEqual(1, code)
        self.assertEqual("failed", summary["status"])
        self.assertEqual(0, summary["summary"]["evidence_missing_turn_count"])
        self.assertEqual(1, summary["summary"]["quality_failed_turn_count"])

    def test_public_summary_omits_queries_titles_case_ids_and_unknown_tokens(self):
        captured = json.loads(self.records[0]["files"]["capture.json"])
        case = captured["cases"][0]
        case["id"] = "PRIVATE_CASE"
        case["turns"][0]["query"] = "PRIVATE_QUERY"
        reply = case["turns"][0]["response"]
        reply["opaque"] = "PRIVATE_TOKEN"
        reply["result"]["search"]["cards"][0]["title"] = "PRIVATE_TITLE"
        record = make_record(captured)
        self.plan["cases"][0] = copy.deepcopy(record["plan"]["cases"][0])
        self.write(self.planfile, self.plan)
        self.write(self.subplans[0], record["plan"])
        for name, raw in record["files"].items():
            self.write(self.batchdirs[0] / name, raw)
        code, _, stdout = self.run_cli()
        self.assertEqual(0, code)
        self.assertNotIn("PRIVATE_", stdout)
        private_report = self.output.read_text()
        self.assertIn("PRIVATE_CASE", private_report)
        self.assertIn("PRIVATE_TITLE", private_report)

    def test_explicit_baseline_is_checked_without_echoing_its_content(self):
        baselinefile = self.root / "baseline.json"
        baseline = json.loads(self.records[0]["files"]["run.json"])["identity"]
        self.write(baselinefile, baseline)
        code, summary, _ = self.run_cli(extra=("--baseline", str(baselinefile)))
        self.assertEqual(0, code)
        self.assertEqual("passed", summary["status"])
        self.output = self.root / "mismatched-baseline.json"
        baseline["request_policy_sha256"] = "9" * 64
        self.write(baselinefile, baseline)
        self.rejected(extra=("--baseline", str(baselinefile)))
        self.assertFalse(self.output.exists())

    def test_malformed_inputs_have_fixed_diagnostics(self):
        for number, raw in enumerate(
            (
                b'{"PRIVATE_TOKEN":"value"}',
                b'{"schema_version":1,"schema_version":1}',
                b'{"schema_version":NaN}',
                b"\xff",
                b"not json PRIVATE_QUERY",
            )
        ):
            with self.subTest(raw=number):
                self.write(self.planfile, raw)
                self.rejected()
                self.assertFalse(self.output.exists())

    def test_missing_artifact_is_rejected_and_no_report_is_written(self):
        (self.batchdirs[1] / "outcomes.json").unlink()
        self.rejected()
        self.assertFalse(self.output.exists())

    def test_input_file_permissions_must_be_owner_only(self):
        for path in (self.planfile, self.subplans[0], self.batchdirs[0] / "run.json"):
            for mode in (0o644, 0o660):
                with self.subTest(name=path.name, mode=oct(mode)):
                    path.chmod(mode)
                    self.rejected()
                    self.assertFalse(self.output.exists())
                    path.chmod(0o600)

    def test_input_directories_reject_group_access_and_group_writes(self):
        for path in (self.batchdirs[0], self.root):
            for mode in (0o750, 0o770):
                with self.subTest(name=path.name, mode=oct(mode)):
                    path.chmod(mode)
                    self.rejected()
                    self.assertFalse(self.output.exists())
                    path.chmod(0o700)

    def test_symlink_and_hardlink_inputs_are_rejected(self):
        for link_type in ("symlink", "hardlink"):
            with self.subTest(link_type=link_type):
                original = self.root / f"original-{link_type}.json"
                original.write_bytes(self.planfile.read_bytes())
                original.chmod(0o600)
                self.planfile.unlink()
                if link_type == "symlink":
                    self.planfile.symlink_to(original)
                else:
                    os.link(original, self.planfile)
                self.rejected()
                self.assertFalse(self.output.exists())
                self.planfile.unlink()
                self.write(self.planfile, self.plan)

    def test_symlink_capture_directory_is_rejected(self):
        linked = self.root / "linked-batch"
        linked.symlink_to(self.batchdirs[0], target_is_directory=True)
        self.rejected(batchargs=["--batch", str(self.subplans[0]), str(linked)])
        self.assertFalse(self.output.exists())

    def test_fifo_input_is_rejected_without_waiting_for_a_writer(self):
        artifact = self.batchdirs[0] / "capture.json"
        artifact.unlink()
        os.mkfifo(artifact, mode=0o600)
        self.rejected()
        self.assertFalse(self.output.exists())

    def test_existing_output_is_never_overwritten(self):
        original = b"PRIVATE_EXISTING_REPORT"
        self.write(self.output, original)
        self.rejected()
        self.assertEqual(original, self.output.read_bytes())

    def test_output_symlink_does_not_overwrite_its_target(self):
        target = self.root / "existing.json"
        original = b"PRIVATE_EXISTING_TARGET"
        self.write(target, original)
        self.output.symlink_to(target)
        self.rejected()
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(original, target.read_bytes())

    def test_bounded_plan_receipt_and_total_inputs_are_rejected(self):
        for owner, name in (
            (cli, "MAX_PLAN_BYTES"),
            (cli.batches, "MAX_RECEIPT_BYTES"),
            (cli.batches, "MAX_TOTAL_BYTES"),
        ):
            with self.subTest(limit=name), patch.object(owner, name, 1):
                self.rejected()
                self.assertFalse(self.output.exists())

    def test_excess_batch_count_is_rejected_before_reading_inputs(self):
        self.rejected(batchargs=self.batchargs[:3] * 33)
        self.assertFalse(self.output.exists())

    def test_malformed_arguments_do_not_echo_private_values(self):
        for args in (
            [
                "--plan",
                str(self.planfile),
                "--output",
                str(self.output),
                "--PRIVATE_TOKEN",
                "PRIVATE_VALUE",
            ],
            ["--plan", "PRIVATE_PATH"],
            ["--batch", "PRIVATE_SUBPLAN"],
        ):
            with self.subTest(arguments=len(args)):
                output, error = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    code = cli.main(args)
                self.assertEqual(2, code)
                self.assertEqual(
                    {
                        "status": "invalid_input",
                        "error": "quality_batches_input_invalid",
                    },
                    json.loads(output.getvalue()),
                )
                self.assertNotIn("PRIVATE_", output.getvalue() + error.getvalue())


if __name__ == "__main__":
    unittest.main()
