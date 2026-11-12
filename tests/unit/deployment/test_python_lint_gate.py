"""Exercise the fast gate with real locked tools and isolated source trees."""

from __future__ import annotations

import hashlib
from importlib.metadata import distribution
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv


ROOT = Path(__file__).resolve().parents[3]
MODULE = "tests.unit.deployment.test_python_lint_gate"


class PythonLintGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="python-lint-gate-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "source"
        self.root.mkdir()
        for relative in (
            "Makefile",
            "ruff.toml",
            "scripts/ci/python-lint.sh",
            "scripts/ci/python-unused.sh",
            "scripts/ci/complexity.py",
            "search/api/requirements-test.lock",
        ):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)
        (self.root / "deploy/docker").mkdir(parents=True)
        signatures = {
            "search/catalog/transport.py": "fp, message, new_url",
            "search/operations/metadata/source.py": (
                "req, fp, code, msg, newurl"
            ),
            "search/reranker/server.py": "message, explain",
            "search/transport/http.py": "message, new_url",
            "tests/test_search_runtime_helpers.py": "exc_type, exc, traceback",
        }
        for relative, arguments in signatures.items():
            self.write(relative, f"def callback({arguments}):\n    pass\n")
        self.write("search/example.py", "value = 1\n")
        self.write("search/planning/__init__.py", "")
        self.write("search/api/retrieval/__init__.py", "")
        self.write("search/api/conversation/calendar.py", "")
        self.write("search/api/conversation/engine.py", "")
        self.write("search/api/worker.py", "")
        self.write("search/scope.py", "")
        self.write("search/operations/cli.py", "")
        self.write("search/operations/config.py", "")
        self.write("search/operations/doctor.py", "")
        self.write("search/operations/local_status.py", "")
        self.write("search/operations/state.py", "")
        self.write("search/operations/shared/runtime_probe.py", "")
        self.write("search/operations/shared/bounded_process.py", "")
        self.write("search/operations/catalog/policy.py", "")
        self.write("search/operations/catalog/transport.py", "")
        self.write("search/catalog/session.py", "")
        self.write("search/catalog/build/builder.py", "")
        self.write("search/catalog/build/normalize.py", "")
        self.write("search/api/shared/middleware.py", "")
        self.write("search/http/__init__.py", "")
        self.write("search/web/app.py", "")
        self.write("search/web/trace.py", "")
        self.write("search/web/turn.py", "")
        self.write("search/tests/web_fixture.py", "")
        self.write("search/tests/catalog_fixtures.py", "")
        self.write("search/quality/__init__.py", "")
        self.write("search/trace/__init__.py", "")
        self.write(
            "search/operations/deployment/candidate/batch/__init__.py", ""
        )
        self.write("search/operations/deployment/provider/__init__.py", "")
        self.write("search/operations/deployment/transition/__init__.py", "")
        self.write(
            "search/operations/documents/__init__.py", '"""Fixtures."""\n'
        )
        self.write("search/operations/jobs/archive.py", "")
        self.write("search/operations/metadata/sync.py", "")
        self.write("scripts/search_jobs.py", "")
        self.write("scripts/search_model_source.py", "")
        self.write("scripts/search_dictionary_permissions.py", "")
        self.write("scripts/update_search_backend.py", "")
        self.write("scripts/participate_backend_binding.py", "")
        self.write("search/operations/deployment/frontend/policy.py", "")
        self.write("search/operations/deployment/frontend/binding.py", "")
        self.write("search/operations/deployment/frontend/traces.py", "")
        self.write("search/operations/deployment/images/build.py", "")
        self.write("search/operations/deployment/source_bundle.py", "")
        self.write("search/operations/deployment/release/runtime.py", "")
        self.write("search/operations/deployment/inference_runtime.py", "")
        self.write("search/operations/deployment/credentials.py", "")
        self.write("search/operations/deployment/gateway/probe_runtime.py", "")
        self.write("search/operations/deployment/payload/contract.py", "")
        self.write("search/operations/deployment/payload/plan.py", "")
        self.write("search/operations/deployment/payload/archive.py", "")
        self.write("search/operations/deployment/payload/export.py", "")
        self.write("search/operations/deployment/public_entry/host.py", "")
        self.write("search/operations/deployment/public_entry/profile.py", "")
        self.environment = {
            **os.environ,
            "PATH": str(Path(sys.executable).parent)
            + os.pathsep
            + os.environ["PATH"],
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = self.run_command("make", "format")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def write(self, relative, source):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def run_command(self, *command):
        return subprocess.run(
            command,
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def shadow_tool(self, name, *, package=False):
        marker = self.directory / f"{name}-shadow-called"
        source = (
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
            "print('[]')\n"
        )
        if package:
            self.write(f"{name}/__init__.py", "")
            self.write(f"{name}/__main__.py", source)
        else:
            self.write(f"{name}.py", source)
        return marker

    def assert_ruff_shadow_is_ignored(self, *, package):
        marker = self.shadow_tool("ruff", package=package)
        self.write("search/example.py", "import os\n")
        before = self.snapshot()
        result = self.run_command("make", "lint")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("F401", result.stdout)
        self.assertFalse(marker.exists())
        self.assertEqual(before, self.snapshot())

        self.write("search/example.py", "value=1\n")
        before = self.snapshot()
        result = self.run_command("make", "format")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        after = self.snapshot()
        self.assertEqual(
            {"search/example.py"},
            {name for name in before if before[name] != after[name]},
        )
        self.assertEqual(
            "value = 1\n", (self.root / "search/example.py").read_text()
        )
        self.assertFalse(marker.exists())

    def test_ruff_module_shadow_does_not_replace_real_tools(self):
        self.assert_ruff_shadow_is_ignored(package=False)

    def test_ruff_package_shadow_does_not_replace_real_tools(self):
        self.assert_ruff_shadow_is_ignored(package=True)

    def test_vulture_shadow_does_not_replace_any_unused_check(self):
        marker = self.shadow_tool("vulture")
        self.write(
            "search/unused_example.py",
            "def component(unread_probe):\n    return 1\n",
        )
        before = self.snapshot()
        result = self.run_command("bash", "scripts/ci/python-unused.sh")
        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        self.assertIn("unused variable 'unread_probe'", result.stdout)
        self.assertFalse(marker.exists())
        self.assertEqual(before, self.snapshot())

        (self.root / "search/unused_example.py").unlink()
        before = self.snapshot()
        result = self.run_command("bash", "scripts/ci/python-unused.sh")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Python unused-code checks passed.", result.stdout)
        self.assertFalse(marker.exists())
        self.assertEqual(before, self.snapshot())

    def test_source_metadata_does_not_replace_locked_tool_versions(self):
        for name in ("ruff", "vulture"):
            self.write(
                f"{name}-0.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: 0\n",
            )
        before = self.snapshot()
        result = self.run_command("make", "lint")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_isolated_tools_do_not_write_bytecode_to_their_environment(self):
        environment = self.directory / "copied-tool-environment"
        venv.EnvBuilder(with_pip=False).create(environment)
        interpreter = environment / "bin/python3"
        purelib = subprocess.check_output(
            [
                str(interpreter),
                "-I",
                "-B",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
        ).strip()
        for name in ("ruff", "vulture", "tomli"):
            spec = importlib.util.find_spec(name)
            if spec is None:
                continue
            shutil.copytree(
                Path(spec.origin).parent,
                Path(purelib) / name,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            package = distribution(name)
            metadata = next(
                path.parts[0]
                for path in package.files
                if path.parts[0].endswith(".dist-info")
            )
            shutil.copytree(
                package.locate_file(metadata), Path(purelib) / metadata
            )
        shutil.copy2(
            Path(sys.executable).parent / "ruff", interpreter.parent / "ruff"
        )
        self.environment["PATH"] = (
            str(interpreter.parent) + os.pathsep + self.environment["PATH"]
        )
        before = self.snapshot()
        result = self.run_command("make", "lint")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse(list(environment.rglob("__pycache__")))
        self.assertEqual(before, self.snapshot())

    def test_lint_accepts_valid_source_without_mutation(self):
        before = self.snapshot()
        result = self.run_command("make", "lint")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Python unused-code checks passed.", result.stdout)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(list(self.root.rglob(".ruff_cache")))

    def test_each_real_tool_failure_is_read_only(self):
        cases = (
            ("import os\n", "F401"),
            ("value=1\n", "Would reformat"),
            (
                "def component(unread_probe):\n    return 1\n",
                "unused variable 'unread_probe'",
            ),
        )
        for source, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                self.write("search/example.py", source)
                before = self.snapshot()
                result = self.run_command("make", "lint")
                self.assertNotEqual(0, result.returncode)
                self.assertIn(diagnostic, result.stdout + result.stderr)
                self.assertEqual(before, self.snapshot())

    def test_format_is_an_explicit_source_mutation(self):
        self.write("search/example.py", "value=1\n")
        before = self.snapshot()
        result = self.run_command("make", "format")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        after = self.snapshot()
        changed = {name for name in before if before[name] != after[name]}
        self.assertEqual({"search/example.py"}, changed)
        self.assertEqual(
            "value = 1\n", (self.root / "search/example.py").read_text()
        )
        self.assertNotIn("Python unused-code checks passed.", result.stdout)

    def test_complexity_debt_is_reported_and_new_domain_debt_fails(self):
        report = self.directory / "complexity.json"
        self.environment["COMPLEXITY_REPORT"] = str(report)
        source = "def choose(value):\n" + "".join(
            f"    if value == {index}:\n        return {index}\n"
            for index in range(10)
        )
        self.write("search/legacy.py", source + "    return -1\n")
        formatted = self.run_command("make", "format")
        self.assertEqual(0, formatted.returncode, formatted.stderr)
        before = self.snapshot()
        result = self.run_command("make", "lint")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(json.loads(report.read_text()))

        for relative in (
            "search/planning/branch.py",
            "search/trace/future_policy.py",
            "search/web/trace.py",
            "search/web/turn.py",
            "search/tests/catalog_fixtures.py",
            "search/operations/doctor.py",
            "search/operations/local_status.py",
            "search/operations/state.py",
            "search/operations/shared/runtime_probe.py",
            "search/operations/shared/bounded_process.py",
            "search/operations/catalog/policy.py",
            "search/operations/catalog/transport.py",
            "search/catalog/session.py",
            "search/catalog/build/builder.py",
            "search/catalog/build/normalize.py",
            "search/api/conversation/engine.py",
            "search/api/worker.py",
            "search/scope.py",
            "search/operations/deployment/images/build.py",
            "search/operations/deployment/source_bundle.py",
            "search/operations/deployment/release/runtime.py",
            "search/operations/deployment/inference_runtime.py",
            "search/operations/deployment/credentials.py",
            "search/operations/deployment/gateway/probe_runtime.py",
            "search/operations/deployment/payload/contract.py",
            "search/operations/deployment/payload/plan.py",
            "search/operations/deployment/payload/archive.py",
            "search/operations/deployment/payload/export.py",
            "search/operations/deployment/public_entry/host.py",
            "search/operations/deployment/public_entry/profile.py",
            "scripts/search_model_source.py",
            "scripts/search_dictionary_permissions.py",
            "scripts/update_search_backend.py",
            "scripts/participate_backend_binding.py",
            "search/operations/deployment/transition/future_policy.py",
            "search/operations/deployment/frontend/binding.py",
            "search/operations/deployment/frontend/traces.py",
            "search/operations/documents/future_policy.py",
            "search/transport/http.py",
        ):
            with self.subTest(path=relative):
                path = self.root / relative
                shutil.move(self.root / "search/legacy.py", path)
                before = self.snapshot()
                result = self.run_command("make", "lint")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("C901", result.stdout)
                self.assertEqual(before, self.snapshot())
                findings = json.loads(report.read_text())
                self.assertEqual(len(findings), 1)
                self.assertEqual(Path(findings[0]["filename"]), path)
                self.assertEqual(findings[0]["code"], "C901")
                shutil.move(path, self.root / "search/legacy.py")

    def test_wrong_or_missing_tool_version_refuses_before_checks(self):
        for tool, replacement in (("ruff", "0"), ("vulture", None)):
            with self.subTest(tool=tool):
                lock = ROOT / "search/api/requirements-test.lock"
                source = lock.read_text()
                pattern = rf"(?m)^{tool}==[^\n]+\n"
                if replacement is None:
                    source = re.sub(pattern, "", source)
                else:
                    source = re.sub(
                        rf"(?m)^({tool}==)[^ ;]+",
                        rf"\g<1>{replacement}",
                        source,
                    )
                self.write("search/api/requirements-test.lock", source)
                before = self.snapshot()
                result = self.run_command("make", "lint")
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn("All checks passed!", result.stdout)
                self.assertEqual(before, self.snapshot())

    def test_absent_tool_packages_refuse_without_installing(self):
        environment = self.directory / "empty-environment"
        venv.EnvBuilder(with_pip=False).create(environment)
        self.environment["PATH"] = (
            str(environment / "bin") + os.pathsep + self.environment["PATH"]
        )
        lock = (ROOT / "search/api/requirements-test.lock").read_text()
        for name in ("ruff", "vulture"):
            pinned = re.search(rf"(?m)^{name}==([^ ;]+)", lock)[1]
            self.write(
                f"{name}-{pinned}.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: {name}\nVersion: {pinned}\n",
            )
        before = self.snapshot()
        result = self.run_command("make", "lint")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Install the locked lint tools", result.stderr)
        self.assertNotIn("All checks passed!", result.stdout)
        self.assertEqual(before, self.snapshot())

    def pip_capture(self, *, base_python=False):
        shim_directory = self.directory / "bin"
        shim_directory.mkdir()
        ledger = self.directory / "pip.json"
        shim = shim_directory / "python3"
        interpreter = (
            str(Path(sys.executable).resolve())
            if base_python
            else sys.executable
        )
        shim.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "module = sys.argv.index('-m') if '-m' in sys.argv else -1\n"
            "if module > 0 and sys.argv[module + 1] == 'pip':\n"
            "    arguments = sys.argv[module + 2:]\n"
            "    path = pathlib.Path(arguments[arguments.index('-r') + 1])\n"
            f"    pathlib.Path({str(ledger)!r}).write_text(json.dumps(\n"
            "        {'python_flags': sys.argv[1:module], "
            "'arguments': arguments, 'requirements': "
            "path.read_text(),\n"
            "         'temporary': str(path)}))\n"
            "    raise SystemExit(0)\n"
            f"os.execv({interpreter!r}, [{interpreter!r}, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        shim.chmod(0o700)
        self.environment["PATH"] = (
            str(shim_directory) + os.pathsep + self.environment["PATH"]
        )
        return ledger

    def test_installer_selects_original_hashed_lines_without_api_packages(
        self,
    ):
        ledger = self.pip_capture()
        before = self.snapshot()
        result = self.run_command(
            "bash", "scripts/ci/python-lint.sh", "install"
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        captured = json.loads(ledger.read_text())
        self.assertEqual(["-I", "-B"], captured["python_flags"])
        self.assertIn("--no-deps", captured["arguments"])
        self.assertIn("--require-hashes", captured["arguments"])
        lock_lines = (
            (ROOT / "search/api/requirements-test.lock")
            .read_text()
            .splitlines()
        )
        expected = [
            line
            for line in lock_lines
            if re.match(r"(ruff|vulture|tomli)==", line)
        ]
        self.assertEqual(expected, captured["requirements"].splitlines())
        self.assertFalse(Path(captured["temporary"]).exists())
        self.assertEqual(before, self.snapshot())

    def test_installer_refuses_global_python_before_pip(self):
        ledger = self.pip_capture(base_python=True)
        before = self.snapshot()
        result = self.run_command(
            "bash", "scripts/ci/python-lint.sh", "install"
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("virtual environment", result.stderr)
        self.assertFalse(ledger.exists())
        self.assertEqual(before, self.snapshot())


class PythonLintWorkflowTests(unittest.TestCase):
    def test_required_gate_rejects_every_unsuccessful_lint_outcome(self):
        source = (ROOT / ".github/workflows/ci.yml").read_text()
        jobs = dict(
            re.findall(
                r"(?m)^  ([a-z][a-z-]*):\n(.*?)(?=^  [a-z][a-z-]*:|\Z)",
                source.split("\njobs:\n", 1)[1],
                re.S,
            )
        )
        lint = jobs["lint"]
        validate = jobs["validate"]
        self.assertIn(MODULE, lint)
        self.assertLess(lint.index(MODULE), lint.index("make lint"))
        self.assertIn("scripts/ci/python-lint.sh install", lint)
        self.assertIn("tests.test_complexity", lint)
        self.assertIn("python-complexity-${{ github.sha }}", lint)
        self.assertIn(
            "path: ${{ runner.temp }}/depositar-complexity.json", lint
        )
        self.assertIn("persist-credentials: false", lint)
        self.assertIn(
            "needs: [lint, core-source, core-host, core-api]", validate
        )
        self.assertIn("if: always()", validate)
        self.assertIn("LINT_RESULT: ${{ needs.lint.result }}", validate)
        command = 'test "$LINT_RESULT" = success'
        block = re.search(
            r"(?m)^        run: \|\n((?:          [^\n]*\n)+)", validate
        )
        self.assertIsNotNone(block)
        script = "\n".join(line[10:] for line in block.group(1).splitlines())
        self.assertIn(command, script)
        self.assertLess(validate.index(command), validate.index("uses:"))
        for outcome in ("success", "failure", "cancelled", "skipped", ""):
            with self.subTest(outcome=outcome):
                result = subprocess.run(
                    ["bash", "-c", script],
                    env={
                        **os.environ,
                        "LINT_RESULT": outcome,
                        "SOURCE_RESULT": "success",
                        "HOST_RESULT": "success",
                        "API_RESULT": "success",
                    },
                    capture_output=True,
                    timeout=5,
                )
                self.assertEqual(outcome == "success", result.returncode == 0)
        self.assertIn("scripts/ci/source-package.sh", jobs["core-source"])
        self.assertIn("--phase host", jobs["core-host"])
        self.assertIn("--phase api", jobs["core-api"])
        self.assertIn("permissions:\n  contents: read", source)
        for job in (
            "private-website-oss-policy",
            "private-website-oss-gateway",
            "private-website-browser",
            "private-website",
        ):
            self.assertIn(job, jobs)

    def test_shell_entry_is_registered_with_existing_syntax_and_shellcheck(
        self,
    ):
        source = (ROOT / "scripts/search-ci.sh").read_text()
        shell_files = source.split("shell_files=(", 1)[1].split(")", 1)[0]
        self.assertIn("scripts/ci/python-lint.sh", shell_files)
        self.assertIn('bash -n "${shell_files[@]}"', source)
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "scripts/ci/python-lint.sh")],
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
