"""Importing bounded capture provides a helper without starting operator IO."""

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

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
                bounded_process.selectors,
                "DefaultSelector",
                side_effect=failure,
            ),
            patch.object(
                bounded_process.os, "killpg", wraps=os.killpg
            ) as killed,
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
                bounded_process.selectors,
                "DefaultSelector",
                return_value=selector,
            ),
            patch.object(
                bounded_process.os, "killpg", wraps=os.killpg
            ) as killed,
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
    def test_isolated_import_needs_no_application_dependencies_or_runtime_io(
        self,
    ):
        script = (
            """
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
    'search.api', 'search.operations.deployment', """
            """'search.operations.catalog',
    'search.operations.metadata', 'search.operations.shared.runtime_probe',
    'fastapi', 'django', 'rest_framework', 'adrf', """
            """'pydantic', 'torch', 'haystack',
):
    assert not any(key == name or key.startswith(name """
            """+ '.') for key in sys.modules), name
"""
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script, str(ROOT)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)
        self.assertEqual("", result.stderr)


class BoundedInputTests(unittest.TestCase):
    def spawn(self, program, *, arguments=(), pass_fds=()):
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", "-c", program, *arguments],
            env={},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=pass_fds,
        )
        self.addCleanup(self.cleanup, process)
        return process

    def filled_input(self, program):
        gate_read, gate_write = os.pipe()
        try:
            process = self.spawn(
                "import os,sys\n"
                "os.read(int(sys.argv[1]), 1)\n"
                "os.close(int(sys.argv[1]))\n" + program,
                arguments=(str(gate_read),),
                pass_fds=(gate_read,),
            )
            os.close(gate_read)
            gate_read = None
            os.set_blocking(process.stdin.fileno(), False)
            prefix = bytearray()
            while len(prefix) <= 2 * 1024**2:
                try:
                    size = os.write(process.stdin.fileno(), b"p" * 65536)
                except BlockingIOError:
                    break
                prefix.extend(b"p" * size)
            self.assertTrue(prefix)
            self.assertLessEqual(len(prefix), 2 * 1024**2)
            os.write(gate_write, b"go")
            return process, bytes(prefix)
        finally:
            if gate_read is not None:
                os.close(gate_read)
            os.close(gate_write)

    @staticmethod
    def cleanup(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()

    def assert_closed_and_reaped(self, process):
        self.assertIsNotNone(process.returncode)
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        for stream in (process.stdin, process.stdout, process.stderr):
            self.assertTrue(stream.closed)

    def capture(self, process, payload, *, maximum=1024, timeout=2):
        with bounded_process.captured_pipes(
            process,
            timeout=timeout,
            max_bytes=maximum,
            timeout_error=TimeoutError,
            output_error=ValueError,
            input_bytes=payload,
        ) as (remaining, stdout, stderr):
            self.assertEqual(process.wait(timeout=remaining), 0)
            return b"".join(stdout), b"".join(stderr)

    def test_binary_input_reaches_only_the_consumer_and_stdin_is_closed(self):
        program = (
            """
import hashlib, json, sys
raw = sys.stdin.buffer.read()
print(json.dumps({'bytes': len(raw), 'sha256': """
            """hashlib.sha256(raw).hexdigest()}))
"""
        )
        payload = b"private-input-sentinel-100\x00\xff\n"
        process = self.spawn(program)
        self.assertNotIn(payload[:26].decode(), repr(process.args))
        output, errors = self.capture(process, payload)
        self.assertEqual(
            json.loads(output),
            {
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
        )
        self.assertEqual(errors, b"")
        self.assertNotIn(payload, output + errors)
        self.assert_closed_and_reaped(process)

    def test_partial_writes_and_a_temporarily_full_pipe_preserve_exact_bytes(
        self,
    ):
        process = self.spawn(
            "import hashlib,sys; "
            "print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"
        )
        payload = b"private-input-sentinel-100\x00\xff"
        original = os.write
        input_fd = process.stdin.fileno()
        attempts = []

        def write(descriptor, data):
            if descriptor == input_fd:
                attempts.append(len(data))
                if len(attempts) == 1:
                    raise BlockingIOError()
                return original(descriptor, data[:7])
            return original(descriptor, data)

        with patch.object(bounded_process.os, "write", side_effect=write):
            output, errors = self.capture(process, payload)
        self.assertGreater(len(attempts), 2)
        self.assertEqual(
            output.strip(), hashlib.sha256(payload).hexdigest().encode()
        )
        self.assertEqual(errors, b"")
        self.assert_closed_and_reaped(process)

    def test_input_and_output_backpressure_are_drained_concurrently(self):
        program = (
            """
import hashlib, json, os, sys
os.write(1, b'o' * 65536)
os.write(2, b'e' * 65536)
raw = sys.stdin.buffer.read()
print(json.dumps({'bytes': len(raw), 'sha256': """
            """hashlib.sha256(raw).hexdigest()}))
"""
        )
        payload = b"private-input-sentinel-100" * 150
        process, prefix = self.filled_input(program)
        output, errors = self.capture(process, payload, maximum=132000)
        self.assertEqual(output[:65536], b"o" * 65536)
        self.assertEqual(errors, b"e" * 65536)
        self.assertEqual(
            json.loads(output[65536:]),
            {
                "bytes": len(prefix + payload),
                "sha256": hashlib.sha256(prefix + payload).hexdigest(),
            },
        )
        self.assertNotIn(payload, output + errors)
        self.assert_closed_and_reaped(process)

    def test_consumer_closing_stdin_early_still_closes_and_reaps(self):
        process = self.spawn(
            "import os,time; os.close(0); "
            "os.write(1,b'closed'); time.sleep(10)"
        )
        self.assertEqual(process.stdout.read(6), b"closed")
        with self.assertRaises(BrokenPipeError):
            self.capture(process, b"private-input-sentinel-100")
        self.assert_closed_and_reaped(process)

    def test_full_input_pipe_keeps_the_absolute_capture_deadline(self):
        process, _ = self.filled_input("import time; time.sleep(10)")
        with self.assertRaises(TimeoutError):
            self.capture(process, b"private-input-sentinel-100", timeout=0.1)
        self.assert_closed_and_reaped(process)

    def test_output_overflow_while_input_is_blocked_closes_and_reaps(self):
        process, _ = self.filled_input(
            "import os,time; os.write(2,b'e'*65536); time.sleep(10)"
        )
        with self.assertRaises(ValueError):
            self.capture(process, b"private-input-sentinel-100", maximum=64)
        self.assert_closed_and_reaped(process)

    def test_selector_failure_also_closes_the_input_pipe(self):
        process = self.spawn("import time; time.sleep(10)")
        with (
            patch.object(
                bounded_process.selectors,
                "DefaultSelector",
                side_effect=OSError("selector unavailable"),
            ),
            self.assertRaises(OSError),
        ):
            self.capture(process, b"private-input-sentinel-100")
        self.assert_closed_and_reaped(process)


class BoundedPipeContractTests(unittest.TestCase):
    def setUp(self):
        self.pipes = {name: Mock() for name in ("stdin", "stdout", "stderr")}
        for descriptor, stream in enumerate(self.pipes.values(), 10):
            stream.fileno.return_value = descriptor
        self.process = Mock(pid=424242, **self.pipes)
        self.process.poll.return_value = 0
        self.keys = {
            name: SimpleNamespace(
                fd=stream.fileno(), fileobj=stream, data=name
            )
            for name, stream in self.pipes.items()
        }
        self.selector = Mock()
        self.selector.get_map.side_effect = [True, True, False]
        self.selector.select.return_value = [
            (self.keys[name], bounded_process.selectors.EVENT_READ)
            for name in ("stdout", "stderr")
        ]
        self.constructor = Mock(return_value=self.selector)
        self.clock = Mock(side_effect=[10.0, 10.1, 10.2, 10.3])
        self.read = Mock(side_effect=[b"abc", b"de", b"", b""])
        self.write = Mock()
        self.killed = Mock()
        patches = (
            patch.object(
                bounded_process.selectors, "DefaultSelector", self.constructor
            ),
            patch.object(bounded_process.os, "set_blocking"),
            patch.object(bounded_process.os, "read", self.read),
            patch.object(bounded_process.os, "write", self.write),
            patch.object(bounded_process.os, "killpg", self.killed),
            patch.object(bounded_process.time, "monotonic", self.clock),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cleaned = Mock()
        for mock, name in (
            (self.selector.close, "selector_close"),
            (self.killed, "kill"),
            (self.process.poll, "poll"),
            (self.process.wait, "wait"),
            (self.pipes["stdin"].close, "stdin_close"),
            (self.pipes["stdout"].close, "stdout_close"),
            (self.pipes["stderr"].close, "stderr_close"),
        ):
            self.cleaned.attach_mock(mock, name)
        self.timeout_failure = TimeoutError("synthetic capture timeout")
        self.output_failure = LookupError("synthetic combined output cap")
        self.timeout_error = Mock(return_value=self.timeout_failure)
        self.output_error = Mock(return_value=self.output_failure)

    def capture(self, *, payload=None, maximum=5):
        return bounded_process.captured_pipes(
            self.process,
            timeout=1,
            max_bytes=maximum,
            timeout_error=self.timeout_error,
            output_error=self.output_error,
            input_bytes=payload,
        )

    def assert_normal_cleanup(self):
        self.assertEqual(
            self.cleaned.mock_calls,
            [
                call.selector_close(),
                call.kill(self.process.pid, signal.SIGKILL),
                call.poll(),
                call.stdin_close(),
                call.stdout_close(),
                call.stderr_close(),
            ],
        )

    def test_combined_cap_preserves_raw_chunks_eof_order_and_remaining_time(
        self,
    ):
        with self.capture() as (remaining, stdout, stderr):
            self.assertAlmostEqual(remaining, 0.7)
            self.assertEqual(stdout, [b"abc"])
            self.assertEqual(stderr, [b"de"])
        self.assertEqual(
            self.read.call_args_list,
            [call(11, 65536), call(12, 65536)] * 2,
        )
        self.assertEqual(
            self.selector.unregister.call_args_list,
            [call(self.pipes["stdout"]), call(self.pipes["stderr"])],
        )
        self.assertEqual(self.clock.call_count, 4)
        self.output_error.assert_not_called()
        self.assert_normal_cleanup()

    def test_one_byte_over_the_combined_cap_uses_the_exact_output_error(self):
        with self.assertRaises(LookupError) as raised:
            with self.capture(maximum=4):
                self.fail("combined output cap was not enforced")
        self.assertIs(raised.exception, self.output_failure)
        self.output_error.assert_called_once_with()
        self.timeout_error.assert_not_called()
        self.assertEqual(self.read.call_count, 2)
        self.assert_normal_cleanup()

    def test_invalid_input_is_refused_before_selector_creation(self):
        for payload in ("text", bytearray(b"x"), b"x" * 4097):
            with (
                self.subTest(kind=type(payload).__name__),
                self.assertRaisesRegex(ValueError, "^process_input_invalid$"),
            ):
                with self.capture(payload=payload):
                    self.fail("invalid input was accepted")
        self.constructor.assert_not_called()
        self.clock.assert_not_called()
        self.read.assert_not_called()
        self.write.assert_not_called()
        self.assertEqual(self.killed.call_count, 3)

    def test_empty_input_closes_before_capture_and_again_during_cleanup(self):
        with self.capture(payload=b"") as (_, stdout, stderr):
            self.assertEqual((stdout, stderr), ([b"abc"], [b"de"]))
        self.write.assert_not_called()
        self.assertEqual(self.cleaned.mock_calls[0], call.stdin_close())
        self.assertEqual(
            self.pipes["stdin"].close.call_count,
            2,
        )
        self.selector.register.assert_any_call(
            self.pipes["stdout"],
            bounded_process.selectors.EVENT_READ,
            "stdout",
        )
        self.assertEqual(self.selector.register.call_count, 2)

    def test_maximum_input_preserves_bytes_and_unregisters_before_eof(self):
        payload = b"x" * 4096
        self.write.return_value = len(payload)
        output_keys = self.selector.select.return_value
        self.selector.select.side_effect = [
            [(self.keys["stdin"], bounded_process.selectors.EVENT_WRITE)]
            + output_keys,
            output_keys,
        ]
        with self.capture(payload=payload) as (_, stdout, stderr):
            self.assertEqual((stdout, stderr), ([b"abc"], [b"de"]))
        self.write.assert_called_once_with(10, payload)
        self.assertEqual(
            self.selector.unregister.call_args_list,
            [
                call(self.pipes["stdin"]),
                call(self.pipes["stdout"]),
                call(self.pipes["stderr"]),
            ],
        )

    def test_zero_write_is_a_fixed_oserror_and_still_cleans_up(self):
        self.write.return_value = 0
        self.selector.select.return_value = [
            (self.keys["stdin"], bounded_process.selectors.EVENT_WRITE)
        ]
        with self.assertRaises(OSError) as raised:
            with self.capture(payload=b"input"):
                self.fail("a zero write was accepted")
        self.assertEqual(raised.exception.args, ("process_input_closed",))
        self.output_error.assert_not_called()
        self.timeout_error.assert_not_called()
        self.assert_normal_cleanup()

    def test_broken_input_pipe_preserves_the_original_exception(self):
        failure = BrokenPipeError("synthetic closed stdin")
        self.write.side_effect = failure
        self.selector.select.return_value = [
            (self.keys["stdin"], bounded_process.selectors.EVENT_WRITE)
        ]
        with self.assertRaises(BrokenPipeError) as raised:
            with self.capture(payload=b"input"):
                self.fail("broken input was accepted")
        self.assertIs(raised.exception, failure)
        self.read.assert_not_called()
        self.assert_normal_cleanup()

    def test_read_error_is_not_wrapped_or_converted_to_a_factory_error(self):
        failure = OSError("synthetic read failure")
        self.read.side_effect = failure
        with self.assertRaises(OSError) as raised:
            with self.capture():
                self.fail("read failure was accepted")
        self.assertIs(raised.exception, failure)
        self.timeout_error.assert_not_called()
        self.output_error.assert_not_called()
        self.assert_normal_cleanup()

    def test_final_deadline_is_checked_without_another_select(self):
        self.selector.get_map.side_effect = [False]
        self.clock.side_effect = [10, 11]
        with self.assertRaises(TimeoutError) as raised:
            with self.capture():
                self.fail("expired final deadline was accepted")
        self.assertIs(raised.exception, self.timeout_failure)
        self.selector.select.assert_not_called()
        self.assert_normal_cleanup()

    def test_cleanup_keeps_selector_then_kill_error_precedence_and_context(
        self,
    ):
        body = ValueError("synthetic body failure")
        selector = LookupError("synthetic selector close failure")
        kill = RuntimeError("synthetic group kill failure")
        self.selector.close.side_effect = selector
        self.killed.side_effect = kill
        self.pipes["stdout"].close.side_effect = OSError(
            "synthetic pipe close"
        )
        with self.assertRaises(RuntimeError) as raised:
            with self.capture():
                raise body
        self.assertIs(raised.exception, kill)
        self.assertIs(kill.__context__, selector)
        self.assertIs(selector.__context__, body)
        self.assertIsNone(kill.__cause__)
        self.process.poll.assert_not_called()
        self.assertEqual(
            self.cleaned.mock_calls,
            [
                call.selector_close(),
                call.kill(self.process.pid, signal.SIGKILL),
                call.stdin_close(),
                call.stdout_close(),
                call.stderr_close(),
            ],
        )

    def test_wait_failure_still_closes_all_three_pipes(self):
        failure = subprocess.TimeoutExpired("synthetic child", 5)
        self.process.poll.return_value = None
        self.process.wait.side_effect = failure
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            with self.capture():
                pass
        self.assertIs(raised.exception, failure)
        self.process.wait.assert_called_once_with(timeout=5)
        self.assertEqual(
            self.cleaned.mock_calls[-3:],
            [call.stdin_close(), call.stdout_close(), call.stderr_close()],
        )

    def test_keyboard_interrupt_in_the_body_keeps_original_cleanup(self):
        failure = KeyboardInterrupt("synthetic interruption")
        with self.assertRaises(KeyboardInterrupt) as raised:
            with self.capture():
                raise failure
        self.assertIs(raised.exception, failure)
        self.assert_normal_cleanup()

    def test_generator_close_runs_the_same_cleanup(self):
        context = self.capture()
        context.__enter__()
        context.gen.close()
        self.assert_normal_cleanup()


if __name__ == "__main__":
    unittest.main()
