"""Protect closed transaction admission without native deployment effects."""

import copy
from pathlib import Path
import tempfile
import unittest

from search.operations.deployment.provider import authority
from search.operations.deployment.transition import contract
from search.operations.shared import private_io
from .fixtures import operation_record


class TransactionContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="transition-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)

    def operation(self, kind="backend-code-update"):
        path = self.root / (kind + ".json")
        path.write_bytes(
            private_io.canonical(
                {"schema_version": 1, "state_root": str(self.root / kind)}
            )
        )
        path.chmod(0o600)
        with authority.open_control(path, create=True) as control:
            return operation_record(control, kind=kind)

    def decision(self, operation, side="target", *, cancelled=False):
        roles = (
            operation["predecessor_roles"]
            if cancelled
            else contract.terminal_roles(operation, side)
        )
        reason = "target_verified" if side == "target" else "verified_return"
        return {
            "schema_version": 1,
            "mode": "cancelled_before_begin" if cancelled else "terminal",
            "operation_id": operation["operation_id"],
            "intent_sha256": contract.digest(operation),
            "roles": copy.deepcopy(roles),
            "roles_sha256": contract.digest(roles),
            "side": side,
            "resolution": "no_dispatch" if cancelled else reason,
            "observations": {"backend": {}, "frontend": {}},
        }

    def discard(self, operation):
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
                "exclusive_network_ids": ["4" * 64],
                "lease_sha256": "5" * 64,
                "retain_paths": [],
            },
            "owner_source": copy.deepcopy(operation["operator"]),
        }

    def test_all_three_kinds_preserve_input_and_complete_roles_snapshots(self):
        for kind in sorted(contract.KINDS):
            with self.subTest(kind=kind):
                operation = self.operation(kind)
                frozen = copy.deepcopy(operation)
                self.assertIs(contract.intent(operation), operation)
                before = operation["predecessor_roles"]
                pending = contract.pending_roles(operation)
                self.assertEqual(pending["epoch"], before["epoch"] + 1)
                self.assertEqual(
                    pending["pending_operation_sha256"],
                    contract.digest(operation),
                )
                self.assertEqual(
                    contract.phase(operation, before), ("prepared", None)
                )
                self.assertEqual(
                    contract.phase(operation, pending), ("pending", None)
                )
                self.assertEqual(operation, frozen)

    def test_update_swaps_active_and_rollback_at_exact_second_epoch(self):
        operation = self.operation()
        before = operation["predecessor_roles"]
        after = contract.terminal_roles(operation, "target")
        self.assertEqual(after["epoch"], before["epoch"] + 2)
        self.assertEqual(
            after["rollback_backend_sha256"], before["active_backend_sha256"]
        )
        self.assertEqual(
            after["active_backend_sha256"],
            operation["candidate"]["backend"]["sha256"],
        )
        self.assertIsNone(after["pending_operation_sha256"])
        self.assertEqual(
            contract.phase(operation, after), ("terminal", "target")
        )

    def test_rollback_preserves_old_active_as_the_new_rollback(self):
        operation = self.operation("backend-code-rollback")
        before = operation["predecessor_roles"]
        after = contract.terminal_roles(operation, "target")
        self.assertEqual(
            after["active_backend_sha256"], before["rollback_backend_sha256"]
        )
        self.assertEqual(
            after["rollback_backend_sha256"], before["active_backend_sha256"]
        )
        self.assertEqual(
            after["tls_plan_ref"], operation["sides"]["target"]["tls"]["plan"]
        )

    def test_retire_keeps_active_and_tls_and_clears_only_rollback(self):
        operation = self.operation("backend-code-retire")
        before = operation["predecessor_roles"]
        expected = {
            **before,
            "epoch": before["epoch"] + 2,
            "rollback_backend_sha256": None,
        }
        self.assertEqual(
            contract.terminal_roles(operation, "target"), expected
        )
        self.assertEqual(
            contract.terminal_roles(operation, "predecessor"),
            {**before, "epoch": before["epoch"] + 2},
        )

    def test_roles_same_epoch_with_changed_identity_is_not_a_recovery_phase(
        self,
    ):
        operation = self.operation()
        changed = contract.pending_roles(operation)
        changed["pending_operation_sha256"] = "0" * 64
        with self.assertRaisesRegex(contract.TransitionError, "cas_conflict"):
            contract.phase(operation, changed)

    def test_candidate_original_epoch_and_snapshot_cannot_be_rebound(self):
        operation = self.operation()
        for field, value in (
            ("original_epoch", 2),
            ("original_roles_sha256", "0" * 64),
        ):
            changed = copy.deepcopy(operation)
            changed["candidate"][field] = value
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    contract.TransitionError, "candidate_changed"
                ),
            ):
                contract.intent(changed)

    def test_update_refuses_a_third_client_even_with_matching_candidate_epoch(
        self,
    ):
        operation = self.operation()
        roles = operation["predecessor_roles"]
        roles["rollback_backend_sha256"] = "0" * 64
        digest = contract.digest(roles)
        operation["predecessor_roles_sha256"] = digest
        operation["candidate"]["original_roles_sha256"] = digest
        with self.assertRaisesRegex(contract.TransitionError, "capacity"):
            contract.intent(operation)

    def test_epoch_reservation_rejects_bool_and_overflow_before_begin(self):
        operation = self.operation()
        for epoch in (True, 0, contract.MAX_EPOCH - 1, contract.MAX_EPOCH):
            changed = copy.deepcopy(operation)
            changed["predecessor_roles"]["epoch"] = epoch
            with self.subTest(epoch=epoch), self.assertRaises(ValueError):
                contract.intent(changed)

    def test_intent_is_closed_and_rejects_unhashable_kind_as_typed_error(self):
        operation = self.operation()
        for kind in ([], {}, True, None, "backend-code-other"):
            changed = {**operation, "kind": kind}
            with (
                self.subTest(kind=kind),
                self.assertRaises(contract.TransitionError),
            ):
                contract.intent(changed)
        with self.assertRaises(contract.TransitionError):
            contract.intent({**operation, "allow_pending": True})

    def test_backend_only_change_keeps_frontend_history_and_material_fixed(
        self,
    ):
        operation = self.operation()
        for field in (
            "previous",
            "env",
            "api_key_mount",
            "ca_mount",
            "composition_sha256",
            "monitor_lock_identity",
        ):
            changed = copy.deepcopy(operation)
            value = {"path": "/private/different", "sha256": "0" * 64}
            if field == "previous":
                value = {"historical": True}
            elif field in {"composition_sha256", "monitor_lock_identity"}:
                value = "0" * 64
            changed["sides"]["target"]["frontend"][field] = value
            with (
                self.subTest(field=field),
                self.assertRaises(contract.TransitionError),
            ):
                contract.intent(changed)

    def test_stable_frontend_profile_and_state_paths_cannot_move(self):
        operation = self.operation()
        for field in ("canonical_profile", "state"):
            changed = copy.deepcopy(operation)
            changed["sides"]["target"]["frontend"][field]["path"] = (
                "/private/another-namespace.json"
            )
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    contract.TransitionError, "binding_changed"
                ),
            ):
                contract.intent(changed)

    def test_tls_origin_and_material_are_fixed_in_a_backend_only_update(self):
        operation = self.operation()
        for field, value in (
            ("material_sha256", "0" * 64),
            ("project", "depositar-private-api-tls-other"),
        ):
            changed = copy.deepcopy(operation)
            changed["sides"]["target"]["tls"][field] = value
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    contract.TransitionError, "binding_changed"
                ),
            ):
                contract.intent(changed)
        changed = copy.deepcopy(operation)
        changed["sides"]["target"]["tls"]["config"]["outer_server_name"] = (
            "other.example.test"
        )
        with self.assertRaisesRegex(
            contract.TransitionError, "binding_changed"
        ):
            contract.intent(changed)

    def test_prebegin_cancellation_has_original_epoch_and_zero_roles_change(
        self,
    ):
        operation = self.operation()
        decision = self.decision(operation, "predecessor", cancelled=True)
        self.assertIs(contract.resolution(decision, operation), decision)
        decision["roles"] = contract.terminal_roles(operation, "predecessor")
        decision["roles_sha256"] = contract.digest(decision["roles"])
        with self.assertRaises(contract.TransitionError):
            contract.resolution(decision, operation)

    def test_terminal_resolution_binds_side_reason_and_complete_roles(self):
        operation = self.operation()
        decision = self.decision(operation)
        self.assertIs(contract.resolution(decision, operation), decision)
        for field, value in (
            ("resolution", "no_effect"),
            ("intent_sha256", "0" * 64),
            ("operation_id", "0" * 32),
        ):
            changed = {**copy.deepcopy(decision), field: value}
            with (
                self.subTest(field=field),
                self.assertRaises(contract.TransitionError),
            ):
                contract.resolution(changed, operation)

    def test_terminal_resolution_rejects_array_object_reason_as_typed_error(
        self,
    ):
        operation = self.operation()
        decision = self.decision(operation)
        for value in ([], {}, True, None):
            changed = {**copy.deepcopy(decision), "resolution": value}
            with (
                self.subTest(value=value),
                self.assertRaises(contract.TransitionError),
            ):
                contract.resolution(changed, operation)

    def test_discard_is_fixed_to_original_candidate_pending_epoch_and_owner(
        self,
    ):
        operation = self.operation()
        decision = self.discard(operation)
        self.assertIs(contract.discard(decision, operation), decision)
        for field, value in (
            ("expected_pending_epoch", 1),
            ("candidate_snapshot_sha256", "0" * 64),
            ("candidate_backend_sha256", "0" * 64),
            ("owner_source", operation["frontend_owner"]["source"]),
            ("outcome", "target"),
        ):
            changed = {**copy.deepcopy(decision), field: value}
            with (
                self.subTest(field=field),
                self.assertRaises(contract.TransitionError),
            ):
                contract.discard(changed, operation)

    def test_discard_rejects_bool_epoch_and_duplicate_resource_ownership(self):
        operation = self.operation()
        decision = self.discard(operation)
        changed = {**copy.deepcopy(decision), "expected_pending_epoch": True}
        with self.assertRaises(contract.TransitionError):
            contract.discard(changed, operation)
        ids = decision["resources"]["cpu_container_ids"]
        ids["conversation-api"] = ids["web"]
        with self.assertRaises(contract.TransitionError):
            contract.discard(decision, operation)

    def test_each_kind_has_valid_prebegin_cancel_and_two_terminal_outcomes(
        self,
    ):
        for kind in sorted(contract.KINDS):
            operation = self.operation(kind)
            cancelled = self.decision(operation, "predecessor", cancelled=True)
            self.assertIs(contract.resolution(cancelled, operation), cancelled)
            for side in sorted(contract.SIDES):
                decision = self.decision(operation, side)
                with self.subTest(kind=kind, side=side):
                    self.assertIs(
                        contract.resolution(decision, operation), decision
                    )

    def test_budget_numbers_are_exact_json_integers_not_equivalent_floats(
        self,
    ):
        operation = self.operation()
        for field, maximum in contract.LIMITS.items():
            changed = copy.deepcopy(operation)
            changed["limits"][field] = float(maximum)
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    contract.TransitionError, "limits_invalid"
                ),
            ):
                contract.intent(changed)
