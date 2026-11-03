"""CPU-only shared-backend ownership, locking and publication regressions."""

from contextlib import ExitStack, contextmanager
import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest import mock

from search.operations.config import GPUConfig, ProductConfig
from search.operations.deployment.candidate import controller
from search.operations.deployment.candidate import policy as request_policy
from search.operations.deployment.release import runtime
from tests.unit.deployment.release.test_runtime import (
    evidence as release_evidence,
)


ROOT = Path(__file__).resolve().parents[4]
REVISION = "a" * 40


class FakeDocker:
    """Inspect genuine fixtures; mutations affect only these in-memory
    objects.
    """

    def __init__(self, values, *, created=True):
        self.seed_rows = copy.deepcopy(values["containers"])
        self.seed_network = copy.deepcopy(values["networks"][0])
        self.rows = copy.deepcopy(self.seed_rows) if created else []
        self.networks = [copy.deepcopy(self.seed_network)] if created else []
        self.production_row = copy.deepcopy(self.seed_rows[0])
        self.production_row["Id"] = "9" * 64
        self.production_row["Config"]["Labels"].update(
            {
                "com.docker.compose.project": "production",
                "com.docker.compose.project.config_files": (
                    "/production/compose.json"
                ),
                "com.docker.compose.project.working_dir": "/production",
            }
        )
        self.production_network = {
            "Id": "8" * 64,
            "Name": "production_website",
            "Containers": {"9" * 64: {}},
            "Labels": {"com.docker.compose.project": "production"},
        }
        self.rows.append(copy.deepcopy(self.production_row))
        self.networks.append(copy.deepcopy(self.production_network))
        self.calls = []
        self.mutations = []
        self.fail_removal = None
        self.wrong_network_recheck = False

    @staticmethod
    def encoded(value):
        return json.dumps(value).encode()

    def call(self, arguments, *, timeout=30):
        args = list(arguments)
        self.calls.append(args)
        if args[0] == "ps":
            project = args[-1].split("label=com.docker.compose.project=", 1)[1]
            identifiers = [
                r["Id"]
                for r in self.rows
                if r["Config"]["Labels"]["com.docker.compose.project"]
                == project
            ]
            return (
                "\n".join(identifiers) + ("\n" if identifiers else "")
            ).encode()
        if args[:2] == ["network", "ls"]:
            prefix = args[-1].removeprefix("name=")
            return b"\n".join(
                self.encoded({"Name": n["Name"]})
                for n in self.networks
                if n["Name"].startswith(prefix)
            )
        if args[0] == "inspect":
            rows = [
                r
                for identifier in args[1:]
                for r in self.rows
                if r["Id"] == identifier
            ]
            if len(rows) != len(args) - 1:
                raise ValueError("missing fake container")
            return self.encoded(rows)
        if args[:2] == ["network", "inspect"]:
            rows = [
                n for n in self.networks if args[2] in {n["Id"], n["Name"]}
            ]
            if len(rows) != 1:
                raise ValueError("missing fake network")
            if (
                self.wrong_network_recheck
                and args[2] == self.seed_network["Id"]
            ):
                changed = copy.deepcopy(rows)
                changed[0]["Id"] = "e" * 64
                return self.encoded(changed)
            return self.encoded(rows)
        if args[:2] == ["rm", "--force"]:
            if args[2] == self.fail_removal:
                raise OSError("simulated interrupted cleanup")
            self.mutations.append(args)
            self.rows[:] = [r for r in self.rows if r["Id"] != args[2]]
            for network in self.networks:
                network["Containers"].pop(args[2], None)
            return b""
        if args[:2] == ["network", "rm"]:
            self.mutations.append(args)
            self.networks[:] = [n for n in self.networks if n["Id"] != args[2]]
            return b""
        if args[0] == "compose" and "create" in args:
            self.mutations.append(args)
            self.rows.extend(copy.deepcopy(self.seed_rows))
            self.networks.append(copy.deepcopy(self.seed_network))
            return b""
        if args[0] == "compose" and "start" in args:
            self.mutations.append(args)
            return b""
        raise AssertionError(f"unexpected Docker operation: {args}")

    def assert_production_unchanged(self, test):
        test.assertIn(self.production_row, self.rows)
        test.assertIn(self.production_network, self.networks)
        test.assertFalse(
            any("9" * 64 in c or "8" * 64 in c for c in self.mutations)
        )


class FakeControl:
    def __init__(self, root, source, config):
        self.root = root
        self.provider = {
            "source": {"root": str(source), "revision": "b" * 40},
            "configuration": {
                "path": str(config),
                "sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
            },
        }
        self.provider_sha256 = "b" * 64
        self.roles_sha256 = "e" * 64
        self.roles = {"epoch": 1, "rollback_backend_sha256": None}
        self.checked = []
        self.protected = {
            "container_ids": set(),
            "network_ids": set(),
            "owned_paths": set(),
        }

    def refresh(self):
        pass

    def require_disposable(self, resources):
        self.checked.append(copy.deepcopy(resources))
        for kind in ("container_ids", "network_ids"):
            if self.protected[kind] & set(resources[kind]):
                raise controller.ProviderError(
                    "shared_backend_provider_backend_protected"
                )
        for raw in resources["owned_paths"]:
            target = Path(raw)
            for protected in self.protected["owned_paths"]:
                protected = Path(protected)
                if (
                    target == protected
                    or target in protected.parents
                    or protected in target.parents
                ):
                    raise controller.ProviderError(
                        "shared_backend_provider_backend_protected"
                    )


class SharedBackendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="shared-backend-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.state = self.root / "candidate-state"
        self.state.mkdir(mode=0o700)
        self.production = self.root / "production-source"
        self.source = self.root / "candidate-source"
        for source in (self.production, self.source):
            for relative in request_policy.SOURCE_FILES:
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
        roots = [
            self.root / name
            for name in (
                "production-state",
                "api-models",
                "embedding-models",
                "reranker-models",
            )
        ]
        for path in roots:
            path.mkdir(mode=0o700)
        self.product = ProductConfig(
            2,
            *roots,
            GPUConfig(
                "GPU-00000000-0000-4000-8000-000000000001",
                (
                    "GPU-00000001-0000-4000-8000-000000000001",
                    "GPU-00000002-0000-4000-8000-000000000001",
                ),
                "GPU-00000003-0000-4000-8000-000000000001",
            ),
        )
        self.production_config = self.root / "product.json"
        self.write_json(self.production_config, self.product.to_dict())
        self.control_root = self.root / "control-state"
        self.control_root.mkdir(mode=0o700)
        self.control_config = self.root / "control.json"
        self.write_json(
            self.control_config,
            {
                "schema_version": 1,
                "state_root": str(self.control_root),
            },
        )
        self.control = FakeControl(
            self.control_root, self.production, self.production_config
        )
        self.config = self.root / "shared.json"
        self.configuration = {
            "schema_version": 2,
            "state_root": str(self.state),
            "control_config": str(self.control_config),
            "catalog_config": str(self.production_config),
            "catalog_source": str(self.production),
        }
        self.write_json(self.config, self.configuration)
        self.profile = controller.load_config(self.config)
        self.profile["_control"] = self.control
        self.patch(
            "search.operations.deployment.candidate.controller."
            "provider_authority.open_control",
            side_effect=self.control_context,
        )
        self.values = release_evidence()
        compose_file = self.state / "compose.json"
        for row in self.values["containers"]:
            row["Config"]["Labels"].update(
                {
                    "com.docker.compose.project.config_files": str(
                        compose_file
                    ),
                    "com.docker.compose.project.working_dir": str(self.state),
                }
            )
            row["HostConfig"].update(PidMode="", IpcMode="private")
            for mount in row["Mounts"]:
                if mount["Type"] == "bind":
                    mount["Propagation"] = "rprivate"
        compose = self.values["compose"]
        website = compose["networks"]["website"]["name"]
        self.record = {
            "schema_version": 2,
            "phase": "running",
            "revision": REVISION,
            "compose": compose,
            "container_ids": {
                r["Config"]["Labels"]["com.docker.compose.service"]: r["Id"]
                for r in self.values["containers"]
            },
            "network_id": self.values["networks"][0]["Id"],
            "external_network_ids": {
                name: identifier
                for name, identifier in self.values["lease"][
                    "network_ids"
                ].items()
                if name != website
            },
            "build_complete": False,
            "lease": copy.deepcopy(self.values["lease"]),
            "provider_authority_sha256": self.control.provider_sha256,
            "roles_sha256": self.control.roles_sha256,
            "roles_epoch": self.control.roles["epoch"],
            "model_file_identity_sha256": "c" * 64,
            "compute_sha256": "d" * 64,
        }

    @contextmanager
    def control_context(self, path):
        self.assertEqual(path, self.control_config)
        yield self.control
        self.control.refresh()

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def patch(self, target, **options):
        return self.stack.enter_context(mock.patch(target, **options))

    def test_config_is_strict_private_and_disjoint_from_production(self):
        self.assertEqual(self.profile["state_root"], self.state)
        for change, error in [
            ({"schema_version": True}, "config_invalid"),
            ({"extra": "untrusted"}, "config_invalid"),
            ({"schema_version": 1}, "config_invalid"),
            (
                {"state_root": str(self.control_root)},
                "state_overlaps_production",
            ),
            (
                {"state_root": str(self.product.state_root)},
                "state_overlaps_production",
            ),
            (
                {"state_root": str(self.product.api_model_root / "candidate")},
                "state_overlaps_production",
            ),
            ({"state_root": "relative"}, "path_invalid"),
        ]:
            with self.subTest(change=change):
                self.write_json(self.config, self.configuration | change)
                with self.assertRaisesRegex(
                    controller.SharedBackendError, error
                ):
                    controller.load_config(self.config)
        self.write_json(self.config, self.configuration)
        self.config.chmod(0o644)
        with self.assertRaises(ValueError):
            controller.load_config(self.config)

    def test_config_rejects_symlink_and_git_contained_journal(self):
        alias = self.root / "state-alias"
        alias.symlink_to(self.state, target_is_directory=True)
        self.write_json(
            self.config, self.configuration | {"state_root": str(alias)}
        )
        with self.assertRaisesRegex(
            controller.SharedBackendError, "path_invalid"
        ):
            controller.load_config(self.config)
        (self.source / ".git").mkdir()
        self.write_json(
            self.config,
            self.configuration
            | {"state_root": str(self.source / "private-state")},
        )
        with self.assertRaisesRegex(
            controller.SharedBackendError, "state_inside_git"
        ):
            controller.load_config(self.config)

    def test_catalog_config_and_source_are_required_and_control_is_disjoint(
        self,
    ):
        for key in ("catalog_config", "catalog_source", "control_config"):
            value = self.configuration.copy()
            del value[key]
            self.write_json(self.config, value)
            with (
                self.subTest(missing=key),
                self.assertRaisesRegex(
                    controller.SharedBackendError, "config_invalid"
                ),
            ):
                controller.load_config(self.config)
        self.write_json(self.config, self.configuration)
        self.write_json(
            self.control_config,
            {
                "schema_version": 1,
                "state_root": str(self.state / "nested-control"),
            },
        )
        with self.assertRaisesRegex(
            controller.SharedBackendError, "state_overlaps_production"
        ):
            controller.load_config(self.config)

    def test_catalog_or_embedding_drift_rejects_before_image_access(
        self,
    ):
        profile = controller.catalog.query_profile(ROOT)
        session = object.__new__(controller.Session)
        session.profile = self.profile
        images = mock.Mock()
        session.provider = SimpleNamespace(
            source_root=self.production, _verified_backend_images=images
        )
        session.context = {"query_profile": profile}
        session.query_context = {"query_profile": profile}
        session.query_catalog = SimpleNamespace(
            identity=lambda: {"selected": "B"}
        )
        record = {
            "revision": REVISION,
            "phase": "prepared",
            "policy": {},
            "catalog_binding": {"selected": "A"},
        }
        with (
            mock.patch.object(
                controller, "require_clean_revision", return_value=REVISION
            ),
            mock.patch.object(
                controller, "validate_shared_request_policy", return_value={}
            ),
            mock.patch.object(
                controller.catalog, "query_profile", return_value=profile
            ),
        ):
            with self.assertRaisesRegex(
                ValueError, "catalog_selection_changed"
            ):
                session.candidate_inputs(record)
            record["catalog_binding"] = {"selected": "B"}
            for field in (
                "MODEL",
                "MODEL_DIGEST",
                "DIMENSIONS",
                "VERSION",
                "VECTOR_PATH",
            ):
                with self.subTest(field=field):
                    session.query_context = {
                        "query_profile": profile | {field: "different"}
                    }
                    with self.assertRaisesRegex(
                        ValueError, "query_profile_changed"
                    ):
                        session.candidate_inputs(record)
            images.assert_not_called()

    def test_attach_keeps_selected_catalog_and_provider_distinct(self):
        session = object.__new__(controller.Session)
        active_image_binding = object()
        active_metadata = {
            "record": {"canonical": "A"},
            "config": object(),
            "config_file": object(),
            "key": object(),
        }
        active = SimpleNamespace(
            context={"catalog": "A"},
            authority={"authority": "A"},
            backend_sha256="9" * 64,
            namespace_sha256="8" * 64,
            image_binding=active_image_binding,
            **active_metadata,
        )
        session.profile = self.profile
        session.control = self.control
        session.provider = SimpleNamespace(
            backends=[active], inference_images={"provider": "P"}
        )
        session.query_context = {"catalog": "B"}
        session.query_authority = {"authority": "B"}
        session.query_catalog = SimpleNamespace(
            images={"core": "B"},
            inspections={"core_image": "B"},
            config=object(),
            config_file=object(),
            key=object(),
        )
        session._candidate_image_binding = object()
        session.candidate_inputs = lambda _record: ({"cpu": "B"}, {}, {})
        record = {
            "revision": REVISION,
            "provider_authority_sha256": self.control.provider_sha256,
            "roles_sha256": self.control.roles_sha256,
            "roles_epoch": self.control.roles["epoch"],
            "compose": {"cpu": "B"},
            "lease": {"catalog": "B"},
        }
        session.attach_record(record)
        self.assertIs(session.candidate.context, session.query_context)
        self.assertIs(session.candidate.authority, session.query_authority)
        self.assertIs(session.candidate.catalog, session.query_catalog)
        self.assertIs(
            session.candidate.image_binding, session._candidate_image_binding
        )
        self.assertIsNot(session.candidate.image_binding, active.image_binding)
        self.assertIs(active.image_binding, active_image_binding)
        for field in ("config", "config_file", "key"):
            self.assertIs(
                getattr(session.candidate, field),
                getattr(session.query_catalog, field),
            )
        for field, value in active_metadata.items():
            self.assertIs(getattr(active, field), value)
        self.assertEqual(active.context, {"catalog": "A"})
        self.assertEqual(active.authority, {"authority": "A"})
        self.assertIsNone(session.candidate.record)
        self.assertIsNone(session.candidate.backend_sha256)
        self.assertIsNone(session.candidate.namespace_sha256)
        self.assertEqual(session.candidate.source_revision, REVISION)
        self.assertEqual(
            session.candidate.source_root,
            controller.frozen_source(self.profile, REVISION),
        )

    def live_catalog_fd(self, *, final_error=None):
        events, descriptors = [], []
        baseline = SimpleNamespace(context={"plan": {"project": "A"}})
        selected = SimpleNamespace(
            context={"plan": {"project": "B"}},
            authority={},
            refresh=lambda: os.fstat(descriptors[0]),
        )

        def refresh():
            os.fstat(descriptors[0])
            events.append("provider_refresh")

        provider = SimpleNamespace(
            config=self.product,
            backends=[SimpleNamespace(catalog=baseline)],
            docker=object(),
            refresh=refresh,
        )

        @contextmanager
        def open_provider(control):
            self.assertIs(control, self.control)
            with ExitStack() as stack:
                provider.stack = stack
                try:
                    yield provider
                finally:
                    refresh()
                    events.append("provider_final_verified")
                    if final_error is not None:
                        raise final_error

        def select(profile, stack, config, bindings, docker):
            self.assertIs(profile, self.profile)
            self.assertIs(config, self.product)
            self.assertEqual(bindings, [baseline])
            self.assertIs(docker, provider.docker)
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            descriptors.append(descriptor)

            def close():
                os.close(descriptor)
                events.append("selected_closed")

            stack.callback(close)
            return selected

        self.patch(
            "search.operations.deployment.candidate.controller."
            "provider_runtime.open_provider",
            side_effect=open_provider,
        )
        self.patch(
            "search.operations.deployment.candidate.controller.catalog.select",
            side_effect=select,
        )
        return selected, descriptors, events

    def test_live_keeps_selected_fd_until_provider_final_refresh_completes(
        self,
    ):
        selected, descriptors, events = self.live_catalog_fd()
        with controller.live(self.profile) as session:
            self.assertIs(session.query_catalog, selected)
            os.fstat(descriptors[0])
        self.assertLess(
            events.index("provider_final_verified"),
            events.index("selected_closed"),
        )
        self.assertEqual(events.count("selected_closed"), 1)
        with self.assertRaises(OSError) as caught:
            os.fstat(descriptors[0])
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_live_final_refresh_failure_closes_selected_fd_without_success(
        self,
    ):
        error = controller.ProviderError(
            "shared_backend_provider_authority_changed"
        )
        _, descriptors, events = self.live_catalog_fd(final_error=error)
        with self.assertRaisesRegex(
            controller.ProviderError, "authority_changed"
        ) as caught:
            with controller.live(self.profile):
                os.fstat(descriptors[0])
            events.append("returned")
        self.assertIs(caught.exception, error)
        self.assertNotIn("returned", events)
        self.assertLess(
            events.index("provider_final_verified"),
            events.index("selected_closed"),
        )
        self.assertEqual(events.count("selected_closed"), 1)
        with self.assertRaises(OSError) as closed:
            os.fstat(descriptors[0])
        self.assertEqual(closed.exception.errno, errno.EBADF)

    def observation_session(self):
        baseline = SimpleNamespace(context={"plan": {"project": "A"}})
        selected = SimpleNamespace(
            context={"plan": {"project": "B"}}, authority={}
        )
        snapshot = {
            "catalog_snapshots": {
                "A": ({}, [], {}),
                "B": ({"B": True}, [], {}),
            }
        }
        provider = SimpleNamespace(
            config=self.product,
            backends=[SimpleNamespace(catalog=baseline)],
            docker=object(),
            observe=mock.Mock(return_value=snapshot),
            _checkpoint_authorities=mock.Mock(),
        )
        control = SimpleNamespace(refresh=mock.Mock())
        calls = mock.Mock()
        calls.attach_mock(provider.observe, "runtime")
        calls.attach_mock(provider._checkpoint_authorities, "metadata")
        calls.attach_mock(control.refresh, "control")
        session = controller.Session(self.profile, provider, control, selected)
        return session, calls, snapshot

    def test_real_observe_checks_runtime_then_metadata_before_returning(self):
        session, calls, snapshot = self.observation_session()
        result = session.observe()
        calls.returned()
        self.assertEqual(
            calls.mock_calls,
            [
                mock.call.runtime(
                    session.provider.backends,
                    [session.baseline_catalog, session.query_catalog],
                ),
                mock.call.metadata(),
                mock.call.control(),
                mock.call.returned(),
            ],
        )
        self.assertIs(result, snapshot["catalog_snapshots"]["A"])

    def test_real_observe_metadata_failure_never_publishes_a_snapshot(self):
        session, calls, _ = self.observation_session()
        session.catalog_snapshots = mock.sentinel.unpublished
        session.provider._checkpoint_authorities.side_effect = (
            controller.ProviderError(
                "shared_backend_provider_authority_changed"
            )
        )
        with self.assertRaisesRegex(
            controller.ProviderError, "authority_changed"
        ):
            session.observe()
            calls.returned()
        self.assertEqual(
            calls.mock_calls,
            [
                mock.call.runtime(
                    session.provider.backends,
                    [session.baseline_catalog, session.query_catalog],
                ),
                mock.call.metadata(),
            ],
        )
        self.assertIs(session.catalog_snapshots, mock.sentinel.unpublished)

    def test_both_catalog_audits_finish_before_model_checkpoint_or_success(
        self,
    ):
        events = []
        bindings = [
            SimpleNamespace(
                context={"plan": {"project": name}},
                authority={"runtime": {}},
                audit=mock.Mock(
                    side_effect=lambda *_args,
                    name=name,
                    **_kwargs: events.append(name)
                ),
            )
            for name in ("A", "B")
        ]
        session = object.__new__(controller.Session)
        session.baseline_catalog, session.query_catalog = bindings
        session.query_context = bindings[1].context
        session.provider = SimpleNamespace(
            backends=[SimpleNamespace(catalog=bindings[0])]
        )
        session.remaining = lambda maximum: maximum
        session.catalog_snapshots = {name: ({}, [], {}) for name in ("A", "B")}
        with (
            mock.patch.object(
                session,
                "observe",
                side_effect=lambda: events.append("observe"),
            ),
            mock.patch.object(
                session,
                "check_models",
                create=True,
                side_effect=lambda *_args: events.append("models"),
            ),
            mock.patch(
                "search.operations.catalog.connection.snapshot_peers",
                return_value={},
            ),
        ):
            session.verify({})
            self.assertEqual(
                events, ["observe", "A", "B", "observe", "models"]
            )
            events.clear()
            bindings[1].audit.side_effect = ValueError("B_changed")
            with self.assertRaisesRegex(ValueError, "B_changed"):
                session.verify({})
            self.assertEqual(events, ["observe", "A"])

    def test_real_flock_rejects_another_process_and_releases_after_exit(self):
        program = """import fcntl,os,sys
fd=os.open(sys.argv[1],os.O_RDWR)
try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:sys.exit(73)
finally:os.close(fd)
"""

        def contend():
            return subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    program,
                    str(self.state / "operations.lock"),
                ],
                capture_output=True,
                timeout=5,
            ).returncode

        with controller.journal(self.state):
            self.assertEqual(contend(), 73)
            with self.assertRaisesRegex(
                controller.SharedBackendError, "shared_backend_busy"
            ):
                with controller.journal(self.state):
                    self.fail("a second operator acquired the journal")
        self.assertEqual(contend(), 0)

    def test_journal_rejects_linked_lock_without_touching_target(self):
        target = self.root / "other-lock"
        target.write_text("unchanged")
        target.chmod(0o600)
        lock = self.state / "operations.lock"
        for symbolic in (True, False):
            with self.subTest(symbolic=symbolic):
                if symbolic:
                    lock.symlink_to(target)
                else:
                    os.link(target, lock)
                with self.assertRaises((OSError, ValueError)):
                    with controller.journal(self.state):
                        self.fail("linked lock was accepted")
                self.assertEqual(target.read_text(), "unchanged")
                lock.unlink()

    def test_incompatible_policy_fails_before_worktree_build_or_live_access(
        self,
    ):
        model_policy = self.source / "search/model_policy.py"
        model_policy.write_text(
            model_policy.read_text().replace(
                "SCOPE_NUM_CTX = 4_096", "SCOPE_NUM_CTX = 8_192"
            )
        )
        self.patch(
            "search.operations.deployment.candidate.controller.require_clean_revision",
            side_effect=lambda source: (
                "b" * 40 if source == self.production else REVISION
            ),
        )
        git = self.patch(
            "search.operations.deployment.candidate.controller.git",
            side_effect=AssertionError("must not create a worktree"),
        )
        build = self.patch(
            "search.operations.deployment.candidate.controller.lifecycle.build_images",
            side_effect=AssertionError("must not build"),
        )
        live = self.patch(
            "search.operations.deployment.candidate.controller.live",
            side_effect=AssertionError("must not inspect live services"),
        )
        with controller.journal(self.state) as store:
            with self.assertRaises(request_policy.SharedRequestPolicyError):
                controller.prepare(self.profile, store, self.source)
            self.assertIsNone(controller.current(store))
        git.assert_not_called()
        build.assert_not_called()
        live.assert_not_called()

    def test_changed_clean_production_head_is_rejected_before_catalog_audit(
        self,
    ):
        binding = SimpleNamespace(
            context={"plan": {"project": "native"}},
            audit=mock.Mock(
                side_effect=controller.catalog.SharedCatalogError(
                    "shared_backend_catalog_source_changed"
                )
            ),
        )
        session = object.__new__(controller.Session)
        session.baseline_catalog = session.query_catalog = binding
        session.query_context = binding.context
        session.provider = SimpleNamespace(
            backends=[SimpleNamespace(catalog=binding)]
        )
        session.catalog_snapshots = {"native": ({}, [], {})}
        session.remaining = lambda maximum: maximum
        with (
            mock.patch.object(session, "observe") as observe,
            mock.patch.object(controller.catalog.image_build, "_run") as run,
        ):
            with self.assertRaisesRegex(
                controller.catalog.SharedCatalogError, "source_changed"
            ):
                session.verify({})
        observe.assert_called_once_with()
        run.assert_not_called()

    def test_candidate_rows_use_real_capability_checks_and_sealed_launcher(
        self,
    ):
        docker = FakeDocker(self.values)
        rows, names = controller.candidate_rows(
            docker, self.record, self.state / "compose.json"
        )
        self.assertEqual(
            {r["Id"] for r in rows}, set(self.record["container_ids"].values())
        )
        self.assertEqual(names, {self.values["networks"][0]["Name"]})
        self.assertEqual(docker.mutations, [])
        for drift in ("config", "working_dir", "id", "gpu", "network"):
            with self.subTest(drift=drift):
                changed = FakeDocker(self.values)
                row = changed.rows[0]
                if drift == "config":
                    row["Config"]["Labels"][
                        "com.docker.compose.project.config_files"
                    ] = "/foreign/same-sha/compose.json"
                elif drift == "working_dir":
                    row["Config"]["Labels"][
                        "com.docker.compose.project.working_dir"
                    ] = "/foreign"
                elif drift == "id":
                    row["Id"] = "f" * 64
                elif drift == "gpu":
                    row["HostConfig"]["DeviceRequests"] = [
                        {"Driver": "nvidia"}
                    ]
                else:
                    website = self.values["networks"][0]["Name"]
                    row["NetworkSettings"]["Networks"][website][
                        "NetworkID"
                    ] = "f" * 64
                with self.assertRaises(ValueError):
                    controller.candidate_rows(
                        changed, self.record, self.state / "compose.json"
                    )
                self.assertEqual(changed.mutations, [])

    def invoke_stop(self, docker, record=None):
        with (
            mock.patch(
                "search.operations.deployment.runtime_docker.Docker",
                return_value=docker,
            ),
            controller.journal(self.state) as store,
        ):
            return controller.stop(
                self.profile, store, self.record if record is None else record
            )

    def test_stop_removes_only_exact_sealed_candidate_resources(self):
        docker = FakeDocker(self.values)
        result = self.invoke_stop(docker)
        self.assertEqual(result["status"], "stopped")
        removed = {
            c[2] for c in docker.mutations if c[:2] == ["rm", "--force"]
        }
        self.assertEqual(removed, set(self.record["container_ids"].values()))
        self.assertEqual(
            [c for c in docker.mutations if c[:2] == ["network", "rm"]],
            [["network", "rm", self.record["network_id"]]],
        )
        docker.assert_production_unchanged(self)

    def test_foreign_same_sha_unknown_peer_or_network_blocks_deletion(
        self,
    ):
        for drift in ("same_sha", "foreign_peer", "network_id"):
            with self.subTest(drift=drift):
                docker = FakeDocker(self.values)
                if drift == "same_sha":
                    docker.rows[0]["Config"]["Labels"][
                        "com.docker.compose.project.config_files"
                    ] = "/foreign/compose.json"
                elif drift == "foreign_peer":
                    docker.networks[0]["Containers"]["f" * 64] = {}
                else:
                    docker.networks[0]["Id"] = "f" * 64
                with self.assertRaises(controller.SharedBackendError):
                    self.invoke_stop(docker, copy.deepcopy(self.record))
                self.assertEqual(docker.mutations, [])
                docker.assert_production_unchanged(self)

    def test_partial_cleanup_can_retry_without_adopting_missing_containers(
        self,
    ):
        docker = FakeDocker(self.values)
        identifiers = sorted(self.record["container_ids"].values())
        docker.fail_removal = identifiers[1]
        with self.assertRaises(OSError):
            self.invoke_stop(docker)
        self.assertEqual([c[2] for c in docker.mutations], [identifiers[0]])
        docker.fail_removal = None
        self.assertEqual(self.invoke_stop(docker)["status"], "stopped")
        removed = [
            c[2] for c in docker.mutations if c[:2] == ["rm", "--force"]
        ]
        self.assertEqual(sorted(removed), identifiers)
        docker.assert_production_unchanged(self)

    def test_network_identity_is_rechecked_before_removal(self):
        docker = FakeDocker(self.values)
        docker.wrong_network_recheck = True
        with self.assertRaisesRegex(
            controller.SharedBackendError, "candidate_network_changed"
        ):
            self.invoke_stop(docker)
        self.assertEqual(
            {c[2] for c in docker.mutations if c[:2] == ["rm", "--force"]},
            set(self.record["container_ids"].values()),
        )
        self.assertFalse(
            any(c[:2] == ["network", "rm"] for c in docker.mutations)
        )
        docker.assert_production_unchanged(self)

    def test_status_reports_incomplete_phases_but_query_requires_running(self):
        live = self.patch(
            "search.operations.deployment.candidate.controller.live",
            side_effect=AssertionError("not running"),
        )
        self.assertEqual(
            controller.operate(self.config, "status"), {"status": "empty"}
        )
        for phase in (
            "preparing",
            "prepared",
            "creating",
            "starting",
            "failed",
            "stopped",
        ):
            with self.subTest(phase=phase):
                with controller.journal(self.state) as store:
                    controller.save(store, self.record | {"phase": phase})
                self.assertEqual(
                    controller.operate(self.config, "status")["status"], phase
                )
                with self.assertRaisesRegex(
                    controller.SharedBackendError, "candidate_not_running"
                ):
                    controller.operate(self.config, "query", text="test")
        live.assert_not_called()

    def test_protected_resources_refuse_cleanup_before_any_mutation(self):
        source = controller.frozen_source(self.profile, REVISION)
        build = controller.build_root(self.profile, REVISION)
        for path in (source, build):
            path.mkdir(parents=True, mode=0o700)
            (path / "keep").write_text("retained")
        for kind, protected in (
            (
                "container_ids",
                next(iter(self.record["container_ids"].values())),
            ),
            ("network_ids", self.record["network_id"]),
            ("owned_paths", source),
            ("owned_paths", build),
        ):
            with self.subTest(kind=kind):
                self.control.protected[kind].add(protected)
                docker = FakeDocker(self.values)
                with (
                    mock.patch.object(controller, "git") as git,
                    mock.patch.object(controller.shutil, "rmtree") as remove,
                ):
                    with self.assertRaisesRegex(
                        controller.ProviderError, "backend_protected"
                    ):
                        self.invoke_stop(docker, copy.deepcopy(self.record))
                    git.assert_not_called()
                    remove.assert_not_called()
                self.assertEqual(docker.calls, [])
                self.assertEqual(docker.mutations, [])
                for path in (source, build):
                    self.assertEqual((path / "keep").read_text(), "retained")
                self.control.protected[kind].clear()

    def test_control_lock_wraps_the_entire_candidate_journal(self):
        events = []
        original = controller.journal

        @contextmanager
        def canonical(path):
            self.assertEqual(path, self.control_config)
            events.append("control_enter")
            try:
                yield self.control
            finally:
                events.append("control_exit")

        @contextmanager
        def journal(path):
            events.append("journal_enter")
            try:
                with original(path) as store:
                    yield store
            finally:
                events.append("journal_exit")

        with (
            mock.patch.object(
                controller.provider_authority,
                "open_control",
                side_effect=canonical,
            ),
            mock.patch.object(controller, "journal", side_effect=journal),
        ):
            self.assertEqual(
                controller.operate(self.config, "status"), {"status": "empty"}
            )
        self.assertEqual(
            events,
            ["control_enter", "journal_enter", "journal_exit", "control_exit"],
        )

    def test_missing_anchor_never_falls_back_to_native_runtime(self):
        with (
            mock.patch.object(
                controller.provider_authority,
                "open_control",
                side_effect=controller.ProviderError(
                    "shared_backend_provider_not_adopted"
                ),
            ),
            mock.patch.object(controller, "journal") as journal,
            mock.patch.object(controller, "live") as live,
        ):
            with self.assertRaisesRegex(
                controller.ProviderError, "not_adopted"
            ):
                controller.operate(self.config, "status")
            journal.assert_not_called()
            live.assert_not_called()

    def test_control_exit_failure_prevents_successful_status(self):
        @contextmanager
        def changed_control(_path):
            yield self.control
            raise controller.ProviderError(
                "shared_backend_provider_authority_changed"
            )

        with mock.patch.object(
            controller.provider_authority,
            "open_control",
            side_effect=changed_control,
        ):
            with self.assertRaisesRegex(
                controller.ProviderError, "authority_changed"
            ):
                controller.operate(self.config, "status")

    def test_provider_or_roles_drift_refuses_start_before_creation(self):
        for key, replacement in (
            ("provider_sha256", "7" * 64),
            ("roles_sha256", "8" * 64),
            ("epoch", 2),
        ):
            with self.subTest(key=key):
                record = copy.deepcopy(self.record)
                record.update(
                    phase="prepared",
                    container_ids={},
                    network_id=None,
                    policy={"policy": {}},
                )
                docker = FakeDocker(self.values, created=False)
                original = (
                    self.control.roles["epoch"]
                    if key == "epoch"
                    else getattr(self.control, key)
                )
                if key == "epoch":
                    self.control.roles["epoch"] = replacement
                else:
                    setattr(self.control, key, replacement)
                try:
                    with controller.journal(self.state) as store:
                        controller.save(store, record)
                        _, context = self.fake_session(docker, store, None)
                        with mock.patch.object(controller, "live", context):
                            with self.assertRaisesRegex(
                                controller.SharedBackendError,
                                "live_binding_changed",
                            ):
                                controller.start(self.profile, store, record)
                        self.assertEqual(controller.current(store), record)
                finally:
                    if key == "epoch":
                        self.control.roles["epoch"] = original
                    else:
                        setattr(self.control, key, original)
                self.assertEqual(docker.mutations, [])
                self.assertFalse((self.state / "compose.json").exists())

    def test_journal_epoch_has_exact_integer_and_signed_64_bit_bounds(self):
        with controller.journal(self.state) as store:
            for epoch in (1, 2**63 - 1):
                with self.subTest(valid_epoch=epoch):
                    record = self.record | {"roles_epoch": epoch}
                    controller.save(store, record)
                    self.assertEqual(controller.current(store), record)
            for epoch in (0, -1, 2**63, True, 1.0):
                with self.subTest(invalid_epoch=epoch):
                    controller.save(
                        store, self.record | {"roles_epoch": epoch}
                    )
                    with self.assertRaisesRegex(
                        controller.SharedBackendError, "journal_invalid"
                    ):
                        controller.current(store)

    def test_binding_epoch_refuses_coercion_and_overflow_even_when_equal(self):
        for epoch in (1, 2**63 - 1):
            with self.subTest(valid_epoch=epoch):
                control = SimpleNamespace(
                    provider_sha256=self.control.provider_sha256,
                    roles_sha256=self.control.roles_sha256,
                    roles={"epoch": epoch},
                )
                controller.require_binding(
                    control, self.record | {"roles_epoch": epoch}
                )
        for epoch in (0, -1, 2**63, True, 1.0):
            with self.subTest(invalid_equal_epoch=epoch):
                control = SimpleNamespace(
                    provider_sha256=self.control.provider_sha256,
                    roles_sha256=self.control.roles_sha256,
                    roles={"epoch": epoch},
                )
                with self.assertRaisesRegex(
                    controller.SharedBackendError, "live_binding_changed"
                ):
                    controller.require_binding(
                        control, self.record | {"roles_epoch": epoch}
                    )

    def recipe_session(self):
        from tests.unit.deployment.test_composition import catalog

        projection = controller.catalog.query_profile(ROOT)
        source = controller.frozen_source(self.profile, REVISION)
        source.parent.mkdir(parents=True, mode=0o700)
        shutil.copytree(self.source, source)
        for relative in (
            "search/catalog/embedding.py",
            "search/planning/index_filters.py",
        ):
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        contract = source / "search/reranker/contract.py"
        contract.write_text(contract.read_text() + "\n# CPU source update.\n")
        gpu = controller.composition._inference(
            self.product,
            "b" * 40,
            os.geteuid(),
            os.getegid(),
            inference_source_sha256="6" * 64,
        )
        gpu_images = {
            role: {"Id": "sha256:" + str(index) * 64}
            for index, role in enumerate(gpu["services"], 1)
        }
        for role, image in gpu_images.items():
            gpu["services"][role]["image"] = image["Id"]
        session = object.__new__(controller.Session)
        session.profile, session.config = self.profile, self.product
        session.provider = SimpleNamespace(
            source_root=self.production,
            inference_compose=gpu,
            inference_images=gpu_images,
            backends=[SimpleNamespace(compose={"name": "active-backend"})],
        )
        session.context = session.query_context = {
            "query_profile": projection,
            "plan": catalog(self.product, revision="b" * 40),
        }
        session.query_catalog = SimpleNamespace(config=self.product)
        session.docker = object()
        record = {
            "revision": REVISION,
            "phase": "preparing",
            "policy": request_policy.validate_shared_request_policy(
                self.production, source
            ),
        }
        authority = {
            "images": {
                role: {"source_sha256": str(index) * 64}
                for index, role in enumerate(("conversation-api", "web"), 7)
            }
        }
        images = {
            role: copy.deepcopy(self.values["images"][role])
            for role in ("conversation-api", "web")
        }
        for role, image in images.items():
            image["Config"]["Labels"].update(
                {
                    "org.opencontainers.image.revision": REVISION,
                    "io.depositar.ai.source.revision": REVISION,
                    "io.depositar.ai.source.service": role,
                    "io.depositar.ai.source.sha256": authority["images"][role][
                        "source_sha256"
                    ],
                }
            )
        images["filtered-embedding"] = gpu_images["scope-model"]
        binding = SimpleNamespace(
            images=MappingProxyType(images),
            proof=authority["images"]["conversation-api"],
        )
        session.provider._verified_backend_images = mock.Mock(
            return_value=binding
        )
        plan = {"services": {"reranker": {"source_sha256": "f" * 64}}}
        return session, record, binding, plan

    def test_cpu_source_uses_the_authenticated_provider_recipe(self):
        session, record, binding, plan = self.recipe_session()
        original = copy.deepcopy(session.provider.inference_compose)
        original_images = copy.deepcopy(dict(binding.images))
        with mock.patch.object(
            controller, "require_clean_revision", return_value=REVISION
        ):
            result = session.candidate_inputs(record)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        compose, images, proof = result
        factory = session.provider._verified_backend_images
        factory.assert_called_once_with(
            mock.ANY,
            controller.frozen_source(self.profile, REVISION),
            REVISION,
        )
        self.assertEqual(
            factory.call_args.args[0].state_root,
            controller.build_root(self.profile, REVISION),
        )
        self.assertIs(session._candidate_image_binding, binding)
        self.assertIs(images, binding.images)
        self.assertEqual(dict(images), original_images)
        self.assertEqual(session.provider.inference_compose, original)
        self.assertEqual(compose["x-depositar-plan"]["revision"], REVISION)
        self.assertIs(proof, binding.proof)
        self.assertEqual(
            original["x-depositar-plan"]["inference_source_sha256"], "6" * 64
        )
        self.assertNotEqual(
            plan["services"]["reranker"]["source_sha256"],
            original["x-depositar-plan"]["inference_source_sha256"],
        )

    def fake_session(self, docker, store, exit_error):
        values = self.values
        inference_name = values["compose"]["networks"]["existing-inference"][
            "name"
        ]
        catalog_binding = SimpleNamespace(
            context={
                "plan": {
                    "project": values["lease"]["catalog_project"],
                    "revision": REVISION,
                }
            },
            authority={"catalog_identity": values["catalog_identity"]},
            identity=lambda: {
                "source_revision": REVISION,
                "plan_sha256": "1" * 64,
                "configuration_sha256": "2" * 64,
                "query_authority_sha256": "3" * 64,
                "api_key_authority_sha256": "4" * 64,
            },
        )
        provider = SimpleNamespace(
            model_file_identity_sha256="f" * 64,
            backends=[SimpleNamespace(lease=values["lease"])],
            inference_compose={
                "name": inference_name.removesuffix("_inference"),
                "networks": {
                    "inference": {"name": inference_name},
                    "ranking": values["compose"]["networks"]["ranking"],
                },
            },
        )
        session = SimpleNamespace(
            docker=docker,
            control=self.control,
            provider=provider,
            compute_sha256="e" * 64,
            catalogs=lambda: [catalog_binding],
            candidate_inputs=lambda _: (
                values["compose"],
                values["images"],
                values["image_proof"],
            ),
            record=None,
            query_authority={
                "catalog_identity": values["catalog_identity"],
                "runtime": {
                    "network_id": values["lease"]["catalog_network_id"]
                },
            },
            query_catalog=catalog_binding,
        )

        def attach(record):
            session.record = record

        def verify(*_):
            if session.record is not None:
                candidate = copy.deepcopy(values)
                candidate["lease"] = session.record["lease"]
                candidate["containers"] = [
                    copy.deepcopy(r)
                    for r in docker.rows
                    if r["Config"]["Labels"]["com.docker.compose.project"]
                    == values["compose"]["name"]
                ]
                session.last_candidate = runtime.validate_runtime(**candidate)

        session.attach_record, session.verify = attach, verify
        session.check_binding = (
            lambda record: controller.Session.check_binding(session, record)
        )

        @contextmanager
        def context(*_):
            yield session
            self.assertNotIn(
                controller.current(store)["phase"], {"prepared", "running"}
            )
            if exit_error is not None:
                raise exit_error

        return session, context

    def test_prepare_rejects_provider_revision_before_policy_or_mutation(self):
        with (
            controller.journal(self.state) as store,
            mock.patch.object(
                controller,
                "require_clean_revision",
                side_effect=lambda source: (
                    "c" * 40 if source == self.production else REVISION
                ),
            ) as revision,
            mock.patch.object(
                controller, "validate_shared_request_policy"
            ) as policy,
            mock.patch.object(controller, "save") as save,
            mock.patch.object(controller, "git") as git,
            mock.patch.object(controller.lifecycle, "build_images") as build,
            mock.patch.object(controller, "live") as live,
        ):
            before = set(self.state.iterdir())
            with self.assertRaisesRegex(
                controller.SharedBackendError,
                "shared_backend_provider_source_changed",
            ):
                controller.prepare(self.profile, store, self.source)
            self.assertIsNone(controller.current(store))
            self.assertEqual(set(self.state.iterdir()), before)
        self.assertEqual(
            revision.call_args_list,
            [mock.call(self.source), mock.call(self.production)],
        )
        for operation in (policy, save, git, build, live):
            operation.assert_not_called()

    def test_prepare_rejects_configuration_drift_before_any_publication(self):
        original_check = self.control.require_disposable
        reference = copy.deepcopy(self.control.provider["configuration"])

        def corrupt_after_ownership_check(resources):
            original_check(resources)
            self.production_config.write_bytes(
                self.production_config.read_bytes() + b"\n"
            )

        with (
            controller.journal(self.state) as store,
            mock.patch.object(
                controller,
                "require_clean_revision",
                side_effect=lambda source: (
                    "b" * 40 if source == self.production else REVISION
                ),
            ),
            mock.patch.object(
                self.control,
                "require_disposable",
                side_effect=corrupt_after_ownership_check,
            ),
            mock.patch.object(controller, "save") as save,
            mock.patch.object(controller, "git") as git,
            mock.patch.object(controller.lifecycle, "build_images") as build,
            mock.patch.object(controller, "live") as live,
        ):
            with self.assertRaisesRegex(
                controller.ProviderError,
                "shared_backend_provider_authority_changed",
            ):
                controller.prepare(self.profile, store, self.source)
            self.assertIsNone(controller.current(store))
        self.assertEqual(len(self.control.checked), 1)
        self.assertEqual(self.control.provider["configuration"], reference)
        self.assertNotEqual(
            hashlib.sha256(self.production_config.read_bytes()).hexdigest(),
            reference["sha256"],
        )
        self.assertFalse((self.state / "sources").exists())
        self.assertFalse((self.state / "builds").exists())
        for operation in (save, git, build, live):
            operation.assert_not_called()

    def test_prepare_never_publishes_success_before_live_context_exits(self):
        self.patch(
            "search.operations.deployment.candidate.controller.require_clean_revision",
            side_effect=lambda source: (
                "b" * 40 if source == self.production else REVISION
            ),
        )
        self.patch(
            "search.operations.deployment.candidate.controller.lifecycle.build_images",
            return_value={},
        )

        def worktree(_source, *args):
            self.assertEqual(args[:3], ("worktree", "add", "--detach"))
            Path(args[3]).mkdir(mode=0o700)
            return b""

        self.patch(
            "search.operations.deployment.candidate.controller.git",
            side_effect=worktree,
        )
        for index, error in enumerate(
            (
                controller.SharedBackendError(
                    "shared_backend_production_source_changed"
                ),
                OSError("late FD verification failed"),
                None,
            )
        ):
            with self.subTest(error=type(error)):
                state = self.root / f"prepare-{index}"
                state.mkdir(mode=0o700)
                profile = self.profile | {"state_root": state}
                with controller.journal(state) as store:
                    _, context = self.fake_session(
                        FakeDocker(self.values, created=False), store, error
                    )
                    with mock.patch.object(controller, "live", context):
                        if error is None:
                            self.assertEqual(
                                controller.prepare(
                                    profile, store, self.source
                                )["status"],
                                "prepared",
                            )
                        else:
                            with self.assertRaises(type(error)):
                                controller.prepare(profile, store, self.source)
                            self.assertEqual(
                                controller.current(store)["phase"], "preparing"
                            )

    def test_start_never_publishes_running_before_source_and_fd_exit_checks(
        self,
    ):
        self.patch(
            "search.operations.deployment.candidate.controller.release_probe.verify_private_release",
            return_value={"readiness_verified": True},
        )
        for index, error in enumerate(
            (
                controller.SharedBackendError(
                    "shared_backend_production_source_changed"
                ),
                OSError("late FD verification failed"),
                None,
            )
        ):
            with self.subTest(error=type(error)):
                state = self.root / f"start-{index}"
                state.mkdir(mode=0o700)
                profile = self.profile | {"state_root": state}
                values = copy.deepcopy(self.values)
                for row in values["containers"]:
                    row["Config"]["Labels"].update(
                        {
                            "com.docker.compose.project.config_files": str(
                                state / "compose.json"
                            ),
                            "com.docker.compose.project.working_dir": str(
                                state
                            ),
                        }
                    )
                docker = FakeDocker(values, created=False)
                record = copy.deepcopy(self.record)
                record.update(
                    phase="prepared",
                    container_ids={},
                    network_id=None,
                    policy={"policy": {}},
                    provider_authority_sha256=self.control.provider_sha256,
                    roles_sha256=self.control.roles_sha256,
                    roles_epoch=self.control.roles["epoch"],
                    compute_sha256="e" * 64,
                )
                with controller.journal(state) as store:
                    controller.save(store, record)
                    session, context = self.fake_session(docker, store, error)
                    with (
                        mock.patch.object(controller, "live", context),
                        mock.patch.object(
                            controller.catalog,
                            "collect",
                            return_value=(
                                {},
                                {
                                    name: {"Id": identifier}
                                    for name, identifier in values["lease"][
                                        "network_ids"
                                    ].items()
                                },
                            ),
                        ),
                        mock.patch.object(
                            controller.catalog,
                            "client_identity",
                            wraps=controller.catalog.client_identity,
                        ) as identity,
                    ):
                        if error is None:
                            self.assertEqual(
                                controller.start(profile, store, record)[
                                    "status"
                                ],
                                "running",
                            )
                        else:
                            with self.assertRaises(type(error)):
                                controller.start(profile, store, record)
                            self.assertEqual(
                                controller.current(store)["phase"], "starting"
                            )
                        identity.assert_called_once_with(
                            session.query_catalog, REVISION
                        )
                        self.assertNotIn(
                            "catalog_revision",
                            controller.current(store)["lease"],
                        )
                docker.assert_production_unchanged(self)


if __name__ == "__main__":
    unittest.main()
