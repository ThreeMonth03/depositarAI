"""Private CPU inputs with real controller, TLS and VM participant owners.

Provider source/image/model preparation is the existing native-input fixture
boundary. Runtime, TLS, VM checker, Control and operation records stay real.
"""

from contextlib import contextmanager, ExitStack
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

from search.operations.catalog import execution
from search.operations.deployment import private_api_tls, source_bundle
from search.operations.deployment.candidate import catalog
from search.operations.deployment.candidate import controller as candidate
from search.operations.deployment.frontend import binding, check, delivery
from search.operations.deployment.gpu import collect as gpu_collect
from search.operations.deployment.images import build
from search.operations.deployment.provider import authority
from search.operations.deployment.runtime_docker import Docker
from search.operations.deployment.transition import backend, contract
from search.operations.deployment.transition import controller, enrollment
from search.operations.deployment.transition import transport
from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision
from tests.unit.deployment.frontend.test_binding import (
    LocalHTTP,
    VersionRunner,
)
from tests.unit.deployment.frontend.test_check import (
    CATALOG,
    FRONT,
    NGINX_IMAGE,
    WEB_IMAGE,
)
from tests.unit.deployment.provider.fixtures import (
    _checkpoint_evidence,
    dual_snapshot,
)
from . import test_backend as tls_fixture
from .test_retirement import CleanupDocker


class ProcessBoundary(tls_fixture.TLSProcessBoundary):
    """Add full TLS inspect and synthetic Git/compute to the process seam."""

    def __init__(self, data, sources):
        super().__init__(data)
        self.sources = sources
        self.compute = b""
        self.ledger = []
        self.cpu_runner = None
        self.catalog_records = {}

    def activate(self, directory):
        super().activate(directory)
        projected = list(self.current.projected.values())
        self.current.projected = {}
        for row, runtime in zip(projected, self.current.runtime):
            row["Id"] = runtime["ID"]
            self.current.projected[row["Id"]] = row

    def __call__(self, argv, **kwargs):
        self.ledger.append(list(argv))
        if argv[0] == "git":
            return self.git(argv, timeout=kwargs["timeout"])
        if argv[0] == "nvidia-smi":
            return SimpleNamespace(
                returncode=0, stdout=self.compute, stderr=b""
            )
        if argv[:5] == ["python3", "-I", "-S", "-B", "-c"]:
            assert argv[5] == catalog._BOUNDED_CONTEXT
            assert len(argv) == 12 and argv[10] == "256"
            value = copy.deepcopy(self.catalog_records[argv[7]])
            assert value["source_revision"] == argv[8]
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(value).encode(), stderr=b""
            )
        if tuple(argv[:5]) == gpu_collect.DOCKER_PREFIX:
            argv = ["docker", *argv[5:]]
        elif argv[:2] == ["docker", "--host"]:
            return self.cpu_runner(argv, **kwargs)
        if (
            argv[:2] == ["docker", "inspect"]
            and argv[3] == gpu_collect.INSPECT_FORMAT
        ):
            value = copy.deepcopy(self.current.projected[argv[-1]])
            value["HostConfig"]["DeviceRequests"].append(None)
            value["NVIDIAEnv"].append(None)
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(value).encode(), stderr=b""
            )
        return super().__call__(argv, **kwargs)

    def git(self, argv, *, timeout):
        assert timeout > 0
        root = Path(argv[argv.index("-C") + 1])
        value = self.sources[root] if "rev-parse" in argv else ""
        return SimpleNamespace(
            returncode=0, stdout=(value + "\n").encode(), stderr=b""
        )


class _AwaitNextPacket(Exception):
    """The retained fake pipe has no new packet yet; this is not EOF."""


class _PacketFrames:
    def __init__(self, packet):
        self.packet, self.response = packet, None

    def read(self):
        if self.packet is None:
            raise _AwaitNextPacket
        packet, self.packet = self.packet, None
        return packet

    def write(self, response):
        assert self.response is None
        self.response = response


