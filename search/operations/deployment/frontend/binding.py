"""Own the VM side of one backend binding operation without image cutovers."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import time

from search.operations.deployment.transition import contract, enrollment
from search.operations.deployment.runtime_docker import Docker
from search.operations.shared import namespace, private_io
from search.operations.shared.source_identity import require_clean_revision
from search.serialization import strict_json

from . import check, delivery


CONFIG_FIELDS = {
    "schema_version",
    "kind",
    "source",
    "delivery_configuration",
    "coordinator_peer_sha256",
}


def read_input(path, *, maximum=contract.MAX_BYTES):
    path = Path(contract.path(str(path)))
    namespace.check_directory(path.parent, private=True)
    from search.operations.deployment.public_entry import profile

    return profile._read_private(path, uid=os.geteuid(), maximum=maximum)


def input_reference(path, *, maximum=contract.MAX_BYTES):
    raw = read_input(path, maximum=maximum)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def read_reference(value, *, maximum=contract.MAX_BYTES):
    contract.reference(value)
    raw = read_input(value["path"], maximum=maximum)
    contract.require(
        hashlib.sha256(raw).hexdigest() == value["sha256"],
        "configuration_changed",
    )
    return raw


def load_config(path):
    raw = read_input(path, maximum=16384)
    value = strict_json(raw)
    contract.version(value, CONFIG_FIELDS, "frontend-binding-participant")
    contract.source(value["source"])
    contract.reference(value["delivery_configuration"])
    contract.hexadecimal(value["coordinator_peer_sha256"])
    return value, {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


class Participant:
    """Retain local locks and one operation-bound durable reservation."""

    def __init__(
        self,
        config_path,
        config,
        config_ref,
        state,
        docker,
        lock,
        *,
        runner=None,
        source_runner=None,
        deadline=None,
    ):
        self.config_path, self.config = Path(config_path), config
        self.config_ref, self.state = config_ref, state
        self.deadline = (
            time.monotonic() + 1800 if deadline is None else deadline
        )
        self.runner, self.source_runner = runner, source_runner
        self.calls = 0
        self.sequence = 0
        self._source_sequence = None
        self.last_observation = None
        self.operation_id = None
        self.operation = None
        self.uncertain = False
        self.delivery_config = delivery.load_config(
            config["delivery_configuration"]["path"]
        )
        self.delivery = delivery.Delivery(
            self.delivery_config, state, docker, self._run
        )
        self.delivery.docker.runner = self._run
        self.monitor_identity = self._lock_identity(lock)
        owner = enrollment.owner(state.root)
        contract.require(owner is not None, "not_enrolled")
        contract.require(
            owner["source"] == config["source"]
            and owner["configuration"] == config_ref,
            "owner_changed",
        )
        self.reservation = enrollment.BindingReservation(
            state.root, state.fd, owner
        )
        self.before = None
        self.refresh()

    def _lock_identity(self, lock):
        row = os.fstat(lock)
        return contract.digest(
            [row.st_dev, row.st_ino, row.st_uid, row.st_gid, row.st_mode]
        )

    def _remaining(self, timeout):
        left = self.deadline - time.monotonic()
        contract.require(left > 0, "participant_timeout")
        self.calls += 1
        contract.require(
            self.calls <= contract.PARTICIPANT_COMMANDS, "command_limit"
        )
        return min(timeout, left)

    def _run(self, argv, **kwargs):
        kwargs["timeout"] = self._remaining(kwargs["timeout"])
        if self.runner is None:
            from search.operations.deployment.images import build

            return build._run(argv, **kwargs)
        return self.runner(argv, **kwargs)

    def _source_call(self, argv, *, timeout):
        timeout = self._remaining(timeout)
        if self.source_runner is not None:
            return self.source_runner(argv, timeout=timeout)
        return subprocess.run(
            argv,
            timeout=timeout,
            check=False,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
        )

    def refresh(self):
        contract.require(not self.uncertain, "outcome_unconfirmed")
        if self._source_sequence != self.sequence:
            contract.require(
                require_clean_revision(
                    Path(self.config["source"]["root"]),
                    runner=self._source_call,
                )
                == self.config["source"]["revision"],
                "source_changed",
            )
            self._source_sequence = self.sequence
        read_reference(self.config_ref, maximum=16384)
        read_reference(self.config["delivery_configuration"], maximum=16384)
        self.reservation.refresh()
        contract.require(
            delivery.load_config(self.config["delivery_configuration"]["path"])
            == self.delivery_config,
            "configuration_changed",
        )

    def _state_value(self):
        value = self.state.read("state.json")
        contract.require(value is not None, "frontend_not_adopted")
        self.delivery.saved_state(value)
        return value

    def _mount_reference(self, composition, target):
        mounts = composition["services"]["web"]["volumes"]
        selected = [item for item in mounts if item["target"] == target]
        if not selected:
            return None
        contract.require(
            len(selected) == 1 and selected[0]["read_only"] is True,
            "binding_changed",
        )
        return input_reference(selected[0]["source"], maximum=65536)

    def projection(self):
        self.refresh()
        value = self._state_value()
        contract.require(
            self.delivery.canonical() == value["current"]["profile"],
            "frontend_changed",
        )
        current = value["current"]
        config = self.delivery.canonical()
        composition = current["composition"]
        result = {
            "delivery_config": self.config["delivery_configuration"],
            "canonical_profile": input_reference(
                self.delivery_config["frontend_check_file"], maximum=16384
            ),
            "profile": config,
            "env": input_reference(config["frontend_env_file"], maximum=16384),
            "composition_sha256": contract.digest(composition),
            "api_key_mount": self._mount_reference(
                composition, "/run/secrets/api_key"
            ),
            "ca_mount": self._mount_reference(
                composition, "/run/certs/upstream-ca.pem"
            ),
            "state": input_reference(self.state.root / "state.json"),
            "current": copy.deepcopy(current),
            "previous": copy.deepcopy(value["previous"]),
            "namespace_sha256": self.reservation.namespace_sha256,
            "monitor_lock_identity": self.monitor_identity,
        }
        contract.frontend(result)
        self.refresh()
        return result

    def preflight(self):
        contract.require(self.operation is None, "phase")
        enrollment.require_unreserved(self.state.root)
        self.before = self.projection()
        return self._observation(
            "predecessor",
            self.before,
            {
                "projection": copy.deepcopy(self.before),
                "proof": self._actual(self.before, None),
            },
        )

    def prepare(self, operation):
        self.refresh()
        contract.intent(operation)
        owner = operation["frontend_owner"]
        contract.require(
            owner["source"] == self.config["source"]
            and owner["participant_config"] == self.config_ref
            and operation["authority"]["backend_peer_sha256"]
            == self.config["coordinator_peer_sha256"],
            "owner_changed",
        )
        expected = operation["sides"]["predecessor"]["frontend"]
        contract.require(
            expected["namespace_sha256"] == self.reservation.namespace_sha256
            and expected["monitor_lock_identity"] == self.monitor_identity,
            "namespace_changed",
        )
        self.operation = copy.deepcopy(operation)
        released = self.reservation.released(operation)
        if released is not None:
            return "released", self._observation(
                released["side"],
                operation["sides"][released["side"]]["frontend"],
                {"released": released},
            )
        current = self.reservation.current(operation)
        if current is None:
            contract.require(self.projection() == expected, "frontend_changed")
            self._save_original_profile(expected)
        self.reservation.prepare(operation)
        if current is not None:
            self._validate_partial_metadata()
            self._verify_fixed_inputs(expected)
            return "prepared", self._observation(
                "mixed",
                expected,
                {
                    "reservation": self._reservation_projection(current),
                    "reattached": True,
                },
            )
        return "prepared", self.observe("auto")

    def _expected_state(self, side):
        value = self.operation["sides"][side]["frontend"]
        return {
            "schema_version": 1,
            "current": copy.deepcopy(value["current"]),
            "previous": copy.deepcopy(value["previous"]),
        }

    def _original_profile_name(self):
        return "binding-profile-" + contract.digest(self.operation) + ".json"

    def _save_original_profile(self, expected):
        raw = read_reference(expected["canonical_profile"])
        name = self._original_profile_name()
        try:
            old = private_io.read_file(
                self.state.fd, name, mode=0o600, max_bytes=16384
            )
        except FileNotFoundError:
            private_io.write_file(self.state.fd, name, raw, max_bytes=16384)
        else:
            contract.require(old == raw, "input_changed")

    def _publish_profile(self, side, allowed):
        target = self.operation["sides"][side]["frontend"]
        if side == "predecessor":
            raw = private_io.read_file(
                self.state.fd,
                self._original_profile_name(),
                mode=0o600,
                max_bytes=16384,
            )
        else:
            raw = private_io.canonical(target["profile"])
        contract.require(
            strict_json(raw) == target["profile"]
            and hashlib.sha256(raw).hexdigest()
            == target["canonical_profile"]["sha256"],
            "input_changed",
        )
        contract.require(self.delivery.canonical() in allowed, "input_changed")
        path = self.delivery_config["frontend_check_file"]
        fd = private_io.open_directory(path.parent)
        try:
            private_io.write_file(
                fd, path.name, raw, replace=True, max_bytes=16384
            )
        finally:
            os.close(fd)

    def _actual(self, projection, reservation):
        proof = self.delivery.checked(projection["profile"])
        contract.require(
            proof["runtime"] == projection["current"]["runtime"],
            "runtime_changed",
        )
        return {
            "canonical_profile_sha256": projection["canonical_profile"][
                "sha256"
            ],
            "frontend_state_sha256": projection["state"]["sha256"],
            "env_sha256": projection["env"]["sha256"],
            "composition_sha256": projection["composition_sha256"],
            "runtime": proof["runtime"],
            "version": {
                key: proof[key]
                for key in (
                    "authenticated_version_status",
                    "unauthenticated_version_status",
                    "backend_identity_verified",
                    "upstream_tls_verified",
                )
            },
            "homepage_verified": proof["homepage_verified"],
            "assets_verified": proof["assets_verified"],
            "input_bindings": {
                "profile": projection["canonical_profile"],
                "key": projection["api_key_mount"],
                "ca": projection["ca_mount"],
            },
            "reservation": self._reservation_projection(reservation),
        }

    def _reservation_projection(self, value):
        if value is None:
            return None
        result = {
            "op_id": value["operation_id"],
            "intent_sha256": value["intent_sha256"],
        }
        if value["cleanup_decision_sha256"] is not None:
            result["cleanup_decision_sha256"] = value[
                "cleanup_decision_sha256"
            ]
        return result

    def _observation(self, side, projection, actual):
        return {
            "side": side,
            "input_projection_sha256": contract.digest(projection),
            "actual": actual,
        }

    def observe(self, side):
        self.refresh()
        contract.require(self.operation is not None, "phase")
        contract.choice(side, {*contract.SIDES, "auto"})
        self._require_attached()
        current = self.projection()
        selected = [
            name
            for name in contract.SIDES
            if current == self.operation["sides"][name]["frontend"]
        ]
        contract.require(bool(selected), "frontend_changed")
        selected_side = side if side != "auto" else selected[0]
        contract.require(selected_side in selected, "binding_changed")
        reservation = self.reservation.require_side(
            self.operation, selected_side
        )
        actual = self._actual(current, reservation)
        self.refresh()
        if reservation is not None:
            self.reservation.mark(self.operation, "frozen")
        result = self._observation(selected_side, current, actual)
        self.last_observation = copy.deepcopy(result)
        return result

    def _require_attached(self):
        reserved = self.reservation.current(self.operation)
        released = self.reservation.released(self.operation)
        contract.require(
            reserved is not None or released is not None, "reservation_missing"
        )

    def _dispatch_name(self):
        return "binding-dispatch-" + contract.digest(self.operation) + ".json"

    def _write_dispatch(self):
        value = {
            "schema_version": 1,
            "operation_id": self.operation["operation_id"],
            "intent_sha256": contract.digest(self.operation),
        }
        existing = self.state.read(self._dispatch_name())
        if existing is None:
            self.state.write(self._dispatch_name(), value)
        else:
            contract.require(existing == value, "record_changed")

    def publish(self, side):
        self.refresh()
        contract.choice(side, contract.SIDES)
        contract.require(self.operation is not None, "phase")
        contract.require(
            self.reservation.released(self.operation) is None,
            "already_released",
        )
        contract.require(
            self.reservation.require_side(self.operation, side) is not None,
            "reservation_missing",
        )
        target = self.operation["sides"][side]["frontend"]
        self._validate_partial_metadata()
        self._verify_fixed_inputs(target)
        # Verify the selected backend before writing either metadata record.
        actual = self._actual(target, self.reservation.current(self.operation))
        self._write_dispatch()
        allowed = tuple(
            self.operation["sides"][name]["frontend"]["profile"]
            for name in contract.SIDES
        )
        self._publish_profile(side, allowed)
        self.state.write("state.json", self._expected_state(side))
        self.reservation.mark(self.operation, "published")
        contract.require(self.projection() == target, "frontend_changed")
        return self._observation(side, target, actual)

    def _validate_partial_metadata(self):
        canonical = self.delivery.canonical()
        value = self._state_value()
        expected = [
            self.operation["sides"][name]["frontend"]
            for name in contract.SIDES
        ]
        contract.require(
            canonical in [item["profile"] for item in expected],
            "frontend_changed",
        )
        contract.require(
            value in [self._expected_state(name) for name in contract.SIDES],
            "frontend_changed",
        )

    def _verify_fixed_inputs(self, target):
        for key in ("delivery_config", "env", "api_key_mount", "ca_mount"):
            reference = target[key]
            if reference is not None:
                read_reference(reference)
        self.delivery.record(target["current"])
        contract.require(
            target["previous"]
            == self.operation["sides"]["predecessor"]["frontend"]["previous"],
            "previous_changed",
        )

    def reconcile(self, side, *, decision=None):
        self.refresh()
        contract.require(self.operation is not None, "phase")
        if side == "inspect":
            observation = self.observe("auto")
            observation["actual"]["undispatched"] = (
                self.state.read(self._dispatch_name()) is None
            )
            return observation
        contract.choice(side, contract.SIDES)
        if decision is not None:
            contract.discard(decision, self.operation)
            contract.require(side == "predecessor", "outcome_fixed")
            self.reservation.lock_predecessor(self.operation, decision)
        contract.require(
            self.reservation.released(self.operation) is None,
            "already_released",
        )
        return self.publish(side)

    def release(self, decision, expected_digest):
        self.refresh()
        contract.require(self.operation is not None, "phase")
        contract.resolution(decision, self.operation)
        contract.require(
            contract.digest(decision) == expected_digest, "decision_changed"
        )
        existing = self.reservation.released(self.operation)
        if existing is not None:
            released = self.reservation.release(self.operation, decision, {})
        else:
            self._cancel_admission(decision)
            observed = self._release_observation(decision["side"])
            released = self.reservation.release(
                self.operation, decision, observed
            )
        return self._observation(
            decision["side"],
            self.operation["sides"][decision["side"]]["frontend"],
            {"released": released},
        )

    def _release_observation(self, side):
        expected = self.operation["sides"][side]["frontend"]
        contract.require(
            self.last_observation is not None
            and self.last_observation["side"] == side,
            "fresh_observation_required",
        )
        contract.require(self.projection() == expected, "frontend_changed")
        runtime = check._runtime(
            self.delivery.docker,
            expected["current"]["composition"],
            expected["profile"],
        )
        contract.require(
            runtime == expected["current"]["runtime"], "runtime_changed"
        )
        self._verify_fixed_inputs(expected)
        return copy.deepcopy(self.last_observation)

    def _cancel_admission(self, decision):
        if decision["mode"] == "cancelled_before_begin":
            contract.require(
                self.state.read(self._dispatch_name()) is None, "dispatched"
            )

    def request(self, packet):
        contract.wire_request(packet)
        contract.require(packet["seq"] == self.sequence + 1, "replay")
        if self.operation_id is None:
            self.operation_id = packet["op_id"]
        contract.require(
            packet["op_id"] == self.operation_id, "operation_changed"
        )
        self.sequence = packet["seq"]
        phase, observation = self._request(packet)
        return {
            **{
                key: packet[key]
                for key in ("v", "op_id", "intent_sha256", "seq", "nonce")
            },
            "owner_source": self.config["source"],
            "owner_namespace_sha256": self.reservation.namespace_sha256,
            "phase": phase,
            "observation": observation,
        }

    def _request(self, packet):
        verb, body = packet["verb"], packet["body"]
        if packet["intent_sha256"] is None:
            contract.require(
                verb == "observe"
                and body == {"stage": "preflight", "side": "auto"},
                "phase",
            )
            return "observed", self.preflight()
        if verb == "prepare":
            contract.closed(body, {"intent"})
            contract.require(
                contract.digest(body["intent"]) == packet["intent_sha256"]
                and body["intent"]["operation_id"] == packet["op_id"],
                "intent_changed",
            )
            return self.prepare(body["intent"])
        contract.require(
            self.operation is not None
            and contract.digest(self.operation) == packet["intent_sha256"],
            "intent_changed",
        )
        return self._attached_request(verb, body)

    def _attached_request(self, verb, body):
        if verb == "observe":
            contract.closed(body, {"stage", "side"})
            contract.require(body["stage"] == "terminal")
            return "frozen", self.observe(body["side"])
        if verb == "publish":
            contract.closed(body, {"side"})
            return "published", self.publish(body["side"])
        if verb == "reconcile":
            return "prepared", self._reconcile_request(body)
        contract.require(verb == "release", "verb")
        contract.closed(body, {"decision", "canonical_decision_sha256"})
        return "released", self.release(
            body["decision"], body["canonical_decision_sha256"]
        )

    def _reconcile_request(self, body):
        expected = {"requested_side"}
        if "cleanup_decision" in body:
            expected.add("cleanup_decision")
        contract.closed(body, expected)
        return self.reconcile(
            body["requested_side"], decision=body.get("cleanup_decision")
        )

    def serve(self, frames):
        while True:
            packet = frames.read()
            if packet is None:
                return
            try:
                response = self.request(packet)
            except (ValueError, OSError, TypeError, KeyError, RecursionError):
                self.uncertain = True
                response = self._unknown(packet)
                frames.write(response)
                return
            frames.write(response)
            if packet["verb"] == "release" and response["phase"] == "released":
                return

    def _unknown(self, packet):
        return {
            **{
                key: packet[key]
                for key in ("v", "op_id", "intent_sha256", "seq", "nonce")
            },
            "owner_source": self.config["source"],
            "owner_namespace_sha256": self.reservation.namespace_sha256,
            "phase": "unknown",
            "observation": {
                "side": "unknown",
                "input_projection_sha256": "0" * 64,
                "actual": {"error": "backend_transition_recovery_required"},
            },
        }


@contextmanager
def open_participant(
    config_path,
    *,
    expected_source,
    expected_entry,
    runner=None,
    source_runner=None,
    deadline=None,
    source_root=None,
):
    config, config_ref = load_config(config_path)
    loaded = (
        Path(__file__).resolve().parents[4]
        if source_root is None
        else Path(source_root).resolve()
    )
    contract.require(
        Path(config["source"]["root"]) == loaded, "source_changed"
    )
    contract.require(
        config["source"]["revision"] == expected_source, "source_changed"
    )
    entry = (
        Path(config["source"]["root"])
        / "scripts/participate_backend_binding.py"
    )
    contract.require(
        hashlib.sha256(entry.read_bytes()).hexdigest() == expected_entry,
        "source_changed",
    )
    read_reference(config["delivery_configuration"], maximum=16384)
    delivery_config = delivery.load_config(
        config["delivery_configuration"]["path"]
    )
    state = delivery.State(delivery_config["state_root"])
    limit = (
        time.monotonic() + contract.MAX_SECONDS
        if deadline is None
        else deadline
    )
    try:
        with state.locked(), ExitStack() as stack:
            monitor = delivery_config["monitor_lock_file"]
            fd = private_io.open_directory(monitor.parent)
            stack.callback(os.close, fd)
            lock = stack.enter_context(
                delivery._lock(fd, monitor.name, wait=0)
            )
            directory = stack.enter_context(
                tempfile.TemporaryDirectory(
                    prefix=".binding-docker-", dir=state.root
                )
            )
            docker = Docker(
                directory,
                runner,
                deadline=limit,
                max_commands=contract.PARTICIPANT_COMMANDS,
            )
            participant = Participant(
                config_path,
                config,
                config_ref,
                state,
                docker,
                lock,
                runner=runner,
                source_runner=source_runner,
                deadline=limit,
            )
            yield participant
    finally:
        state.close()
