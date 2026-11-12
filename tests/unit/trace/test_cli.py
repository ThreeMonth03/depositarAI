"""Keep operator exports private and stdout free of captured payloads."""

from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest

from search.trace.cli import main
from search.trace.limits import TraceLimits
from search.trace.store import TraceStore
from .fixtures import event, IDENTIFIER, plan, sealed_bundle


class CLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "traces"
        self.store = TraceStore(self.root)

    def run_cli(self, command, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([command, "--root", str(self.root), *arguments])
        value = json.loads(output.getvalue())
        self.assertNotIn("climate 2024 CSV", output.getvalue())
        self.assertNotIn("Climate observations", output.getvalue())
        return code, value

    def test_inspect_then_export_twenty_turns_to_private_new_directory(self):
        for turn in range(1, 21):
            self.store.append(event(turn))
        self.store.seal(IDENTIFIER)
        code, value = self.run_cli("inspect", "--session", IDENTIFIER)
        self.assertEqual(code, 0)
        self.assertEqual(value["sessions"][0]["turns"], 20)
        destination = self.base / "export"
        code, value = self.run_cli(
            "export", "--session", IDENTIFIER, "--output", str(destination)
        )
        self.assertEqual(code, 0, value)
        self.assertTrue(value["complete"])
        exported = json.loads((destination / "manifest.json").read_bytes())
        self.assertEqual(len(exported["event_files"]), 20)
        self.assertEqual(len(list(destination.iterdir())), 21)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        for name in exported["event_files"]:
            path = destination / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn("input", json.loads(path.read_bytes()))

    def test_raw_unsealed_export_is_honestly_incomplete(self):
        self.store.append(event())
        code, value = self.run_cli(
            "export",
            "--session",
            IDENTIFIER,
            "--output",
            str(self.base / "export"),
        )
        self.assertEqual(code, 0, value)
        self.assertFalse(value["complete"])

    def test_unknown_input_and_colliding_output_get_fixed_diagnostic(self):
        code, value = self.run_cli("delete", "--session", "../secret-sentinel")
        self.assertEqual(code, 2)
        self.assertEqual(
            value, {"status": "unavailable", "error": "trace_operation_failed"}
        )
        self.store.append(event())
        destination = self.base / "export"
        destination.mkdir(mode=0o700)
        sentinel = destination / "foreign"
        sentinel.write_text("preserve")
        code, value = self.run_cli(
            "export", "--session", IDENTIFIER, "--output", str(destination)
        )
        self.assertEqual(code, 2)
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_delete_and_expiry_prune_are_local_id_bound(self):
        self.store.append(event())
        code, value = self.run_cli("delete", "--session", IDENTIFIER)
        self.assertEqual((code, value["removed"]), (0, True))
        expired = TraceStore(
            self.root,
            replace(TraceLimits(), ttl_seconds=1),
            clock=lambda: 1000,
        )
        expired.append(event())
        code, value = self.run_cli("prune")
        self.assertEqual((code, value["removed"]), (0, 1))

    def test_private_config_is_bounded_and_unknown_fields_do_not_leak(self):
        path = self.base / "limits.json"
        path.write_text(json.dumps({"max_turns": 21}))
        path.chmod(0o600)
        code, value = self.run_cli("inspect", "--limits", str(path))
        self.assertEqual(code, 0, value)
        path.write_text(json.dumps({"secret-sentinel": "never print"}))
        code, value = self.run_cli("inspect", "--limits", str(path))
        self.assertEqual(code, 2)
        self.assertEqual(value["error"], "trace_operation_failed")

    def test_quality_uses_independent_plan_matching_bundle_and_private_output(
        self,
    ):
        bundle, manifest = sealed_bundle(self, self.base)
        self.store.append(event(manifest=manifest))
        self.store.append(event(2, manifest=manifest))
        self.store.seal(IDENTIFIER)
        expected = self.base / "plan.json"
        expected.write_text(json.dumps(plan()))
        expected.chmod(0o600)
        destination = self.base / "quality"
        arguments = [
            "--session",
            IDENTIFIER,
            "--output",
            str(destination),
            "--plan",
            str(expected),
            "--case",
            "climate",
            "--bundle",
            str(bundle),
            "--manifest",
            manifest,
        ]
        code, value = self.run_cli("quality", *arguments)
        self.assertEqual((code, value["quality"]), (0, "passed"))
        report = json.loads((destination / "report.json").read_bytes())
        self.assertEqual(report["summary"]["passed_turn_count"], 2)
        self.assertEqual(
            stat.S_IMODE((destination / "capture.json").stat().st_mode), 0o600
        )
        arguments[-1] = "e" * 64
        code, value = self.run_cli("quality", *arguments)
        self.assertEqual(code, 2)
        self.assertEqual(value["error"], "trace_operation_failed")
