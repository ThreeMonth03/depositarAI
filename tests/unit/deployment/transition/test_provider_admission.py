"""Run real provider admission and lock owners over private CPU inputs.

Only Git command outputs are fake. Empty model roots intentionally cannot
satisfy production pins; reaching their rejection is not cold-load success.
"""

from contextlib import contextmanager, ExitStack
import copy
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from search.operations import state
from search.operations.catalog import execution
from search.operations.deployment import source_bundle
from search.operations.deployment.candidate import controller as candidate
from search.operations.deployment.candidate import policy
from search.operations.deployment.provider import authority, runtime
from search.operations.deployment.transition import backend, contract, journal
from search.operations.shared import private_io, source_identity
from tests.unit.deployment.images.fixtures import (
    fixture_sources,
    fixture_tree,
)
from tests.unit.deployment.provider.test_contract import records
from tests.unit.deployment.test_composition import product
from .fixtures import frontend_projection, reference, resources, tls_projection


ROOT = Path(__file__).resolve().parents[4]
REVISION = "a" * 40


class AdmissionFixture:
    """Use actual Control, product config, StateStores and catalog locks."""

    def __init__(
        self, test, *, candidate_state=False, cold=False, mismatch=None
    ):
        self.test, self.stack = test, ExitStack()
        test.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="provider-admission-")
        )
        self.root = Path(temporary)
        self.root.chmod(0o700)
        self.repo = self.directory("source")
        self.calls, self.dirty = [], False
        self.config = replace(
            product(),
            state_root=self.root / "z-provider",
            api_model_root=self.directory("model-api"),
            embedding_model_root=self.directory("model-embedding"),
            reranker_model_root=self.directory("model-reranker"),
        )
        self.sources, self.tree, self.plan = {}, {}, None
        self.profile = None
        self.configurations, self.catalogs, self.config_files = {}, [], {}
        self.add_state(self.config)
        self.products = [self.add_catalog(name) for name in ("b", "a")]
        self.builds = [
            self.add_build(name, cfg)
            for name, cfg in zip(("b", "a"), self.products)
        ]
        if candidate_state:
            self.add_candidate()
        if cold:
            self.cold_sources()
        self.control_file = self.root / "control.json"
        self.write(
            self.control_file,
            {"schema_version": 1, "state_root": str(self.root / "control")},
        )
        self.install(mismatch)
        self.forbidden = Mock(side_effect=AssertionError("default Git denied"))
        self.stack.enter_context(
            patch.object(source_identity, "_runner", self.forbidden)
        )
        self.stack.enter_context(
            patch.object(source_bundle, "_runner", self.git)
        )
        self.control = self.stack.enter_context(
            authority.open_control(self.control_file)
        )

    def directory(self, name):
        path = self.root / name
        path.mkdir(mode=0o700)
        return path

    def write(self, path, value):
        path.write_bytes(private_io.canonical(value))
        path.chmod(0o600)

    def add_state(self, config):
        with state.locked_state(config, create=True):
            pass
        filename = self.root / (config.state_root.name + ".json")
        self.write(filename, config.to_dict())
        self.configurations[config.state_root] = config
        self.config_files[config.state_root] = filename
        return config

    def add_catalog(self, name):
        config = self.add_state(
            replace(self.config, state_root=self.root / (name + "-catalog"))
        )
        data = config.state_root / "data"
        data.mkdir(mode=0o700)
        native = data / "state"
        native.mkdir(mode=0o700)
        lock = native / "operations.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        with state.locked_state(config) as store:
            store.write("index-candidate.json", {"data_root": str(data)})
        self.catalogs.append(data)
        return config

    def add_build(self, name, config):
        return self.add_state(
            replace(config, state_root=self.root / (name + "-build"))
        )

    def add_candidate(self):
        selected = self.add_catalog("c")
        root = self.directory("candidate")
        (root / "builds").mkdir(mode=0o700)
        self.profile = {
            "state_root": root,
            "catalog_config": self.config_files[selected.state_root],
            "catalog_source": self.repo,
            "_candidate_record": {"revision": "b" * 40},
        }
        self.add_state(
            replace(
                selected,
                state_root=candidate.build_root(self.profile, "b" * 40),
            )
        )

    def cold_sources(self):
        self.sources = fixture_sources()
        for name in policy.SOURCE_FILES:
            raw = (ROOT / name).read_bytes()
            self.sources[name] = raw
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        self.tree = fixture_tree(self.sources)
        self.plan = source_bundle.plan_source_bundle(
            REVISION, self.tree, self.sources
        )

    def install(self, mismatch):
        anchor, original, roles = records()
        anchor["source"] = {"root": str(self.repo), "revision": REVISION}
        anchor["configuration"] = backend.reference(
            self.config_files[self.config.state_root]
        )
        if self.plan is not None:
            anchor["source_plan_sha256"] = contract.digest(self.plan)
            anchor["policy"] = policy.validate_shared_request_policy(
                self.repo, self.repo
            )
        if mismatch == "source_plan":
            anchor["source_plan_sha256"] = "0" * 64
        if mismatch == "policy":
            anchor["policy"] = records()[0]["policy"]
        self.anchor = anchor
        self.backends = []
        with authority.open_control(self.control_file, create=True) as control:
            provider_sha = control.publish("provider", anchor)
            for index, config in enumerate(self.products):
                value = copy.deepcopy(original)
                value.update(
                    source=dict(anchor["source"]),
                    catalog_source=dict(anchor["source"]),
                    configuration=backend.reference(
                        self.config_files[config.state_root]
                    ),
                    build_state_root=str(self.builds[index].state_root),
                    provider_sha256=provider_sha,
                )
                value["compose"]["name"] = (
                    "depositar-ai-release-"
                    + REVISION[:12]
                    + "-"
                    + format(index + 1, "012x")
                )
                name = control.publish("backend", value)
                self.backends.append((name, value))
            roles.update(
                provider_sha256=provider_sha,
                active_backend_sha256=self.backends[0][0],
                rollback_backend_sha256=None,
            )
            control.adopt_roles(roles)
            previous = dict(roles)
            roles = {
                **roles,
                "epoch": roles["epoch"] + 1,
                "rollback_backend_sha256": self.backends[1][0],
            }
            control._compare_and_swap_roles(
                roles,
                expected_epoch=previous["epoch"],
                expected_sha256=contract.digest(previous),
            )
        self.roles = roles

    def git(self, argv, *, timeout):
        self.test.assertTrue(0 < timeout <= 15)
        self.test.assertEqual(Path(argv[argv.index("-C") + 1]), self.repo)
        self.calls.append(list(argv))
        if "rev-parse" in argv:
            value = REVISION.encode()
        elif "status" in argv:
            value = b" M search/scope.py" if self.dirty else b""
        elif "ls-tree" in argv:
            value = self.git_tree()
        elif "show" in argv:
            revision, name = argv[-1].split(":", 1)
            self.test.assertEqual(revision, REVISION)
            value = self.sources[name]
        else:
            raise AssertionError("unexpected fixture Git command")
        return SimpleNamespace(returncode=0, stdout=value, stderr=b"")

    def git_tree(self):
        return b"".join(
            (f"{row['mode']} {row['type']} {row['object']}\t{name}\0").encode()
            for name, row in sorted(self.tree.items())
        )

    def git_text(self, argv, *, timeout):
        result = self.git(argv, timeout=timeout)
        return SimpleNamespace(returncode=0, stdout=result.stdout.decode())

    def provider(self, stack, *, operation=None):
        return backend.TransitionProvider(
            self.control,
            stack,
            time.monotonic() + 1800,
            operation=operation,
            profile=self.profile,
        )

    def retire_intent(self):
        control = self.control
        a_sha, a = self.backends[0]
        b_sha, b = self.backends[1]
        before = {
            "backend": reference(
                control.root / ("backend-" + a_sha + ".json"), a_sha
            ),
            "tls": tls_projection(a, self.roles["tls_plan_ref"]),
            "frontend": frontend_projection(REVISION),
        }
        return {
            "schema_version": 1,
            "kind": "backend-code-retire",
            "operation_id": "a" * 32,
            "operator": dict(self.anchor["source"]),
            "authority": {
                "control_config": backend.reference(self.control_file),
                "control_root": str(control.root),
                "owner_uid": os.geteuid(),
                "namespace_sha256": journal.namespace_identity(control),
                "backend_peer_sha256": "7" * 64,
            },
            "predecessor_roles": copy.deepcopy(self.roles),
            "predecessor_roles_sha256": contract.digest(self.roles),
            "candidate": None,
            "sides": {"predecessor": before, "target": copy.deepcopy(before)},
            "frontend_owner": {
                "transport_profile": reference(self.root / "transport.json"),
                "participant_config": reference(
                    self.root / "participant.json"
                ),
                "source": {
                    "root": str(self.root / "vm"),
                    "revision": "d" * 40,
                },
                "entry_sha256": "8" * 64,
                "peer_sha256": "9" * 64,
            },
            "limits": dict(contract.LIMITS),
            "retirement": {
                "decision": reference(self.root / "retirement.json"),
                "backend": reference(
                    control.root / ("backend-" + b_sha + ".json"), b_sha
                ),
                "dispose": resources(b),
                "retain": {"paths": [], "network_ids": [], "records": []},
            },
        }


