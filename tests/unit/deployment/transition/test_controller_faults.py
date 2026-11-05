"""Keep exact transaction ownership across lost ACK and partial cleanup."""

from contextlib import contextmanager
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from search.operations.deployment import private_api_tls
from search.operations.deployment.candidate import controller as candidate
from search.operations.deployment.frontend import delivery
from search.operations.deployment.images import build
from search.operations.deployment.provider import authority
from search.operations.deployment.runtime_docker import InferenceError
from search.operations.deployment.transition import backend, contract
from search.operations.deployment.transition import controller, enrollment
from search.operations.deployment.transition import journal
from .harness import ControllerHarness, PeerChannel


class OnePacket:
    """Only the retained peer's frames are replaced, not its handler."""

    def __init__(self, packet):
        self.incoming, self.outgoing = [packet], []

    def read(self):
        return self.incoming.pop() if self.incoming else None

    def write(self, value):
        self.outgoing.append(value)


def serve_exchange(channel, packet, *, timeout):
    assert timeout > 0
    channel.harness.sync_vm()
    channel.calls.append(copy.deepcopy(packet))
    frames = OnePacket(packet)
    channel.participant.serve(frames)
    assert len(frames.outgoing) == 1
    return frames.outgoing[0]


class ControllerFaultTests(unittest.TestCase):
    def prepare(self, harness, kind="backend-code-retire"):
        options = {}
        if kind == "backend-code-update":
            options = {
                "target_tls": harness.plans[1]["plan"],
                "candidate_profile": {
                    "path": str(harness.candidate_profile),
                    "sha256": hashlib.sha256(
                        harness.candidate_profile.read_bytes()
                    ).hexdigest(),
                },
            }
        return controller.prepare(
            harness.settings,
            kind=kind,
            expected_epoch=harness.roles["epoch"],
            expected_roles_sha256=harness.roles_sha,
            session_factory=harness.session,
            **options,
        )

    def operate(self, harness, record, *, action="apply", side="target"):
        return controller.operate(
            harness.settings,
            action=action,
            side=side,
            intent_sha256=record["intent_sha256"],
            expected_epoch=record["epoch"],
            expected_roles_sha256=record["roles_sha256"],
            session_factory=harness.session,
        )

    def orphan(self, harness):
        original = authority.Control._write

        def missing_hold(control, name, value):
            if name == journal.HOLD:
                raise OSError("owned HOLD publication interrupted")
            return original(control, name, value)

        with patch.object(authority.Control, "_write", missing_hold):
            with self.assertRaisesRegex(OSError, "HOLD publication"):
                self.prepare(harness)
        intents = list((harness.root / "control").glob("intent-*.json"))
        self.assertEqual(len(intents), 1)
        value = json.loads(intents[0].read_bytes())
        self.assertFalse((harness.root / "control" / journal.HOLD).exists())
        return {
            "intent_sha256": contract.digest(value),
            "epoch": harness.roles["epoch"],
            "roles_sha256": harness.roles_sha,
        }, value

    def test_unclaimed_intent_refuses_before_either_participant_reserves(self):
        harness = ControllerHarness(self)
        record, _value = self.orphan(harness)
        before = (harness.root / "control/roles.json").read_bytes()
        calls = []
        original_prepare = backend.TLSOwner.prepare
        original_exchange = PeerChannel.exchange

        def tls_prepare(owner, operation):
            calls.append("TLS.prepare")
            return original_prepare(owner, operation)

        def undelivered(channel, packet, *, timeout):
            if packet["verb"] == "prepare":
                raise OSError("owned VM prepare was not delivered")
            return original_exchange(channel, packet, timeout=timeout)

        with (
            patch.object(backend.TLSOwner, "prepare", tls_prepare),
            patch.object(PeerChannel, "exchange", undelivered),
        ):
            with self.assertRaises((contract.TransitionError, OSError)):
                self.operate(harness, record)
        self.assertEqual(calls, [])
        self.assertFalse((harness.endpoint / enrollment.RESERVATION).exists())
        self.assertFalse((harness.vm_state / enrollment.RESERVATION).exists())
        self.assertEqual(
            (harness.root / "control/roles.json").read_bytes(), before
        )
        prepared = self.prepare(harness)
        self.assertNotEqual(prepared["intent_sha256"], record["intent_sha256"])
        result = self.operate(
            harness, prepared, action="cancel", side="predecessor"
        )
        self.assertTrue(result["released"])
        self.assertEqual(result["epoch"], record["epoch"])

    def test_old_tls_residue_refuses_before_creating_a_new_canonical_hold(
        self,
    ):
        harness = ControllerHarness(self)
        _record, value = self.orphan(harness)
        with enrollment.locked(harness.endpoint) as fd:
            reservation = enrollment.BindingReservation(
                harness.endpoint, fd, enrollment.owner(harness.endpoint)
            )
            reservation.prepare(value)
        before = (harness.endpoint / enrollment.RESERVATION).read_bytes()
        roles = (harness.root / "control/roles.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "binding_reserved"):
            self.prepare(harness)
        self.assertFalse((harness.root / "control" / journal.HOLD).exists())
        self.assertEqual(
            (harness.endpoint / enrollment.RESERVATION).read_bytes(), before
        )
        self.assertFalse((harness.vm_state / enrollment.RESERVATION).exists())
        self.assertEqual(
            (harness.root / "control/roles.json").read_bytes(), roles
        )

    def pending(self, harness, prepared):
        roles = json.loads((harness.root / "control/roles.json").read_bytes())
        self.assertEqual(roles["epoch"], harness.roles["epoch"] + 1)
        self.assertEqual(
            roles["pending_operation_sha256"], prepared["intent_sha256"]
        )
        return {
            **prepared,
            "epoch": roles["epoch"],
            "roles_sha256": contract.digest(roles),
        }

    def before_dispatch(self, harness):
        prepared = self.prepare(harness, "backend-code-update")
        with patch.object(
            controller,
            "_complete_pending",
            side_effect=OSError("owned crash before terminal dispatch"),
        ):
            with self.assertRaisesRegex(OSError, "before terminal dispatch"):
                self.operate(harness, prepared)
        return self.pending(harness, prepared)

    def retained(self, harness, original, groups):
        current = harness.root / "candidate/current.json"
        self.assertEqual(current.read_bytes(), original)
        record = json.loads(current.read_bytes())
        self.assertEqual(record["phase"], "running")
        self.assertEqual(record["roles_epoch"], harness.roles["epoch"])
        self.assertEqual(harness.data.groups, groups)
        with authority.open_control(harness.control_file) as control:
            with self.assertRaisesRegex(ValueError, "live_binding_changed"):
                candidate.require_binding(control, record)
        # Retained original-E work cannot start/query at E+2. Protected stop
        # deliberately has no freshness exemption: its real Control and
        # disposable admission must still reach the existing Docker boundary.
        with patch.object(
            build, "_run", side_effect=OSError("owned stop boundary")
        ) as run:
            with self.assertRaisesRegex(InferenceError, "command_failed"):
                candidate.operate(harness.candidate_profile, "stop")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][5], "ps")
        self.assertEqual(current.read_bytes(), original)
        self.assertEqual(harness.data.groups, groups)

    def test_update_no_effect_retains_running_candidate_for_protected_stop(
        self,
    ):
        harness = ControllerHarness(self, registered_rollback=False)
        original = (harness.root / "candidate/current.json").read_bytes()
        groups = copy.deepcopy(harness.data.groups)
        pending = self.before_dispatch(harness)
        with self.writes() as terminal:
            result = self.operate(
                harness, pending, action="no-effect", side="predecessor"
            )
        self.assertEqual(len(terminal), 1)
        self.assertTrue(result["released"])
        decision = json.loads(
            (
                harness.root
                / "control"
                / ("resolution-" + pending["intent_sha256"] + ".json")
            ).read_bytes()
        )
        self.assertEqual(decision["resolution"], "no_effect")
        self.retained(harness, original, groups)

    def test_lost_no_effect_resolution_recovers_same_classification_and_e2(
        self,
    ):
        harness = ControllerHarness(self, registered_rollback=False)
        original = (harness.root / "candidate/current.json").read_bytes()
        groups = copy.deepcopy(harness.data.groups)
        pending = self.before_dispatch(harness)
        with self.writes() as terminal:
            with patch.object(
                journal.Operation,
                "_decision",
                side_effect=OSError("owned resolution publication lost"),
            ):
                with self.assertRaisesRegex(OSError, "resolution publication"):
                    self.operate(
                        harness,
                        pending,
                        action="no-effect",
                        side="predecessor",
                    )
        self.assertEqual(len(terminal), 1)
        roles = json.loads((harness.root / "control/roles.json").read_bytes())
        self.assertEqual(roles["epoch"], harness.roles["epoch"] + 2)
        record = {
            **pending,
            "epoch": roles["epoch"],
            "roles_sha256": contract.digest(roles),
        }
        with self.writes() as repeated:
            result = self.operate(
                harness, record, action="reconcile", side="predecessor"
            )
        self.assertEqual(repeated, [])
        self.assertTrue(result["released"])
        decision = json.loads(
            (
                harness.root
                / "control"
                / ("resolution-" + pending["intent_sha256"] + ".json")
            ).read_bytes()
        )
        self.assertEqual(decision["resolution"], "no_effect")
        self.retained(harness, original, groups)

    def frozen(self, harness):
        paths = [
            harness.root / "control/roles.json",
            harness.root / "control/operation-hold.json",
            harness.endpoint / "binding-reservation.json",
            harness.vm_state / "binding-reservation.json",
            harness.vm_state / "state.json",
            harness.canonical,
        ]
        return {
            "files": {str(path): path.read_bytes() for path in paths},
            "containers": copy.deepcopy(harness.data.groups),
            "networks": copy.deepcopy(harness.data.networks),
        }

    @contextmanager
    def writes(self):
        values = []
        original = authority.Control._compare_and_swap_roles

        def record(control, value, **kwargs):
            values.append(copy.deepcopy(value))
            return original(control, value, **kwargs)

        with patch.object(
            authority.Control, "_compare_and_swap_roles", record
        ):
            yield values

    def test_prepare_ack_loss_cancels_same_e_with_both_reservations_and_no_cas(
        self,
    ):
        harness = ControllerHarness(self)
        roles_file = harness.root / "control/roles.json"
        original = roles_file.read_bytes()
        harness.lose_ack = "prepare"
        with self.writes() as writes:
            with self.assertRaisesRegex(OSError, "lost acknowledgement"):
                self.prepare(harness)
            hold = json.loads(
                (harness.root / "control/operation-hold.json").read_bytes()
            )
            record = controller.status(
                harness.settings,
                intent_sha256=hold["intent_sha256"],
                expected_epoch=harness.roles["epoch"],
                expected_roles_sha256=harness.roles_sha,
            )
            self.assertEqual(record["status"], "backend_transition_prepared")
            self.assertEqual(roles_file.read_bytes(), original)
            self.assertTrue(
                (harness.endpoint / "binding-reservation.json").exists()
            )
            self.assertTrue(
                (harness.vm_state / "binding-reservation.json").exists()
            )
            result = self.operate(
                harness, record, action="cancel", side="predecessor"
            )
        self.assertEqual(writes, [])
        self.assertTrue(result["released"])
        self.assertEqual(result["epoch"], harness.roles["epoch"])
        self.assertEqual(roles_file.read_bytes(), original)
        self.assertFalse(
            (harness.root / "control/operation-hold.json").exists()
        )
        self.assertFalse(
            (harness.vm_state / "binding-reservation.json").exists()
        )
        self.assertFalse(
            (harness.endpoint / "binding-reservation.json").exists()
        )
        self.assertEqual(harness.vm_runner.mutations(), [])

    def test_unknown_vm_metadata_returns_unknown_and_keeps_pending_hold(self):
        harness = ControllerHarness(self, registered_rollback=False)
        prepared = self.prepare(harness, "backend-code-update")
        harness.lose_ack = "reconcile"
        with self.writes() as first:
            with self.assertRaisesRegex(OSError, "lost acknowledgement"):
                self.operate(harness, prepared)
        self.assertEqual(len(first), 1)
        pending = self.pending(harness, prepared)
        foreign = json.loads(harness.canonical.read_bytes())
        foreign["api_revision"] = "f" * 40
        harness.write(harness.canonical, foreign)
        before = self.frozen(harness)
        with (
            self.writes() as rejected,
            patch.object(PeerChannel, "exchange", serve_exchange),
        ):
            with self.assertRaisesRegex(
                contract.TransitionError, "participant_unknown"
            ):
                self.operate(
                    harness, pending, action="reconcile", side="predecessor"
                )
        self.assertEqual(rejected, [])
        self.assertEqual(self.frozen(harness), before)
        self.assertEqual(harness.cleanup_docker.mutations(), [])
        self.assertEqual(harness.vm_runner.mutations(), [])

    def test_normal_writers_are_fenced_before_any_effect_during_prepare(self):
        harness = ControllerHarness(self, registered_rollback=False)
        self.prepare(harness, "backend-code-update")
        before = self.frozen(harness)
        commands = copy.deepcopy(harness.process.ledger)
        with self.assertRaisesRegex(ValueError, "recovery_required"):
            with authority.open_control(harness.control_file):
                self.fail("normal provider entry cannot bypass hold")
        with self.assertRaisesRegex(ValueError, "recovery_required"):
            candidate.operate(harness.candidate_profile, "stop")
        with self.assertRaisesRegex(ValueError, "binding_reserved"):
            private_api_tls.operate(
                "stop",
                harness.plans[0]["plan"]["path"],
                runner=harness.process,
            )
        with self.assertRaisesRegex(ValueError, "backend_binding_reserved"):
            delivery.operate(
                harness.vm_delivery, "rollback", runner=harness.vm_runner
            )
        self.assertEqual(self.frozen(harness), before)
        self.assertEqual(harness.process.ledger, commands)
        self.assertEqual(harness.vm_runner.mutations(), [])
        self.assertEqual(harness.cleanup_docker.mutations(), [])

    def test_retire_dispatch_crash_fixes_target_and_resumes_partial_absence(
        self,
    ):
        harness = ControllerHarness(self)
        protected = copy.deepcopy(harness.data.groups)
        prepared = self.prepare(harness)
        harness.cleanup_docker.crash_after_remove = True
        with self.writes() as first:
            with self.assertRaises((OSError, InferenceError)):
                self.operate(harness, prepared)
        self.assertEqual(len(first), 1)
        pending = self.pending(harness, prepared)
        group = harness.data.hosts[1].compose["name"]
        self.assertEqual(len(harness.data.groups[group]), 2)
        dispatch = harness.root / (
            "control/retire-dispatch-" + prepared["intent_sha256"] + ".json"
        )
        original_dispatch = dispatch.read_bytes()
        before = self.frozen(harness)
        with self.writes() as rejected:
            with self.assertRaisesRegex(
                contract.TransitionError, "outcome_fixed"
            ):
                self.operate(
                    harness, pending, action="reconcile", side="predecessor"
                )
        self.assertEqual(rejected, [])
        self.assertEqual(self.frozen(harness), before)
        with self.writes() as completed:
            result = self.operate(harness, pending, action="reconcile")
        self.assertEqual(len(completed), 1)
        self.assertTrue(result["released"])
        self.assertEqual(dispatch.read_bytes(), original_dispatch)
        self.assertEqual(harness.data.groups[group], [])
        for project, rows in protected.items():
            if project != group:
                self.assertEqual(harness.data.groups[project], rows)
        self.assertTrue(
            all(
                args[0] == "rm" or args[:2] == ["network", "rm"]
                for args in harness.cleanup_docker.mutations()
            )
        )
        self.assertEqual(harness.vm_runner.mutations(), [])
