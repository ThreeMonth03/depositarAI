"""Explicit bootstrap acceptance never precedes native and FD exit fences."""

from contextlib import ExitStack, contextmanager, nullcontext
import copy
import hashlib
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from search.operations.deployment.provider import adoption, authority, contract
from .test_authority import PrivateFixture
from .test_contract import records


class ProviderAdoptionTests(PrivateFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.bootstrap = self.root / "bootstrap.json"
        self.input = {
            "schema_version": 1,
            "profile": {
                "schema_version": 1,
                "state_root": "/private/old-candidate",
                "production_source": "/private/source",
                "production_config": "/private/product.json",
                "tls_plan": "/private/tls/plan.json",
            },
            "expected_revision": "a" * 40,
            "release_receipt_sha256": "b" * 64,
            "inference_receipt_sha256": "c" * 64,
            "tls_plan_sha256": "d" * 64,
        }
        self.write(self.bootstrap, self.input)
        self.expected = hashlib.sha256(self.bootstrap.read_bytes()).hexdigest()

    def invoke(self):
        return adoption.adopt_running(
            self.config,
            self.bootstrap,
            expected_bootstrap_sha256=self.expected,
        )

    def test_bootstrap_external_digest_precedes_control_and_native_work(self):
        self.expected = "0" * 64
        with patch.object(adoption, "_verified_native") as native:
            with self.assertRaisesRegex(
                contract.ProviderError, "bootstrap_changed"
            ):
                self.invoke()
        native.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_publication_waits_for_native_context_exit(self):
        events = []

        @contextmanager
        def verified(*_args):
            events.append("native")
            yield records()[:2]
            self.assertFalse((self.state / "roles.json").exists())
            events.append("fd-exit")

        with (
            patch.object(adoption, "_disjoint"),
            patch.object(adoption, "_verified_native", verified),
        ):
            result = self.invoke()
        self.assertEqual(events, ["native", "fd-exit"])
        self.assertEqual(result["epoch"], 1)
        with authority.open_control(self.config) as control:
            self.assertIsNone(control.roles["rollback_backend_sha256"])
            self.assertIsNone(control.roles["pending_operation_sha256"])
        self.assertEqual(
            self.bootstrap.read_bytes(), authority.read_private(self.bootstrap)
        )

    def test_late_native_fence_failure_publishes_no_provider_or_roles(self):
        @contextmanager
        def changed(*_args):
            yield records()[:2]
            raise contract.ProviderError(
                "shared_backend_provider_source_changed"
            )

        with (
            patch.object(adoption, "_disjoint"),
            patch.object(adoption, "_verified_native", changed),
        ):
            with self.assertRaisesRegex(
                contract.ProviderError, "source_changed"
            ):
                self.invoke()
        self.assertEqual(
            {path.name for path in self.state.iterdir()}, {"operations.lock"}
        )

    def test_bootstrap_change_after_native_verification_refuses_publication(
        self,
    ):
        @contextmanager
        def changed(*_args):
            yield records()[:2]
            self.write(self.bootstrap, {**self.input, "unknown": "PRIVATE"})

        with (
            patch.object(adoption, "_disjoint"),
            patch.object(adoption, "_verified_native", changed),
        ):
            with self.assertRaisesRegex(
                contract.ProviderError, "bootstrap_changed"
            ):
                self.invoke()
        self.assertFalse((self.state / "roles.json").exists())

    def test_existing_or_partial_control_is_never_automatically_adopted(self):
        self.state.mkdir(mode=0o700)
        self.write(self.state / "unconfirmed.json", {"attempt": "retained"})
        with (
            patch.object(adoption, "_disjoint"),
            patch.object(adoption, "_verified_native") as native,
        ):
            with self.assertRaisesRegex(
                contract.ProviderError, "already_adopted"
            ):
                self.invoke()
        native.assert_not_called()
        self.assertTrue((self.state / "unconfirmed.json").exists())

    def test_quiescence_is_explicit_old_bootstrap_not_normal_runtime_fallback(
        self,
    ):
        empty = SimpleNamespace(read=lambda _name: None)
        with self.assertRaises(contract.ProviderError):
            adoption._quiescent(empty)
        stopped = {
            "schema_version": 1,
            "phase": "stopped",
            "revision": "a" * 40,
            "container_ids": {},
        }
        self.assertEqual(
            adoption._quiescent(SimpleNamespace(read=lambda _name: stopped)),
            stopped,
        )
        for change in (
            {"phase": "running"},
            {"schema_version": 2},
            {"extra": "PRIVATE"},
            {"container_ids": None},
        ):
            changed = {**stopped, **change}
            with (
                self.subTest(change=change),
                self.assertRaises(contract.ProviderError),
            ):
                adoption._quiescent(
                    SimpleNamespace(read=lambda _name: changed)
                )

    def test_bootstrap_has_one_closed_native_representation(self):
        for changed in (
            {**self.input, "allow_dirty": True},
            {**self.input, "schema_version": True},
        ):
            self.write(self.bootstrap, changed)
            digest = hashlib.sha256(self.bootstrap.read_bytes()).hexdigest()
            with self.assertRaises(contract.ProviderError):
                adoption._bootstrap(self.bootstrap, digest)

    def test_unverified_backend_reference_cannot_replace_provider_identity(
        self,
    ):
        provider, backend, _roles = records()
        backend = copy.deepcopy(backend)
        backend["provider_sha256"] = "f" * 64

        @contextmanager
        def wrong(*_args):
            yield provider, backend

        with (
            patch.object(adoption, "_disjoint"),
            patch.object(adoption, "_verified_native", wrong),
        ):
            with self.assertRaisesRegex(
                contract.ProviderError, "authority_changed"
            ):
                self.invoke()
        self.assertFalse((self.state / "roles.json").exists())

    def test_native_exit_recheck_cannot_reseal_changed_compute_or_namespace(
        self,
    ):
        original = {"provider": "a" * 64, "backend": "b" * 64}
        state = SimpleNamespace()
        adoption._seal_observation(state, "c" * 64, original)
        adoption._seal_observation(state, "c" * 64, dict(original))
        for compute, namespaces, code in (
            ("d" * 64, original, "model_changed"),
            ("c" * 64, {**original, "provider": "e" * 64}, "runtime_changed"),
        ):
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(contract.ProviderError, code),
            ):
                adoption._seal_observation(state, compute, namespaces)
        self.assertEqual(state.compute_sha256, "c" * 64)
        self.assertEqual(state.namespaces, original)

    def _native_exit_fixture(self):
        native_root = self.root / "native"
        native_root.mkdir(mode=0o700)
        config_file = self.root / "native-product.json"
        tls = self.root / "tls-plan.json"
        files = {
            config_file: {"unchanged": "configuration"},
            tls: {"unchanged": "TLS"},
            native_root / "release-candidate.json": {"unchanged": "release"},
            native_root / "inference-candidate.json": {"unchanged": "model"},
        }
        for path, value in files.items():
            self.write(path, value)
        before = {path: path.read_bytes() for path in files}
        self.input["profile"].update(
            production_source=str(self.root / "source"),
            production_config=str(config_file),
            state_root=str(self.root / "journal"),
            tls_plan=str(tls),
        )
        for key, path in (
            ("release_receipt_sha256", native_root / "release-candidate.json"),
            (
                "inference_receipt_sha256",
                native_root / "inference-candidate.json",
            ),
            ("tls_plan_sha256", tls),
        ):
            self.input[key] = hashlib.sha256(before[path]).hexdigest()
        self.write(self.bootstrap, self.input)
        self.expected = hashlib.sha256(self.bootstrap.read_bytes()).hexdigest()
        before[self.bootstrap] = self.bootstrap.read_bytes()
        return SimpleNamespace(state_root=native_root), before

    def _reject_native_close_drift(self, drift):
        from search.operations import state as product_state
        from search.operations.catalog import execution, query
        from search.operations.deployment import credentials, inference
        from search.operations.deployment.candidate import catalog, controller
        from search.operations.deployment.candidate import policy
        from search.operations.deployment.release import controller as native

        config, before = self._native_exit_fixture()
        context = {}
        expected = {
            "key": {"proof": "old-key"},
            "query": {"proof": "old-query"},
        }
        current = copy.deepcopy(expected)
        held, events = {"catalog": False, "state": False}, []
        store = SimpleNamespace(
            read=lambda _name: {"data_root": str(self.root / "catalog-data")},
            _check_directory=lambda: events.append("native-state-fence"),
        )
        deadline = time.monotonic() + 60
        inputs = SimpleNamespace(
            key=expected["key"], authority=expected["query"]
        )

        def missing(_name):
            raise FileNotFoundError

        @contextmanager
        def native_lock(*_args, **_kwargs):
            held["state"] = True
            try:
                yield store
            finally:
                held["state"] = False

        @contextmanager
        def catalog_lock(_context):
            held["catalog"] = True
            try:
                yield
            finally:
                held["catalog"] = False

        def native_inputs(stack, *_args):
            def close_models():
                events.append("native-handles-closed")
                current[drift] = {"proof": "changed-after-models-closed"}

            def refresh():
                native._same(current["key"], expected["key"])
                native._same(current["query"], expected["query"])
                events.append("last-model-check")

            stack.callback(close_models)
            inputs.refresh = refresh
            return SimpleNamespace(inputs=inputs, deadline=deadline)

        final_authority = native._final_authority

        def final_fence(*args):
            self.assertTrue(held["catalog"])
            self.assertTrue(held["state"])
            self.assertEqual(events[-1], "native-handles-closed")
            self.assertEqual(args[-1], deadline)
            events.append("final-native-authority")
            return final_authority(*args)

        with ExitStack() as patches:
            for owner, name, value in (
                (adoption, "_disjoint", lambda *_args: None),
                (adoption, "require_clean_revision", lambda _root: "a" * 40),
                (adoption.runtime, "configuration", lambda *_args: config),
                (
                    controller,
                    "journal",
                    lambda _root: nullcontext(SimpleNamespace(read=missing)),
                ),
                (
                    product_state,
                    "locked_state",
                    native_lock,
                ),
                (product_state, "operation_receipts", lambda _store: []),
                (execution, "operation_lock", catalog_lock),
                (catalog, "load_context", lambda *_args: (context, {})),
                (
                    policy,
                    "validate_shared_request_policy",
                    lambda *_args: {"policy": {}},
                ),
                (adoption, "_native_inputs", native_inputs),
                (adoption, "_verify_native", lambda *_args: None),
                (adoption, "_records", lambda *_args: records()[:2]),
                (query, "read_authority", lambda _context: current["query"]),
                (
                    credentials,
                    "read_api_key_authority",
                    lambda *_args: current["key"],
                ),
                (inference, "_source_revision", lambda *_args: None),
                (native, "_final_authority", final_fence),
            ):
                patches.enter_context(patch.object(owner, name, value))
            with self.assertRaisesRegex(
                native.ReleaseError, "authority_changed"
            ):
                self.invoke()
        self.assertEqual(
            events[-2:], ["native-handles-closed", "final-native-authority"]
        )
        self.assertEqual(
            {path.name for path in self.state.iterdir()}, {"operations.lock"}
        )
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_key_drift_during_native_handle_close_prevents_publication(self):
        self._reject_native_close_drift("key")

    def test_query_drift_during_native_handle_close_prevents_publication(self):
        self._reject_native_close_drift("query")

    def test_native_inputs_retain_the_original_deadline_for_final_fence(self):
        import os

        from search.operations.deployment.release import controller as native
        from search.operations.shared import private_io

        native_root = self.root / "native-inputs"
        native_root.mkdir(mode=0o700)
        parent = native_root / "releases"
        parent.mkdir(mode=0o700)
        name = "release-" + "a" * 12 + "-" + "b" * 12
        (parent / name).mkdir(mode=0o700)
        completed = {"fixture": "unchanged-native-receipt"}
        inputs = SimpleNamespace(
            host=SimpleNamespace(compose={"name": "depositar-ai-" + name})
        )
        with ExitStack() as stack:
            fd = private_io.open_directory(native_root)
            stack.callback(os.close, fd)
            store = SimpleNamespace(fd=fd, read=lambda _name: completed)
            with (
                patch.object(
                    native, "_runtime_inputs", return_value=inputs
                ) as builder,
                patch.object(
                    native, "_load_completed", return_value=completed
                ),
                patch.object(adoption.time, "monotonic", return_value=100),
            ):
                state = adoption._native_inputs(
                    stack,
                    SimpleNamespace(state_root=native_root),
                    store,
                    {},
                    self.root / "source",
                    "a" * 40,
                    self.input,
                )
            self.assertEqual(
                state.deadline, 100 + native.runtime_docker.TOTAL_SECONDS
            )
            self.assertEqual(builder.call_args.args[-1], state.deadline)


if __name__ == "__main__":
    unittest.main()
