"""Shared worker envelope keeps bounds and wire bytes without model calls."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import io
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from search.api.shared import worker_io
from search.api.shared.backend import (
    MAX_WORKER_BYTES,
    BackendTimeout,
    SubprocessBackend,
)


class WorkerIOTests(unittest.TestCase):
    def invoke(self, raw=b'{"query":"rain"}', callback=None, *, argv=None, limit=64):
        callback = Mock(return_value={"ready": True}) if callback is None else callback
        source, output, error = io.BytesIO(raw), io.BytesIO(), io.StringIO()
        with (
            patch.object(worker_io, "MAX_WORKER_BYTES", limit),
            contextlib.redirect_stderr(error),
        ):
            result = worker_io.run_worker(
                callback,
                argv=["ready"] if argv is None else argv,
                stdin=source,
                stdout=output,
            )
        self.assertEqual("", error.getvalue())
        return result, output.getvalue(), callback

    def test_limit_is_the_existing_backend_limit(self):
        self.assertEqual(MAX_WORKER_BYTES, worker_io.MAX_WORKER_BYTES)

    def test_success_preserves_legacy_unicode_json_wire_bytes(self):
        value = {"主題": "降雨", "ready": True}
        callback = Mock(return_value=value)
        result, output, _ = self.invoke(callback=callback)
        self.assertEqual(0, result)
        self.assertEqual(
            json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), output
        )
        callback.assert_called_once_with("ready", {"query": "rain"})

    def test_defaults_follow_sys_streams_patched_immediately_before_call(self):
        callback = Mock(return_value={"ok": True})
        source, output = io.BytesIO(b"{}"), io.BytesIO()
        with (
            patch.object(sys, "stdin", SimpleNamespace(buffer=source)),
            patch.object(sys, "stdout", SimpleNamespace(buffer=output)),
            patch.object(sys, "argv", ["worker", "scope"]),
        ):
            result = worker_io.run_worker(callback)
        self.assertEqual(0, result)
        callback.assert_called_once_with("scope", {})
        self.assertEqual(b'{"ok": true}', output.getvalue())

    def test_input_reads_at_most_limit_plus_one(self):
        source, output = Mock(), io.BytesIO()
        source.read.return_value = b"{}"
        with patch.object(worker_io, "MAX_WORKER_BYTES", 64):
            self.assertEqual(
                0,
                worker_io.run_worker(
                    lambda *_: {}, argv=["ready"], stdin=source, stdout=output
                ),
            )
        source.read.assert_called_once_with(65)

    def test_exact_byte_input_limit_is_accepted(self):
        raw = b'"' + b"x" * 62 + b'"'
        result, output, callback = self.invoke(raw)
        self.assertEqual(0, result)
        self.assertTrue(output)
        callback.assert_called_once_with("ready", "x" * 62)

    def test_oversized_input_never_calls_worker_or_writes_output(self):
        result, output, callback = self.invoke(b'"' + b"x" * 63 + b'"')
        self.assertEqual(1, result)
        self.assertEqual(b"", output)
        callback.assert_not_called()

    def test_invalid_duplicate_nonfinite_utf8_and_empty_json_are_silent(self):
        for raw in (b"", b"{", b'{"a":1,"a":2}', b"NaN", b"Infinity", b"\xff"):
            with self.subTest(raw=raw):
                result, output, callback = self.invoke(raw)
                self.assertEqual(1, result)
                self.assertEqual(b"", output)
                callback.assert_not_called()

    def test_missing_operation_never_calls_worker(self):
        result, output, callback = self.invoke(argv=[])
        self.assertEqual(1, result)
        self.assertEqual(b"", output)
        callback.assert_not_called()

    def test_operation_policy_is_owned_by_callback_not_envelope(self):
        callback = Mock(return_value={"ok": True})
        result, _, _ = self.invoke(callback=callback, argv=["custom-operation"])
        self.assertEqual(0, result)
        callback.assert_called_once_with("custom-operation", {"query": "rain"})

    def test_worker_exception_is_silent_and_returns_one(self):
        result, output, _ = self.invoke(
            callback=Mock(side_effect=ValueError("sensitive-model-text"))
        )
        self.assertEqual(1, result)
        self.assertEqual(b"", output)

    def test_unserializable_or_nonfinite_output_is_silent(self):
        for value in (object(), {"value": float("nan")}, {"value": float("inf")}):
            with self.subTest(value=type(value)):
                result, output, _ = self.invoke(callback=Mock(return_value=value))
                self.assertEqual(1, result)
                self.assertEqual(b"", output)

    def test_response_bound_counts_utf8_bytes_and_accepts_exact_limit(self):
        for value, expected in (("x" * 62, 0), ("x" * 63, 1), ("雨" * 21, 1)):
            with self.subTest(value=value):
                result, output, _ = self.invoke(callback=Mock(return_value=value))
                self.assertEqual(expected, result)
                if expected:
                    self.assertEqual(b"", output)
                else:
                    self.assertEqual(64, len(output))

    def test_input_read_and_output_write_errors_return_one_without_diagnostics(self):
        for broken_input in (True, False):
            with self.subTest(input=broken_input):
                source, output, error = io.BytesIO(b"{}"), io.BytesIO(), io.StringIO()
                if broken_input:
                    source = Mock(read=Mock(side_effect=OSError("secret input")))
                else:
                    output = Mock(write=Mock(side_effect=OSError("secret output")))
                with contextlib.redirect_stderr(error):
                    result = worker_io.run_worker(
                        lambda *_: {}, argv=["ready"], stdin=source, stdout=output
                    )
                self.assertEqual(1, result)
                self.assertEqual("", error.getvalue())
                if broken_input:
                    self.assertEqual(b"", output.getvalue())


@unittest.skipUnless(
    importlib.util.find_spec("pydantic"),
    "worker integration requires API image dependencies",
)
class WorkerMainTests(unittest.TestCase):
    def invoke_main(self, module, operation, raw=b"{}"):
        output, error = io.BytesIO(), io.StringIO()
        with (
            patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))),
            patch.object(sys, "stdout", SimpleNamespace(buffer=output)),
            patch.object(sys, "stderr", error),
            patch.object(sys, "argv", ["worker", operation]),
        ):
            result = module.main()
        self.assertEqual("", error.getvalue())
        return result, output.getvalue()

    def test_api_and_retrieval_load_settings_only_after_valid_envelope(self):
        for name in ("search.api.worker", "search.api.retrieval.worker"):
            module = importlib.import_module(name)
            with (
                self.subTest(module=name),
                patch.object(module, "run", return_value={"ok": True}) as run,
                patch.object(module.Settings, "from_environment") as settings,
            ):
                self.assertEqual((1, b""), self.invoke_main(module, "ready", b"{"))
                settings.assert_not_called()
                run.assert_not_called()
                self.assertEqual(
                    (0, b'{"ok": true}'), self.invoke_main(module, "ready")
                )
                settings.assert_called_once_with()
                run.assert_called_once_with("ready", {}, settings.return_value)

    def test_conversation_plan_keeps_settings_and_retrieval_worker_unloaded(self):
        module = importlib.import_module("search.api.conversation.worker")
        with (
            patch.object(module, "plan", return_value={"action": "clarify"}) as plan,
            patch.object(module, "retrieval_run") as retrieval,
            patch.object(module.Settings, "from_environment") as settings,
        ):
            self.assertEqual(
                (0, b'{"action": "clarify"}'),
                self.invoke_main(module, "conversation_plan"),
            )
        plan.assert_called_once_with({})
        retrieval.assert_not_called()
        settings.assert_not_called()

    def test_conversation_delegation_preserves_allowlist_and_lazy_settings(self):
        module = importlib.import_module("search.api.conversation.worker")
        with (
            patch.object(module, "plan") as plan,
            patch.object(
                module, "retrieval_run", return_value={"ok": True}
            ) as retrieval,
            patch.object(module.Settings, "from_environment") as settings,
        ):
            self.assertEqual((1, b""), self.invoke_main(module, "execute-command"))
            self.assertEqual((1, b""), self.invoke_main(module, "retrieve", b"{"))
            plan.assert_not_called()
            retrieval.assert_not_called()
            settings.assert_not_called()
            self.assertEqual((0, b'{"ok": true}'), self.invoke_main(module, "retrieve"))
            settings.assert_called_once_with()
            retrieval.assert_called_once_with("retrieve", {}, settings.return_value)


class BackendCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_worker_is_killed_and_reaped(self):
        original = asyncio.create_subprocess_exec
        processes = []

        async def spawn(*_args, **kwargs):
            process = await original(
                "python", "-c", "import time; time.sleep(30)", **kwargs
            )
            processes.append(process)
            return process

        with patch(
            "search.api.shared.backend.asyncio.create_subprocess_exec",
            side_effect=spawn,
        ):
            with self.assertRaises(BackendTimeout):
                await SubprocessBackend().call("scope", {"query": "test"}, 0.05)
        self.assertIsNotNone(processes[0].returncode)


if __name__ == "__main__":
    unittest.main()
