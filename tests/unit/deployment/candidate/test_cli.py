"""Bounded operator stdin and closed CLI diagnostics; no Docker or queries."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from search.operations.deployment.candidate import controller


ROOT = Path(__file__).resolve().parents[4]
ARGUMENTS = ["--config", "/fixture/product.json", "query", "--stdin"]
CHILD = (
    "\n"
    "import hashlib\n"
    "import os\n"
    "from pathlib import Path\n"
    "import runpy\n"
    "import sys\n"
    "\n"
    "root = Path(sys.argv[1])\n"
    "sys.path.insert(0, str(root))\n"
    "from search.operations.deployment.candidate import "
    "controller\n"
    "\n"
    "controller.QUERY_INPUT_SECONDS = 0.2\n"
    "\n"
    "def checked(config, operation, *, source, text, "
    "batch_identity):\n"
    "    assert config == '/fixture/product.json'\n"
    "    assert operation == 'query' and source is None and "
    "batch_identity is False\n"
    "    assert text not in '\\0'.join(sys.argv)\n"
    "    assert all(text not in item for pair in "
    "os.environ.items() for item in pair)\n"
    "    return {'status': 'accepted',\n"
    "            'query_sha256': "
    "hashlib.sha256(text.encode('utf-8')).hexdigest()}\n"
    "\n"
    "controller.operate = checked\n"
    "launcher = root / 'scripts/share_search_backend.py'\n"
    "sys.argv = [str(launcher), '--config', "
    "'/fixture/product.json', 'query', '--stdin']\n"
    "runpy.run_path(str(launcher), run_name='__main__')\n"
)
REJECTED_CHILD = r"""
from pathlib import Path
import runpy
import sys

root = Path(sys.argv[1])
arguments = sys.argv[2:]
sys.path.insert(0, str(root))
from search.operations.deployment.candidate import controller

def forbidden(*args, **kwargs):
    raise AssertionError('rejected arguments reached an operation')

