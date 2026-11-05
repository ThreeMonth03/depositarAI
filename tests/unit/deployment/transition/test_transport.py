"""Run the real framing pump with owned pipes and a CPU-only peer thread."""

import copy
import json
import os
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from search.operations.deployment.transition import contract, transport


class PipeProcess:
    """The bottom process boundary: no child, signal, SSH or socket exists."""

    def __init__(self, callback):
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        error_read, error_write = os.pipe()
        self.stdin = os.fdopen(input_write, "wb", buffering=0)
        self.stdout = os.fdopen(output_read, "rb", buffering=0)
        self.stderr = os.fdopen(error_read, "rb", buffering=0)
        self.reader = os.fdopen(input_read, "rb", buffering=0)
        self.writer = os.fdopen(output_write, "wb", buffering=0)
        self.error_writer = os.fdopen(error_write, "wb", buffering=0)
        self.callback = callback
        self.failure = None
        self.calls = []
        self.thread = threading.Thread(target=self._serve)
        self.thread.start()

    def _serve(self):
        try:
            for raw in self.reader:
                packet = json.loads(raw)
                self.calls.append(packet)
                result = self.callback(packet)
                if result is None:
                    return
                raw = (
                    result
                    if isinstance(result, bytes)
                    else transport.frame(result)
                )
                self.writer.write(raw)
        except BaseException as error:
            self.failure = error
        finally:
            self.reader.close()
            self.writer.close()
            self.error_writer.close()

    def poll(self):
        return None if self.thread.is_alive() else 0

    def wait(self, timeout):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired("owned fixture pipe", timeout)
        return 0


def profile():
    return {
        "schema_version": 1,
        "hops": [
            {"destination": "maintainer@example.test", "ssh_config": None}
        ],
        "participant_source": {
            "root": "/private/operator",
            "revision": "a" * 40,
        },
        "participant_config": {
            "path": "/private/participant.json",
            "sha256": "b" * 64,
        },
        "entry_sha256": "c" * 64,
        "peer_sha256": "d" * 64,
    }


