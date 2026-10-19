"""Importing bounded capture provides a helper without starting operator IO."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from search.operations.shared import bounded_process


ROOT = Path(__file__).resolve().parents[3]


class BoundedProcessCleanupTests(unittest.TestCase):
    def sleeping_process(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        # Keep the negative control safe when a broken helper skips cleanup.
        self.addCleanup(self.cleanup_process, process)
        return process

    @staticmethod
    def cleanup_process(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()

    def assert_cleaned(self, process, killed):
        killed.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(-signal.SIGKILL, process.returncode)
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_selector_construction_failure_kills_reaps_and_closes_pipes(self):
        process = self.sleeping_process()
        failure = OSError("selector unavailable")
        with (
            patch.object(
                bounded_process.selectors, "DefaultSelector", side_effect=failure
            ),
            patch.object(bounded_process.os, "killpg", wraps=os.killpg) as killed,
        ):
            with self.assertRaises(OSError) as caught:
                with bounded_process.captured_pipes(
                    process,
                    timeout=1,
                    max_bytes=1024,
                    timeout_error=TimeoutError,
                    output_error=ValueError,
                ):
                    self.fail("selector construction unexpectedly succeeded")
            self.assertIs(failure, caught.exception)
            self.assert_cleaned(process, killed)

    def test_selector_close_failure_still_kills_reaps_and_closes_pipes(self):
        process = self.sleeping_process()
        selector = bounded_process.selectors.DefaultSelector()
        original_close = selector.close
        self.addCleanup(original_close)
        failure = OSError("selector close failed")

        def close_then_fail():
            original_close()
            raise failure

        selector.close = Mock(side_effect=close_then_fail)
        with (
            patch.object(
                bounded_process.selectors, "DefaultSelector", return_value=selector
            ),
            patch.object(bounded_process.os, "killpg", wraps=os.killpg) as killed,
        ):
            with self.assertRaises(OSError) as caught:
                with bounded_process.captured_pipes(
                    process,
                    timeout=0.1,
                    max_bytes=1024,
                    timeout_error=TimeoutError,
                    output_error=ValueError,
                ):
                    self.fail("sleeping process unexpectedly reached EOF")
            self.assertIs(failure, caught.exception)
            selector.close.assert_called_once_with()
            self.assert_cleaned(process, killed)


class BoundedProcessImportTests(unittest.TestCase):
    def test_isolated_import_needs_no_application_dependencies_or_runtime_io(self):
        script = """
import builtins, os, selectors, socket, subprocess, sys
sys.path.insert(0, sys.argv[1])
def unexpected(*args, **kwargs):
    raise AssertionError('runtime IO during helper import')
builtins.open = os.open = os.read = os.write = os.killpg = unexpected
subprocess.Popen = socket.create_connection = unexpected
selectors.DefaultSelector = unexpected
from search.operations.shared import bounded_process
assert callable(bounded_process.captured_pipes)
for name in (
    'search.api', 'search.operations.deployment', 'search.operations.catalog',
    'search.operations.metadata', 'search.operations.shared.runtime_probe',
    'fastapi', 'django', 'rest_framework', 'adrf', 'pydantic', 'torch', 'haystack',
):
    assert not any(key == name or key.startswith(name + '.') for key in sys.modules), name
"""
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script, str(ROOT)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)
        self.assertEqual("", result.stderr)


if __name__ == "__main__":
    unittest.main()