class PeerChannel:
    """A real retained participant; only the process/pipe boundary is fake."""

    def __init__(self, harness, deadline):
        self.harness, self.stack = harness, ExitStack()
        self.participant = self.stack.enter_context(
            binding.open_participant(
                harness.vm_config,
                expected_source=harness.vm_source["revision"],
                expected_entry=harness.entry_sha,
                runner=harness.vm_runner,
                source_runner=harness.vm_source_runner,
                deadline=deadline,
                source_root=harness.vm_source["root"],
            )
        )
        self.calls, self.closed = [], False

    def exchange(self, packet, *, timeout):
        assert timeout > 0
        if self.closed:
            raise EOFError("owned fake participant already closed")
        self.harness.sync_vm()
        self.calls.append(copy.deepcopy(packet))
        frames = _PacketFrames(packet)
        try:
            self.participant.serve(frames)
        except _AwaitNextPacket:
            pass
        else:
            self.closed = True
        assert frames.response is not None
        fault = self.harness.lose_ack
        if fault == packet["verb"]:
            self.harness.lose_ack, self.closed = None, True
            raise OSError("owned fake peer lost acknowledgement")
        return frames.response

    def close(self):
        self.closed = True
        self.stack.close()


class ControllerHarness:
    """Compose domain inputs, not a replacement transaction implementation."""

    def __init__(
        self, test, *, registered_rollback=True, separate_catalogs=False
    ):
        self.test, self.stack = test, ExitStack()
        test.addCleanup(self.stack.close)
        directory = self.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="transition-controller-")
        )
        self.root = Path(directory)
        self.root.chmod(0o700)
        self.lose_ack, self.channels, self.owner_commands = None, [], []
        self.source_record_calls = {"snapshot": 0, "no_snapshot": 0}
        self.source_plan_calls = 0
        self.gpu_calls = 0
        self._storage_boundary()
        self.data = dual_snapshot(separate_catalogs=separate_catalogs)
        self._catalog_configurations(separate_catalogs)
        self._catalog_addresses()
        self.data.provider.backends = [self.data.hosts[0]]
        original_read = authority.read_private
        original_backend = authority.read_backend
        original_tls_proof = private_api_tls.gpu_ownership_proofs
        fixture_test = SimpleNamespace(
            addCleanup=self.stack.callback, assertEqual=test.assertEqual
        )
        self.native_source_record = catalog.source_record
        self.native = _checkpoint_evidence(fixture_test, self.data)
        self.stack.enter_context(
            patch.object(
                catalog, "require_clean_revision", require_clean_revision
            )
        )
        self.stack.enter_context(
            patch.object(catalog, "source_record", self.source_record)
        )
        self.native_gpu = gpu_collect.run_gpu_ownership
        self.stack.enter_context(
            patch.object(gpu_collect, "run_gpu_ownership", self.gpu_input)
        )
        self.provider = self.data.provider
        self.provider.__class__ = backend.TransitionProvider
        assert "_checkpoint_authorities" not in self.provider.__dict__
        assert "_checkpoint_runtime" not in self.provider.__dict__
        self.stack.enter_context(
            patch.object(
                catalog.product_config, "load_config", self.product_config
            )
        )
        self.provider.anchor["policy"] = {
            "schema_version": 1,
            "status": "compatible",
            "request_policy_sha256": contract.digest(self.native.policy),
            "policy": copy.deepcopy(self.native.policy),
        }
        self.provider.provider_sha256 = contract.digest(self.provider.anchor)
        self.provider.roles["provider_sha256"] = self.provider.provider_sha256
        self.stack.enter_context(
            patch.object(authority, "read_private", original_read)
        )
        self.stack.enter_context(
            patch.object(authority, "read_backend", original_backend)
        )
        self.stack.enter_context(
            patch.object(
                private_api_tls, "gpu_ownership_proofs", original_tls_proof
            )
        )
        self.sources = self.native.sources
        self.process = ProcessBoundary(self.data, self.sources)
        self.process.catalog_records = self.native.current["catalogs"]
        self.process.compute = "\n".join(
            f"{uuid},{index}"
            for index, uuid in enumerate(
                self.provider.config.gpus.all_uuids(), 100
            )
        ).encode()
        self.stack.enter_context(patch.object(build, "_run", self.process))
        self.stack.enter_context(
            patch.object(source_bundle, "_runner", self.process.git)
        )
        self.stack.enter_context(
            patch.object(
                source_bundle, "committed_source_plan", self.source_plan
            )
        )
        self.endpoint = self.root / "tls"
        self.endpoint.mkdir(mode=0o700)
        self.plans = [
            self.plan(index, host)
            for index, host in enumerate(self.data.hosts)
        ]
        self.process.activate(Path(self.plans[0]["plan"]["path"]).parent)
        self.source = {
            "root": str(self.root / "operator"),
            "revision": "e" * 40,
        }
        self.sources[Path(self.source["root"])] = self.source["revision"]
        self.control_file = self.root / "control.json"
        self.write(
            self.control_file,
            {"schema_version": 1, "state_root": str(self.root / "control")},
        )
        self._control(registered_rollback)
        self._vm()
        self._configuration()
        self._docker_boundary()
        self.stack.enter_context(
            patch.object(backend, "open_owner", self.open_owner)
        )
        self.stack.enter_context(
            patch.object(backend.runtime, "_backend", self.load_backend)
        )
        self.stack.enter_context(
            patch.object(
                authority, "_configuration", self.native_configuration
            )
        )
        self.stack.enter_context(
            patch.object(
                authority, "_provider_resources", self.provider_resources
            )
        )
        self.stack.enter_context(
            patch.object(
                candidate.Session,
                "candidate_inputs",
                lambda session, record: self.candidate_inputs(session, record),
            )
        )
        self.stack.enter_context(
            patch.object(candidate, "frozen_source", self.frozen_source)
        )
        if not registered_rollback:
            self._candidate()

    def _storage_boundary(self):
        self.stack.enter_context(
            patch.object(execution, "require_storage", return_value=object())
        )
        self.stack.enter_context(
            patch.object(execution, "validate_storage_mounts")
        )

    def _catalog_addresses(self):
        for index, selected in enumerate(self.data.catalogs):
            project = selected.context["plan"]["project"]
            name = project + "_search_backend"
            ids = set(
                selected.authority["catalog_identity"][
                    "core_container_ids"
                ].values()
            )
            rows = [
                row for row in self.data.groups[project] if row["Id"] in ids
            ]
            for offset, row in enumerate(rows, 2):
                address = f"172.19.{index}.{offset}"
                row["NetworkSettings"]["Networks"][name]["IPAddress"] = address
                self.data.networks[name]["Containers"][row["Id"]][
                    "IPv4Address"
                ] = address + "/24"

    def _catalog_configurations(self, separate):
        for index, selected in enumerate(self.data.catalogs):
            if separate:
                root = self.root / ("catalog-state-" + str(index))
                root.mkdir(mode=0o700)
                selected.config = replace(selected.config, state_root=root)
            selected.config_file = self.root / (
                "catalog-" + str(index) + ".json"
            )
            self.write(selected.config_file, selected.config.to_dict())
        for host in self.data.hosts:
            host.config = host.catalog.config
            host.config_file = host.catalog.config_file

    def product_config(self, path, **kwargs):
        path = Path(path)
        for selected in self.data.catalogs:
            if path == selected.config_file:
                contract.require(
                    path.read_bytes()
                    == private_io.canonical(selected.config.to_dict()),
                    "configuration_changed",
                )
                return selected.config
        if getattr(self, "candidate_catalog", None) == path:
            selected = self.data.hosts[1].catalog.config
            contract.require(
                path.read_bytes() == private_io.canonical(selected.to_dict()),
                "configuration_changed",
            )
            return selected
        if str(path) == self.provider.anchor["configuration"]["path"]:
            return self.provider.config
        raise AssertionError("unknown native configuration constructor input")

    def write(self, path, value):
        path.write_bytes(private_io.canonical(value))
        path.chmod(0o600)

    def source_plan(self, root, revision, *, runner=None):
        self.source_plan_calls += 1
        plans = next(iter(self.native.images.values())).shared["plans"]
        value = (
            self.provider.source_plan
            if root == self.provider.source_root
            else plans[root]
        )
        assert value["revision"] == revision
        return copy.deepcopy(value)

    def source_record(self, *args, **kwargs):
        kind = (
            "snapshot" if kwargs.get("snapshot") is not None else "no_snapshot"
        )
        self.source_record_calls[kind] += 1
        return self.native_source_record(*args, **kwargs)

    def plan(self, index, host):
        return tls_fixture.BackendTLSOwnerTests.plan(self, index, host)

    def gpu_input(self, config, *, allowed, runner, cpu_proofs=()):
        self.gpu_calls += 1
        value = self.native_gpu(config, allowed=allowed, runner=runner)
        return {**value, "sealed_cpu_container_count": len(cpu_proofs)}

    def _control(self, registered_rollback):
        with authority.open_control(self.control_file, create=True) as control:
            control.publish("provider", self.provider.anchor)
            for host in self.data.hosts:
                record = copy.deepcopy(self.data.hosts[0].record)
                image = self.native.images[host.source_revision]
                record.update(
                    source={
                        "root": str(host.source_root),
                        "revision": host.source_revision,
                    },
                    build_state_root=str(image.config.state_root),
                    compose=host.compose,
                    lease=host.lease,
                    runtime=host.expected_runtime,
                    image_proof=host.image_proof,
                    provider_sha256=self.provider.provider_sha256,
                    configuration=backend.reference(host.catalog.config_file),
                    catalog_source={
                        "root": str(host.context["repo"]),
                        "revision": host.context["source_revision"],
                    },
                    image_authority_sha256=host.image_binding.authority_sha256,
                    catalog_binding=host.catalog.identity(),
                    namespace_sha256=host.namespace_sha256,
                )
                host.record = copy.deepcopy(record)
                host.backend_sha256 = control.publish("backend", record)
            roles = {
                **self.provider.roles,
                "active_backend_sha256": self.data.hosts[0].backend_sha256,
                "rollback_backend_sha256": self.data.hosts[1].backend_sha256
                if registered_rollback
                else None,
                "tls_plan_ref": self.plans[0]["plan"],
            }
            initial = {**roles, "rollback_backend_sha256": None}
            control.adopt_roles(initial)
            if registered_rollback:
                roles["epoch"] = initial["epoch"] + 1
                control._compare_and_swap_roles(
                    roles,
                    expected_epoch=initial["epoch"],
                    expected_sha256=contract.digest(initial),
                )
            self.roles, self.roles_sha = (
                copy.deepcopy(roles),
                control.roles_sha256,
            )
            self.control_ref = backend.reference(self.control_file)
        enrollment.enroll(
            self.endpoint,
            participant="tls",
            source=self.source,
            configuration=self.control_ref,
        )
        self.product_root = self.root / "product"
        self.product_root.mkdir(mode=0o700)
        enrollment.enroll(
            self.product_root,
            participant="product",
            source=self.source,
            configuration=self.control_ref,
        )
        self.provider.stores = {self.product_root: object()}

    def _vm(self):
        self.vm_root = self.root / "vm"
        self.vm_root.mkdir(mode=0o700)
        self.vm_frontend = self.vm_root / "frontend"
        self.vm_frontend.mkdir(mode=0o700)
        self.vm_state = self.vm_root / "state"
        self.vm_state.mkdir(mode=0o700)
        monitor = self.vm_root / "monitor"
        monitor.mkdir(mode=0o700)
        lock = monitor / ".check.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        gh = self.vm_root / "gh"
        gh.write_bytes(b"#!/bin/sh\nexit 0\n")
        gh.chmod(0o700)
        env = self.vm_frontend / "web.env"
        env.write_bytes(b"WEB_UID=1003\n")
        env.chmod(0o600)
        key = self.vm_frontend / "api_key"
        key.write_bytes(b"a" * 64)
        key.chmod(0o600)
        self.canonical = self.vm_frontend / "check.json"
        profile = {
            "schema_version": 3,
            "deployment_mode": "intranet-http",
            "conversation_contract_version": 4,
            "frontend_env_file": str(env),
            "frontend_revision": FRONT,
            "api_revision": self.data.hosts[0].source_revision,
            "catalog_manifest_sha256": CATALOG,
            "web_platform_manifest": WEB_IMAGE,
            "web_nginx_platform_manifest": NGINX_IMAGE,
        }
        self.write(self.canonical, profile)
        self.vm_delivery = self.vm_root / "delivery.json"
        self.write(
            self.vm_delivery,
            {
                "schema_version": 1,
                "repository": delivery.REPOSITORY,
                "repository_id": 1425391257,
                "state_root": str(self.vm_state),
                "frontend_check_file": str(self.canonical),
                "gh_binary": str(gh),
                "monitor_lock_file": str(lock),
            },
        )
        self.vm_runner = VersionRunner(self.vm_frontend)
        self.vm_runner.backend_revision = self.data.hosts[0].source_revision
        self.stack.enter_context(patch.object(check, "_Endpoint", LocalHTTP))
        settings = delivery.load_config(self.vm_delivery)
        state = delivery.State(settings["state_root"])
        try:
            with state.locked():
                docker = Docker(self.root / "vm-client", self.vm_runner)
                owner = delivery.Delivery(
                    settings, state, docker, self.vm_runner
                )
                state.write("state.json", owner.baseline())
        finally:
            state.close()
        self._vm_participant()

    def _vm_participant(self):
        program = self.vm_root / "operator"
        (program / "scripts").mkdir(parents=True, mode=0o700)
        entry = program / "scripts/participate_backend_binding.py"
        entry.write_bytes(b"# owned source-identity fixture\n")
        self.entry_sha = hashlib.sha256(entry.read_bytes()).hexdigest()
        self.vm_source = {"root": str(program), "revision": "e" * 40}
        self.sources[program] = self.vm_source["revision"]
        self.vm_config = self.vm_root / "participant.json"
        self.write(
            self.vm_config,
            {
                "schema_version": 1,
                "kind": "frontend-binding-participant",
                "source": self.vm_source,
                "delivery_configuration": backend.reference(self.vm_delivery),
                "coordinator_peer_sha256": "7" * 64,
            },
        )
        enrollment.enroll(
            self.vm_state,
            participant="frontend",
            source=self.vm_source,
            configuration=backend.reference(self.vm_config),
        )

    def _configuration(self):
        self.transport_file = self.root / "transport.json"
        self.write(
            self.transport_file,
            {
                "schema_version": 1,
                "hops": [
                    {
                        "destination": "operator@example.test",
                        "ssh_config": None,
                    }
                ],
                "participant_source": self.vm_source,
                "participant_config": backend.reference(self.vm_config),
                "entry_sha256": self.entry_sha,
                "peer_sha256": "8" * 64,
            },
        )
        self.settings_file = self.root / "controller.json"
        self.write(
            self.settings_file,
            {
                "schema_version": 1,
                "kind": "backend-binding-controller",
                "operator": self.source,
                "control_config": self.control_ref,
                "frontend_transport": backend.reference(self.transport_file),
                "backend_peer_sha256": "7" * 64,
            },
        )
        self.settings = controller.Settings(
            self.settings_file,
            source_runner=self.vm_source_runner,
            source_root=self.source["root"],
        )

    def vm_source_runner(self, argv, *, timeout):
        value = self.process.git(argv, timeout=timeout)
        return SimpleNamespace(
            returncode=value.returncode,
            stdout=value.stdout.decode(),
            stderr="",
        )

    def sync_vm(self):
        current = self.process.current
        if current is None:
            self.vm_runner.backend_revision = None
            return
        mode = next(
            row["NetworkMode"]
            for row in current.owned.values()
            if row["NetworkMode"].startswith("container:")
        )
        host = next(
            host
            for host in self.data.hosts
            if host.lease["api_container_id"] in mode
        )
        self.vm_runner.backend_revision = host.source_revision

    def session(self, profile, *, deadline):
        def channel(_argv, *, deadline):
            value = PeerChannel(self, deadline)
            self.channels.append(value)
            return value

        return transport.FrontendSession(
            profile, deadline=deadline, channel_factory=channel
        )

    def _docker_boundary(self):
        original = self.provider.docker.runner
        self.cleanup_docker = CleanupDocker(
            self.data.groups, self.data.networks
        )

        def runner(argv, **kwargs):
            args = argv[5:]
            if args[0] == "rm" or args[:2] == ["network", "rm"]:
                self.cleanup_docker.call(args)
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return original(argv, **kwargs)

        self.process.cpu_runner = runner

    def load_backend(self, provider, record, digest):
        host = next(
            h
            for h in self.data.hosts
            if h.source_revision == record["source"]["revision"]
        )
        contract.require(
            record["compose"] == host.compose
            and record["lease"] == host.lease
            and record["runtime"] == host.expected_runtime,
            "authority_changed",
        )
        host.record, host.backend_sha256 = copy.deepcopy(record), digest
        return host

    def _candidate(self):
        host = self.data.hosts[1]
        model_identity = self.provider.model_file_identity_sha256
        root = self.root / "candidate"
        root.mkdir(mode=0o700)
        self.candidate_profile = self.root / "candidate.json"
        self.candidate_catalog = host.catalog.config_file
        self.write(
            self.candidate_profile,
            {
                "schema_version": 2,
                "state_root": str(root),
                "control_config": str(self.control_file),
                "catalog_config": str(self.candidate_catalog),
                "catalog_source": str(host.context["repo"]),
            },
        )
        self.candidate_record = {
            "schema_version": 2,
            "phase": "running",
            "revision": host.source_revision,
            "container_ids": host.expected_runtime["container_ids"],
            "runtime": host.expected_runtime,
            "lease": host.lease,
            "compose": host.compose,
            "policy": self.provider.anchor["policy"],
            "provider_authority_sha256": self.provider.provider_sha256,
            "roles_epoch": self.roles["epoch"],
            "roles_sha256": self.roles_sha,
            "model_file_identity_sha256": model_identity,
            "compute_sha256": self.provider.anchor["compute_sha256"],
            "catalog_binding": host.catalog.identity(),
        }
        self.write(root / "current.json", self.candidate_record)

    def native_configuration(self, reference, source):
        if reference == self.provider.anchor["configuration"]:
            return self.provider.config
        for selected in self.data.catalogs:
            if reference == backend.reference(selected.config_file):
                return self.product_config(selected.config_file)
        raise contract.TransitionError("backend_transition_authority_changed")

    def provider_resources(self, anchor):
        contract.require(anchor == self.provider.anchor, "authority_changed")
        provider = self.provider
        return {
            "container_ids": [
                row["Id"]
                for row in self.data.groups[provider.inference_compose["name"]]
            ],
            "network_ids": [
                self.data.networks[value["name"]]["Id"]
                for value in provider.inference_compose["networks"].values()
            ],
            "owned_paths": [
                str(provider.source_root),
                provider.anchor["configuration"]["path"],
                str(provider.config.state_root),
                str(provider.config.api_model_root),
                str(provider.config.embedding_model_root),
                str(provider.config.reranker_model_root),
            ],
        }

    def frozen_source(self, profile, revision):
        host = self.data.hosts[1]
        contract.require(host.source_revision == revision, "candidate_changed")
        return host.source_root

    def candidate_inputs(self, session, record):
        host = self.data.hosts[1]
        contract.require(
            record["revision"] == host.source_revision
            and record["compose"] == host.compose,
            "candidate_changed",
        )
        session._candidate_image_binding = host.image_binding
        return host.compose, host.images, host.image_proof

    @contextmanager
    def native_counts(self, commands):
        before = dict(self.source_record_calls)
        plans = self.source_plan_calls
        gpu = self.gpu_calls
        try:
            yield
        finally:
            commands.fixture_source_record_calls = {
                key: value - before[key]
                for key, value in self.source_record_calls.items()
            }
            commands.fixture_source_plan_calls = self.source_plan_calls - plans
            commands.fixture_gpu_calls = self.gpu_calls - gpu

    @contextmanager
    def open_owner(
        self,
        control,
        *,
        source,
        tls_plan,
        operation=None,
        profile=None,
        candidate_store=None,
        deadline=None,
        commands=None,
    ):
        provider = self.provider
        provider.control, provider.operation = control, operation
        provider.roles = copy.deepcopy(
            control.roles
            if operation is None
            else operation.intent["predecessor_roles"]
        )
        provider.roles_sha256 = contract.digest(provider.roles)
        provider.deadline = (
            time.monotonic() + contract.MAX_SECONDS
            if deadline is None
            else deadline
        )
        provider.commands = (
            backend.OwnerCommands(provider.deadline)
            if commands is None
            else commands
        )
        provider.source_plans = {}
        self.owner_commands.append(provider.commands)
        provider.selected_tls, provider.cleanup = (
            control.roles["tls_plan_ref"],
            None,
        )
        provider.profile, provider._topology = profile, None
        provider.selected_catalog = lambda: self.data.hosts[1].catalog
        provider.docker.calls = 0
        provider.docker.deadline = provider.deadline
        provider.docker.runner = provider.commands.process
        for selected in self.data.catalogs:
            selected.context.update(
                source_runner=provider.commands.git_text,
                process_runner=provider.commands.process,
                audit_budget={"commands": 256, "seconds": 120},
            )
        names = [
            provider.roles["active_backend_sha256"],
            provider.roles["rollback_backend_sha256"],
        ]
        provider.backends = [
            h
            for name in names
            for h in self.data.hosts
            if h.backend_sha256 == name
        ]
        with (
            self.native_counts(provider.commands),
            enrollment.locked(self.endpoint) as fd,
        ):
            tls = backend.TLSOwner(
                provider, self.endpoint, fd, source, self.control_ref
            )
            owner = backend.BackendOwner(
                provider, tls, profile=profile, candidate_store=candidate_store
            )
            yield owner
            provider.refresh()