class FramingTests(unittest.TestCase):
    def test_failed_pipe_setup_closes_the_owned_child_and_all_streams(self):
        process = PipeProcess(lambda packet: {"ok": True})
        try:
            with patch.object(
                transport.PipeChannel,
                "_setup",
                side_effect=OSError("owned setup failure"),
            ):
                with self.assertRaisesRegex(OSError, "owned setup failure"):
                    transport.PipeChannel(
                        ["fixed-participant"],
                        deadline=time.monotonic() + 10,
                        process_factory=lambda *args, **kwargs: process,
                    )
            self.assertTrue(process.stdin.closed)
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
            self.assertEqual(process.poll(), 0)
        finally:
            process.stdin.close()
            process.wait(timeout=2)
            process.stdout.close()
            process.stderr.close()

    def channel(self, callback):
        process = PipeProcess(callback)
        channel = transport.PipeChannel(
            ["fixed-participant"],
            deadline=time.monotonic() + 10,
            process_factory=lambda *args, **kwargs: process,
        )
        self.addCleanup(channel.close)
        return channel, process

    def test_actual_pump_exchanges_multiple_frames_without_native_process(
        self,
    ):
        channel, process = self.channel(
            lambda packet: {"echo": packet["number"]}
        )
        self.assertEqual(
            channel.exchange({"number": 1}, timeout=1), {"echo": 1}
        )
        self.assertEqual(
            channel.exchange({"number": 2}, timeout=1), {"echo": 2}
        )
        self.assertEqual(len(process.calls), 2)
        self.assertIsNone(process.failure)

    def test_expired_frame_marks_channel_uncertain_without_write_replay(self):
        channel, _process = self.channel(lambda packet: packet)
        channel.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(
            contract.TransitionError, "participant_timeout"
        ):
            channel.exchange({"number": 1}, timeout=1)
        self.assertTrue(channel.uncertain)
        with self.assertRaisesRegex(
            contract.TransitionError, "outcome_unconfirmed"
        ):
            channel.exchange({"number": 2}, timeout=1)

    def test_unsolicited_second_packet_is_not_accepted_as_the_next_response(
        self,
    ):
        channel, _process = self.channel(lambda packet: packet)
        channel.pending = bytearray(b'{"first":1}\n{"extra":2}\n')
        with self.assertRaisesRegex(
            contract.TransitionError, "unsolicited_frame"
        ):
            channel._packet()

    def test_frame_limit_is_checked_before_any_native_or_pipe_write(self):
        channel, process = self.channel(lambda packet: packet)
        with patch.object(contract, "MAX_FRAME_BYTES", 8):
            with self.assertRaisesRegex(
                contract.TransitionError, "frame_too_large"
            ):
                channel.exchange({"oversized": "input"}, timeout=1)
        self.assertEqual(process.calls, [])

    def test_fixed_ssh_command_never_uses_packet_fields_as_shell_code(self):
        value = profile()
        value["participant_config"]["path"] = (
            "/private/config with spaces.json"
        )
        command = transport.command(value)
        self.assertEqual(command[0], "ssh")
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("ForwardAgent=no", command)
        self.assertNotIn("-L", command)
        self.assertIn("scripts/participate_backend_binding.py", command[-1])
        self.assertIn("'/private/config with spaces.json'", command[-1])

    def test_response_sequence_nonce_owner_and_closed_fields_are_all_bound(
        self,
    ):
        request = {
            "v": 1,
            "op_id": "1" * 32,
            "intent_sha256": "2" * 64,
            "seq": 1,
            "nonce": "3" * 64,
            "verb": "observe",
            "body": {"stage": "terminal", "side": "target"},
        }
        owner = {"source": profile()["participant_source"]}
        response = {
            key: request[key]
            for key in ("v", "op_id", "intent_sha256", "seq", "nonce")
        }
        response.update(
            owner_source=owner["source"],
            owner_namespace_sha256="4" * 64,
            phase="frozen",
            observation={
                "side": "target",
                "input_projection_sha256": "5" * 64,
                "actual": {"reservation": {}},
            },
        )
        self.assertIs(
            contract.wire_response(response, request, owner), response
        )
        for field, bad in (
            ("seq", 2),
            ("nonce", "6" * 64),
            ("owner_source", {"root": "/wrong"}),
            ("v", True),
        ):
            changed = {**copy.deepcopy(response), field: bad}
            with (
                self.subTest(field=field),
                self.assertRaises(contract.TransitionError),
            ):
                contract.wire_response(changed, request, owner)
        with self.assertRaises(contract.TransitionError):
            contract.wire_response(
                {**response, "reservation": {}}, request, owner
            )

    def test_peer_eof_is_uncertain_and_never_replays_the_dispatched_request(
        self,
    ):
        channel, process = self.channel(lambda packet: None)
        with self.assertRaisesRegex(
            contract.TransitionError, "participant_disconnected"
        ):
            channel.exchange({"number": 1}, timeout=1)
        self.assertTrue(channel.uncertain)
        with self.assertRaisesRegex(
            contract.TransitionError, "outcome_unconfirmed"
        ):
            channel.exchange({"number": 2}, timeout=1)
        self.assertEqual(process.calls, [{"number": 1}])

    def test_duplicate_json_keys_after_dispatch_are_not_silently_normalized(
        self,
    ):
        channel, process = self.channel(lambda packet: b'{"ok":1,"ok":2}\n')
        with self.assertRaises(ValueError):
            channel.exchange({"number": 1}, timeout=1)
        self.assertTrue(channel.uncertain)
        self.assertEqual(len(process.calls), 1)

    def test_oversized_peer_output_is_bounded_and_poisons_the_session(self):
        channel, _process = self.channel(lambda packet: b"x" * 256 + b"\n")
        with patch.object(contract, "MAX_FRAME_BYTES", 128):
            with self.assertRaisesRegex(
                contract.TransitionError, "output_limit"
            ):
                channel.exchange({"number": 1}, timeout=1)
        self.assertTrue(channel.uncertain)

    def test_both_directions_share_one_wire_byte_budget(self):
        channel, _process = self.channel(lambda packet: b'{"ok":true}\n')
        with patch.object(contract, "MAX_WIRE_BYTES", 20):
            with self.assertRaisesRegex(
                contract.TransitionError, "output_limit"
            ):
                channel.exchange({"number": 1}, timeout=1)
        self.assertTrue(channel.uncertain)

    def test_destination_options_and_extra_hop_fields_are_rejected(self):
        for destination in (
            "-oProxyCommand=bad",
            "user@host;touch bad",
            "a\nb",
        ):
            value = profile()
            value["hops"][0]["destination"] = destination
            with (
                self.subTest(destination=destination),
                self.assertRaises(contract.TransitionError),
            ):
                transport.command(value)
        value = profile()
        value["hops"][0]["shell"] = "arbitrary command"
        with self.assertRaises(contract.TransitionError):
            transport.command(value)


