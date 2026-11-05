"""Normal lifecycle admissions cannot bypass an enrolled backend operation."""

from pathlib import Path
import os
import tempfile
import unittest

from search.operations.deployment.provider import authority
from search.operations.deployment.transition import enrollment, journal
from search.operations.shared import private_io
from search.operations.state import StateStore, _admit_operation
from tests.unit.deployment.transition.fixtures import (
    ObservingOwner,
    operation_record,
    reference,
)


class TransitionStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="transition-admission-")
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

    def test_normal_create_flag_cannot_open_pending_existing_roles(self):
        with authority.open_control(self.config, create=True) as control:
            operation = journal.create(control, operation_record(control))
            witness = ObservingOwner()
            operation.begin(witness, witness)
        original = (self.root / "control/roles.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "recovery_required"):
            with authority.open_control(self.config, create=True):
                self.fail("create is for an empty foundation, not recovery")
        self.assertEqual(
            (self.root / "control/roles.json").read_bytes(), original
        )

    def test_enrolled_product_refuses_normal_lifecycle_admission(self):
        root = self.root / "product"
        root.mkdir(mode=0o700)
        enrollment.enroll(
            root,
            participant="product",
            source={"root": "/source/operator", "revision": "a" * 40},
            configuration=reference(self.config),
        )
        fd = private_io.open_directory(root)
        try:
            store = StateStore(root, fd)
            with self.assertRaisesRegex(ValueError, "provider_managed"):
                _admit_operation(store, "start-inference")
        finally:
            os.close(fd)

    def test_loaded_controller_cannot_claim_another_clean_checkout(self):
        from types import SimpleNamespace
        from search.operations.deployment.transition import backend, contract
        from search.operations.deployment.transition import controller

        filename = self.root / "settings.json"
        filename.write_bytes(
            private_io.canonical(
                {
                    "schema_version": 1,
                    "kind": "backend-binding-controller",
                    "operator": {
                        "root": str(self.root / "other-checkout"),
                        "revision": "a" * 40,
                    },
                    "control_config": backend.reference(self.config),
                    "frontend_transport": reference("/private/transport.json"),
                    "backend_peer_sha256": "7" * 64,
                }
            )
        )
        filename.chmod(0o600)

        def clean_git(argv, *, timeout):
            return SimpleNamespace(
                returncode=0, stdout="a" * 40 if "rev-parse" in argv else ""
            )

        with self.assertRaisesRegex(
            contract.TransitionError, "source_changed"
        ):
            controller.Settings(filename, source_runner=clean_git)

    def test_one_settings_instance_has_fresh_non_nested_invocation_budgets(
        self,
    ):
        from types import SimpleNamespace
        from search.operations.deployment.transition import backend, contract
        from search.operations.deployment.transition import controller

        source = self.root / "operator"
        filename = self.root / "settings.json"
        value = {
            "schema_version": 1,
            "kind": "backend-binding-controller",
            "operator": {"root": str(source), "revision": "a" * 40},
            "control_config": backend.reference(self.config),
            "frontend_transport": reference("/private/transport.json"),
            "backend_peer_sha256": "7" * 64,
        }
        filename.write_bytes(private_io.canonical(value))
        filename.chmod(0o600)

        def git(argv, *, timeout):
            self.assertGreater(timeout, 0)
            return SimpleNamespace(
                returncode=0, stdout="a" * 40 if "rev-parse" in argv else ""
            )

        settings = controller.Settings(
            filename, source_runner=git, source_root=source
        )
        first = settings.commands
        self.assertEqual(first.by_kind, {"operator_source": 2})
        with settings.invocation():
            self.assertIs(settings.commands, first)
            self.assertEqual(first.calls, 4)
            with self.assertRaisesRegex(
                contract.TransitionError, "invocation_busy"
            ):
                with settings.invocation():
                    self.fail("nested invocation must refuse before reset")
        with self.assertRaisesRegex(OSError, "failed owned operation"):
            with settings.invocation():
                self.assertIsNot(settings.commands, first)
                self.assertEqual(settings.commands.calls, 2)
                raise OSError("failed owned operation")
        with settings.invocation():
            self.assertEqual(settings.commands.calls, 2)
        self.assertEqual(first.calls, 4)
