"""Exercise the real VM owner/checker with fake Docker and HTTP boundaries."""

import copy
import hashlib
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from search.operations.deployment.frontend import binding, check, delivery
from search.operations.deployment.provider import authority
from search.operations.deployment.runtime_docker import Docker
from search.operations.deployment.transition import contract, enrollment
from search.operations.shared import private_io
from tests.unit.deployment.frontend import test_delivery as delivery_fixture
from tests.unit.deployment.frontend.test_check import API, PROOF
from tests.unit.deployment.transition.fixtures import operation_record


REAL_HOMEPAGE = check._homepage


class LocalHTTP:
    """Only HTTP reads are faked; the real homepage/assets parser runs."""

    def __init__(self, host, port):
        self.host, self.port, self.path = host, port, None

    def request(self, method, path, headers):
        assert method == "GET" and headers["Connection"] == "close"
        self.path = path

    def getresponse(self):
        data = (
            b'<script type="module" src="/assets/main.js"></script>'
            if self.path == "/"
            else b"fixture javascript"
        )
        response = io.BytesIO(data)
        response.status = 200
        return response

    def abort(self):
        pass

    def finish(self):
        pass


class VersionRunner(delivery_fixture.HostRunner):
    def __init__(self, root):
        super().__init__(root)
        self.backend_revision = API

    def __call__(self, argv, *, environment, timeout):
        args = argv[5:]
        if args[:1] == ["exec"] and len(args) > 3 and args[2] == "python":
            self.assert_local(argv, timeout)
            self.calls.append(list(argv))
            value = dict(PROOF)
            value["backend_identity_verified"] = (
                args[-2] == self.backend_revision
            )
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(value).encode(), stderr=b""
            )
        return super().__call__(argv, environment=environment, timeout=timeout)


class LostAcknowledgement:
    """The peer disconnects only after the real owner completed its action."""

    def __init__(self, packet):
        self.packet = packet

    def read(self):
        packet, self.packet = self.packet, None
        return packet

    def write(self, response):
        raise OSError("owned fake acknowledgement loss")


class TypedFrames:
    """An ordered stream with visible reads and actual owner responses."""

    def __init__(self, packets):
        self.packets, self.responses, self.reads = list(packets), [], 0

    def read(self):
        self.reads += 1
        return self.packets.pop(0) if self.packets else None

    def write(self, response):
        self.responses.append(response)