def request_packet(sequence=1):
    return {
        "v": 1,
        "op_id": "1" * 32,
        "intent_sha256": "2" * 64,
        "seq": sequence,
        "nonce": "3" * 64,
        "verb": "observe",
        "body": {"stage": "terminal", "side": "target"},
    }


class PeerFrameTests(unittest.TestCase):
    def peer(self, raw):
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        streams = [
            os.fdopen(input_read, "rb", buffering=0),
            os.fdopen(input_write, "wb", buffering=0),
            os.fdopen(output_read, "rb", buffering=0),
            os.fdopen(output_write, "wb", buffering=0),
        ]
        for stream in streams:
            self.addCleanup(stream.close)
        streams[1].write(raw)
        streams[1].close()
        peer = transport.PeerFrames(
            streams[0], streams[3], deadline=time.monotonic() + 2
        )
        return peer, streams[2]

    def test_clean_eof_and_truncated_json_are_distinct(self):
        empty, _output = self.peer(b"")
        self.assertIsNone(empty.read())
        truncated, _output = self.peer(b'{"v":1')
        with self.assertRaisesRegex(
            contract.TransitionError, "truncated_frame"
        ):
            truncated.read()

    def test_frame_count_cannot_be_reset_by_buffered_messages(self):
        raw = transport.frame(request_packet()) * 3
        peer, _output = self.peer(raw)
        with patch.object(contract, "MAX_FRAMES", 2):
            self.assertEqual(peer.read(), request_packet())
            self.assertEqual(peer.read(), request_packet())
            with self.assertRaisesRegex(
                contract.TransitionError, "frame_limit"
            ):
                peer.read()

    def test_invalid_request_type_and_closed_fields_are_refused(self):
        for field, value in (
            ("v", True),
            ("seq", True),
            ("seq", 17),
            ("nonce", "3" * 63),
            ("verb", []),
            ("body", []),
            ("allow_pending", True),
        ):
            packet = {**request_packet(), field: value}
            peer, _output = self.peer(transport.frame(packet))
            with (
                self.subTest(field=field),
                self.assertRaises(contract.TransitionError),
            ):
                peer.read()

    def test_partial_frame_and_total_wire_limits_are_checked_before_decode(
        self,
    ):
        peer, _output = self.peer(b"x" * 64)
        with patch.object(contract, "MAX_FRAME_BYTES", 32):
            with self.assertRaisesRegex(
                contract.TransitionError, "frame_limit"
            ):
                peer.read()
        peer, _output = self.peer(transport.frame(request_packet()))
        with patch.object(contract, "MAX_WIRE_BYTES", 32):
            with self.assertRaisesRegex(
                contract.TransitionError, "output_limit"
            ):
                peer.read()

    def test_participant_write_uses_same_budget_and_complete_json_frame(self):
        peer, output = self.peer(b"")
        value = {"result": "Unicode 氣候"}
        peer.write(value)
        self.assertEqual(output.readline(), transport.frame(value))
        sent = peer.total
        with patch.object(contract, "MAX_WIRE_BYTES", sent):
            with self.assertRaisesRegex(
                contract.TransitionError, "output_limit"
            ):
                peer.write(value)
        self.assertGreater(peer.total, sent)

    def test_expired_participant_deadline_refuses_read_and_write(self):
        peer, _output = self.peer(transport.frame(request_packet()))
        peer.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(
            contract.TransitionError, "participant_timeout"
        ):
            peer.read()
        with self.assertRaisesRegex(
            contract.TransitionError, "participant_timeout"
        ):
            peer.write({"result": True})


def response_packet(packet):
    value = {
        key: packet[key]
        for key in ("v", "op_id", "intent_sha256", "seq", "nonce")
    }
    value.update(
        owner_source=profile()["participant_source"],
        owner_namespace_sha256="4" * 64,
        phase="frozen",
        observation={
            "side": "target",
            "input_projection_sha256": "5" * 64,
            "actual": {"reservation": {"operation_id": packet["op_id"]}},
        },
    )
    return value


