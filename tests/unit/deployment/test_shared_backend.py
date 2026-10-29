"""CPU-only shared-backend ownership, locking and publication regressions."""

from contextlib import ExitStack, contextmanager
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from search.operations.config import GPUConfig, ProductConfig
from search.operations.deployment import shared_backend as shared
from search.operations.deployment import shared_request_policy
from search.operations.deployment.release import runtime
from tests.unit.deployment.release.test_runtime import evidence as release_evidence


ROOT = Path(__file__).resolve().parents[3]
REVISION = "a" * 40


class FakeDocker:
    """Inspect genuine fixtures; mutations affect only these in-memory objects."""

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
                "com.docker.compose.project.config_files": "/production/compose.json",
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
                if r["Config"]["Labels"]["com.docker.compose.project"] == project
            ]
            return ("\n".join(identifiers) + ("\n" if identifiers else "")).encode()
        if args[:2] == ["network", "ls"]:
            prefix = args[-1].removeprefix("name=")
            return b"\n".join(
                self.encoded({"Name": n["Name"]})
                for n in self.networks
                if n["Name"].startswith(prefix)
            )
        if args[0] == "inspect":
            rows = [
                r for identifier in args[1:] for r in self.rows if r["Id"] == identifier
            ]
            if len(rows) != len(args) - 1:
                raise ValueError("missing fake container")
            return self.encoded(rows)
        if args[:2] == ["network", "inspect"]:
            rows = [n for n in self.networks if args[2] in {n["Id"], n["Name"]}]
            if len(rows) != 1:
                raise ValueError("missing fake network")
            if self.wrong_network_recheck and args[2] == self.seed_network["Id"]:
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
        test.assertFalse(any("9" * 64 in c or "8" * 64 in c for c in self.mutations))


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
            for relative in shared_request_policy.SOURCE_FILES:
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
        self.config = self.root / "shared.json"
        self.configuration = {
            "schema_version": 1,
            "state_root": str(self.state),
            "production_config": str(self.production_config),
            "production_source": str(self.production),
            "tls_plan": str(self.root / "tls-plan.json"),
        }
        self.write_json(self.config, self.configuration)
        self.profile = shared.load_config(self.config)
        self.values = release_evidence()
        compose_file = self.state / "compose.json"
        for row in self.values["containers"]:
            row["Config"]["Labels"].update(
                {
                    "com.docker.compose.project.config_files": str(compose_file),
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
            "schema_version": 1,
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
                for name, identifier in self.values["lease"]["network_ids"].items()
                if name != website
            },
            "build_complete": False,
            "lease": copy.deepcopy(self.values["lease"]),
            "baseline_receipt_sha256": "b" * 64,
            "model_file_identity_sha256": "c" * 64,
            "compute_sha256": "d" * 64,
        }

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
            ({"state_root": str(self.product.state_root)}, "state_overlaps_production"),
            (
                {"state_root": str(self.product.api_model_root / "candidate")},
                "state_overlaps_production",
            ),
            ({"state_root": "relative"}, "path_invalid"),
        ]:
            with self.subTest(change=change):
                self.write_json(self.config, self.configuration | change)
                with self.assertRaisesRegex(shared.SharedBackendError, error):
                    shared.load_config(self.config)
        self.write_json(self.config, self.configuration)
        self.config.chmod(0o644)
        with self.assertRaises(ValueError):
            shared.load_config(self.config)

    def test_config_rejects_symlink_and_git_contained_journal(self):
        alias = self.root / "state-alias"
        alias.symlink_to(self.state, target_is_directory=True)
        self.write_json(self.config, self.configuration | {"state_root": str(alias)})
        with self.assertRaisesRegex(shared.SharedBackendError, "path_invalid"):
            shared.load_config(self.config)
        (self.source / ".git").mkdir()
        self.write_json(
            self.config,
            self.configuration | {"state_root": str(self.source / "private-state")},
        )
        with self.assertRaisesRegex(shared.SharedBackendError, "state_inside_git"):
            shared.load_config(self.config)

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

        with shared.journal(self.state):
            self.assertEqual(contend(), 73)
            with self.assertRaisesRegex(
                shared.SharedBackendError, "shared_backend_busy"
            ):
                with shared.journal(self.state):
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
                    with shared.journal(self.state):
                        self.fail("linked lock was accepted")
                self.assertEqual(target.read_text(), "unchanged")
                lock.unlink()

    def test_incompatible_policy_fails_before_worktree_build_or_live_access(self):
        model_policy = self.source / "search/model_policy.py"
        model_policy.write_text(
            model_policy.read_text().replace(
                "SCOPE_NUM_CTX = 4_096", "SCOPE_NUM_CTX = 8_192"
            )
        )
        self.patch(
            "search.operations.deployment.shared_backend.require_clean_revision",
            return_value=REVISION,
        )
        git = self.patch(
            "search.operations.deployment.shared_backend.git",
            side_effect=AssertionError("must not create a worktree"),
        )
        build = self.patch(
            "search.operations.deployment.shared_backend.lifecycle.build_images",
            side_effect=AssertionError("must not build"),
        )
        live = self.patch(
            "search.operations.deployment.shared_backend.live",
            side_effect=AssertionError("must not inspect live services"),
        )
        with shared.journal(self.state) as store:
            with self.assertRaises(shared_request_policy.SharedRequestPolicyError):
                shared.prepare(self.profile, store, self.source)
            self.assertIsNone(shared.current(store))
        git.assert_not_called()
        build.assert_not_called()
        live.assert_not_called()

    def test_changed_clean_production_head_is_rejected_before_catalog_audit(self):
        session = object.__new__(shared.Session)
        session.profile = self.profile
        session.context = {"source_revision": REVISION}
        snapshot = (
            self.values["catalog_identity"],
            self.values["containers"],
            self.values["catalog_network"],
        )
        head = self.patch(
            "search.operations.deployment.shared_backend.require_clean_revision",
            return_value="b" * 40,
        )
        audit = self.patch(
            "search.operations.deployment.shared_backend.image_build._run",
            side_effect=AssertionError("changed source must not run its catalog audit"),
        )
        with mock.patch.object(session, "observe", return_value=snapshot) as observe:
            with self.assertRaisesRegex(
                shared.SharedBackendError, "shared_backend_production_source_changed"
            ):
                session.verify({})
        head.assert_called_once_with(self.production)
        audit.assert_not_called()
        observe.assert_called_once_with()

    def test_candidate_rows_use_real_capability_checks_and_sealed_launcher(self):
        docker = FakeDocker(self.values)
        rows, names = shared.candidate_rows(
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
                    row["HostConfig"]["DeviceRequests"] = [{"Driver": "nvidia"}]
                else:
                    website = self.values["networks"][0]["Name"]
                    row["NetworkSettings"]["Networks"][website]["NetworkID"] = "f" * 64
                with self.assertRaises(ValueError):
                    shared.candidate_rows(
                        changed, self.record, self.state / "compose.json"
                    )
                self.assertEqual(changed.mutations, [])

    def invoke_stop(self, docker, record=None):
        with (
            mock.patch(
                "search.operations.deployment.runtime_docker.Docker",
                return_value=docker,
            ),
            shared.journal(self.state) as store,
        ):
            return shared.stop(
                self.profile, store, self.record if record is None else record
            )

    def test_stop_removes_only_exact_sealed_candidate_resources(self):
        docker = FakeDocker(self.values)
        result = self.invoke_stop(docker)
        self.assertEqual(result["status"], "stopped")
        removed = {c[2] for c in docker.mutations if c[:2] == ["rm", "--force"]}
        self.assertEqual(removed, set(self.record["container_ids"].values()))
        self.assertEqual(
            [c for c in docker.mutations if c[:2] == ["network", "rm"]],
            [["network", "rm", self.record["network_id"]]],
        )
        docker.assert_production_unchanged(self)

    def test_foreign_same_sha_or_unknown_peer_and_network_id_block_all_deletion(self):
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
                with self.assertRaises(shared.SharedBackendError):
                    self.invoke_stop(docker, copy.deepcopy(self.record))
                self.assertEqual(docker.mutations, [])
                docker.assert_production_unchanged(self)

    def test_partial_cleanup_can_retry_without_adopting_missing_containers(self):
        docker = FakeDocker(self.values)
        identifiers = sorted(self.record["container_ids"].values())
        docker.fail_removal = identifiers[1]
        with self.assertRaises(OSError):
            self.invoke_stop(docker)
        self.assertEqual([c[2] for c in docker.mutations], [identifiers[0]])
        docker.fail_removal = None
        self.assertEqual(self.invoke_stop(docker)["status"], "stopped")
        removed = [c[2] for c in docker.mutations if c[:2] == ["rm", "--force"]]
        self.assertEqual(sorted(removed), identifiers)
        docker.assert_production_unchanged(self)

    def test_network_identity_is_rechecked_before_removal(self):
        docker = FakeDocker(self.values)
        docker.wrong_network_recheck = True
        with self.assertRaisesRegex(
            shared.SharedBackendError, "candidate_network_changed"
        ):
            self.invoke_stop(docker)
        self.assertEqual(
            {c[2] for c in docker.mutations if c[:2] == ["rm", "--force"]},
            set(self.record["container_ids"].values()),
        )
        self.assertFalse(any(c[:2] == ["network", "rm"] for c in docker.mutations))
        docker.assert_production_unchanged(self)

    def test_status_reports_incomplete_phases_but_query_requires_running(self):
        live = self.patch(
            "search.operations.deployment.shared_backend.live",
            side_effect=AssertionError("not running"),
        )
        self.assertEqual(shared.operate(self.config, "status"), {"status": "empty"})
        for phase in (
            "preparing",
            "prepared",
            "creating",
            "starting",
            "failed",
            "stopped",
        ):
            with self.subTest(phase=phase):
                with shared.journal(self.state) as store:
                    shared.save(store, self.record | {"phase": phase})
                self.assertEqual(shared.operate(self.config, "status")["status"], phase)
                with self.assertRaisesRegex(
                    shared.SharedBackendError, "candidate_not_running"
                ):
                    shared.operate(self.config, "query", text="test")
        live.assert_not_called()

    def fake_session(self, docker, store, exit_error):
        values = self.values
        completed = {"revision": "b" * 40, "runtime_verified": True}
        session = SimpleNamespace(
            docker=docker,
            completed=completed,
            inputs=SimpleNamespace(model_file_identity_sha256="f" * 64),
            compute_sha256="e" * 64,
            native=SimpleNamespace(
                lease=values["lease"],
                authority={"catalog_identity": values["catalog_identity"]},
                collect=lambda **_: (
                    {},
                    {
                        name: {"Id": identifier}
                        for name, identifier in values["lease"]["network_ids"].items()
                    },
                ),
            ),
            candidate_inputs=lambda _: (
                values["compose"],
                values["images"],
                values["image_proof"],
            ),
            record=None,
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

        @contextmanager
        def context(*_):
            yield session
            self.assertNotIn(shared.current(store)["phase"], {"prepared", "running"})
            if exit_error is not None:
                raise exit_error

        return session, context

    def test_prepare_never_publishes_success_before_live_context_exits(self):
        self.patch(
            "search.operations.deployment.shared_backend.require_clean_revision",
            return_value=REVISION,
        )
        self.patch(
            "search.operations.deployment.shared_backend.lifecycle.build_images",
            return_value={},
        )

        def worktree(_source, *args):
            self.assertEqual(args[:3], ("worktree", "add", "--detach"))
            Path(args[3]).mkdir(mode=0o700)
            return b""

        self.patch(
            "search.operations.deployment.shared_backend.git", side_effect=worktree
        )
        for index, error in enumerate(
            (
                shared.SharedBackendError("shared_backend_production_source_changed"),
                OSError("late FD verification failed"),
                None,
            )
        ):
            with self.subTest(error=type(error)):
                state = self.root / f"prepare-{index}"
                state.mkdir(mode=0o700)
                profile = self.profile | {"state_root": state}
                with shared.journal(state) as store:
                    _, context = self.fake_session(
                        FakeDocker(self.values, created=False), store, error
                    )
                    with mock.patch.object(shared, "live", context):
                        if error is None:
                            self.assertEqual(
                                shared.prepare(profile, store, self.source)["status"],
                                "prepared",
                            )
                        else:
                            with self.assertRaises(type(error)):
                                shared.prepare(profile, store, self.source)
                            self.assertEqual(
                                shared.current(store)["phase"], "preparing"
                            )

    def test_start_never_publishes_running_before_source_and_fd_exit_checks(self):
        self.patch(
            "search.operations.deployment.shared_backend.probe.verify_private_release",
            return_value={"readiness_verified": True},
        )
        for index, error in enumerate(
            (
                shared.SharedBackendError("shared_backend_production_source_changed"),
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
                            "com.docker.compose.project.working_dir": str(state),
                        }
                    )
                docker = FakeDocker(values, created=False)
                record = copy.deepcopy(self.record)
                record.update(
                    phase="prepared",
                    container_ids={},
                    network_id=None,
                    policy={"policy": {}},
                    baseline_receipt_sha256=shared.digest(
                        {"revision": "b" * 40, "runtime_verified": True}
                    ),
                    compute_sha256="e" * 64,
                )
                with shared.journal(state) as store:
                    shared.save(store, record)
                    _, context = self.fake_session(docker, store, error)
                    with mock.patch.object(shared, "live", context):
                        if error is None:
                            self.assertEqual(
                                shared.start(profile, store, record)["status"],
                                "running",
                            )
                        else:
                            with self.assertRaises(type(error)):
                                shared.start(profile, store, record)
                            self.assertEqual(shared.current(store)["phase"], "starting")
                docker.assert_production_unchanged(self)


if __name__ == "__main__":
    unittest.main()
