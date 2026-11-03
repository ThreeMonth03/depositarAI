"""Keep domain enforcement separate from native full-project Ruff findings."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci/complexity.py"
FINDING = {
    "code": "C901",
    "filename": "search/legacy.py",
    "message": "`legacy` is too complex (12 > 10)",
    "location": {"row": 1, "column": 1},
}


class ComplexityEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="complexity-entry-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.report = self.root / "report.json"
        self.config = self.root / "scenario.json"
        self.calls = self.root / "calls.jsonl"
        self.ruff = self.root / "ruff"
        self.ruff.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            f"config = json.loads(pathlib.Path({str(self.config)!r})"
            ".read_text())\n"
            f"with pathlib.Path({str(self.calls)!r}).open('a') as log:\n"
            "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "kind = sys.argv[sys.argv.index('--output-format') + 1]\n"
            "result = config['report' if kind == 'json' else 'gate']\n"
            "sys.stdout.write(result['stdout'])\n"
            "sys.stderr.write(result.get('stderr', ''))\n"
            "sys.exit(result['returncode'])\n"
        )
        self.ruff.chmod(0o700)

    def invoke(self, report, *, gate_code=0):
        self.config.write_text(
            json.dumps(
                {
                    "report": report,
                    "gate": {
                        "returncode": gate_code,
                        "stdout": "domain result\n",
                    },
                }
            )
        )
        self.calls.unlink(missing_ok=True)
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT),
                "--report",
                str(self.report),
                "--ruff",
                str(self.ruff),
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def recorded_calls(self):
        return [json.loads(row) for row in self.calls.read_text().splitlines()]

    def test_project_findings_remain_native_and_do_not_fail_clean_domains(
        self,
    ):
        native = "[\n  " + json.dumps(FINDING) + "\n]\n"
        result = self.invoke({"returncode": 1, "stdout": native})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.report.read_text(), native)
        report, gate = self.recorded_calls()
        prefix = ["check", "--no-cache", "--select", "C901"]
        self.assertEqual(
            report,
            prefix
            + ["--output-format", "json"]
            + ["search", "scripts", "tests", "deploy/docker"],
        )
        self.assertEqual(
            gate,
            prefix
            + ["--output-format", "concise"]
            + [
                "search/planning",
                "search/api/retrieval",
                "search/api/conversation/calendar.py",
                "search/operations/cli.py",
                "search/operations/config.py",
                "search/api/shared/middleware.py",
                "search/http",
                "search/web/app.py",
                "search/tests/web_fixture.py",
                "search/quality",
                "search/operations/deployment/candidate",
                "search/operations/deployment/provider",
                "search/operations/documents/cache.py",
                "search/operations/documents/controller.py",
                "search/operations/documents/derivation.py",
                "search/operations/documents/batches.py",
                "search/operations/documents/source.py",
                "search/operations/documents/enrichment.py",
                "search/operations/documents/extractor.py",
                "search/operations/metadata/source.py",
                "search/operations/deployment/frontend/policy.py",
                "scripts/ci",
                "scripts/search_jobs.py",
            ],
        )

    def test_empty_project_report_passes(self):
        result = self.invoke({"returncode": 0, "stdout": "[]\n"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.report.read_text()), [])

    def test_domain_findings_fail_and_retain_project_report(self):
        result = self.invoke(
            {"returncode": 1, "stdout": json.dumps([FINDING])}, gate_code=1
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(self.report.read_text()), [FINDING])
        self.assertEqual(len(self.recorded_calls()), 2)

    def test_report_execution_failure_cannot_be_a_clean_or_stale_report(self):
        self.report.write_text("[]\n")
        result = self.invoke({"returncode": 2, "stdout": ""})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.report.read_text(), "")
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_invalid_json_or_report_shape_fails_before_domain_gate(self):
        for raw in ("not-json", "{}", "null", "[1]", '[{"code":"F401"}]'):
            with self.subTest(raw=raw):
                result = self.invoke({"returncode": 1, "stdout": raw})
                self.assertEqual(result.returncode, 2)
                self.assertEqual(len(self.recorded_calls()), 1)

    def test_report_exit_status_must_agree_with_native_findings(self):
        for code, raw in ((1, "[]"), (0, json.dumps([FINDING]))):
            with self.subTest(code=code):
                result = self.invoke({"returncode": code, "stdout": raw})
                self.assertEqual(result.returncode, 2)
                self.assertEqual(len(self.recorded_calls()), 1)

    def test_domain_tool_failure_is_not_ignored(self):
        result = self.invoke({"returncode": 0, "stdout": "[]\n"}, gate_code=2)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(self.report.read_text()), [])

    def test_missing_ruff_fails(self):
        self.ruff.unlink()
        self.report.write_text("[]\n")
        result = self.invoke({"returncode": 0, "stdout": "[]\n"})
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.report.read_text(), "")


class RealComplexityGateTests(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("ruff") or shutil.which("ruff"),
        "requires the pinned Ruff development dependency",
    )
    def test_new_complex_branch_fails_only_when_it_enters_accepted_domain(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="complexity-real-") as name:
            root = Path(name) / "source"
            for path in (
                "scripts/ci",
                "tests",
                "deploy/docker",
            ):
                (root / path).mkdir(parents=True, exist_ok=True)
            accepted_files = (
                "search/planning/policy.py",
                "search/api/retrieval/policy.py",
                "search/api/conversation/calendar.py",
                "search/operations/cli.py",
                "search/operations/config.py",
                "search/api/shared/middleware.py",
                "search/http/policy.py",
                "search/web/app.py",
                "search/tests/web_fixture.py",
                "search/quality/policy.py",
                "search/operations/deployment/candidate/batch/policy.py",
                "search/operations/deployment/candidate/policy.py",
                "search/operations/deployment/provider/policy.py",
                "search/operations/documents/cache.py",
                "search/operations/documents/controller.py",
                "search/operations/documents/derivation.py",
                "search/operations/documents/batches.py",
                "search/operations/documents/source.py",
                "search/operations/documents/enrichment.py",
                "search/operations/documents/extractor.py",
                "search/operations/metadata/source.py",
                "search/operations/deployment/frontend/policy.py",
                "scripts/ci/policy.py",
                "scripts/search_jobs.py",
            )
            for relative in accepted_files:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")
            script = root / "scripts/ci/complexity.py"
            shutil.copyfile(SCRIPT, script)
            (root / "ruff.toml").write_text(
                "[lint.mccabe]\nmax-complexity = 10\n"
            )
            complex_source = "def policy(value):\n" + "".join(
                f"    if value == {i}:\n        return {i}\n"
                for i in range(11)
            )
            report = Path(name) / "report.json"
            command = [
                sys.executable,
                "-B",
                str(script),
                "--report",
                str(report),
            ]
            if not importlib.util.find_spec("ruff"):
                command += ["--ruff", shutil.which("ruff")]
            cases = [(0, "search/legacy.py")] + [
                (1, path) for path in accepted_files
            ]
            for expected, relative in cases:
                path = root / relative
                path.write_text(complex_source)
                with self.subTest(path=path.relative_to(root)):
                    result = subprocess.run(
                        command,
                        cwd=root,
                        capture_output=True,
                        text=True,
                        timeout=20,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode, expected, result.stderr
                    )
                    findings = json.loads(report.read_text())
                    self.assertEqual(len(findings), 1)
                    self.assertEqual(Path(findings[0]["filename"]), path)
                    self.assertEqual(findings[0]["code"], "C901")
                path.write_text("")


if __name__ == "__main__":
    unittest.main()
