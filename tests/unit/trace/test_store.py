"""Private ownership, immutable events, limits and expiry using temp roots."""

from dataclasses import replace
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from search.operations.shared.private_io import canonical
from search.trace.contract import validate_event
from search.trace.limits import TraceLimits
from search.trace.store import TraceStore
from .fixtures import event, IDENTIFIER, OTHER


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "traces"
        self.now = [1000.0]
        self.store = TraceStore(self.root, clock=lambda: self.now[0])

    def test_two_immutable_turns_modes_and_sealing(self):
        first = event()
        self.assertEqual(self.store.append(first), "captured")
        first["input"]["query"] = "changed later"
        self.assertEqual(self.store.append(event(2)), "captured")
        self.assertFalse(self.store.export(IDENTIFIER)["complete"])
        self.assertTrue(self.store.seal(IDENTIFIER))
        exported = self.store.export(IDENTIFIER)
        self.assertTrue(exported["complete"])
        self.assertEqual(
            exported["events"][0]["input"]["query"], "climate 2024 CSV"
        )
        self.assertEqual(self.store.append(event()), "incomplete")
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        directory = self.root / IDENTIFIER
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_raw_export_retains_twenty_turns(self):
        for turn in range(1, 21):
            self.assertEqual(self.store.append(event(turn)), "captured")
        self.assertTrue(self.store.seal(IDENTIFIER))
        self.assertEqual(len(self.store.export(IDENTIFIER)["events"]), 20)

    def test_new_writer_cannot_seal_or_extend_open_old_generation(self):
        self.store.append(event())
        restarted = TraceStore(self.root, clock=lambda: self.now[0])
        self.assertEqual(restarted.append(event(2)), "incomplete")
        self.assertFalse(restarted.seal(IDENTIFIER))
        self.assertFalse(restarted.export(IDENTIFIER)["complete"])

    def test_expiry_refuses_export_and_prune_only_expired_session(self):
        self.store.append(event())
        self.now[0] += self.store.limits.ttl_seconds - 1
        self.store.append(event(identifier=OTHER))
        self.now[0] += 1
        with self.assertRaisesRegex(ValueError, "trace_expired"):
            self.store.export(IDENTIFIER)
        self.assertEqual(self.store.prune(), 1)
        self.assertFalse((self.root / IDENTIFIER).exists())
        self.assertTrue((self.root / OTHER).is_dir())

    def test_oversize_turn_and_session_caps(self):
        size = len(canonical(validate_event(event())))
        limits = replace(TraceLimits(), max_event_bytes=size - 1)
        store = TraceStore(self.root, limits)
        self.assertEqual(store.append(event()), "oversize")
        self.assertFalse(self.root.exists())
        limits = replace(TraceLimits(), max_turns=1)
        store = TraceStore(self.root, limits)
        self.assertEqual(store.append(event()), "captured")
        self.assertEqual(store.append(event(2)), "session_limit")
        tiny = TraceStore(
            self.base / "tiny", replace(limits, max_session_bytes=1)
        )
        self.assertEqual(tiny.append(event()), "session_limit")
        self.assertEqual(
            list((self.base / "tiny").iterdir())[0].name, ".trace.lock"
        )

    def test_global_bytes_and_session_count_caps(self):
        store = TraceStore(self.root, replace(TraceLimits(), max_sessions=1))
        self.assertEqual(store.append(event()), "captured")
        self.assertEqual(
            store.append(event(identifier=OTHER)), "session_limit"
        )
        used = sum(
            path.stat().st_size for path in (self.root / IDENTIFIER).iterdir()
        )
        bounded = TraceStore(
            self.base / "bounded",
            replace(TraceLimits(), max_total_bytes=used + 100),
        )
        self.assertEqual(bounded.append(event()), "captured")
        self.assertEqual(
            bounded.append(event(identifier=OTHER)), "total_limit"
        )

    def test_gap_persists_without_payload_and_prevents_sealing(self):
        self.store.append(event())
        self.store.gap(IDENTIFIER, 2, "projection_invalid")
        self.assertFalse(self.store.seal(IDENTIFIER))
        value = self.store.export(IDENTIFIER)
        self.assertEqual(
            value["manifest"]["gaps"],
            [{"turn": 2, "status": "projection_invalid"}],
        )
        self.assertEqual(len(value["events"]), 1)

    def test_delete_idempotent_refuses_unknown_or_symlink_files(self):
        self.store.append(event())
        sentinel = self.root / IDENTIFIER / "foreign.txt"
        sentinel.write_text("preserve")
        with self.assertRaises(ValueError):
            self.store.delete(IDENTIFIER)
        self.assertEqual(sentinel.read_text(), "preserve")
        sentinel.unlink()
        target = self.base / "secret"
        target.write_text("preserve")
        path = self.root / IDENTIFIER / "turn-001.json"
        saved = path.read_bytes()
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            self.store.delete(IDENTIFIER)
        self.assertEqual(target.read_text(), "preserve")
        path.unlink()
        path.write_bytes(saved)
        path.chmod(0o600)
        self.assertTrue(self.store.delete(IDENTIFIER))
        self.assertFalse(self.store.delete(IDENTIFIER))

    def test_bad_ids_and_unsafe_roots_are_rejected(self):
        for identifier in ("../elsewhere", "", "a" * 31, "A" * 32):
            with self.assertRaises(ValueError):
                self.store.delete(identifier)
        self.root.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            self.store.append(event())
        self.root.rmdir()
        self.root.symlink_to(self.base)
        with self.assertRaises(ValueError):
            self.store.append(event())

    def test_inode_replacement_prevents_any_delete(self):
        from search.trace import files

        self.store.append(event())
        original = files.remove_session

        def replaced(root, identifier, owned, directory):
            path = self.root / identifier / "turn-001.json"
            path.unlink()
            path.write_text("foreign replacement")
            path.chmod(0o600)
            return original(root, identifier, owned, directory)

        with patch.object(files, "remove_session", replaced):
            with self.assertRaisesRegex(ValueError, "trace_file_changed"):
                self.store.delete(IDENTIFIER)
        self.assertTrue((self.root / IDENTIFIER / "manifest.json").exists())
        self.assertEqual(
            (self.root / IDENTIFIER / "turn-001.json").read_text(),
            "foreign replacement",
        )

    def test_handmade_sql_union_requires_fixture_only_for_two_turns(self):
        for turn in (1, 2):
            value = event(turn)
            value["execution"] = {
                "kind": "validated_sql",
                "sql": "SELECT dataset_id FROM datasets WHERE year = :year",
                "parameters": {"year": 2024 if turn == 1 else 2023},
                "fixture_only": True,
            }
            self.assertEqual(self.store.append(value), "captured")
            for choice in (False, 1, "true"):
                value["execution"]["fixture_only"] = choice
                with self.assertRaises(ValueError):
                    validate_event(value)
        self.assertEqual(len(self.store.export(IDENTIFIER)["events"]), 2)

    def test_contract_rejects_coercion_and_false_complete(
        self,
    ):
        mutations = (
            {"trace_version": True},
            {"timestamp": "2026-11-12T00:00:00"},
            {"unavailable_trace": ["private error sentinel"]},
            {"capture": {"complete": True, "gaps": ["upstream_unavailable"]}},
            {"timing_ms": {"backend_total": 1.0}},
            {"timing_ms": {"bff_total": float("inf")}},
            {"authorization": "credential sentinel"},
        )
        for mutation in mutations:
            with (
                self.subTest(fields=list(mutation)),
                self.assertRaises(ValueError),
            ):
                validate_event(event() | mutation)
