"""Verify catalog command compatibility with harmless local Python children."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from search.operations.catalog import commands


ROOT = Path(__file__).resolve().parents[3]


class CatalogCommandTests(unittest.TestCase):
    def test_backend_facade_preserves_functions_error_and_shared_source_limit(self):
        from search.operations.catalog import backend

        self.assertIs(backend._runner, commands.default_runner)
        self.assertIs(backend._command, commands.command_output)
        self.assertIs(backend.BackendError, commands.BackendError)
        self.assertIs(backend.MAX_OUTPUT_BYTES, commands.MAX_OUTPUT_BYTES)
        self.assertEqual(4 * 1024**2, commands.MAX_OUTPUT_BYTES)
        self.assertEqual("BackendError", commands.BackendError.__name__)
        self.assertEqual(
            (commands.BackendError,), backend.IncompatibleCatalog.__bases__
        )
        self.assertTrue(issubclass(backend.IncompatibleCatalog, ValueError))

    def test_isolated_import_has_no_orchestration_or_process_side_effects(self):
        script = """
import subprocess
import sys
sys.path.insert(0, sys.argv[1])
def forbidden(*args, **kwargs):
    raise AssertionError("catalog command import started a process")
subprocess.Popen = forbidden
subprocess.run = forbidden
from search.operations.catalog import commands
for name in (
    "search.operations.catalog.backend",
    "search.operations.catalog.execution",
    "search.operations.catalog.runtime",
    "search.operations.catalog.volumes",
    "search.operations.catalog.administration",
    "search.operations.catalog.vectors",
    "search.catalog",
    "search.api", "torch", "fastapi", "django", "rest_framework", "adrf", "pydantic",
):
    assert not any(
        loaded == name or loaded.startswith(name + ".") for loaded in sys.modules
    ), name
assert commands.MAX_OUTPUT_BYTES == 4 * 1024**2
"""
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script, str(ROOT)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_real_runner_snapshots_filtered_environment_cwd_stdin_and_text(self):
        environment = {
            "SEARCH_CATALOG_TEST": "excluded-search",
            "COMPOSE_CATALOG_TEST": "excluded-compose",
            "SOURCE_REVISION": "excluded-revision",
            "SOURCE_REVISION_SUFFIX": "excluded-revision-prefix",
            "SEARCH": "preserved-search",
            "COMPOSE": "preserved-compose",
            "DEPOSITAR_COMMAND_TEST": "factory-snapshot",
        }
        script = """
import json, os, sys
keys = json.loads(sys.argv[1])
record = {
    "cwd": os.getcwd(),
    "stdin": sys.stdin.read(),
    "environment": {key: os.environ.get(key) for key in keys},
}
sys.stdout.buffer.write(json.dumps(record).encode() + b"\\r\\nsecond\\rthird\\n")
sys.stderr.buffer.write(b"private warning\\r\\n")
"""
        with tempfile.TemporaryDirectory(prefix="catalog-command-") as directory:
            with patch.dict(os.environ, environment):
                runner = commands.default_runner(Path(directory))
                with patch.dict(os.environ, {"DEPOSITAR_COMMAND_TEST": "later"}):
                    result = runner(
                        [sys.executable, "-c", script, json.dumps(list(environment))],
                        timeout=10,
                    )
            self.assertEqual(0, result.returncode)
            record, rest = result.stdout.split("\n", 1)
            self.assertEqual("second\nthird\n", rest)
            self.assertEqual("private warning\n", result.stderr)
            expected = {
                key: None
                if key.startswith(("SEARCH_", "COMPOSE_", "SOURCE_REVISION"))
                else value
                for key, value in environment.items()
            }
            self.assertEqual(
                {"cwd": directory, "stdin": "", "environment": expected},
                json.loads(record),
            )

    def test_stdout_bound_counts_utf8_bytes_and_does_not_charge_stderr(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "éé", "x" * 100))
        with patch.object(commands, "MAX_OUTPUT_BYTES", 4):
            self.assertEqual("éé", commands.command_output(runner, ["private"]))
            for stdout in ("ééa", b"bytes", None):
                with self.subTest(stdout=stdout):
                    runner.return_value = subprocess.CompletedProcess([], 0, stdout, "")
                    with self.assertRaisesRegex(
                        commands.BackendError,
                        "^catalog preparation command failed; inspect private evidence$",
                    ):
                        commands.command_output(runner, ["private"])

    def test_real_nonzero_command_keeps_private_output_out_of_error(self):
        script = (
            "import sys; print('private stdout'); "
            "sys.stderr.write('private stderr'); sys.exit(7)"
        )
        with tempfile.TemporaryDirectory(prefix="catalog-command-") as directory:
            runner = commands.default_runner(Path(directory))
            with self.assertRaises(commands.BackendError) as caught:
                commands.command_output(runner, [sys.executable, "-c", script])
        self.assertEqual(
            "catalog preparation command failed; inspect private evidence",
            str(caught.exception),
        )

    def test_runner_failures_propagate_unchanged_with_the_requested_timeout(self):
        argv = ["private-command"]
        for failure in (
            OSError("private launch error"),
            subprocess.TimeoutExpired(argv, 17),
            UnicodeError("private decoding error"),
        ):
            with self.subTest(failure=type(failure).__name__):
                runner = Mock(side_effect=failure)
                with self.assertRaises(type(failure)) as caught:
                    commands.command_output(runner, argv, timeout=17)
                self.assertIs(failure, caught.exception)
                runner.assert_called_once_with(argv, timeout=17)
