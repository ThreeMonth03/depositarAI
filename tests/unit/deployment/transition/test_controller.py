"""Exercise canonical orchestration with real local and remote owners."""

from contextlib import contextmanager
import copy
from dataclasses import replace
import hashlib
import json
import unittest
from unittest.mock import patch

from search.operations.deployment.provider import authority
from search.operations.deployment.runtime_docker import InferenceError
from search.operations.deployment.transition import contract, controller
from .harness import ControllerHarness


class ControllerOwnerTests(unittest.TestCase):
    def prepare(self, harness, kind):
        options = {}
        if kind != "backend-code-retire":
            options["target_tls"] = harness.plans[1]["plan"]
        if kind == "backend-code-update":
            options["candidate_profile"] = {
                "path": str(harness.candidate_profile),
                "sha256": hashlib.sha256(
                    harness.candidate_profile.read_bytes()
                ).hexdigest(),
            }
        return controller.prepare(
            harness.settings,
            kind=kind,
            expected_epoch=harness.roles["epoch"],
            expected_roles_sha256=harness.roles_sha,
            session_factory=harness.session,
            **options,
        )

    def operate(self, harness, prepared, *, action="apply", side="target"):
        return controller.operate(
            harness.settings,
            action=action,
            side=side,
            intent_sha256=prepared["intent_sha256"],
            expected_epoch=prepared["epoch"],
            expected_roles_sha256=prepared["roles_sha256"],
            session_factory=harness.session,
        )

    @contextmanager
    def writes(self):
        rows = []
        original = authority.Control._compare_and_swap_roles

        def record(control, value, **kwargs):
            rows.append(copy.deepcopy(value))
            return original(control, value, **kwargs)

        with patch.object(
            authority.Control, "_compare_and_swap_roles", record
        ):
            yield rows

    def test_retire_prepare_seals_both_owners_without_roles_write(self):
        harness = ControllerHarness(self)
        original = copy.deepcopy(harness.roles)
        with patch.object(
            authority.Control,
            "_compare_and_swap_roles",
            side_effect=AssertionError("prepare cannot write Roles"),
        ):
            result = controller.prepare(
                harness.settings,
                kind="backend-code-retire",
                expected_epoch=original["epoch"],
                expected_roles_sha256=harness.roles_sha,
                session_factory=harness.session,
            )
        self.assertEqual(result["status"], "backend_transition_prepared")
        self.assertEqual(result["epoch"], original["epoch"])
        self.assertFalse(result["released"])
        self.assertTrue(
            (harness.vm_state / "binding-reservation.json").exists()
        )
        self.assertTrue(
            (harness.endpoint / "binding-reservation.json").exists()
        )
        self.assertEqual(harness.vm_runner.mutations(), [])

    def test_retire_target_writes_two_roles_and_preserves_shared_instances(
        self,
    ):
        harness = ControllerHarness(self)
        before = copy.deepcopy(harness.data.groups)
        prepared = self.prepare(harness, "backend-code-retire")
        with self.writes() as writes:
            result = self.operate(harness, prepared)
        self.assertEqual(len(writes), 2)
        self.assertEqual(
            [row["epoch"] for row in writes],
            [harness.roles["epoch"] + 1, harness.roles["epoch"] + 2],
        )
        self.assertIsNone(writes[-1]["rollback_backend_sha256"])
        self.assertTrue(result["released"])
        removed = harness.data.hosts[1].compose["name"]
        self.assertEqual(harness.data.groups[removed], [])
        for project, rows in before.items():
            if project != removed:
                self.assertEqual(harness.data.groups[project], rows)
        with self.writes() as repeated:
            recovered = self.operate(harness, result, action="reconcile")
        self.assertEqual(repeated, [])
        self.assertTrue(recovered["released"])
        self.assertEqual(harness.vm_runner.mutations(), [])

    def test_rollback_target_writes_two_roles_and_keeps_both_backends(self):
        harness = ControllerHarness(self)
        before = copy.deepcopy(harness.data.groups)
        prepared = self.prepare(harness, "backend-code-rollback")
        with self.writes() as writes:
            result = self.operate(harness, prepared)
        self.assertEqual(len(writes), 2)
        self.assertEqual(
            writes[-1]["active_backend_sha256"],
            harness.roles["rollback_backend_sha256"],
        )
        self.assertEqual(
            writes[-1]["rollback_backend_sha256"],
            harness.roles["active_backend_sha256"],
        )
        self.assertEqual(harness.data.groups, before)
        self.assertEqual(
            json.loads(harness.canonical.read_bytes())["api_revision"],
            harness.data.hosts[1].source_revision,
        )
        self.assertTrue(result["released"])
        self.assertEqual(harness.vm_runner.mutations(), [])

    def test_two_catalog_retire_closes_only_original_b_runtime(self):
        harness = ControllerHarness(self, separate_catalogs=True)
        before = copy.deepcopy(harness.data.groups)
        prepared = self.prepare(harness, "backend-code-retire")
        with self.writes() as writes:
            result = self.operate(harness, prepared)
        self.assertEqual(len(writes), 2)
        self.assertTrue(result["released"])
        removed = harness.data.hosts[1].compose["name"]
        self.assertEqual(harness.data.groups[removed], [])
        self.assertEqual(
            {
                name: rows
                for name, rows in harness.data.groups.items()
                if name != removed
            },
            {name: rows for name, rows in before.items() if name != removed},
        )
        self.assertIsNone(writes[-1]["rollback_backend_sha256"])
        self.assertEqual(
            writes[-1]["active_backend_sha256"],
            harness.roles["active_backend_sha256"],
        )

    def test_update_target_two_cas_hands_off_original_e_candidate(self):
        harness = ControllerHarness(self, registered_rollback=False)
        current = harness.root / "candidate/current.json"
        original = current.read_bytes()
        prepared = self.prepare(harness, "backend-code-update")
        self.assertEqual(current.read_bytes(), original)
        with self.writes() as writes:
            result = self.operate(harness, prepared)
        self.assertEqual(len(writes), 2)
        self.assertEqual(
            writes[-1]["rollback_backend_sha256"],
            harness.roles["active_backend_sha256"],
        )
        self.assertFalse(current.exists())
        snapshots = list(
            (harness.root / "candidate").glob("candidate-snapshot-*.json")
        )
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].read_bytes(), original)
        self.assertTrue(
            (
                harness.root
                / "candidate"
                / ("handoff-" + prepared["intent_sha256"] + ".json")
            ).exists()
        )
        self.assertTrue(result["released"])
        self.assertEqual(harness.vm_runner.mutations(), [])

    def test_cancel_lost_release_ack_reconciles_same_e_without_new_roles_write(
        self,
    ):
        harness = ControllerHarness(self)
        prepared = controller.prepare(
            harness.settings,
            kind="backend-code-retire",
            expected_epoch=harness.roles["epoch"],
            expected_roles_sha256=harness.roles_sha,
            session_factory=harness.session,
        )
        roles_file = harness.root / "control/roles.json"
        before = roles_file.read_bytes()
        common = {
            "intent_sha256": prepared["intent_sha256"],
            "expected_epoch": prepared["epoch"],
            "expected_roles_sha256": prepared["roles_sha256"],
            "side": "predecessor",
            "session_factory": harness.session,
        }
        harness.lose_ack = "release"
        with patch.object(
            authority.Control,
            "_compare_and_swap_roles",
            side_effect=AssertionError("cancel must never write Roles"),
        ):
            with self.assertRaisesRegex(OSError, "lost acknowledgement"):
                controller.operate(harness.settings, action="cancel", **common)
            resolution = harness.root / (
                "control/resolution-" + prepared["intent_sha256"] + ".json"
            )
            original_decision = resolution.read_bytes()
            self.assertEqual(roles_file.read_bytes(), before)
            result = controller.operate(
                harness.settings, action="reconcile", **common
            )
        self.assertEqual(result["epoch"], harness.roles["epoch"])
        self.assertTrue(result["released"])
        self.assertEqual(resolution.read_bytes(), original_decision)
        self.assertEqual(roles_file.read_bytes(), before)
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

    def test_partial_b_after_discard_only_resumes_predecessor_with_two_cas(
        self,
    ):
        harness = ControllerHarness(self, registered_rollback=False)
        original = (harness.root / "candidate/current.json").read_bytes()
        protected = copy.deepcopy(harness.data.groups)
        prepared = self.prepare(harness, "backend-code-update")
        harness.cleanup_docker.crash_after_remove = True
        with self.writes() as initial:
            with self.assertRaises((OSError, InferenceError)):
                self.operate(harness, prepared, side="predecessor")
        self.assertEqual(len(initial), 1)
        group = harness.data.hosts[1].compose["name"]
        self.assertEqual(len(harness.data.groups[group]), 2)
        roles = json.loads((harness.root / "control/roles.json").read_bytes())
        pending = {
            **prepared,
            "epoch": roles["epoch"],
            "roles_sha256": contract.digest(roles),
        }
        self.assertEqual(pending["epoch"], harness.roles["epoch"] + 1)
        with self.writes() as rejected:
            with self.assertRaisesRegex(
                contract.TransitionError, "outcome_fixed"
            ):
                self.operate(harness, pending, action="reconcile")
        self.assertEqual(rejected, [])
        self.assertEqual(len(harness.data.groups[group]), 2)
        with self.writes() as completed:
            result = self.operate(
                harness, pending, action="reconcile", side="predecessor"
            )
        self.assertEqual(len(completed), 1)
        self.assertTrue(result["released"])
        self.assertEqual(harness.data.groups[group], [])
        current = json.loads(
            (harness.root / "candidate/current.json").read_bytes()
        )
        self.assertEqual(current, {**json.loads(original), "phase": "stopped"})
        snapshots = list(
            (harness.root / "candidate").glob("candidate-snapshot-*.json")
        )
        self.assertEqual(snapshots[0].read_bytes(), original)
        for project, rows in protected.items():
            if project != group:
                self.assertEqual(harness.data.groups[project], rows)
        self.assertEqual(harness.vm_runner.mutations(), [])

    def test_lost_target_publish_ack_returns_verified_predecessor_once(self):
        harness = ControllerHarness(self, registered_rollback=False)
        protected = copy.deepcopy(harness.data.groups)
        prepared = self.prepare(harness, "backend-code-update")
        harness.lose_ack = "reconcile"
        with self.writes() as initial:
            with self.assertRaisesRegex(OSError, "lost acknowledgement"):
                self.operate(harness, prepared)
        self.assertEqual(len(initial), 1)
        self.assertEqual(
            json.loads(harness.canonical.read_bytes())["api_revision"],
            harness.data.hosts[1].source_revision,
        )
        roles = json.loads((harness.root / "control/roles.json").read_bytes())
        pending = {
            **prepared,
            "epoch": roles["epoch"],
            "roles_sha256": contract.digest(roles),
        }
        with self.writes() as completed:
            result = self.operate(
                harness, pending, action="reconcile", side="predecessor"
            )
        self.assertEqual(len(completed), 1)
        self.assertTrue(result["released"])
        self.assertEqual(
            json.loads(harness.canonical.read_bytes())["api_revision"],
            harness.data.hosts[0].source_revision,
        )
        resolution = json.loads(
            (
                harness.root
                / ("control/resolution-" + prepared["intent_sha256"] + ".json")
            ).read_bytes()
        )
        self.assertEqual(resolution["resolution"], "verified_return")
        removed = harness.data.hosts[1].compose["name"]
        self.assertEqual(harness.data.groups[removed], [])
        for name, rows in protected.items():
            if name != removed:
                self.assertEqual(harness.data.groups[name], rows)
        self.assertEqual(harness.vm_runner.mutations(), [])

    def test_terminal_release_ack_loss_preserves_handoff_and_no_extra_cas(
        self,
    ):
        harness = ControllerHarness(self, registered_rollback=False)
        prepared = self.prepare(harness, "backend-code-update")
        harness.lose_ack = "release"
        with self.writes() as initial:
            with self.assertRaisesRegex(OSError, "lost acknowledgement"):
                self.operate(harness, prepared)
        self.assertEqual(len(initial), 2)
        roles_file = harness.root / "control/roles.json"
        original_roles = roles_file.read_bytes()
        roles = json.loads(original_roles)
        terminal = {
            **prepared,
            "epoch": roles["epoch"],
            "roles_sha256": contract.digest(roles),
        }
        self.assertEqual(roles["epoch"], harness.roles["epoch"] + 2)
        handoff = harness.root / (
            "candidate/handoff-" + prepared["intent_sha256"] + ".json"
        )
        original_handoff = handoff.read_bytes()
        self.assertFalse((harness.root / "candidate/current.json").exists())
        with self.writes() as repeated:
            result = self.operate(harness, terminal, action="reconcile")
        self.assertEqual(repeated, [])
        self.assertEqual(roles_file.read_bytes(), original_roles)
        self.assertEqual(handoff.read_bytes(), original_handoff)
        self.assertTrue(result["released"])
        self.assertFalse(
            (harness.root / "control/operation-hold.json").exists()
        )
        self.assertEqual(harness.vm_runner.mutations(), [])

    def test_two_catalog_update_target_preserves_original_e_and_both_cores(
        self,
    ):
        harness = ControllerHarness(
            self, registered_rollback=False, separate_catalogs=True
        )
        a, b = harness.data.catalogs
        self.assertNotEqual(a.config.state_root, b.config.state_root)
        self.assertEqual(
            replace(b.config, state_root=a.config.state_root), a.config
        )
        before = copy.deepcopy(harness.data.groups)
        current = harness.root / "candidate/current.json"
        original = current.read_bytes()
        prepared = self.prepare(harness, "backend-code-update")
        self.assertEqual(current.read_bytes(), original)
        with self.writes() as writes:
            result = self.operate(harness, prepared)
        self.assertEqual(len(writes), 2)
        self.assertEqual(
            [row["epoch"] for row in writes],
            [harness.roles["epoch"] + 1, harness.roles["epoch"] + 2],
        )
        self.assertTrue(result["released"])
        self.assertEqual(harness.data.groups, before)
        self.assertFalse(current.exists())
        snapshots = list(
            (harness.root / "candidate").glob("candidate-snapshot-*.json")
        )
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].read_bytes(), original)
        candidate = json.loads(original)
        self.assertEqual(candidate["roles_epoch"], harness.roles["epoch"])
        self.assertEqual(candidate["roles_sha256"], harness.roles_sha)
        self.assertEqual(candidate["catalog_binding"], b.identity())
        self.assertTrue(
            (
                harness.root
                / ("candidate/handoff-" + prepared["intent_sha256"] + ".json")
            ).exists()
        )
        self.assertEqual(
            json.loads(harness.canonical.read_bytes())["api_revision"],
            harness.data.hosts[1].source_revision,
        )
        self.assertEqual(harness.vm_runner.mutations(), [])
