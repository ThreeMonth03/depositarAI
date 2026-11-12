"""Exercise actual thread admission, stalled publication and withdrawal."""

from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from search.operations.shared import private_io
from search.trace.limits import TraceLimits
from search.trace.worker import TraceSink
from .fixtures import event, IDENTIFIER, OTHER


def wait_for(predicate, *, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("trace worker deadline exceeded")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "traces"
        self.sink = TraceSink(self.root)
        self.addCleanup(self.sink.close)

    def test_no_sink_has_no_payload_or_disk_side_effect(self):
        disabled = TraceSink(None)
        disabled.start()
        self.assertEqual(disabled.enqueue(event()), "disabled")
        self.assertEqual(disabled.revoke(IDENTIFIER, 1), "complete")
        self.assertFalse(self.root.exists())

    def test_writer_freezes_payload_then_seals_only_on_healthy_close(self):
        value = event()
        self.assertEqual(self.sink.enqueue(value), "queued")
        value["input"]["query"] = "later mutation"
        self.sink.start()
        wait_for(
            lambda: self.sink.status(IDENTIFIER)["capture_status"]
            == "captured"
        )
        self.assertFalse(self.sink.store.export(IDENTIFIER)["complete"])
        self.sink.close()
        exported = self.sink.store.export(IDENTIFIER)
        self.assertTrue(exported["complete"])
        self.assertEqual(
            exported["events"][0]["input"]["query"], "climate 2024 CSV"
        )

    def test_queue_count_rejects_payload_but_persists_gap(self):
        self.sink = TraceSink(self.root, replace(TraceLimits(), queue_items=1))
        self.assertEqual(self.sink.enqueue(event()), "queued")
        self.assertEqual(self.sink.enqueue(event(2)), "queue_full")
        self.sink.start()
        wait_for(
            lambda: self.sink.status(IDENTIFIER)["capture_status"]
            == "incomplete"
        )
        self.sink.close()
        exported = self.sink.store.export(IDENTIFIER)
        self.assertFalse(exported["complete"])
        self.assertEqual(
            exported["manifest"]["gaps"], [{"turn": 2, "status": "queue_full"}]
        )
        self.assertEqual(len(exported["events"]), 1)

    def test_queue_byte_oversize_and_registry_limits(self):
        limits = replace(TraceLimits(), queue_bytes=1, max_sessions=1)
        sink = TraceSink(self.root, limits)
        self.assertEqual(sink.enqueue(event()), "queue_full")
        self.assertEqual(
            sink.enqueue(event(identifier=OTHER)), "session_limit"
        )
        sink = TraceSink(self.root, replace(TraceLimits(), max_event_bytes=1))
        self.assertEqual(sink.enqueue(event()), "oversize")
        self.assertEqual(sink.status(IDENTIFIER)["capture_status"], "oversize")

    def test_note_gap_without_data_queue_and_wrong_epoch_fenced(self):
        self.assertEqual(
            self.sink.note_gap(IDENTIFIER, 0, 1, "projection_invalid"),
            "projection_invalid",
        )
        self.assertEqual(
            self.sink.note_gap(IDENTIFIER, 1, 2, "queue_full"), "revoked"
        )
        self.sink.start()
        wait_for(lambda: (self.root / IDENTIFIER / "manifest.json").exists())
        self.sink.close()
        exported = self.sink.store.export(IDENTIFIER)
        self.assertFalse(exported["complete"])
        self.assertEqual(exported["events"], [])
        self.assertEqual(
            exported["manifest"]["gaps"][0]["status"], "projection_invalid"
        )

    def test_revoke_cancels_queue_and_fences_item_before_publication(self):
        entered, release = threading.Event(), threading.Event()
        original = self.sink.store.append

        def stalled(value, *, allowed):
            entered.set()
            self.assertTrue(release.wait(2))
            return original(value, allowed=allowed)

        with patch.object(self.sink.store, "append", stalled):
            self.sink.enqueue(event())
            self.sink.enqueue(event(2))
            self.sink.start()
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            self.assertEqual(self.sink.revoke(IDENTIFIER, 1), "pending")
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertEqual(self.sink.enqueue(event(2)), "revoked")
            self.assertEqual(
                self.sink.status(IDENTIFIER)["deletion_status"], "pending"
            )
            release.set()
            wait_for(
                lambda: self.sink.status(IDENTIFIER)["deletion_status"]
                == "complete"
            )
        self.assertFalse((self.root / IDENTIFIER).exists())
        self.assertEqual(
            self.sink.status(IDENTIFIER)["capture_status"], "revoked"
        )

    def test_already_entered_publication_keeps_pending_until_removed(self):
        entered, release = threading.Event(), threading.Event()
        original = private_io.write_file

        def publishing(fd, name, raw, **kwargs):
            if name == "turn-001.json":
                entered.set()
                self.assertTrue(release.wait(2))
            return original(fd, name, raw, **kwargs)

        with patch.object(private_io, "write_file", publishing):
            self.sink.enqueue(event())
            self.sink.start()
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            self.assertEqual(self.sink.revoke(IDENTIFIER, 1), "pending")
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertEqual(
                self.sink.status(IDENTIFIER)["deletion_status"], "pending"
            )
            release.set()
            wait_for(
                lambda: self.sink.status(IDENTIFIER)["deletion_status"]
                == "complete"
            )
        self.assertFalse((self.root / IDENTIFIER).exists())

    def test_full_queue_still_accepts_revoke_control(self):
        sink = TraceSink(self.root, replace(TraceLimits(), queue_items=1))
        self.assertEqual(sink.enqueue(event()), "queued")
        self.assertEqual(sink.revoke(IDENTIFIER, 1), "pending")
        sink.start()
        wait_for(
            lambda: sink.status(IDENTIFIER)["deletion_status"] == "complete"
        )
        sink.close()
        self.assertFalse((self.root / IDENTIFIER).exists())

    def test_delete_failure_is_honest_and_repeated_revoke_retries(self):
        self.sink.enqueue(event())
        self.sink.start()
        wait_for(
            lambda: self.sink.status(IDENTIFIER)["capture_status"]
            == "captured"
        )
        with patch.object(
            self.sink.store, "delete", side_effect=OSError("private")
        ):
            self.assertEqual(self.sink.revoke(IDENTIFIER, 1), "pending")
            wait_for(
                lambda: self.sink.status(IDENTIFIER)["deletion_status"]
                == "unavailable"
            )
            self.assertTrue((self.root / IDENTIFIER).exists())
        self.assertEqual(self.sink.revoke(IDENTIFIER, 2), "pending")
        wait_for(
            lambda: self.sink.status(IDENTIFIER)["deletion_status"]
            == "complete"
        )

    def test_disk_failure_and_failed_gap_never_seal_old_prefix(self):
        self.sink.enqueue(event())
        self.sink.start()
        wait_for(
            lambda: self.sink.status(IDENTIFIER)["capture_status"]
            == "captured"
        )
        with (
            patch.object(
                self.sink.store, "append", side_effect=OSError("private")
            ),
            patch.object(
                self.sink.store, "gap", side_effect=OSError("private")
            ),
        ):
            self.sink.enqueue(event(2))
            wait_for(
                lambda: self.sink.status(IDENTIFIER)["capture_status"]
                == "unavailable"
            )
            self.sink.close()
        exported = self.sink.store.export(IDENTIFIER)
        self.assertFalse(exported["complete"])
        self.assertFalse(exported["manifest"]["sealed"])

    def test_shutdown_and_queue_budget_include_stalled_inflight_item(self):
        sink = TraceSink(
            self.root,
            replace(TraceLimits(), queue_items=1, shutdown_seconds=0.05),
        )
        entered, release = threading.Event(), threading.Event()
        original = sink.store.append

        def stalled(value, *, allowed):
            entered.set()
            self.assertTrue(release.wait(2))
            return original(value, allowed=allowed)

        with patch.object(sink.store, "append", stalled):
            sink.enqueue(event())
            sink.start()
            self.assertTrue(entered.wait(2))
            self.assertEqual(sink.enqueue(event(2)), "queue_full")
            started = time.monotonic()
            sink.close()
            self.assertLess(time.monotonic() - started, 0.15)
            release.set()
            sink._thread.join(2)
        self.assertFalse(sink.store.export(IDENTIFIER)["complete"])

    def test_unknown_ready_and_idle_never_fake_disk_recovery(self):
        self.assertEqual(
            self.sink.status(IDENTIFIER)["capture_status"], "ready"
        )
        with patch.object(
            self.sink.store, "prune", side_effect=OSError("private")
        ):
            self.sink.start()
            wait_for(lambda: not self.sink._healthy)
            time.sleep(0.15)
            self.assertEqual(
                self.sink.status(IDENTIFIER)["capture_status"], "unavailable"
            )

    def test_stalled_seal_keeps_marker_after_bounded_close_expires(self):
        entered, release = threading.Event(), threading.Event()
        original = private_io.write_json
        self.sink.enqueue(event())
        self.sink.start()
        wait_for(
            lambda: self.sink.status(IDENTIFIER)["capture_status"]
            == "captured"
        )

        def sealing(fd, name, value, **kwargs):
            if name == "manifest.json" and value.get("sealed") is True:
                entered.set()
                self.assertTrue(release.wait(2))
            return original(fd, name, value, **kwargs)

        with patch.object(private_io, "write_json", sealing):
            closer = threading.Thread(target=lambda: self.sink.close(0.05))
            closer.start()
            self.assertTrue(entered.wait(2))
            closer.join(0.2)
            self.assertFalse(closer.is_alive())
            release.set()
            self.sink._thread.join(2)
        exported = self.sink.store.export(IDENTIFIER)
        self.assertFalse(exported["complete"])
        self.assertTrue((self.root / IDENTIFIER / ".unsealed.json").exists())

    def test_withdraw_during_seal_returns_to_priority_deletion(self):
        entered, release = threading.Event(), threading.Event()
        original = private_io.write_json
        self.sink.enqueue(event())
        self.sink.start()
        wait_for(
            lambda: self.sink.status(IDENTIFIER)["capture_status"]
            == "captured"
        )

        def sealing(fd, name, value, **kwargs):
            if name == "manifest.json" and value.get("sealed") is True:
                entered.set()
                self.assertTrue(release.wait(2))
            return original(fd, name, value, **kwargs)

        with patch.object(private_io, "write_json", sealing):
            closer = threading.Thread(target=lambda: self.sink.close(0.5))
            closer.start()
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.sink.revoke(IDENTIFIER, 1), "pending")
            self.assertEqual(
                self.sink.status(IDENTIFIER)["deletion_status"], "pending"
            )
            release.set()
            wait_for(
                lambda: self.sink.status(IDENTIFIER)["deletion_status"]
                == "complete"
            )
            closer.join(1)
        self.assertFalse((self.root / IDENTIFIER).exists())