class SessionAdmissionTests(unittest.TestCase):
    """Exercise response admission, not full owner or transaction proofs."""

    def session(self, response_changes=None):
        channel = Mock(spec=["exchange", "close"])

        def reply(packet, *, timeout):
            value = response_packet(packet)
            value.update(copy.deepcopy(response_changes or {}))
            return value

        channel.exchange.side_effect = reply
        session = transport.FrontendSession(
            profile(),
            deadline=time.monotonic() + 10,
            channel_factory=lambda *args, **kwargs: channel,
        )
        self.addCleanup(session.close)
        frontend = {"namespace_sha256": "4" * 64}
        operation = SimpleNamespace(
            operation_id="1" * 32,
            sha256="2" * 64,
            intent={
                "frontend_owner": {
                    "source": profile()["participant_source"],
                    "peer_sha256": profile()["peer_sha256"],
                },
                "sides": {
                    "predecessor": {"frontend": frontend},
                    "target": {"frontend": frontend},
                },
            },
        )
        return session, channel, operation

    def test_sequence_and_nonce_are_fresh_and_intent_is_bound_on_each_request(
        self,
    ):
        session, channel, operation = self.session()
        for verb in ("prepare", "observe"):
            session.request(operation, verb, {"test": True})
        first, second = [
            call.args[0] for call in channel.exchange.call_args_list
        ]
        self.assertEqual((first["seq"], second["seq"]), (1, 2))
        self.assertNotEqual(first["nonce"], second["nonce"])
        self.assertEqual(first["intent_sha256"], operation.sha256)
        self.assertEqual(second["intent_sha256"], operation.sha256)
        self.assertEqual(
            channel.exchange.call_args_list[0].kwargs, {"timeout": 180}
        )
        self.assertEqual(
            channel.exchange.call_args_list[1].kwargs, {"timeout": 120}
        )

    def test_replayed_response_blocks_all_further_dispatch(self):
        session, channel, operation = self.session()
        session.request(operation, "observe", {})
        old_packet = channel.exchange.call_args.args[0]
        channel.exchange.side_effect = None
        channel.exchange.return_value = response_packet(old_packet)
        with self.assertRaisesRegex(
            contract.TransitionError, "response_changed"
        ):
            session.request(operation, "publish", {"side": "target"})
        self.assertTrue(session.uncertain)
        with self.assertRaisesRegex(
            contract.TransitionError, "outcome_unconfirmed"
        ):
            session.request(operation, "publish", {"side": "target"})
        self.assertEqual(channel.exchange.call_count, 2)

    def test_source_namespace_unknown_and_top_level_reservation_are_fatal(
        self,
    ):
        for changes in (
            {"owner_source": {"root": "/foreign", "revision": "a" * 40}},
            {"owner_namespace_sha256": "0" * 64},
            {"phase": "unknown"},
            {"reservation": {}},
        ):
            session, channel, operation = self.session(changes)
            with (
                self.subTest(changes=changes),
                self.assertRaises(contract.TransitionError),
            ):
                session.request(operation, "prepare", {})
            self.assertTrue(session.uncertain)
            self.assertEqual(channel.exchange.call_count, 1)

    def test_base_exception_after_dispatch_keeps_session_in_uncertain_hold(
        self,
    ):
        session, channel, operation = self.session()
        channel.exchange.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            session.request(operation, "publish", {"side": "target"})
        self.assertTrue(session.uncertain)
        with self.assertRaisesRegex(
            contract.TransitionError, "outcome_unconfirmed"
        ):
            session.request(operation, "publish", {"side": "target"})
        self.assertEqual(channel.exchange.call_count, 1)

    def test_exhausted_sequence_budget_refuses_before_transport_dispatch(self):
        session, channel, operation = self.session()
        session.sequence = contract.MAX_FRAMES
        with self.assertRaises(contract.TransitionError):
            session.request(operation, "observe", {})
        channel.exchange.assert_not_called()

    def test_observation_keeps_reservation_inside_actual_typed_variant(self):
        session, _channel, operation = self.session()
        value = session.request(operation, "observe", {})
        self.assertEqual(
            set(value), {"side", "input_projection_sha256", "actual"}
        )
        self.assertEqual(
            value["actual"]["reservation"]["operation_id"],
            operation.operation_id,
        )
