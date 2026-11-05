"""Keep one fixed SSH participant alive with bounded typed JSON frames."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import time
import uuid

from search.operations.deployment import demo_connection
from search.operations.deployment.provider import authority
from search.serialization import strict_json

from . import contract


PROFILE_FIELDS = {
    "schema_version",
    "hops",
    "participant_source",
    "participant_config",
    "entry_sha256",
    "peer_sha256",
}
TIMEOUTS = {
    "prepare": 180,
    "observe": 120,
    "publish": 120,
    "reconcile": 180,
    "release": 30,
}


def load_profile(reference):
    contract.reference(reference)
    raw = authority.read_private(reference["path"], maximum=16384)
    import hashlib

    contract.require(
        hashlib.sha256(raw).hexdigest() == reference["sha256"],
        "configuration_changed",
    )
    value = strict_json(raw)
    contract.version(value, PROFILE_FIELDS)
    contract.source(value["participant_source"])
    contract.reference(value["participant_config"])
    contract.hexadecimal(value["entry_sha256"])
    contract.hexadecimal(value["peer_sha256"])
    require_hops(value["hops"])
    return value


def require_hops(hops):
    contract.require(type(hops) is list and 1 <= len(hops) <= 3)
    for hop in hops:
        contract.closed(hop, {"destination", "ssh_config"})
        contract.require(
            type(hop["destination"]) is str
            and demo_connection.DESTINATION.fullmatch(hop["destination"])
            is not None
        )
        if hop["ssh_config"] is not None:
            contract.reference(hop["ssh_config"])


def command(profile):
    """Build the fixed participant command from a reviewed peer profile."""
    require_hops(profile["hops"])
    source = profile["participant_source"]
    script = str(
        Path(source["root"]) / "scripts/participate_backend_binding.py"
    )
    result = [
        "python3",
        "-I",
        "-S",
        "-B",
        script,
        "--config",
        profile["participant_config"]["path"],
        "--expected-source",
        source["revision"],
        "--expected-entry",
        profile["entry_sha256"],
    ]
    for hop in reversed(profile["hops"]):
        setting = hop["ssh_config"]
        if setting is not None:
            raw = authority.read_private(setting["path"], maximum=65536)
            import hashlib

            contract.require(
                hashlib.sha256(raw).hexdigest() == setting["sha256"],
                "configuration_changed",
            )
        prefix = demo_connection.ssh_prefix(
            {"ssh_config": None if setting is None else setting["path"]}
        )
        result = [*prefix, hop["destination"], shlex.join(result)]
    return result


def frame(value):
    raw = (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        + "\n"
    ).encode("utf-8")
    contract.require(len(raw) <= contract.MAX_FRAME_BYTES, "frame_too_large")
    return raw


class PipeChannel:
    """Drain the owned child while exchanging one bounded frame at a time."""

    def __init__(self, argv, *, deadline, process_factory=subprocess.Popen):
        self.deadline = deadline
        self.selector = selectors.DefaultSelector()
        try:
            self.process = process_factory(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                },
            )
        except BaseException:
            self.selector.close()
            raise
        self.pending = bytearray()
        self.errors = bytearray()
        self.total = 0
        self.uncertain = False
        try:
            self._setup()
        except BaseException:
            self.uncertain = True
            self.close()
            raise

    def _setup(self):
        for stream, label in (
            (self.process.stdout, "stdout"),
            (self.process.stderr, "stderr"),
        ):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ, label)
        os.set_blocking(self.process.stdin.fileno(), False)

    def exchange(self, packet, *, timeout):
        contract.require(not self.uncertain, "outcome_unconfirmed")
        raw = frame(packet)
        self.total += len(raw)
        contract.require(self.total <= contract.MAX_WIRE_BYTES, "output_limit")
        end = min(self.deadline, time.monotonic() + timeout)
        self.selector.register(
            self.process.stdin, selectors.EVENT_WRITE, "stdin"
        )
        outgoing = memoryview(raw)
        try:
            return self._exchange(outgoing, end)
        except BaseException:
            self.uncertain = True
            raise
        finally:
            try:
                self.selector.unregister(self.process.stdin)
            except KeyError:
                pass

    def _exchange(self, outgoing, end):
        while True:
            remaining = end - time.monotonic()
            contract.require(remaining > 0, "participant_timeout")
            for event, _mask in self.selector.select(remaining):
                if event.data == "stdin":
                    outgoing = self._write(outgoing)
                else:
                    self._read(event)
            if not outgoing and b"\n" in self.pending:
                return self._packet()
            contract.require(
                self.process.poll() is None, "participant_disconnected"
            )

    def _write(self, outgoing):
        if outgoing:
            sent = os.write(self.process.stdin.fileno(), outgoing)
            outgoing = outgoing[sent:]
        if not outgoing:
            self.selector.unregister(self.process.stdin)
        return outgoing

    def _read(self, event):
        raw = os.read(event.fileobj.fileno(), 65536)
        if not raw:
            self.selector.unregister(event.fileobj)
            contract.require(
                event.data != "stdout", "participant_disconnected"
            )
            return
        self.total += len(raw)
        contract.require(self.total <= contract.MAX_WIRE_BYTES, "output_limit")
        target = self.pending if event.data == "stdout" else self.errors
        target.extend(raw)
        contract.require(
            len(target) <= contract.MAX_FRAME_BYTES, "output_limit"
        )

    def _packet(self):
        line, _separator, remaining = self.pending.partition(b"\n")
        self.pending = bytearray(remaining)
        contract.require(not remaining, "unsolicited_frame")
        return strict_json(line)

    def close(self):
        self.selector.close()
        self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # Signal only the SSH group created here, never remote model PIDs.
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=2)
        finally:
            self.process.stdout.close()
            self.process.stderr.close()


class FrontendSession:
    """Validate every response against this profile, operation and sequence."""

    def __init__(self, profile, *, deadline, channel_factory=PipeChannel):
        self.profile = profile
        self.deadline = deadline
        self.channel = channel_factory(command(profile), deadline=deadline)
        self.sequence = 0
        self.uncertain = False
        self.namespace = None
        self.intent = None

    def request(self, operation, verb, body, *, before=False):
        contract.require(not self.uncertain, "outcome_unconfirmed")
        contract.require(verb in contract.VERBS)
        self.sequence += 1
        packet = {
            "v": 1,
            "op_id": operation.operation_id,
            "intent_sha256": None if before else operation.sha256,
            "seq": self.sequence,
            "nonce": uuid.uuid4().hex * 2,
            "verb": verb,
            "body": body,
        }
        contract.wire_request(packet)
        try:
            response = self.channel.exchange(packet, timeout=TIMEOUTS[verb])
            return self._response(response, packet, operation)
        except BaseException:
            self.uncertain = True
            raise

    def _response(self, response, packet, operation):
        owner = operation.intent["frontend_owner"]
        contract.require(
            self.profile["participant_source"] == owner["source"]
            and self.profile["peer_sha256"] == owner["peer_sha256"],
            "owner_changed",
        )
        contract.wire_response(response, packet, owner)
        observed_namespace = response["owner_namespace_sha256"]
        expected = operation.intent["sides"]["predecessor"]["frontend"][
            "namespace_sha256"
        ]
        contract.require(observed_namespace == expected, "namespace_changed")
        if self.namespace is not None:
            contract.require(
                self.namespace == observed_namespace, "namespace_changed"
            )
        self.namespace = observed_namespace
        contract.require(response["phase"] != "unknown", "participant_unknown")
        return response["observation"]

    def preflight(self, operation_id):
        """Read the pinned peer's baseline before freezing the full intent."""
        contract.operation_id(operation_id)
        self.sequence += 1
        packet = {
            "v": 1,
            "op_id": operation_id,
            "intent_sha256": None,
            "seq": self.sequence,
            "nonce": uuid.uuid4().hex * 2,
            "verb": "observe",
            "body": {"stage": "preflight", "side": "auto"},
        }
        contract.wire_request(packet)
        response = self.channel.exchange(packet, timeout=TIMEOUTS["observe"])
        contract.wire_response(
            response, packet, {"source": self.profile["participant_source"]}
        )
        contract.require(response["phase"] == "observed", "phase")
        self.namespace = response["owner_namespace_sha256"]
        value = response["observation"]["actual"]
        contract.closed(value, {"projection", "proof"})
        contract.frontend(value["projection"])
        contract.require(
            value["projection"]["namespace_sha256"] == self.namespace,
            "namespace_changed",
        )
        contract.frontend_actual(value["proof"], value["projection"])
        return value

    def prepare(self, operation):
        self.intent = operation.intent
        return self.request(operation, "prepare", {"intent": operation.intent})

    def observe(self, operation, side):
        value = self.request(
            operation, "observe", {"stage": "terminal", "side": side}
        )
        contract.require(value["side"] == side, "binding_changed")
        expected = operation.intent["sides"][side]["frontend"]
        contract.require(
            value["input_projection_sha256"] == contract.digest(expected),
            "binding_changed",
        )
        return contract.frontend_actual(
            value["actual"], expected, operation.intent
        )

    def publish(self, operation, side):
        return self.request(operation, "publish", {"side": side})

    def reconcile(self, operation, side, *, decision=None):
        body = {"requested_side": side}
        if decision is not None:
            contract.discard(decision, operation.intent)
            body["cleanup_decision"] = decision
        value = self.request(operation, "reconcile", body)
        if decision is not None:
            reservation = value["actual"]["reservation"]
            contract.require(
                reservation["cleanup_decision_sha256"]
                == contract.digest(decision),
                "decision_changed",
            )
        return value

    def require_undispatched(self, operation):
        value = self.request(
            operation, "reconcile", {"requested_side": "inspect"}
        )
        contract.require(
            value["actual"].get("undispatched") is True, "dispatched"
        )

    def release(self, operation, decision):
        value = self.request(
            operation,
            "release",
            {
                "decision": decision,
                "canonical_decision_sha256": contract.digest(decision),
            },
        )
        return value["actual"]["released"]

    def close(self):
        self.channel.close()


