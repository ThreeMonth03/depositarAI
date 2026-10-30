"""Verify the thin quality CLI preserves its structured output and exit codes."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts import check_search_quality as cli
from search.quality import contract
from tests.unit.quality.fixtures import capture, plan_for, turn


class QualityCLITests(unittest.TestCase):
    def test_domain_functions_are_not_forwarded_through_the_cli(self):
        for name in ("validate_cases", "plan_digest", "evaluate", "normalized"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(cli, name))

    def test_cli_returns_structured_exit_codes_without_echoing_unselected_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            value = capture(turn({"start": "2024", "end": "2024"}))
            value["cases"][0]["turns"][0]["response"]["token"] = (
                "private-token-do-not-print"
            )
            for status in ("passed", "failed", "invalid_input"):
                if status == "failed":
                    value["cases"][0]["turns"][0]["response"]["result"]["state"][
                        "topic"
                    ] = ""
                path.write_text(
                    json.dumps(value)
                    if status != "invalid_input"
                    else '{"schema_version":1,"schema_version":1}'
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    code = cli.main(["--input", str(path)])
                self.assertEqual(
                    code, {"passed": 0, "failed": 1, "invalid_input": 2}[status]
                )
                self.assertEqual(json.loads(output.getvalue())["status"], status)
                self.assertNotIn("private-token-do-not-print", output.getvalue())

    def test_cli_plan_validation_and_bound_capture_have_distinct_failure_codes(self):
        value = capture(turn(None))
        value["cases"][0]["turns"][0]["query"] = "找氣候資料，勿輸出測試字串"
        plan = plan_for(value)
        with tempfile.TemporaryDirectory() as directory:
            plan_path, capture_path = (
                Path(directory) / name for name in ("plan.json", "capture.json")
            )
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(["--plan", str(plan_path)])
            summary = json.loads(output.getvalue())
            self.assertEqual(0, code)
            self.assertEqual("plan_valid", summary["status"])
            self.assertEqual((1, 1), (summary["case_count"], summary["turn_count"]))
            self.assertEqual(contract.plan_digest(plan), summary["plan_sha256"])
            self.assertNotIn("勿輸出測試字串", output.getvalue())
            for expected_code, captured in (
                (0, value),
                (1, {"schema_version": 1, "cases": []}),
                (2, capture(turn(None))),
            ):
                with self.subTest(expected_code=expected_code):
                    capture_path.write_text(json.dumps(captured, ensure_ascii=False))
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = cli.main(
                            ["--input", str(capture_path), "--plan", str(plan_path)]
                        )
                    self.assertEqual(expected_code, code)
                    self.assertEqual(
                        {0: "passed", 1: "failed", 2: "invalid_input"}[code],
                        json.loads(output.getvalue())["status"],
                    )


if __name__ == "__main__":
    unittest.main()