class BindingOwnerTests(unittest.TestCase):
    fetch_release = delivery_fixture.FrontendDeliveryTests.fetch_release
    validate_archive = delivery_fixture.FrontendDeliveryTests.validate_archive

    def setUp(self):
        delivery_fixture.FrontendDeliveryTests.setUp(self)
        check._homepage = REAL_HOMEPAGE
        patch.object(check, "_Endpoint", LocalHTTP).start()
        self.runner = VersionRunner(self.frontend)
        key = self.frontend / "api_key"
        key.write_text("a" * 64)
        key.chmod(0o600)
        settings = delivery.load_config(self.config)
        state = delivery.State(settings["state_root"])
        try:
            with state.locked():
                docker = Docker(self.root / "client", self.runner)
                owner = delivery.Delivery(settings, state, docker, self.runner)
                state.write("state.json", owner.baseline())
        finally:
            state.close()
        self.program = self.root / "operator"
        (self.program / "scripts").mkdir(parents=True, mode=0o700)
        self.entry = self.program / "scripts/participate_backend_binding.py"
        self.entry.write_text("# owned source-identity fixture\n")
        self.source = {"root": str(self.program), "revision": "a" * 40}
        self.participant_config = self.root / "participant.json"
        self.participant_config.write_bytes(
            private_io.canonical(
                {
                    "schema_version": 1,
                    "kind": "frontend-binding-participant",
                    "source": self.source,
                    "delivery_configuration": binding.input_reference(
                        self.config
                    ),
                    "coordinator_peer_sha256": "7" * 64,
                }
            )
        )
        self.participant_config.chmod(0o600)
        self.config_ref = binding.input_reference(self.participant_config)
        enrollment.enroll(
            self.state,
            participant="frontend",
            source=self.source,
            configuration=self.config_ref,
        )
        self.control_file = self.root / "control.json"
        self.control_file.write_bytes(
            private_io.canonical(
                {"schema_version": 1, "state_root": str(self.root / "control")}
            )
        )
        self.control_file.chmod(0o600)
        self.sequence = 0

    def source_runner(self, argv, *, timeout):
        self.assertGreater(timeout, 0)
        value = self.source["revision"] if "rev-parse" in argv else ""
        return SimpleNamespace(returncode=0, stdout=value + "\n", stderr="")

    def participant(self):
        return binding.open_participant(
            self.participant_config,
            expected_source=self.source["revision"],
            expected_entry=hashlib.sha256(self.entry.read_bytes()).hexdigest(),
            runner=self.runner,
            source_runner=self.source_runner,
            source_root=self.program,
        )

    def test_default_participant_cannot_claim_a_different_loaded_checkout(
        self,
    ):
        with self.assertRaisesRegex(
            contract.TransitionError, "source_changed"
        ):
            with binding.open_participant(
                self.participant_config,
                expected_source=self.source["revision"],
                expected_entry=hashlib.sha256(
                    self.entry.read_bytes()
                ).hexdigest(),
                runner=self.runner,
                source_runner=self.source_runner,
            ):
                self.fail(
                    "only the actually loaded participant may claim its source"
                )

    def request(self, participant, operation, verb, body, *, preflight=False):
        self.sequence += 1
        return participant.request(
            {
                "v": 1,
                "op_id": "a" * 32,
                "intent_sha256": None
                if preflight
                else contract.digest(operation),
                "seq": self.sequence,
                "nonce": format(self.sequence, "064x"),
                "verb": verb,
                "body": body,
            }
        )

    def operation(self, before):
        with authority.open_control(self.control_file, create=True) as control:
            value = operation_record(control)
        value["frontend_owner"]["source"] = self.source
        value["frontend_owner"]["participant_config"] = self.config_ref
        value["sides"]["predecessor"]["frontend"] = copy.deepcopy(before)
        target = copy.deepcopy(before)
        target["profile"]["api_revision"] = "b" * 40
        target["current"]["profile"] = copy.deepcopy(target["profile"])
        target["canonical_profile"]["sha256"] = contract.digest(
            target["profile"]
        )
        target["state"]["sha256"] = contract.digest(
            {
                "schema_version": 1,
                "current": target["current"],
                "previous": target["previous"],
            }
        )
        value["sides"]["target"]["frontend"] = target
        contract.intent(value)
        return value

    def prepared(self, participant):
        packet = self.request(
            participant,
            None,
            "observe",
            {"stage": "preflight", "side": "auto"},
            preflight=True,
        )
        before = packet["observation"]["actual"]["projection"]
        operation = self.operation(before)
        response = self.request(
            participant, operation, "prepare", {"intent": operation}
        )
        self.assertEqual(response["phase"], "prepared")
        return operation

    def test_binding_changes_current_pin_preserving_runtime_and_previous(
        self,
    ):
        with self.participant() as participant:
            operation = self.prepared(participant)
            before = copy.deepcopy(self.runner.rows)
            previous = participant.state.read("state.json")["previous"]
            self.runner.backend_revision = "b" * 40
            response = self.request(
                participant, operation, "publish", {"side": "target"}
            )
            self.assertEqual(response["phase"], "published")
            self.assertEqual(
                participant.delivery.canonical()["api_revision"], "b" * 40
            )
            self.assertEqual(
                participant.state.read("state.json")["previous"], previous
            )
            self.assertEqual(self.runner.rows, before)
            self.assertEqual(self.runner.mutations(), [])

    def test_wrong_actual_api_cannot_publish_a_new_expected_revision(self):
        with self.participant() as participant:
            operation = self.prepared(participant)
            original = self.canonical.read_bytes()
            with self.assertRaisesRegex(
                check.FrontendCheckError, "upstream_failed"
            ):
                self.request(
                    participant, operation, "publish", {"side": "target"}
                )
            self.assertEqual(self.canonical.read_bytes(), original)
            self.assertIsNotNone(participant.reservation.current(operation))
            self.assertEqual(self.runner.mutations(), [])

    def test_full_normal_binding_session_stays_inside_shared_owner_budget(
        self,
    ):
        with self.participant() as participant:
            operation = self.prepared(participant)
            self.runner.backend_revision = "b" * 40
            self.request(participant, operation, "publish", {"side": "target"})
            self.request(
                participant,
                operation,
                "observe",
                {"stage": "terminal", "side": "target"},
            )
            self.request(
                participant,
                operation,
                "observe",
                {"stage": "terminal", "side": "target"},
            )
            roles = contract.terminal_roles(operation, "target")
            decision = {
                "schema_version": 1,
                "mode": "terminal",
                "operation_id": operation["operation_id"],
                "intent_sha256": contract.digest(operation),
                "roles": roles,
                "roles_sha256": contract.digest(roles),
                "side": "target",
                "resolution": "target_verified",
                "observations": {"fixture": "owner unit, not a live CAS"},
            }
            response = self.request(
                participant,
                operation,
                "release",
                {
                    "decision": decision,
                    "canonical_decision_sha256": contract.digest(decision),
                },
            )
            self.assertEqual(response["phase"], "released")
            self.assertLessEqual(
                participant.calls, contract.PARTICIPANT_COMMANDS
            )
            self.assertFalse((self.state / enrollment.RESERVATION).exists())
            self.assertEqual(self.runner.mutations(), [])

    def test_old_previous_api_pin_refuses_image_rollback_before_mutation(
        self,
    ):
        current = dict(self.initial)
        previous = {**current, "api_revision": "f" * 40}
        with self.assertRaisesRegex(
            delivery.FrontendDeliveryError, "backend_binding_incompatible"
        ):
            delivery._compatible_backend(current, previous)
        self.assertEqual(self.runner.mutations(), [])

    def attach(self, participant, operation):
        self.sequence = 0
        return self.request(
            participant, operation, "prepare", {"intent": operation}
        )

    def snapshot(self):
        reservation = self.state / enrollment.RESERVATION
        return {
            "canonical": self.canonical.read_bytes(),
            "state": (self.state / "state.json").read_bytes(),
            "reservation": reservation.read_bytes()
            if reservation.exists()
            else None,
            "runtime": copy.deepcopy(self.runner.rows),
        }

    def terminal_decision(self, operation, *, side="target", cancelled=False):
        roles = (
            copy.deepcopy(operation["predecessor_roles"])
            if cancelled
            else contract.terminal_roles(operation, side)
        )
        return {
            "schema_version": 1,
            "mode": "cancelled_before_begin" if cancelled else "terminal",
            "operation_id": operation["operation_id"],
            "intent_sha256": contract.digest(operation),
            "roles": roles,
            "roles_sha256": contract.digest(roles),
            "side": side,
            "resolution": "no_dispatch"
            if cancelled
            else (
                "target_verified" if side == "target" else "verified_return"
            ),
            "observations": {"fixture": "real VM owner, no live Roles CAS"},
        }

    def packet(self, operation, verb, body):
        self.sequence += 1
        return {
            "v": 1,
            "op_id": operation["operation_id"],
            "intent_sha256": contract.digest(operation),
            "seq": self.sequence,
            "nonce": format(self.sequence, "064x"),
            "verb": verb,
            "body": body,
        }

    def release_body(self, decision):
        return {
            "decision": decision,
            "canonical_decision_sha256": contract.digest(decision),
        }

    def test_prepare_ack_loss_reconnects_same_reservation_without_side_effects(
        self,
    ):
        with self.participant() as participant:
            packet = self.request(
                participant,
                None,
                "observe",
                {"stage": "preflight", "side": "auto"},
                preflight=True,
            )
            operation = self.operation(
                packet["observation"]["actual"]["projection"]
            )
            frames = LostAcknowledgement(
                self.packet(operation, "prepare", {"intent": operation})
            )
            with self.assertRaisesRegex(OSError, "acknowledgement loss"):
                participant.serve(frames)
            prepared = self.snapshot()
        with self.participant() as participant:
            response = self.attach(participant, operation)
            self.assertEqual(response["phase"], "prepared")
            self.assertEqual(self.snapshot(), prepared)
            reservation = response["observation"]["actual"]["reservation"]
            self.assertEqual(
                reservation["intent_sha256"], contract.digest(operation)
            )
        self.assertEqual(self.runner.mutations(), [])

    def test_released_ack_loss_reconnects_tombstone_without_reservation_or_cas(
        self,
    ):
        with self.participant() as participant:
            operation = self.prepared(participant)
            self.runner.backend_revision = "b" * 40
            self.request(participant, operation, "publish", {"side": "target"})
            self.request(
                participant,
                operation,
                "observe",
                {"stage": "terminal", "side": "target"},
            )
            decision = self.terminal_decision(operation)
            frames = LostAcknowledgement(
                self.packet(operation, "release", self.release_body(decision))
            )
            with self.assertRaisesRegex(OSError, "acknowledgement loss"):
                participant.serve(frames)
            released = self.snapshot()
            self.assertIsNone(released["reservation"])
        with self.participant() as participant:
            response = self.attach(participant, operation)
            self.assertEqual(response["phase"], "released")
            self.assertEqual(self.snapshot(), released)
            repeated = self.request(
                participant, operation, "release", self.release_body(decision)
            )
            self.assertEqual(repeated["observation"], response["observation"])
            self.assertEqual(self.snapshot(), released)
        self.assertEqual(self.runner.mutations(), [])

    def test_partial_canonical_publish_reattaches_then_repairs_owned_state(
        self,
    ):
        with self.participant() as participant:
            operation = self.prepared(participant)
            before = self.snapshot()
            self.runner.backend_revision = "b" * 40
            write = participant.state.write

            def interrupted_write(name, value):
                if name == "state.json":
                    raise OSError("owned crash between metadata writes")
                return write(name, value)

            with patch.object(participant.state, "write", interrupted_write):
                with self.assertRaisesRegex(
                    OSError, "between metadata writes"
                ):
                    self.request(
                        participant, operation, "publish", {"side": "target"}
                    )
            mixed = self.snapshot()
            self.assertNotEqual(mixed["canonical"], before["canonical"])
            self.assertEqual(mixed["state"], before["state"])
        with self.participant() as participant:
            response = self.attach(participant, operation)
            self.assertEqual(response["phase"], "prepared")
            self.request(
                participant,
                operation,
                "reconcile",
                {"requested_side": "target"},
            )
            target = operation["sides"]["target"]["frontend"]
            self.assertEqual(participant.projection(), target)
            self.assertEqual(self.runner.rows, before["runtime"])
            self.assertEqual(
                participant.state.read("state.json")["previous"],
                target["previous"],
            )
        self.assertEqual(self.runner.mutations(), [])

    def discard_decision(self, operation):
        pending = contract.pending_roles(operation)
        return {
            "schema_version": 1,
            "kind": "update-return-discard",
            "operation_id": operation["operation_id"],
            "intent_sha256": contract.digest(operation),
            "expected_pending_epoch": pending["epoch"],
            "expected_pending_roles_sha256": contract.digest(pending),
            "candidate_snapshot_sha256": operation["candidate"]["record"][
                "sha256"
            ],
            "candidate_backend_sha256": operation["candidate"]["backend"][
                "sha256"
            ],
            "outcome": "predecessor",
            "resources": {
                "cpu_container_ids": {
                    "conversation-api": "1" * 64,
                    "web": "2" * 64,
                    "filtered-embedding": "3" * 64,
                },
                "exclusive_network_ids": [],
                "lease_sha256": "4" * 64,
                "retain_paths": [],
            },
            "owner_source": operation["operator"],
        }

    def test_discard_ack_pins_predecessor_across_reconnection(self):
        with self.participant() as participant:
            operation = self.prepared(participant)
            decision = self.discard_decision(operation)
            response = self.request(
                participant,
                operation,
                "reconcile",
                {
                    "requested_side": "predecessor",
                    "cleanup_decision": decision,
                },
            )
            self.assertEqual(
                response["observation"]["actual"]["reservation"][
                    "cleanup_decision_sha256"
                ],
                contract.digest(decision),
            )
            fixed = self.snapshot()
        with self.participant() as participant:
            self.attach(participant, operation)
            with self.assertRaisesRegex(
                contract.TransitionError, "outcome_fixed"
            ):
                self.request(
                    participant, operation, "publish", {"side": "target"}
                )
            self.assertEqual(self.snapshot(), fixed)
        self.assertEqual(self.runner.mutations(), [])

    def test_prepare_refuses_wrong_namespace_before_reservation(self):
        with self.participant() as participant:
            preflight = self.request(
                participant,
                None,
                "observe",
                {"stage": "preflight", "side": "auto"},
                preflight=True,
            )
            operation = self.operation(
                preflight["observation"]["actual"]["projection"]
            )
            for side in contract.SIDES:
                operation["sides"][side]["frontend"]["namespace_sha256"] = (
                    "0" * 64
                )
            before = self.snapshot()
            with self.assertRaisesRegex(
                contract.TransitionError, "namespace_changed"
            ):
                self.request(
                    participant, operation, "prepare", {"intent": operation}
                )
            self.assertEqual(self.snapshot(), before)
            self.assertIsNone(before["reservation"])
        self.assertEqual(self.runner.mutations(), [])

    def test_prepare_refuses_wrong_source_before_reservation(self):
        with self.participant() as participant:
            preflight = self.request(
                participant,
                None,
                "observe",
                {"stage": "preflight", "side": "auto"},
                preflight=True,
            )
            operation = self.operation(
                preflight["observation"]["actual"]["projection"]
            )
            operation["frontend_owner"]["source"] = {
                **operation["frontend_owner"]["source"],
                "revision": "0" * 40,
            }
            before = self.snapshot()
            with self.assertRaisesRegex(
                contract.TransitionError, "owner_changed"
            ):
                self.request(
                    participant, operation, "prepare", {"intent": operation}
                )
            self.assertEqual(self.snapshot(), before)
            self.assertIsNone(before["reservation"])
        self.assertEqual(self.runner.mutations(), [])

    def test_attached_wrong_operation_preserves_durable_reservation(self):
        with self.participant() as participant:
            operation = self.prepared(participant)
            before = self.snapshot()
            packet = self.packet(operation, "publish", {"side": "target"})
            packet["op_id"] = "0" * 32
            with self.assertRaisesRegex(
                contract.TransitionError, "operation_changed"
            ):
                participant.request(packet)
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.runner.mutations(), [])

    def test_config_change_after_prepare_preserves_metadata_runtime_and_fence(
        self,
    ):
        with self.participant() as participant:
            operation = self.prepared(participant)
            before = self.snapshot()
            self.participant_config.write_bytes(
                self.participant_config.read_bytes() + b"\n"
            )
            with self.assertRaisesRegex(
                contract.TransitionError, "configuration_changed"
            ):
                self.request(
                    participant, operation, "publish", {"side": "target"}
                )
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.runner.mutations(), [])

    def test_http_loss_before_publish_leaves_previous_runtime_and_fence_intact(
        self,
    ):
        with self.participant() as participant:
            operation = self.prepared(participant)
            before = self.snapshot()
            self.runner.backend_revision = "b" * 40
            packet = self.packet(operation, "publish", {"side": "target"})
            frames = SimpleNamespace(
                read=Mock(side_effect=[packet, None]), write=Mock()
            )
            with patch.object(
                LocalHTTP,
                "getresponse",
                side_effect=OSError("owned fake HTTP disconnect"),
            ):
                participant.serve(frames)
            response = frames.write.call_args.args[0]
            self.assertEqual(response["phase"], "unknown")
            self.assertEqual(
                response["observation"]["actual"],
                {"error": "backend_transition_recovery_required"},
            )
            self.assertTrue(participant.uncertain)
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.runner.mutations(), [])

    def test_unknown_mixed_metadata_is_not_repaired_or_unreserved(self):
        with self.participant() as participant:
            operation = self.prepared(participant)
            foreign = {**self.initial, "api_revision": "f" * 40}
            self.canonical.write_bytes(private_io.canonical(foreign))
            unknown = self.snapshot()
        with self.participant() as participant:
            with self.assertRaisesRegex(
                contract.TransitionError, "frontend_changed"
            ):
                self.attach(participant, operation)
            self.assertEqual(self.snapshot(), unknown)
            self.assertIsNotNone(participant.reservation.current(operation))
        self.assertEqual(self.runner.mutations(), [])

    def test_release_unlink_failure_recovers_confirmation_on_reconnect(
        self,
    ):
        with self.participant() as participant:
            operation = self.prepared(participant)
            decision = self.terminal_decision(
                operation, side="predecessor", cancelled=True
            )
            unlink = enrollment.os.unlink

            def interrupted_unlink(path, *args, **kwargs):
                if path == enrollment.RESERVATION:
                    raise OSError("owned failure after durable Released")
                return unlink(path, *args, **kwargs)

            with patch.object(enrollment.os, "unlink", interrupted_unlink):
                with self.assertRaisesRegex(OSError, "durable Released"):
                    self.request(
                        participant,
                        operation,
                        "release",
                        self.release_body(decision),
                    )
            reserved = self.snapshot()
            self.assertIsNotNone(reserved["reservation"])
        with self.participant() as participant:
            response = self.attach(participant, operation)
            self.assertEqual(response["phase"], "released")
            self.request(
                participant, operation, "release", self.release_body(decision)
            )
            released = self.snapshot()
            self.assertIsNone(released["reservation"])
            self.assertEqual(released["state"], reserved["state"])
            self.assertEqual(released["canonical"], reserved["canonical"])
            self.assertEqual(released["runtime"], reserved["runtime"])
        self.assertEqual(self.runner.mutations(), [])

    def test_prebegin_cancel_keeps_exact_metadata_and_runtime_then_releases(
        self,
    ):
        with self.participant() as participant:
            operation = self.prepared(participant)
            before = self.snapshot()
            decision = self.terminal_decision(
                operation, side="predecessor", cancelled=True
            )
            response = self.request(
                participant, operation, "release", self.release_body(decision)
            )
            self.assertEqual(response["phase"], "released")
            released = self.snapshot()
            self.assertIsNone(released["reservation"])
            for field in ("state", "canonical", "runtime"):
                self.assertEqual(released[field], before[field])
            self.assertEqual(
                response["observation"]["actual"]["released"][
                    "resolved_epoch"
                ],
                operation["predecessor_roles"]["epoch"],
            )
        self.assertEqual(self.runner.mutations(), [])

    def test_released_prepare_keeps_serve_alive_for_fresh_observe_and_release(
        self,
    ):
        with self.participant() as participant:
            operation = self.prepared(participant)
            self.runner.backend_revision = "b" * 40
            self.request(participant, operation, "publish", {"side": "target"})
            self.request(
                participant,
                operation,
                "observe",
                {"stage": "terminal", "side": "target"},
            )
            decision = self.terminal_decision(operation)
            lost = LostAcknowledgement(
                self.packet(operation, "release", self.release_body(decision))
            )
            with self.assertRaisesRegex(OSError, "acknowledgement loss"):
                participant.serve(lost)
            released = self.snapshot()
            self.assertIsNone(released["reservation"])
        self.sequence = 0
        frames = TypedFrames(
            [
                self.packet(operation, "prepare", {"intent": operation}),
                self.packet(
                    operation,
                    "observe",
                    {"stage": "terminal", "side": "target"},
                ),
                self.packet(operation, "release", self.release_body(decision)),
            ]
        )
        with self.participant() as participant:
            participant.serve(frames)
            self.assertEqual(
                [response["phase"] for response in frames.responses],
                ["released", "frozen", "released"],
            )
            fresh = frames.responses[1]["observation"]
            self.assertEqual(fresh["side"], "target")
            self.assertTrue(
                fresh["actual"]["version"]["backend_identity_verified"]
            )
            self.assertTrue(fresh["actual"]["homepage_verified"])
            self.assertTrue(fresh["actual"]["assets_verified"])
            self.assertEqual(frames.reads, 3)
            self.assertEqual(frames.packets, [])
            self.assertEqual(self.snapshot(), released)
        self.assertEqual(self.runner.mutations(), [])