class ProviderAdmissionTests(unittest.TestCase):
    @contextmanager
    def lock_order(self):
        events = []
        product_lock = state.locked_state
        catalog_lock = execution.operation_lock

        @contextmanager
        def product_owner(config, **kwargs):
            with product_lock(config, **kwargs) as store:
                events.append(("product", str(config.state_root)))
                yield store

        @contextmanager
        def catalog_owner(context):
            with catalog_lock(context):
                events.append(("catalog", str(context["data"])))
                yield

        with (
            patch.object(state, "locked_state", product_owner),
            patch.object(execution, "operation_lock", catalog_owner),
        ):
            yield events

    def assert_unlocked(self, fixture):
        for config in fixture.configurations.values():
            with state.locked_state(config):
                pass
        for data in fixture.catalogs:
            with execution.operation_lock({"data": data}):
                pass

    def test_real_constructor_locks_all_product_states_before_catalogs(self):
        fixture = AdmissionFixture(self, candidate_state=True)
        before = (fixture.root / "control/roles.json").read_bytes()
        with self.lock_order() as events:
            with ExitStack() as stack:
                owner = fixture.provider(stack)
                self.assertIsInstance(owner, runtime.Provider)
                self.assertIsInstance(owner.control, authority.Control)
                owner._lock_states()
                self.assertEqual(
                    set(owner.stores), set(fixture.configurations)
                )
                for store in owner.stores.values():
                    self.assertIsInstance(store, state.StateStore)
                for config in fixture.configurations.values():
                    with self.assertRaisesRegex(ValueError, "operation_busy"):
                        with state.locked_state(config):
                            self.fail("real product lock must be held")
                for data in fixture.catalogs:
                    with self.assertRaises(BlockingIOError):
                        with execution.operation_lock({"data": data}):
                            self.fail("real catalog lock must be held")
        expected = [
            ("product", str(root))
            for root in sorted(fixture.configurations, key=str)
        ] + [
            ("catalog", str(root))
            for root in sorted(fixture.catalogs, key=str)
        ]
        self.assertEqual(events, expected)
        self.assertEqual(
            (fixture.root / "control/roles.json").read_bytes(), before
        )
        fixture.forbidden.assert_not_called()
        self.assert_unlocked(fixture)

    def test_config_content_identity_drift_rejects_real_constructor(self):
        fixture = AdmissionFixture(self)
        filename = fixture.config_files[fixture.config.state_root]
        fixture.write(
            filename, fixture.config.to_dict() | {"extra": "private"}
        )
        with ExitStack() as stack:
            with self.assertRaisesRegex(ValueError, "authority_changed"):
                fixture.provider(stack)
        self.assert_unlocked(fixture)
        fixture.forbidden.assert_not_called()

    def test_dirty_source_rejects_before_config_or_state_locks(self):
        fixture = AdmissionFixture(self)
        fixture.dirty = True
        with self.lock_order() as events:
            with ExitStack() as stack:
                with self.assertRaisesRegex(
                    ValueError, "product_source_dirty"
                ):
                    fixture.provider(stack)
        self.assertEqual(events, [])
        self.assertEqual(len(fixture.calls), 2)
        self.assert_unlocked(fixture)
        fixture.forbidden.assert_not_called()

    def test_marker_failure_releases_previously_acquired_real_locks(self):
        fixture = AdmissionFixture(self)
        marker = fixture.config.state_root / "product.json"
        original = marker.read_bytes()
        fixture.write(marker, {"invalid": "private"})
        with ExitStack() as stack:
            owner = fixture.provider(stack)
            with self.assertRaisesRegex(ValueError, "configuration_mismatch"):
                owner._lock_states()
        marker.write_bytes(original)
        self.assert_unlocked(fixture)
        fixture.forbidden.assert_not_called()

    def test_candidate_state_nested_in_provider_state_is_rejected(self):
        fixture = AdmissionFixture(self, candidate_state=True)
        nested = fixture.config.state_root / "nested"
        nested.mkdir(mode=0o700)
        path = fixture.root / "nested-config.json"
        fixture.write(
            path, replace(fixture.config, state_root=nested).to_dict()
        )
        fixture.profile["catalog_config"] = path
        with self.lock_order() as events:
            with ExitStack() as stack:
                owner = fixture.provider(stack)
                with self.assertRaisesRegex(ValueError, "state_overlap"):
                    owner._lock_states()
        self.assertEqual(events, [])
        self.assert_unlocked(fixture)

    def test_pending_constructor_keeps_original_e_and_rejects_role_relabel(
        self,
    ):
        fixture = AdmissionFixture(self)
        operation = journal.create(fixture.control, fixture.retire_intent())
        original = copy.deepcopy(fixture.control.roles)
        fixture.control._compare_and_swap_roles(
            contract.pending_roles(operation.intent),
            expected_epoch=original["epoch"],
            expected_sha256=contract.digest(original),
        )
        pending_bytes = (fixture.root / "control/roles.json").read_bytes()
        with ExitStack() as stack:
            owner = fixture.provider(stack, operation=operation)
            self.assertEqual(owner.roles, original)
            self.assertEqual(owner.roles_sha256, contract.digest(original))
            owner._roles_admission()
            owner._lock_states()
            owner.roles["epoch"] += 1
            with self.assertRaisesRegex(ValueError, "cas_conflict"):
                owner._roles_admission()
        self.assertEqual(
            (fixture.root / "control/roles.json").read_bytes(), pending_bytes
        )
        self.assert_unlocked(fixture)

    def test_real_cold_load_reaches_and_rejects_missing_pinned_model_files(
        self,
    ):
        fixture = AdmissionFixture(self, cold=True)
        before = (fixture.root / "control/roles.json").read_bytes()
        with patch.object(source_identity, "_runner", fixture.git_text):
            with ExitStack() as stack:
                owner = runtime.Provider(
                    fixture.control, stack, time.monotonic() + 1800
                )
                with self.assertRaises(FileNotFoundError) as error:
                    owner.load()
                self.assertIn("manifests", str(error.exception))
                self.assertEqual(owner.source_plan, fixture.plan)
                self.assertIsInstance(owner.store_native, state.StateStore)
                self.assertFalse(hasattr(owner, "docker"))
        self.assertEqual(
            (fixture.root / "control/roles.json").read_bytes(), before
        )
        self.assert_unlocked(fixture)

    def test_cold_source_plan_and_policy_digest_must_match_before_models(self):
        for mismatch in ("source_plan", "policy"):
            with self.subTest(mismatch=mismatch):
                fixture = AdmissionFixture(self, cold=True, mismatch=mismatch)
                with patch.object(
                    source_identity, "_runner", fixture.git_text
                ):
                    with ExitStack() as stack:
                        owner = runtime.Provider(
                            fixture.control, stack, time.monotonic() + 1800
                        )
                        with self.assertRaisesRegex(
                            ValueError, "authority_changed"
                        ):
                            owner.load()
                        self.assertEqual(owner.source_plan, fixture.plan)
                        self.assertFalse(hasattr(owner, "models"))
                        self.assertFalse(hasattr(owner, "docker"))
                self.assert_unlocked(fixture)
