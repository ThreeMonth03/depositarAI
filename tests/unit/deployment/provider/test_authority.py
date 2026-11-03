"""Exercise real private-file fences, locks and whole-record CAS on CPU."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from search.operations.deployment.provider import authority, contract
from search.operations.shared import private_io
from .test_contract import records


class PrivateFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="provider-authority-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.state = self.root / "state"
        self.config = self.root / "control.json"
        self.write(
            self.config, {"schema_version": 1, "state_root": str(self.state)}
        )

    def write(self, path, value):
        path.write_bytes(private_io.canonical(value))
        path.chmod(0o600)

    def install(self, control):
        provider, backend, roles = records()
        control.publish("provider", provider)
        control.publish("backend", backend)
        control.adopt_roles(roles)
        return provider, backend, roles


class PrivateControlTests(PrivateFixture, unittest.TestCase):
    def test_load_is_readonly_and_unadopted_control_never_creates_state(self):
        self.assertFalse(self.state.exists())
        parsed = authority.load_config(self.config)
        self.assertEqual(parsed["state_root"], self.state)
        self.assertFalse(self.state.exists())
        self.state.mkdir(mode=0o700)
        with self.assertRaisesRegex(contract.ProviderError, "not_adopted"):
            with authority.open_control(self.config):
                self.fail("missing roles must refuse")

    def test_adoption_creates_immutable_records_and_cannot_overwrite(self):
        with authority.open_control(self.config, create=True) as control:
            provider, backend, roles = self.install(control)
            self.assertEqual(authority.read_provider(control), provider)
            self.assertEqual(
                authority.read_backend(control, contract.digest(backend)),
                backend,
            )
            before = (self.state / "roles.json").read_bytes()
            with self.assertRaisesRegex(
                contract.ProviderError, "already_adopted"
            ):
                control.adopt_roles(roles)
            self.assertEqual(before, (self.state / "roles.json").read_bytes())

    def test_whole_record_and_epoch_cas_reject_stale_and_aba_updates(self):
        with authority.open_control(self.config, create=True) as control:
            _provider, _backend, original = self.install(control)
            before = contract.digest(original)
            intent = {"operation": "fixture-only", "expected": before}
            name = "intent-" + contract.digest(intent) + ".json"
            private_io.write_json(
                control.fd, name, intent, max_bytes=contract.MAX_BYTES
            )
            pending = {
                **original,
                "epoch": 2,
                "pending_operation_sha256": contract.digest(intent),
            }
            control._compare_and_swap_roles(
                pending, expected_epoch=1, expected_sha256=before
            )
            cleared = {**original, "epoch": 3}
            control._compare_and_swap_roles(
                cleared,
                expected_epoch=2,
                expected_sha256=contract.digest(pending),
            )
            self.assertEqual(control.roles, cleared)
            for epoch, digest in (
                (1, before),
                (3, before),
                (True, contract.digest(cleared)),
            ):
                with (
                    self.subTest(epoch=epoch),
                    self.assertRaises(contract.ProviderError),
                ):
                    control._compare_and_swap_roles(
                        {**cleared, "epoch": 4},
                        expected_epoch=epoch,
                        expected_sha256=digest,
                    )

    def test_pending_reference_must_already_exist_and_match(self):
        with authority.open_control(self.config, create=True) as control:
            roles = self.install(control)[2]
            pending = {
                **roles,
                "epoch": 2,
                "pending_operation_sha256": "a" * 64,
            }
            with self.assertRaises(FileNotFoundError):
                control._compare_and_swap_roles(
                    pending,
                    expected_epoch=1,
                    expected_sha256=contract.digest(roles),
                )
            self.assertEqual(control.roles["epoch"], 1)
            self.write(
                self.state / ("intent-" + "a" * 64 + ".json"),
                {"not": "the expected digest"},
            )
            with self.assertRaises(contract.ProviderError):
                control._compare_and_swap_roles(
                    pending,
                    expected_epoch=1,
                    expected_sha256=contract.digest(roles),
                )

    def test_pending_control_refuses_normal_entry_without_clearing(self):
        with authority.open_control(self.config, create=True) as control:
            roles = self.install(control)[2]
            pending = {
                **roles,
                "epoch": 2,
                "pending_operation_sha256": "a" * 64,
            }
            self.write(self.state / "roles.json", pending)
            control._roles = pending  # Fixture-only later-state seed.
        before = (self.state / "roles.json").read_bytes()
        with self.assertRaisesRegex(
            contract.ProviderError, "recovery_required"
        ):
            with authority.open_control(self.config):
                self.fail("pending cannot auto-recover")
        self.assertEqual(before, (self.state / "roles.json").read_bytes())

    def test_namespace_replacement_and_linked_records_are_rejected(self):
        with authority.open_control(self.config, create=True) as control:
            provider = self.install(control)[0]
            name = "provider-" + contract.digest(provider) + ".json"
            saved = self.state / name
            other = self.state / "other.json"
            other.write_bytes(saved.read_bytes())
            other.chmod(0o600)
            saved.unlink()
            saved.symlink_to(other)
            with self.assertRaises(OSError):
                authority.read_provider(control)

    def test_lock_name_replacement_never_gains_control(self):
        with self.assertRaises(contract.ProviderError):
            with authority.open_control(self.config, create=True) as control:
                self.install(control)
                (self.state / "operations.lock").unlink()
                (self.state / "operations.lock").write_bytes(b"")
                (self.state / "operations.lock").chmod(0o600)
                control.refresh()

    def test_linked_or_nonprivate_config_is_refused_before_state_creation(
        self,
    ):
        self.config.chmod(0o644)
        with self.assertRaisesRegex(contract.ProviderError, "config_invalid"):
            authority.load_config(self.config)
        self.assertFalse(self.state.exists())
        self.config.chmod(0o600)
        linked = self.root / "linked.json"
        linked.symlink_to(self.config)
        with self.assertRaisesRegex(contract.ProviderError, "config_invalid"):
            authority.load_config(linked)

    def test_failed_durable_publication_is_uncertain_and_not_retried(self):
        with self.assertRaisesRegex(
            contract.ProviderError, "outcome_unconfirmed"
        ):
            with authority.open_control(self.config, create=True) as control:
                provider, backend, roles = records()
                control.publish("provider", provider)
                control.publish("backend", backend)
                real_write = private_io.write_json

                def uncertain(*args, **kwargs):
                    real_write(*args, **kwargs)
                    raise OSError("PRIVATE")

                with patch.object(private_io, "write_json", uncertain):
                    control.adopt_roles(roles)
        self.assertTrue((self.state / "roles.json").exists())

    def test_epoch_limit_is_closed_and_cas_cannot_wrap(self):
        with authority.open_control(self.config, create=True) as control:
            original = self.install(control)[2]
            maximum = {**original, "epoch": 2**63 - 1}
            contract.roles(maximum)
            self.write(self.state / "roles.json", maximum)
            control._roles = maximum  # Fixture-only distant epoch.
            before = (self.state / "roles.json").read_bytes()
            with self.assertRaises(contract.ProviderError):
                control._compare_and_swap_roles(
                    {**maximum, "epoch": 2**63},
                    expected_epoch=maximum["epoch"],
                    expected_sha256=contract.digest(maximum),
                )
            self.assertEqual(before, (self.state / "roles.json").read_bytes())

    def test_control_lock_is_real_and_checks_are_readonly(self):
        with authority.open_control(self.config, create=True) as control:
            self.install(control)
            before = (self.state / "roles.json").read_bytes()
            with self.assertRaisesRegex(contract.ProviderError, "busy"):
                with authority.open_control(self.config):
                    self.fail("second file description cannot take the lock")
            control.refresh()
            self.assertEqual(before, (self.state / "roles.json").read_bytes())

    def test_control_config_change_between_parse_and_lock_is_not_accepted(
        self,
    ):
        original = authority._lock

        def changed(fd, *, create):
            self.write(
                self.config,
                {
                    "schema_version": 1,
                    "state_root": str(self.root / "other"),
                },
            )
            return original(fd, create=create)

        with (
            patch.object(authority, "_lock", changed),
            self.assertRaisesRegex(
                contract.ProviderError, "authority_changed"
            ),
        ):
            with authority.open_control(self.config, create=True):
                self.fail("unhashed first config must not gain control")
        self.assertFalse((self.state / "roles.json").exists())
        self.assertFalse((self.root / "other").exists())

    def test_interrupted_durable_write_marks_instance_uncertain(self):
        with self.assertRaisesRegex(
            contract.ProviderError, "outcome_unconfirmed"
        ):
            with authority.open_control(self.config, create=True) as control:
                provider, backend, roles = records()
                control.publish("provider", provider)
                control.publish("backend", backend)
                write = private_io.write_json

                def interrupted(*args, **kwargs):
                    write(*args, **kwargs)
                    raise KeyboardInterrupt

                with patch.object(private_io, "write_json", interrupted):
                    with self.assertRaises(KeyboardInterrupt):
                        control.adopt_roles(roles)
                self.assertTrue(control.uncertain)
                with self.assertRaisesRegex(
                    contract.ProviderError, "outcome_unconfirmed"
                ):
                    control.adopt_roles(roles)
        self.assertTrue((self.state / "roles.json").exists())

    def test_backend_lifetime_closure_does_not_depend_on_optional_paths(self):
        backend = records()[1]
        backend["owned_paths"] = []
        state = self.root / "catalog-state"
        with patch.object(
            authority,
            "_configuration",
            return_value=SimpleNamespace(state_root=state),
        ) as configuration:
            protected = authority.backend_resources(backend)
        configuration.assert_called_once_with(
            backend["configuration"], backend["catalog_source"]["root"]
        )
        self.assertGreaterEqual(
            set(protected["owned_paths"]),
            {
                backend["source"]["root"],
                backend["build_state_root"],
                backend["catalog_source"]["root"],
                backend["configuration"]["path"],
                str(state),
            },
        )

    def test_control_and_tls_dependencies_cannot_be_removed_by_a_candidate(
        self,
    ):
        empty = {"container_ids": [], "network_ids": [], "owned_paths": []}
        with authority.open_control(self.config, create=True) as control:
            roles = self.install(control)[2]
            before = (self.state / "roles.json").read_bytes()
            with (
                patch.object(
                    authority, "_provider_resources", return_value=empty
                ),
                patch.object(
                    authority, "backend_resources", return_value=empty
                ),
            ):
                for path in (
                    self.state,
                    self.config,
                    roles["tls_plan_ref"]["path"],
                ):
                    with (
                        self.subTest(path=path),
                        self.assertRaisesRegex(
                            contract.ProviderError, "backend_protected"
                        ),
                    ):
                        control.require_disposable(
                            {**empty, "owned_paths": [str(path)]}
                        )
            self.assertEqual(before, (self.state / "roles.json").read_bytes())

    def test_provider_configuration_remains_a_mandatory_dependency(self):
        provider = records()[0]
        raw = private_io.canonical({"container_ids": {}, "network_ids": {}})
        provider["inference_complete_sha256"] = hashlib.sha256(raw).hexdigest()
        config = SimpleNamespace(
            state_root=self.root / "native",
            api_model_root=self.root / "api",
            embedding_model_root=self.root / "embedding",
            reranker_model_root=self.root / "reranker",
        )
        with (
            patch.object(authority, "_configuration", return_value=config),
            patch.object(authority, "read_private", return_value=raw),
        ):
            protected = authority._provider_resources(provider)
        self.assertIn(
            provider["configuration"]["path"], protected["owned_paths"]
        )


if __name__ == "__main__":
    unittest.main()
