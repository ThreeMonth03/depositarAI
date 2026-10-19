"""Keep native unused-code checks narrow without hiding future dead code."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/ci/python-unused.sh"


class PythonUnusedGateWiringTests(unittest.TestCase):
    def test_api_gate_runs_the_tool_and_its_negative_probes_in_the_test_image(self):
        source = (ROOT / "scripts/test-search-api.sh").read_text(encoding="utf-8")
        for command in (
            "depositar-search-api:ci bash scripts/ci/python-unused.sh",
            "depositar-search-api:ci python -m unittest -v",
            "tests.unit.deployment.test_python_unused_gate",
        ):
            with self.subTest(command=command):
                self.assertIn(command, source)
        self.assertNotIn("|| true", source)


@unittest.skipUnless(
    importlib.util.find_spec("vulture"), "Vulture is installed in the CI test image"
)
class PythonUnusedGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="python-unused-gate-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for relative in ("scripts/ci", "search", "tests", "deploy/docker"):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SCRIPT, self.root / "scripts/ci/python-unused.sh")
        # The interpreter running these tests owns the installed Vulture.
        self.environment = {
            **os.environ,
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        signatures = {
            "search/catalog/transport.py": "fp, message, new_url",
            "search/operations/metadata/source.py": "req, fp, code, msg, newurl",
            "search/reranker/server.py": "message, explain",
            "search/transport/http.py": "message, new_url",
            "tests/test_search_runtime_helpers.py": "exc_type, exc, traceback",
        }
        for relative, arguments in signatures.items():
            self.write(relative, f"def framework_override({arguments}):\n    pass\n")

    def write(self, relative, source):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def run_gate(self):
        return subprocess.run(
            ["bash", str(self.root / "scripts/ci/python-unused.sh")],
            cwd=self.root.parent,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_required_framework_arguments_are_accepted(self):
        result = self.run_gate()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Python unused-code checks passed.", result.stdout)

    def test_new_unused_argument_in_each_product_root_is_rejected(self):
        for root in ("search", "scripts", "tests", "deploy/docker"):
            with self.subTest(root=root):
                relative = root + "/new_component.py"
                self.write(
                    relative, "def component(unread_argument_probe):\n    pass\n"
                )
                result = self.run_gate()
                self.assertEqual(3, result.returncode, result.stdout + result.stderr)
                self.assertIn("unused variable 'unread_argument_probe'", result.stdout)
                self.assertNotIn("Python unused-code checks passed.", result.stdout)
                (self.root / relative).unlink()

    def test_waived_names_in_a_different_file_are_still_rejected(self):
        self.write(
            "search/new_component.py",
            "def component(fp, message, new_url, req, code, msg, newurl, "
            "explain, exc_type, exc, traceback):\n"
            "    pass\n",
        )
        result = self.run_gate()
        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        for name in (
            "fp",
            "message",
            "new_url",
            "req",
            "code",
            "msg",
            "newurl",
            "explain",
            "exc_type",
            "exc",
            "traceback",
        ):
            with self.subTest(name=name):
                self.assertIn(f"unused variable '{name}'", result.stdout)

    def test_framework_files_still_reject_other_unused_arguments(self):
        self.write(
            "search/catalog/transport.py",
            "def framework_override(new_url, unexpected_unused_probe):\n    pass\n",
        )
        result = self.run_gate()
        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        self.assertIn("unused variable 'unexpected_unused_probe'", result.stdout)
        self.assertNotIn("unused variable 'new_url'", result.stdout)

    def test_framework_files_still_reject_unreachable_code(self):
        self.write(
            "search/reranker/server.py",
            "def framework_override(explain):\n    return\n    print('unreachable')\n",
        )
        result = self.run_gate()
        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        self.assertIn("unreachable code after 'return'", result.stdout)


if __name__ == "__main__":
    unittest.main()
