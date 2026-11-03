"""Protect the jobs CLI boundary without registry or daemon operations."""

import builtins
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts import search_jobs
from search.operations.jobs import archive, queue
from search.operations.jobs.pueue import JobError


class JobsCLITests(unittest.TestCase):
    def run_cli(self, argv, *, failure=None, fail_at=None, value=None):
        registry = {"fixture": "registry"}
        request = {"fixture": "request"}
        report = {"z": 2, "a": 1} if value is None else value
        calls, imports = [], []
        output, errors = io.StringIO(), io.StringIO()
        original_import = builtins.__import__

        def import_call(name, *args, **kwargs):
            imports.append(name)
            return original_import(name, *args, **kwargs)

        def handler(name):
            def call(*args, **kwargs):
                calls.append((name, args, kwargs))
                if name == fail_at:
                    raise failure
                if name == "load_registry":
                    return registry
                if name == "read":
                    return request
                return report

            return call

        with ExitStack() as stack:
            for name in (
                "load_registry",
                "read",
                "submit",
                "statuses",
                "status",
                "result",
                "cancel",
                "execute",
            ):
                stack.enter_context(
                    patch.object(queue, name, side_effect=handler(name))
                )
            stack.enter_context(
                patch.object(
                    archive, "archive", side_effect=handler("archive")
                )
            )
            stack.enter_context(
                patch.object(builtins, "__import__", side_effect=import_call)
            )
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            try:
                code = search_jobs.main(argv)
            except BaseException as error:
                return {
                    "raised": type(error).__name__,
                    "exception": error,
                    "out": output.getvalue(),
                    "err": errors.getvalue(),
                    "calls": calls,
                    "imports": imports,
                }
        return {
            "code": code,
            "out": output.getvalue(),
            "err": errors.getvalue(),
            "calls": calls,
            "imports": imports,
        }

    def test_importing_the_cli_does_not_load_archival(self):
        program = """
import importlib.abc
import sys

sys.path.insert(0, sys.argv[1])

class BlockArchive(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "search.operations.jobs.archive":
            raise AssertionError("unexpected archival import")

sys.meta_path.insert(0, BlockArchive())
from scripts import search_jobs
assert "search.operations.jobs.archive" not in sys.modules
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                program,
                str(Path(__file__).resolve().parents[3]),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout + result.stderr)

    def test_commands_preserve_dispatch_and_evaluation_order(self):
        identifier = "a" * 32
        registry, request = {"fixture": "registry"}, {"fixture": "request"}
        cases = [
            (
                ["submit", "--request", "fixture.json"],
                [
                    ("load_registry", (), {}),
                    ("read", ("fixture.json",), {}),
                    ("submit", (registry, request), {}),
                ],
            ),
            (
                ["status"],
                [("load_registry", (), {}), ("statuses", (registry,), {})],
            ),
        ]
        for name in ("status", "result", "cancel", "execute", "archive"):
            cases.append(
                (
                    [name, "--job", identifier],
                    [
                        ("load_registry", (), {}),
                        (name, (registry, identifier), {}),
                    ],
                )
            )
        cases.append(
            (
                ["status", "--job", ""],
                [("load_registry", (), {}), ("status", (registry, ""), {})],
            )
        )
        for argv, calls in cases:
            with self.subTest(argv=argv):
                result = self.run_cli(argv)
                self.assertEqual(0, result["code"])
                self.assertEqual('{"a": 1, "z": 2}\n', result["out"])
                self.assertEqual("", result["err"])
                self.assertEqual(calls, result["calls"])
                self.assertEqual(
                    argv[0] == "archive",
                    "search.operations.jobs.archive" in result["imports"],
                )

    def test_parser_errors_and_help_never_load_registry(self):
        cases = [
            ([], 2),
            (["unknown"], 2),
            (["submit"], 2),
            (["result"], 2),
            (["cancel"], 2),
            (["execute"], 2),
            (["archive"], 2),
            (["status", "--request", "x"], 2),
            (["--help"], 0),
            (["archive", "--help"], 0),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                result = self.run_cli(argv)
                self.assertEqual("SystemExit", result["raised"])
                self.assertEqual(expected, result["exception"].code)
                self.assertEqual([], result["calls"])

    def test_safe_errors_cover_registry_and_dispatch(self):
        private = "private-fixture-path-and-child-output"
        cases = [
            (JobError("job_busy"), '{"error": "job_busy"}\n'),
            *[
                (kind(private), '{"error":"job_failed"}\n')
                for kind in (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    UnicodeError,
                )
            ],
            (KeyboardInterrupt(private), '{"error":"job_interrupted"}\n'),
        ]
        for stage in ("load_registry", "execute"):
            for failure, expected in cases:
                with self.subTest(stage=stage, kind=type(failure).__name__):
                    result = self.run_cli(
                        ["execute", "--job", "a" * 32],
                        failure=failure,
                        fail_at=stage,
                    )
                    self.assertEqual(
                        130 if isinstance(failure, KeyboardInterrupt) else 1,
                        result["code"],
                    )
                    self.assertEqual(expected, result["out"])
                    self.assertNotIn(private, result["out"] + result["err"])
                    self.assertEqual("", result["err"])
                    self.assertEqual(
                        1 if stage == "load_registry" else 2,
                        len(result["calls"]),
                    )

    def test_failed_request_read_does_not_submit(self):
        for failure in (
            ValueError("private-fixture-detail"),
            KeyboardInterrupt("private-fixture-detail"),
        ):
            result = self.run_cli(
                ["submit", "--request", "fixture.json"],
                failure=failure,
                fail_at="read",
            )
            self.assertEqual(
                ["load_registry", "read"],
                [call[0] for call in result["calls"]],
            )
            self.assertEqual(
                130 if isinstance(failure, KeyboardInterrupt) else 1,
                result["code"],
            )

    def test_unhandled_and_output_errors_keep_their_boundaries(self):
        for failure in (
            RuntimeError("private-fixture-detail"),
            SystemExit(7),
            GeneratorExit("private-fixture-detail"),
        ):
            for stage in ("load_registry", "execute"):
                result = self.run_cli(
                    ["execute", "--job", "a" * 32],
                    failure=failure,
                    fail_at=stage,
                )
                self.assertEqual(type(failure).__name__, result["raised"])
                self.assertEqual("", result["out"] + result["err"])
        result = self.run_cli(["execute", "--job", "a" * 32], value={1, 2})
        self.assertEqual("TypeError", result["raised"])
        self.assertEqual("", result["out"] + result["err"])
        self.assertEqual(
            ["load_registry", "execute"],
            [call[0] for call in result["calls"]],
        )