class PeerFrames:
    """Bound both directions of the fixed participant's foreground session."""

    def __init__(self, input_stream, output_stream, *, deadline):
        self.input, self.output = input_stream, output_stream
        self.deadline = deadline
        self.buffer = bytearray()
        self.count = 0
        self.total = 0
        os.set_blocking(self.input.fileno(), False)
        os.set_blocking(self.output.fileno(), False)

    def _ready(self, stream, events):
        remaining = self.deadline - time.monotonic()
        contract.require(remaining > 0, "participant_timeout")
        with selectors.DefaultSelector() as selector:
            selector.register(stream, events)
            contract.require(
                bool(selector.select(remaining)), "participant_timeout"
            )

    def read(self):
        while b"\n" not in self.buffer:
            self._ready(self.input, selectors.EVENT_READ)
            raw = os.read(self.input.fileno(), 65536)
            if not raw:
                contract.require(not self.buffer, "truncated_frame")
                return None
            self.buffer.extend(raw)
            self.total += len(raw)
            self._bounds()
        line, _separator, remaining = self.buffer.partition(b"\n")
        self.buffer = bytearray(remaining)
        self.count += 1
        contract.require(self.count <= contract.MAX_FRAMES, "frame_limit")
        return contract.wire_request(strict_json(line))

    def _bounds(self):
        contract.require(
            len(self.buffer) <= contract.MAX_FRAME_BYTES, "frame_limit"
        )
        contract.require(self.total <= contract.MAX_WIRE_BYTES, "output_limit")

    def write(self, value):
        raw = memoryview(frame(value))
        self.total += len(raw)
        self._bounds()
        while raw:
            self._ready(self.output, selectors.EVENT_WRITE)
            sent = os.write(self.output.fileno(), raw)
            raw = raw[sent:]