controller.operate = controller._query_stdin = forbidden
launcher = root / 'scripts/share_search_backend.py'
sys.argv = [str(launcher), *arguments]
runpy.run_path(str(launcher), run_name='__main__')
"""


class CandidateCLIInputTests(unittest.TestCase):
    def invoke(self, raw, *, arguments=ARGUMENTS, failure=None):
        output, errors = io.StringIO(), io.StringIO()
        with tempfile.TemporaryFile() as source:
            source.write(raw)
            source.seek(0)
            blocking = os.get_blocking(source.fileno())
            with (
                patch.object(sys, "stdin", source),
                patch.object(
                    controller,
                    "operate",
                    return_value={"status": "accepted"},
                    side_effect=failure,
                ) as operate,
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                code = controller.main(arguments)
            self.assertIs(os.get_blocking(source.fileno()), blocking)
        self.assertEqual(errors.getvalue(), "")
        return code, json.loads(output.getvalue()), operate

    def test_stdin_is_normalized_before_any_runtime_operation(self):
        code, report, operate = self.invoke("  ＣＳＶ\t找資料\n".encode())
        self.assertEqual((code, report), (0, {"status": "accepted"}))
        operate.assert_called_once_with(
            "/fixture/product.json",
            "query",
            source=None,
            text="CSV 找資料",
            batch_identity=False,
        )

    def test_original_query_length_boundary_is_preserved(self):
        for ending in (b"", b"\n", b"\r\n"):
            with self.subTest(ending=ending):
                code, _, operate = self.invoke("河".encode() * 512 + ending)
                self.assertEqual(code, 0)
                self.assertEqual(operate.call_args.kwargs["text"], "河" * 512)
        for raw in (
            b"x" * 513,
            "河".encode() * 513,
            "河".encode() * 513 + b"\n",
            "河".encode() * 512 + b"\n\n",
        ):
            with self.subTest(bytes=len(raw)):
                code, report, operate = self.invoke(raw)
                self.assertEqual(code, 1)
                self.assertEqual(
                    report, {"error": "shared_backend_query_invalid"}
                )
                operate.assert_not_called()

    def test_invalid_or_oversized_input_never_reaches_runtime(self):
        for raw in (
            b"",
            b" \n\t",
            b"\xff",
            b"\xed\xa0\x80",
            b"before\0after",
            "before\u200bafter".encode(),
            b"x" * (controller.MAX_QUERY_INPUT_BYTES + 1),
        ):
            with self.subTest(bytes=len(raw)):
                code, report, operate = self.invoke(raw)
                self.assertEqual(code, 1)
                self.assertEqual(
                    report, {"error": "shared_backend_query_invalid"}
                )
                operate.assert_not_called()

    def test_terminal_input_is_rejected_without_reading_or_echoing(self):
        with (
            patch.object(controller.os, "isatty", return_value=True),
            patch.object(controller.os, "read") as read,
        ):
            code, report, operate = self.invoke(b"synthetic private query")
        self.assertEqual(code, 1)
        self.assertEqual(report, {"error": "shared_backend_query_invalid"})
        read.assert_not_called()
        operate.assert_not_called()

    def test_unreadable_input_uses_a_fixed_diagnostic(self):
        with patch.object(
            controller.select,
            "select",
            side_effect=OSError("PRIVATE_SENTINEL"),
        ):
            code, report, operate = self.invoke(b"synthetic private query")
        self.assertEqual(code, 1)
        self.assertEqual(report, {"error": "shared_backend_query_invalid"})
        operate.assert_not_called()

    def test_nonquery_commands_do_not_read_stdin(self):
        with patch.object(
            controller,
            "_query_stdin",
            side_effect=AssertionError("unexpected read"),
        ):
            code, report, operate = self.invoke(
                b"", arguments=["--config", "/fixture/product.json", "status"]
            )
        self.assertEqual((code, report), (0, {"status": "accepted"}))
        self.assertIsNone(operate.call_args.kwargs["text"])

    def test_invalid_and_legacy_arguments_never_repeat_their_values(self):
        sentinel = "synthetic-private-argument-" + uuid.uuid4().hex
        for arguments in (
            ["--config", "/fixture/product.json", "query"],
            [
                "--config",
                "/fixture/product.json",
                "query",
                "--query",
                sentinel,
            ],
            [*ARGUMENTS, sentinel],
            ["--config", "/fixture/product.json", sentinel],
            [
                "--config",
                "/fixture/product.json",
                "query",
                "--stdin",
                "--bad",
                sentinel,
            ],
        ):
            with self.subTest(arguments_count=len(arguments)):
                output, errors = io.StringIO(), io.StringIO()
                with (
                    patch.object(controller, "operate") as operate,
                    patch.object(controller, "_query_stdin") as read,
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    with self.assertRaises(SystemExit) as caught:
                        controller.main(arguments)
                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(
                    json.loads(errors.getvalue()),
                    {"error": "shared_backend_arguments_invalid"},
                )
                self.assertNotIn(sentinel, errors.getvalue())
                operate.assert_not_called()
                read.assert_not_called()


class CandidateCLIProcessTests(unittest.TestCase):
    def arguments(self):
        return [sys.executable, "-I", "-S", "-B", "-c", CHILD, str(ROOT)]

    def environment(self):
        return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}

    def test_real_child_receives_query_only_from_stdin(self):
        sentinel = "synthetic-stdin-query-" + uuid.uuid4().hex
        result = subprocess.run(
            self.arguments(),
            input=sentinel.encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment(),
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(
            json.loads(result.stdout),
            {
                "status": "accepted",
                "query_sha256": hashlib.sha256(sentinel.encode()).hexdigest(),
            },
        )
        self.assertNotIn(sentinel, "\0".join(result.args))
        self.assertNotIn(sentinel.encode(), result.stdout + result.stderr)

    def test_launcher_never_echoes_rejected_legacy_or_unknown_arguments(
        self,
    ):
        sentinel = "synthetic-rejected-argument-" + uuid.uuid4().hex
        for arguments in (
            [
                "--config",
                "/fixture/product.json",
                "query",
                "--query",
                sentinel,
            ],
            [*ARGUMENTS, "--unknown", sentinel],
        ):
            with self.subTest(arguments_count=len(arguments)):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        "-B",
                        "-c",
                        REJECTED_CHILD,
                        str(ROOT),
                        *arguments,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self.environment(),
                    timeout=5,
                    check=False,
                )
                # The caller already supplied unsafe argv; rejection must not
                # repeat it in an additional diagnostic channel.
                self.assertIn(sentinel, result.args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(
                    json.loads(result.stderr),
                    {"error": "shared_backend_arguments_invalid"},
                )
                self.assertNotIn(
                    sentinel.encode(), result.stdout + result.stderr
                )

    def assert_timeout(self, *, trickle):
        process = subprocess.Popen(
            self.arguments(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment(),
        )
        cancelled = threading.Event()

        def write():
            try:
                while not cancelled.wait(0.03):
                    process.stdin.write(b"x")
                    process.stdin.flush()
                    if not trickle:
                        break
            except BrokenPipeError:
                pass

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        started = time.monotonic()
        try:
            process.wait(timeout=5)
            output, errors = process.stdout.read(), process.stderr.read()
        finally:
            cancelled.set()
            writer.join(timeout=1)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            process.stdout.close()
            process.stderr.close()
        self.assertFalse(writer.is_alive())
        self.assertEqual(process.returncode, 1)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(
            json.loads(output), {"error": "shared_backend_query_input_timeout"}
        )
        self.assertEqual(errors, b"")

    def test_stalled_stdin_expires_without_waiting_for_eof(self):
        self.assert_timeout(trickle=False)

    def test_trickling_stdin_does_not_extend_the_absolute_deadline(self):
        self.assert_timeout(trickle=True)


if __name__ == "__main__":
    unittest.main()
