"""Exercise durable Roles ordering and same-operation recovery on CPU."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.operations.deployment.provider import authority
from search.operations.deployment.provider import contract as provider
from search.operations.deployment.transition import contract, journal
from search.operations.shared import private_io
from .fixtures import ObservingOwner, operation_record


class OperationJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="transition-journal-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.config = self.root / "control.json"
        self.config.write_bytes(
            private_io.canonical(
                {"schema_version": 1, "state_root": str(self.root / "state")}
            )
        )
        self.config.chmod(0o600)
        self.backend, self.frontend = ObservingOwner(), ObservingOwner()

    def test_three_kinds_use_two_roles_writes_and_keep_original_records(self):
        for kind in sorted(contract.KINDS):
            with self.subTest(kind=kind):
                self._two_writes(kind)

    def _two_writes(self, kind):
        path = self.root / (kind + ".json")
        path.write_bytes(
            private_io.canonical(
                {"schema_version": 1, "state_root": str(self.root / kind)}
            )
        )
        path.chmod(0o600)
        with authority.open_control(path, create=True) as control:
            value = operation_record(control, kind=kind)
            original = copy.deepcopy(value)
            operation = journal.create(control, value)
            with patch.object(
                control,
                "_compare_and_swap_roles",
                wraps=control._compare_and_swap_roles,
            ) as writer:
                operation.begin(self.backend, self.frontend)
                operation.resolve(
                    self.backend,
                    self.frontend,
                    "target",
                    reason="target_verified",
                )
                self.assertEqual(writer.call_count, 2)
                self.assertEqual(
                    control.roles["epoch"],
                    original["predecessor_roles"]["epoch"] + 2,
                )
            self.assertEqual(value, original)
            operation.release(self.backend, self.frontend)
            self.assertFalse((control.root / journal.HOLD).exists())

    def test_prebegin_cancel_releases_without_any_roles_write(self):
        with authority.open_control(self.config, create=True) as control:
            operation = journal.create(control, operation_record(control))
            original = dict(control.roles)
            with patch.object(control, "_compare_and_swap_roles") as writer:
                decision = operation.cancel(self.backend, self.frontend)
                operation.release(self.backend, self.frontend)
            self.assertEqual(decision["mode"], "cancelled_before_begin")
            writer.assert_not_called()
            self.assertEqual(control.roles, original)

    def test_normal_entry_refuses_prebegin_hold_and_preserves_it(self):
        with authority.open_control(self.config, create=True) as control:
            operation = journal.create(control, operation_record(control))
            root = control.root
            expected = (root / journal.HOLD).read_bytes()
            self.assertEqual(operation.phase, ("prepared", None))
        with self.assertRaisesRegex(
            provider.ProviderError, "recovery_required"
        ):
            with authority.open_control(self.config):
                self.fail("a prepared operation must fence normal entry")
        self.assertEqual((root / journal.HOLD).read_bytes(), expected)

    def test_reopen_exact_terminal_completes_release_without_extra_cas(self):
        with authority.open_control(self.config, create=True) as control:
            operation = journal.create(control, operation_record(control))
            operation.begin(self.backend, self.frontend)
            operation.resolve(
                self.backend, self.frontend, "target", reason="target_verified"
            )
            sha, roles_sha = operation.sha256, control.roles_sha256
        with journal.open_operation(
            self.config,
            intent_sha256=sha,
            expected_epoch=3,
            expected_roles_sha256=roles_sha,
        ) as operation:
            with patch.object(
                operation.control, "_compare_and_swap_roles"
            ) as writer:
                operation.recover_terminal(self.backend, self.frontend)
                operation.release(self.backend, self.frontend)
            writer.assert_not_called()

    def test_discard_decision_locks_predecessor_before_vm_ack_and_stop(self):
        with authority.open_control(self.config, create=True) as control:
            value = operation_record(control)
            operation = journal.create(control, value)
            operation.begin(self.backend, self.frontend)
            resources = {
                "cpu_container_ids": {
                    name: str(index) * 64
                    for index, name in enumerate(sorted(provider.CPU_ROLES), 1)
                },
                "exclusive_network_ids": [],
                "lease_sha256": "a" * 64,
                "retain_paths": [],
            }
            operation.lock_predecessor(resources, self.frontend)
            with self.assertRaisesRegex(
                contract.TransitionError, "outcome_fixed"
            ):
                operation.resolve(
                    self.backend,
                    self.frontend,
                    "target",
                    reason="target_verified",
                )
            self.assertEqual(control.roles["epoch"], 2)

    def test_changed_snapshot_refuses_recovery_before_any_owner_call(self):
        with authority.open_control(self.config, create=True) as control:
            operation = journal.create(control, operation_record(control))
            sha, old = operation.sha256, control.roles_sha256
        with self.assertRaisesRegex(contract.TransitionError, "cas_conflict"):
            with journal.open_operation(
                self.config,
                intent_sha256=sha,
                expected_epoch=2,
                expected_roles_sha256=old,
            ):
                self.fail("wrong complete snapshot must refuse")
