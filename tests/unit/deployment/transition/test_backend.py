"""Run actual TLS owner checks with private files and fake process outputs."""

from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.deployment import private_api_tls
from search.operations.deployment.images import build
from search.operations.deployment.provider import authority, runtime
from search.operations.deployment.transition import backend, contract
from search.operations.deployment.transition import enrollment, journal
from search.operations.shared import private_io
from search.transport.version import CONVERSATION_CONTRACT_VERSION
from tests.unit.deployment.provider.fixtures import dual_snapshot
from tests.unit.deployment.test_private_api_tls import TLSProofRunner
from .fixtures import ObservingOwner, operation_record


class TLSProcessBoundary:
    """Return process output while real plan and ownership code runs."""

    def __init__(self, data):
        self.data, self.calls = data, []
        self.current, self.generation = None, 0

    def detail(self, row):
        return {
            "Id": row["Id"],
            "Image": row["Image"],
            "Pid": row["State"]["Pid"],
            "StartedAt": row["State"]["StartedAt"],
            "SandboxID": row["NetworkSettings"]["SandboxID"],
            "Labels": row["Config"]["Labels"],
            "Networks": row["NetworkSettings"]["Networks"],
            "Mounts": row["Mounts"],
            "State": row["State"],
            "Ports": row["NetworkSettings"]["Ports"],
            "PortBindings": row["HostConfig"]["PortBindings"],
        }

    def activate(self, directory):
        self.generation += 1
        self.current = TLSProofRunner(None, directory)
        remapped = {}
        for index, record in enumerate(self.current.runtime):
            old = record["ID"]
            new = format(9000 + self.generation * 10 + index, "064x")
            record["ID"] = new
            row = self.current.owned[old]
            row["Id"] = new
            remapped[new] = row
        self.current.owned = remapped

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        value = self.output(argv)
        raw = value if type(value) is bytes else json.dumps(value).encode()
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")

    def output(self, argv):
        if argv[0] == "openssl":
            if "subjectAltName" in argv:
                return b"DNS:api.example.test, DNS:inner.example.test\n"
            return b"owned fake process output\n"
        if argv[:2] == ["docker", "ps"]:
            return (
                b"\n".join(row["ID"].encode() for row in self.current.runtime)
                if self.current
                else b""
            )
        if argv[:2] == ["docker", "compose"]:
            return self.compose(argv)
        if argv[:3] == ["docker", "image", "inspect"]:
            return ("sha256:" + "f" * 64 + "\n").encode()
        if argv[:3] == ["docker", "network", "inspect"]:
            return self.data.networks[argv[-1]]
        if argv[:2] == ["docker", "inspect"]:
            return self.inspect(argv)
        if argv[:2] == ["docker", "stop"]:
            return b""
        if argv[:2] == ["docker", "rm"]:
            self.current = None
            return b""
        raise AssertionError("unexpected process command")

    def compose(self, argv):
        if "ps" in argv:
            return self.current.runtime if self.current else []
        if "up" in argv:
            directory = Path(argv[argv.index("-f") + 1]).parent
            self.activate(directory)
            return b""
        raise AssertionError("unexpected compose command")

    def inspect(self, argv):
        template = argv[3]
        if template == "{{json .Config.Labels}}":
            return self.current.owned[argv[-1]]["Labels"]
        if '"NetworkMode"' in template:
            return self.current.owned[argv[-1]]
        rows = [
            row
            for members in self.data.groups.values()
            for row in members
            if row["Id"] in argv[4:]
        ]
        return b"\n".join(
            json.dumps(self.detail(row)).encode() for row in rows
        )


class BackendTLSOwnerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="transition-tls-")
        )
        self.root = Path(directory)
        self.root.chmod(0o700)
        self.data = dual_snapshot()
        self.process = TLSProcessBoundary(self.data)
        self.stack.enter_context(patch.object(build, "_run", self.process))
        self.endpoint = self.root / "tls"
        self.endpoint.mkdir(mode=0o700)
        self.config_file = self.root / "control.json"
        self.config_file.write_bytes(
            private_io.canonical(
                {"schema_version": 1, "state_root": str(self.root / "control")}
            )
        )
        self.config_file.chmod(0o600)
        self.control = self.stack.enter_context(
            authority.open_control(self.config_file, create=True)
        )
        self.value = operation_record(self.control)
        self.plans = [
            self.plan(index, host)
            for index, host in enumerate(self.data.hosts)
        ]
        old = self.control.roles
        roles = {
            **old,
            "epoch": old["epoch"] + 1,
            "tls_plan_ref": self.plans[0]["plan"],
        }
        self.control._compare_and_swap_roles(
            roles,
            expected_epoch=old["epoch"],
            expected_sha256=contract.digest(old),
        )
        self.value["predecessor_roles"] = roles
        self.value["predecessor_roles_sha256"] = contract.digest(roles)
        self.value["candidate"].update(
            original_epoch=roles["epoch"],
            original_roles_sha256=contract.digest(roles),
        )
        for side, plan in zip(("predecessor", "target"), self.plans):
            self.value["sides"][side]["tls"] = plan
        self.operation = journal.create(self.control, self.value)
        enrollment.enroll(
            self.endpoint,
            participant="tls",
            source=self.value["operator"],
            configuration=self.value["authority"]["control_config"],
        )
        fd = self.stack.enter_context(enrollment.locked(self.endpoint))
        provider = SimpleNamespace(
            docker=SimpleNamespace(calls=0),
            selected_tls=None,
            commands=backend.OwnerCommands(time.monotonic() + 1800),
        )
        provider._tls_runner = (
            lambda argv, **kwargs: provider.commands.process(
                argv, timeout=kwargs["timeout"], environment={}
            )
        )
        self.owner = backend.TLSOwner(
            provider,
            self.endpoint,
            fd,
            self.value["operator"],
            self.value["authority"]["control_config"],
        )
        self.owner.prepare(self.operation)
        witness = ObservingOwner()
        self.operation.begin(witness, witness)
        self.process.activate(Path(self.plans[0]["plan"]["path"]).parent)

    def plan(self, index, host):
        from .fixtures import reference, tls_projection

        # Only synthetic material is used; crypto subprocess output is faked.
        seed = tls_projection(
            {"compose": host.compose, "runtime": host.expected_runtime},
            reference("/private/unused"),
        )
        config = copy.deepcopy(seed["config"])
        config["nginx_image"] = "sha256:" + "f" * 64
        release = self.root / ("release" + str(index) + ".json")
        release.write_bytes(private_io.canonical(host.compose))
        release.chmod(0o600)
        config["release_compose"] = str(release)
        for name in private_api_tls.TLS_FIELDS:
            path = self.endpoint / name
            path.write_bytes(b"owned synthetic TLS material\n")
            path.chmod(0o600)
            config[name] = str(path)
        for offset, row in enumerate(
            self.data.groups[host.compose["name"]], 2
        ):
            website = host.compose["networks"]["website"]["name"]
            row["NetworkSettings"]["Networks"][website]["IPAddress"] = (
                "192.0.2." + str(offset + index * 10)
            )
            if row["Id"] == host.lease["api_container_id"]:
                row["NetworkSettings"]["Networks"][website]["Aliases"] = [
                    "conversation-api"
                ]
                row["Config"]["Labels"][
                    "io.depositar.ai.conversation.version"
                ] = str(CONVERSATION_CONTRACT_VERSION)
        filename = self.root / ("tls-config" + str(index) + ".json")
        filename.write_bytes(private_io.canonical(config))
        filename.chmod(0o600)
        output = self.endpoint / ("version" + str(index))
        private_api_tls.prepare(filename, output, runner=self.process)
        return backend.tls_projection(
            backend.reference(output / "plan.json"), runner=self.process
        )

    def test_actual_tls_target_switch_replaces_only_the_known_pair(self):
        before = [row["ID"] for row in self.process.current.runtime]
        observed = self.owner.publish(self.operation, "target")
        self.assertEqual(
            observed["binding_sha256"], contract.digest(self.plans[1])
        )
        self.assertTrue(
            set(before).isdisjoint(observed["container_ids"].values())
        )
        self.assertEqual(
            self.owner.provider.selected_tls, self.plans[1]["plan"]
        )

    def test_same_operation_explicit_return_handles_current_target_binding(
        self,
    ):
        self.owner.publish(self.operation, "target")
        observed = self.owner.publish(self.operation, "predecessor")
        self.assertEqual(
            observed["binding_sha256"], contract.digest(self.plans[0])
        )

    def test_unknown_pair_configuration_refuses_before_any_stop(self):
        for row in self.process.current.owned.values():
            row["Labels"]["com.docker.compose.project.config_files"] = (
                "/unrecognized/compose.json"
            )
        before = len(self.process.calls)
        with self.assertRaisesRegex(
            contract.TransitionError, "runtime_changed"
        ):
            self.owner.publish(self.operation, "target")
        calls = self.process.calls[before:]
        self.assertFalse(any(args[:2] == ["docker", "stop"] for args in calls))

    def test_transition_roles_admission_keeps_normal_provider_strict(self):
        normal = runtime.Provider.__new__(runtime.Provider)
        normal.control, normal.roles_sha256 = (
            self.control,
            contract.digest(self.value["predecessor_roles"]),
        )
        with self.assertRaisesRegex(ValueError, "cas_conflict"):
            normal._roles_admission()
        current = backend.TransitionProvider.__new__(
            backend.TransitionProvider
        )
        current.operation, current.control = self.operation, self.control
        current.roles = copy.deepcopy(self.value["predecessor_roles"])
        current._roles_admission()
        current.roles["epoch"] += 1
        with self.assertRaisesRegex(contract.TransitionError, "cas_conflict"):
            current._roles_admission()

    def test_failed_tls_up_uses_reserved_cleanup_without_restoring_a(self):
        def failed_up(argv, **kwargs):
            result = self.process(argv, **kwargs)
            if "up" in argv:
                raise OSError("owned startup acknowledgement lost")
            return result

        with (
            patch.object(contract, "BACKEND_COMMAND_CREDITS", 1024),
            patch.object(build, "_run", side_effect=failed_up),
        ):
            with self.assertRaisesRegex(OSError, "acknowledgement lost"):
                self.owner.publish(self.operation, "target")
        commands = self.owner.provider.commands
        self.assertEqual(commands.cleanup_calls, 6)
        self.assertEqual(commands.by_kind["failure_cleanup"], 6)
        self.assertEqual(commands.calls, commands.parent_attempts)
        self.assertIsNone(self.process.current)
        self.assertEqual(self.operation.refresh(), ("pending", None))
        self.assertTrue((self.control.root / journal.HOLD).exists())
        self.assertIsNone(self.operation.read("resolution"))


