"""Retain release confirmations before unlink and across lost ACKs."""

import copy
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from search.operations.deployment.provider import authority
from search.operations.deployment.provider import contract as provider
from search.operations.deployment.transition import (
    contract,
    enrollment,
    journal,
)
from search.operations.shared import private_io
from .fixtures import ObservingOwner, operation_record, reference


class ReservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="transition-owner-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.config = self.root / "control.json"
        self.config.write_bytes(
            private_io.canonical(
                {"schema_version": 1, "state_root": str(self.root / "control")}
            )
        )
        self.config.chmod(0o600)
        self.endpoint = self.root / "endpoint"
        self.endpoint.mkdir(mode=0o700)
        self.source = {"root": "/private/operator", "revision": "a" * 40}
        self.owner = enrollment.enroll(
            self.endpoint,
            participant="tls",
            source=self.source,
            configuration=reference("/private/tls-config.json"),
        )

    def decision(self, control):
        operation = journal.create(control, operation_record(control))
        owner = ObservingOwner()
        return operation, operation.cancel(owner, owner)

    def test_release_is_durable_before_unlink_and_repeated_ack_is_stable(self):
        with authority.open_control(self.config, create=True) as control:
            operation, decision = self.decision(control)
            with enrollment.locked(self.endpoint) as fd:
                owner = enrollment.BindingReservation(
                    self.endpoint, fd, self.owner
                )
                owner.prepare(operation.intent)
                released = owner.release(
                    operation.intent, decision, {"fresh": True}
                )
                again = owner.release(
                    operation.intent, decision, {"fresh": False}
                )
                self.assertEqual(released, again)
                self.assertFalse(
                    (self.endpoint / enrollment.RESERVATION).exists()
                )
                self.assertEqual(owner.released(operation.intent), released)

    def test_unlink_completed_but_error_returned_can_reconcile_without_replay(
        self,
    ):
        with authority.open_control(self.config, create=True) as control:
            operation, decision = self.decision(control)
            with enrollment.locked(self.endpoint) as fd:
                owner = enrollment.BindingReservation(
                    self.endpoint, fd, self.owner
                )
                owner.prepare(operation.intent)
                unlink = os.unlink

                def lost_ack(*args, **kwargs):
                    unlink(*args, **kwargs)
                    raise OSError("fixture acknowledgement lost")

                with patch.object(os, "unlink", lost_ack):
                    with self.assertRaises(OSError):
                        owner.release(
                            operation.intent, decision, {"fresh": True}
                        )
                self.assertTrue(owner.uncertain)
                replacement = enrollment.BindingReservation(
                    self.endpoint, fd, self.owner
                )
                result = replacement.release(
                    operation.intent, decision, {"fresh": True}
                )
                self.assertEqual(result["phase"], "released")
                self.assertFalse(
                    (self.endpoint / enrollment.RESERVATION).exists()
                )

    def test_historical_release_never_removes_a_different_reservation(self):
        with authority.open_control(self.config, create=True) as control:
            operation, decision = self.decision(control)
            with enrollment.locked(self.endpoint) as fd:
                owner = enrollment.BindingReservation(
                    self.endpoint, fd, self.owner
                )
                owner.prepare(operation.intent)
                original = owner.release(
                    operation.intent, decision, {"fresh": True}
                )
                newer = {"intent_sha256": "e" * 64, "operation_id": "b" * 32}
                private_io.write_json(
                    fd,
                    enrollment.RESERVATION,
                    newer,
                    max_bytes=contract.MAX_BYTES,
                )
                repeated = owner.release(
                    operation.intent, decision, {"fresh": True}
                )
                self.assertEqual(repeated, original)
                self.assertEqual(
                    enrollment._read(fd, enrollment.RESERVATION), newer
                )

    def test_discard_phase_is_typed_and_locks_predecessor(self):
        with authority.open_control(self.config, create=True) as control:
            operation = journal.create(control, operation_record(control))
            with enrollment.locked(self.endpoint) as fd:
                owner = enrollment.BindingReservation(
                    self.endpoint, fd, self.owner
                )
                owner.prepare(operation.intent)
                operation.begin(ObservingOwner(), ObservingOwner())
                resources = {
                    "cpu_container_ids": {
                        role: str(index) * 64
                        for index, role in enumerate(
                            sorted(provider.CPU_ROLES),
                            1,
                        )
                    },
                    "exclusive_network_ids": [],
                    "lease_sha256": "a" * 64,
                    "retain_paths": [],
                }
                decision = operation.lock_predecessor(
                    resources, ObservingOwner()
                )
                current = owner.lock_predecessor(operation.intent, decision)
                self.assertEqual(
                    current["cleanup_decision_sha256"],
                    contract.digest(decision),
                )
                with self.assertRaisesRegex(
                    contract.TransitionError, "outcome_fixed"
                ):
                    owner.require_side(operation.intent, "target")
                altered = copy.deepcopy(decision)
                altered["resources"]["lease_sha256"] = "b" * 64
                with self.assertRaisesRegex(
                    contract.TransitionError, "outcome_fixed"
                ):
                    owner.lock_predecessor(operation.intent, altered)

    def test_normal_mutator_refuses_existing_owner_and_reservation(self):
        with self.assertRaisesRegex(
            contract.TransitionError, "provider_managed"
        ):
            enrollment.require_unmanaged(self.endpoint)
        with enrollment.locked(self.endpoint) as fd:
            private_io.write_json(
                fd,
                enrollment.RESERVATION,
                {"unknown": True},
                max_bytes=contract.MAX_BYTES,
            )
            with self.assertRaisesRegex(
                contract.TransitionError, "binding_reserved"
            ):
                enrollment.require_unreserved(self.endpoint)

    def test_released_mode_epoch_and_roles_digest_cannot_disagree(self):
        with authority.open_control(self.config, create=True) as control:
            operation, decision = self.decision(control)
            with enrollment.locked(self.endpoint) as fd:
                owner = enrollment.BindingReservation(
                    self.endpoint, fd, self.owner
                )
                owner.prepare(operation.intent)
                released = owner.release(
                    operation.intent, decision, {"fresh": True}
                )
                for changes in (
                    {"decision_mode": "unrecognized"},
                    {"decision_mode": "terminal"},
                    {"resolved_epoch": released["resolved_epoch"] + 1},
                    {"resolved_roles_sha256": "0" * 64},
                    {"side": "target"},
                ):
                    with self.subTest(changes=changes):
                        private_io.write_json(
                            fd,
                            owner._released_name(operation.intent),
                            {**released, **changes},
                            replace=True,
                            max_bytes=contract.MAX_BYTES,
                        )
                        with self.assertRaises(contract.TransitionError):
                            owner.released(operation.intent)