class OwnerCommandTests(unittest.TestCase):
    def test_candidate_source_admission_uses_the_same_counted_git_runner(self):
        from search.operations.deployment import source_bundle
        from search.operations.deployment.candidate import controller

        provider = backend.TransitionProvider.__new__(
            backend.TransitionProvider
        )
        provider.commands = backend.OwnerCommands(time.monotonic() + 1800)
        provider.source_root = Path("/owned/provider")
        session = controller.Session.__new__(controller.Session)
        session.provider = provider
        session.profile = {"state_root": Path("/owned/candidate")}

        def git(argv, *, timeout):
            self.assertGreater(timeout, 0)
            return SimpleNamespace(
                returncode=0,
                stdout=("a" * 40 if "rev-parse" in argv else "").encode(),
                stderr=b"",
            )

        with (
            patch.object(contract, "BACKEND_COMMAND_CREDITS", 1024),
            patch.object(source_bundle, "_runner", side_effect=git),
            patch.object(
                controller,
                "validate_shared_request_policy",
                side_effect=ValueError("owned next admission boundary"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "next admission boundary"):
                session.candidate_inputs({"revision": "a" * 40})
        self.assertEqual(provider.commands.by_kind, {"source_git": 2})
        self.assertEqual(provider.commands.calls, 2)

    def test_immutable_source_plan_reuse_still_checks_each_clean_checkout(
        self,
    ):
        from search.operations.deployment import source_bundle

        owner = backend.TransitionProvider.__new__(backend.TransitionProvider)
        owner.commands = backend.OwnerCommands(time.monotonic() + 5)
        owner.source_plans = {}
        plan = {"revision": "a" * 40, "files": {"fixed": {"sha256": "b" * 64}}}
        with (
            patch.object(
                backend, "require_clean_revision", return_value="a" * 40
            ) as clean,
            patch.object(
                source_bundle, "committed_source_plan", return_value=plan
            ) as build_plan,
        ):
            first = owner._source_plan(Path("/owned/first"), "a" * 40)
            first["files"]["fixed"]["sha256"] = "c" * 64
            self.assertEqual(
                owner._source_plan(Path("/owned/first"), "a" * 40), plan
            )
            owner._source_plan(Path("/owned/second"), "a" * 40)
            self.assertEqual(build_plan.call_count, 2)
            self.assertEqual(clean.call_count, 3)
            with patch.object(
                backend,
                "require_clean_revision",
                side_effect=ValueError("product_source_dirty"),
            ):
                with self.assertRaisesRegex(
                    ValueError, "product_source_dirty"
                ):
                    owner._source_plan(Path("/owned/first"), "a" * 40)

    def test_runtime_runners_share_a_failed_attempt_and_clipped_deadline(self):
        from search.operations.deployment import source_bundle
        from search.operations.deployment.gpu import collect

        values = []
        result = SimpleNamespace(
            returncode=0, stdout=b"a" * 40 + b"\n", stderr=b""
        )

        def process(argv, **kwargs):
            values.append((argv[0], kwargs["timeout"]))
            return result

        commands = backend.OwnerCommands(time.monotonic() + 125)
        provider = backend.TransitionProvider.__new__(
            backend.TransitionProvider
        )
        provider.commands, provider.deadline = commands, commands.deadline
        with (
            patch.object(build, "_run", side_effect=process),
            patch.object(source_bundle, "_runner", side_effect=process),
            patch.object(collect, "bounded_runner", side_effect=process),
        ):
            provider._tls_runner(["openssl", "verify"], timeout=30)
            provider._gpu_runner(["nvidia-smi"], timeout=20, max_bytes=1024)
            provider._compute_process()
            commands.git_text(["git", "status"], timeout=15)
            with patch.object(
                build, "_run", side_effect=OSError("owned failed attempt")
            ):
                with self.assertRaises(OSError):
                    commands.process(["docker", "inspect"], timeout=30)
        self.assertEqual(commands.calls, 5)
        self.assertEqual(
            commands.by_kind, {"process": 3, "gpu": 1, "source_git": 1}
        )
        self.assertEqual(
            [name for name, _timeout in values],
            ["openssl", "nvidia-smi", "nvidia-smi", "git"],
        )
        self.assertTrue(all(0 < timeout <= 5 for _name, timeout in values))

    def test_expiry_and_exhaustion_refuse_before_another_process(self):
        commands = backend.OwnerCommands(time.monotonic() + 125)
        with (
            patch.object(contract, "BACKEND_COMMAND_CREDITS", 513),
            patch.object(build, "_run") as process,
        ):
            commands.process(["docker", "inspect"], timeout=30)
            with self.assertRaisesRegex(
                contract.TransitionError, "command_budget"
            ):
                commands.process(["docker", "inspect"], timeout=30)
            self.assertEqual(process.call_count, 1)
        expired = backend.OwnerCommands(time.monotonic() - 1)
        with patch.object(build, "_run") as process:
            with self.assertRaisesRegex(contract.TransitionError, "deadline"):
                expired.process(["docker", "inspect"], timeout=30)
            process.assert_not_called()

    def test_catalog_child_failure_spends_its_full_bound_without_refund(self):
        from search.operations.deployment.candidate import catalog

        argv = [
            "python3",
            "-I",
            "-S",
            "-B",
            "-c",
            catalog._BOUNDED_CONTEXT,
            "/owned/source",
            "/owned/config.json",
            "a" * 40,
            "-",
            "256",
            "120",
        ]
        commands = backend.OwnerCommands(time.monotonic() + 1800)
        with (
            patch.object(contract, "BACKEND_COMMAND_CREDITS", 1026),
            patch.object(
                build, "_run", side_effect=OSError("failed child")
            ) as run,
        ):
            for unused in range(2):
                with self.assertRaisesRegex(OSError, "failed child"):
                    commands.process(argv, timeout=120)
            with self.assertRaisesRegex(
                contract.TransitionError, "command_budget"
            ):
                commands.process(argv, timeout=120)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(commands.calls, 514)
        self.assertEqual(commands.parent_attempts, 2)
        self.assertEqual(commands.child_charges, 512)
        self.assertEqual(commands.cleanup_calls, 0)

    def test_normal_exhaustion_preserves_bounded_owned_cleanup(self):
        commands = backend.OwnerCommands(time.monotonic() + 1800)
        with (
            patch.object(backend, "CLEANUP_COMMANDS", 2),
            patch.object(contract, "BACKEND_COMMAND_CREDITS", 4),
            patch.object(build, "_run") as run,
        ):
            commands.process(["docker", "ps"], timeout=30)
            commands.process(["docker", "inspect"], timeout=30)
            with self.assertRaisesRegex(
                contract.TransitionError, "command_budget"
            ):
                commands.process(["docker", "ps"], timeout=30)
            commands.cleanup_process(["docker", "stop"], timeout=30)
            commands.cleanup_process(["docker", "rm"], timeout=30)
            with self.assertRaisesRegex(
                contract.TransitionError, "cleanup_budget"
            ):
                commands.cleanup_process(["docker", "ps"], timeout=30)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(commands.calls, 4)
        self.assertEqual(commands.cleanup_calls, 2)

    def test_phase_expiry_still_permits_cleanup_before_hard_deadline(self):
        clock = [0]
        commands = backend.OwnerCommands(1800)
        with (
            patch.object(
                backend.time, "monotonic", side_effect=lambda: clock[0]
            ),
            patch.object(contract, "BACKEND_COMMAND_CREDITS", 32768),
            patch.object(build, "_run") as run,
        ):
            with commands.phase("prepare"):
                commands.process(["docker", "inspect"], timeout=900)
                self.assertEqual(run.call_args.kwargs["timeout"], 600)
                clock[0] = 610
                with self.assertRaisesRegex(
                    contract.TransitionError, "deadline"
                ):
                    commands.process(["docker", "ps"], timeout=30)
                commands.cleanup_process(["docker", "stop"], timeout=900)
                self.assertEqual(run.call_args.kwargs["timeout"], 120)
            with self.assertRaisesRegex(
                contract.TransitionError, "phase_budget"
            ):
                with commands.phase("prepare"):
                    self.fail("expired phase must not be renewed")
            clock[0] = 1801
            with self.assertRaisesRegex(contract.TransitionError, "deadline"):
                commands.cleanup_process(["docker", "rm"], timeout=30)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(commands.calls, 2)

    def test_phase_admission_requires_full_bound_and_cleanup_reserve(self):
        commands = backend.OwnerCommands(time.monotonic() + 1800)
        with patch.object(contract, "BACKEND_COMMAND_CREDITS", 8192):
            with self.assertRaisesRegex(
                contract.TransitionError, "phase_budget"
            ):
                with commands.phase("begin"):
                    self.fail("must not enter an underfunded effect phase")
        self.assertEqual(commands.calls, 0)
        self.assertIsNone(commands.phase_name)
