"""Compose retained provider, catalog, candidate and TLS runtime owners."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
import copy
from dataclasses import replace
import hashlib
from pathlib import Path
import time
from types import SimpleNamespace

from search.operations.catalog import execution
from search.operations.deployment import private_api_tls
from search.operations.deployment.candidate import catalog
from search.operations.deployment.candidate import controller as candidate
from search.operations.deployment.provider import authority
from search.operations.deployment.provider import runtime
from search.operations.shared.source_identity import require_clean_revision
from search.serialization import strict_json

from . import contract, enrollment


CLEANUP_COMMANDS = 512
CLEANUP_SECONDS = 120
PHASE_LIMITS = {
    "prepare": (8192, 600),
    "open": (8192, 600),
    "begin": (8192, 600),
    "terminal": (8192, 900),
    "cleanup": (8192, 900),
    "release": (8192, 600),
}


class OwnerCommands:
    """Count one retained backend invocation's explicit external runners."""

    def __init__(self, deadline):
        self.deadline, self.calls = deadline, 0
        self.by_kind = {}
        self.parent_attempts, self.child_charges = 0, 0
        self.cleanup_calls, self.cleanup_deadline = 0, None
        self.phase_name, self.phase_deadline = None, None
        self.by_phase, self.phase_seconds = {}, {}

    @contextmanager
    def phase(self, name):
        contract.require(self.phase_name is None, "phase_busy")
        contract.choice(name, PHASE_LIMITS, "phase_invalid")
        credits, seconds = PHASE_LIMITS[name]
        used = self.by_phase.get(name, 0)
        elapsed = self.phase_seconds.get(name, 0)
        contract.require(used < credits and elapsed < seconds, "phase_budget")
        now = time.monotonic()
        contract.require(
            contract.BACKEND_COMMAND_CREDITS - self.calls
            >= credits - used + CLEANUP_COMMANDS - self.cleanup_calls,
            "phase_budget",
        )
        contract.require(
            self.deadline - now >= seconds - elapsed + CLEANUP_SECONDS,
            "phase_deadline",
        )
        self.phase_name, self.phase_deadline = name, now + seconds - elapsed
        try:
            yield
        finally:
            self.phase_seconds[name] = elapsed + time.monotonic() - now
            self.phase_name, self.phase_deadline = None, None

    def remaining(self, kind, timeout, *, units=1):
        return self._remaining(kind, timeout, units=units, cleanup=False)

    def _remaining(self, kind, timeout, *, units=1, cleanup):
        contract.require(type(units) is int and units >= 1, "command_budget")
        now = time.monotonic()
        end = self.deadline - CLEANUP_SECONDS
        reserve = CLEANUP_COMMANDS - self.cleanup_calls
        if cleanup:
            if self.cleanup_deadline is None:
                self.cleanup_deadline = min(
                    self.deadline, now + CLEANUP_SECONDS
                )
            end, reserve = self.cleanup_deadline, 0
            contract.require(
                self.cleanup_calls + units <= CLEANUP_COMMANDS,
                "cleanup_budget",
            )
        elif self.phase_deadline is not None:
            end = min(end, self.phase_deadline)
        left = end - now
        contract.require(left > 0, "deadline")
        contract.require(
            self.calls + units + reserve <= contract.BACKEND_COMMAND_CREDITS,
            "command_budget",
        )
        self._charge_phase(units, cleanup=cleanup)
        self.calls += units
        self.parent_attempts += 1
        self.child_charges += units - 1
        self.by_kind[kind] = self.by_kind.get(kind, 0) + 1
        return min(timeout, left)

    def _charge_phase(self, units, *, cleanup):
        if cleanup:
            self.cleanup_calls += units
            return
        name = self.phase_name or "admission"
        used = self.by_phase.get(name, 0) + units
        if self.phase_name is not None:
            contract.require(used <= PHASE_LIMITS[name][0], "phase_budget")
        self.by_phase[name] = used

    def cleanup_process(self, argv, **kwargs):
        from search.operations.deployment.images import build

        kwargs["timeout"] = self._remaining(
            "failure_cleanup", kwargs["timeout"], cleanup=True
        )
        return build._run(argv, **kwargs)

    def process(self, argv, **kwargs):
        from search.operations.deployment.images import build

        kind, units = "process", 1
        if (
            argv[:5] == ["python3", "-I", "-S", "-B", "-c"]
            and argv[5] == catalog._BOUNDED_CONTEXT
        ):
            catalog._audit_budget(
                {"commands": int(argv[10]), "seconds": float(argv[11])}
            )
            kind, units = "catalog_capsule", 1 + int(argv[10])
        kwargs["timeout"] = self.remaining(
            kind, kwargs["timeout"], units=units
        )
        return build._run(argv, **kwargs)

    def git(self, argv, *, timeout):
        from search.operations.deployment import source_bundle

        timeout = self.remaining("source_git", timeout)
        return source_bundle._runner(argv, timeout=timeout)

    def git_text(self, argv, *, timeout):
        result = self.git(argv, timeout=timeout)
        return SimpleNamespace(
            returncode=result.returncode,
            stdout=result.stdout.decode("utf-8"),
            stderr="",
        )

    def gpu(self, argv, *, timeout, max_bytes):
        from search.operations.deployment.gpu import collect

        timeout = self.remaining("gpu", timeout)
        return collect.bounded_runner(
            argv, timeout=timeout, max_bytes=max_bytes
        )


def reference(path):
    raw = authority.read_private(path)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def read_reference(value):
    contract.reference(value)
    raw = authority.read_private(value["path"])
    contract.require(
        hashlib.sha256(raw).hexdigest() == value["sha256"], "input_changed"
    )
    return strict_json(raw)


def tls_inputs(value):
    path = Path(value["path"])
    plan = private_api_tls._read_plan(path)
    contract.require(reference(value["path"]) == value, "input_changed")
    compose = strict_json(
        authority.read_private(Path(value["path"]).parent / "compose.json")
    )
    result = {
        "plan": copy.deepcopy(value),
        **{
            key: copy.deepcopy(plan[key])
            for key in contract.TLS_FIELDS
            if key in plan
        },
        "project": compose["name"],
        "compose_sha256": contract.digest(compose),
    }
    contract.tls(result)
    for name, expected in plan["files"].items():
        contract.require(
            reference(path.parent / name)["sha256"] == expected,
            "input_changed",
        )
    return result


def tls_projection(value, *, runner):
    private_api_tls.check(Path(value["path"]), runner=runner)
    return tls_inputs(value)


class TransitionProvider(runtime.Provider):
    """Admit one sealed operation, retaining normal provider checks."""

    def __init__(
        self,
        control,
        stack,
        deadline,
        *,
        operation=None,
        profile=None,
        commands=None,
    ):
        self.commands = (
            OwnerCommands(deadline) if commands is None else commands
        )
        deadline = min(deadline, self.commands.deadline)
        self.source_plans = {}
        super().__init__(control, stack, deadline)
        self.operation, self.profile = operation, profile
        self.selected_tls = self.roles["tls_plan_ref"]
        self.cleanup = None
        if operation is not None:
            self.roles = copy.deepcopy(operation.intent["predecessor_roles"])
            self.roles_sha256 = contract.digest(self.roles)

    @property
    def source_options(self):
        return {"runner": self.commands.git_text}

    def _source(self, value):
        root = Path(value["root"])
        contract.require(
            require_clean_revision(root, runner=self.commands.git_text)
            == value["revision"],
            "source_changed",
        )
        return root

    def _source_plan(self, root, revision):
        from search.operations.deployment import source_bundle

        self._source({"root": str(root), "revision": revision})
        key = (str(root), revision)
        if key not in self.source_plans:
            plan = source_bundle.committed_source_plan(
                root, revision, runner=self.commands.git
            )
            self.source_plans[key] = copy.deepcopy(plan)
        return copy.deepcopy(self.source_plans[key])

    def _docker(self):
        super()._docker()
        self.docker.runner = self.commands.process

    def _catalog_context(self, source, filename, config, store):
        context, value = catalog.load_context(
            source,
            filename,
            config,
            store,
            source_runner=self.commands.git_text,
            process_runner=self.commands.process,
            audit_budget={"commands": 256, "seconds": 120},
        )
        original = context["runner"]

        def native(argv, *, timeout):
            if argv[0] == "docker":
                raw = self.docker.call(argv[1:], timeout=timeout)
                return SimpleNamespace(
                    returncode=0, stdout=raw.decode("utf-8"), stderr=""
                )
            timeout = self.commands.remaining("catalog_runtime", timeout)
            return original(argv, timeout=timeout)

        context["runner"] = native
        return context, value

    def _tls_runner(self, argv, *, deadline=None, **kwargs):
        return self.commands.process(
            argv,
            timeout=self.remaining(kwargs["timeout"], deadline=deadline),
            environment={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
            },
        )

    def _gpu_runner(self, argv, *, timeout, max_bytes, deadline=None):
        return self.commands.gpu(
            argv,
            timeout=self.remaining(timeout, deadline=deadline),
            max_bytes=max_bytes,
        )

    def _compute_process(self, *, deadline=None):
        return self.commands.process(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid",
                "--format=csv,noheader,nounits",
            ],
            timeout=self.remaining(15, deadline=deadline),
            environment={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
            },
        )

    def _roles_admission(self):
        if self.operation is None:
            return super()._roles_admission()
        self.operation.refresh()
        contract.require(
            self.roles == self.operation.intent["predecessor_roles"],
            "cas_conflict",
        )

    def _tls_reference(self):
        return self.selected_tls

    def _records(self):
        names = [
            self.roles["active_backend_sha256"],
            self.roles["rollback_backend_sha256"],
        ]
        records = [
            (name, authority.read_backend(self.control, name))
            for name in names
            if name is not None
        ]
        extra = None
        if self.operation is not None:
            proposed = self.operation.intent["candidate"]
            if proposed is not None:
                name = proposed["backend"]["sha256"]
                extra = (name, authority.read_backend(self.control, name))
        return records, extra

    def _lock_states(self):
        self.backend_records, extra = self._records()
        configurations, catalogs = {self.config.state_root: self.config}, {}
        records = [*self.backend_records, *([] if extra is None else [extra])]
        for _name, record in records:
            value = runtime.configuration(
                record["configuration"],
                self._source(record["catalog_source"]),
            )
            catalogs[value.state_root] = value
            for item in (
                value,
                replace(value, state_root=Path(record["build_state_root"])),
            ):
                contract.require(
                    configurations.get(item.state_root, item) == item
                )
                configurations[item.state_root] = item
        self._candidate_states(configurations, catalogs)
        for root in sorted(configurations, key=str):
            self.store(configurations[root])
        locks = {}
        for value in catalogs.values():
            plan = self.store(value).read("index-candidate.json")
            data = Path(contract.path(plan["data_root"]))
            locks[data] = {"data": data}
        for root in sorted(locks, key=str):
            self.stack.enter_context(execution.operation_lock(locks[root]))

    def _candidate_states(self, configurations, catalogs):
        if self.profile is None:
            return
        selected = runtime.configuration(
            reference(self.profile["catalog_config"]),
            self.profile["catalog_source"],
        )
        contract.require(
            replace(selected, state_root=self.config.state_root)
            == self.config,
            "configuration_changed",
        )
        record = self.profile["_candidate_record"]
        contract.require(
            selected.state_root == self.config.state_root
            or selected.state_root not in self.config.state_root.parents
            and self.config.state_root not in selected.state_root.parents,
            "state_overlap",
        )
        build = replace(
            selected,
            state_root=candidate.build_root(self.profile, record["revision"]),
        )
        for item in (selected, build):
            contract.require(configurations.get(item.state_root, item) == item)
            configurations[item.state_root] = item
        catalogs[selected.state_root] = selected

    def selected_catalog(self):
        from search.operations.deployment import credentials

        source, filename = (
            self.profile["catalog_source"],
            self.profile["catalog_config"],
        )
        config = runtime.configuration(reference(filename), source)
        for host in self.backends:
            binding = host.catalog
            if binding.config.state_root == config.state_root:
                contract.require(
                    binding.config == config
                    and binding.context["repo"] == source,
                    "catalog_changed",
                )
                return binding
        store = self.store(config)
        context, value = self._catalog_context(source, filename, config, store)
        catalog.validate_chunk_profile(context)
        inspections = {
            role: self.docker.inspect(proof["image_id"])
            for role, proof in value["images"].items()
        }
        binding = catalog.Catalog(
            filename,
            config,
            store,
            context,
            value["authority"],
            value["images"],
            inspections,
            credentials.read_api_key_authority(config, store),
        )
        binding.refresh()
        return binding

    def _checkpoint_runtime(self, *, deadline=None):
        if self.cleanup is None:
            return super()._checkpoint_runtime(deadline=deadline)
        self.cleanup.observe()


class TLSOwner:
    """Retain the endpoint lock and observe the selected actual TLS pair."""

    def __init__(self, provider, root, fd, source, configuration):
        self.provider, self.root = provider, root
        enrolled = enrollment.owner(root)
        contract.require(enrolled is not None, "enrollment_required")
        contract.require(
            enrolled["participant"] == "tls"
            and enrolled["source"] == source
            and enrolled["configuration"] == configuration,
            "owner_changed",
        )
        self.reservation = enrollment.BindingReservation(root, fd, enrolled)
        self.dispatched = False

    def preflight(self):
        self.reservation.refresh()
        enrollment.require_unreserved(self.root)

    def prepare(self, operation):
        self.reservation.refresh()
        for side in contract.SIDES:
            expected = operation.intent["sides"][side]["tls"]
            contract.require(
                enrollment.endpoint_root(Path(expected["plan"]["path"]))
                == self.root,
                "namespace_changed",
            )
            contract.require(
                tls_inputs(expected["plan"]) == expected,
                "input_changed",
            )
        if self.reservation.released(operation.intent) is None:
            self.reservation.prepare(operation.intent)

    def runner(self, argv, **kwargs):
        # The provider's injected runner owns runtime command accounting.
        return self.provider._tls_runner(argv, **kwargs)

    def cleanup_runner(self, argv, **kwargs):
        return self.provider.commands.cleanup_process(
            argv,
            timeout=kwargs["timeout"],
            environment={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
            },
        )

    def observe(self, operation, side):
        self.reservation.require_side(operation.intent, side)
        expected = operation.intent["sides"][side]["tls"]
        observed = tls_projection(expected["plan"], runner=self.runner)
        contract.require(observed == expected, "binding_changed")
        path = Path(expected["plan"]["path"])
        plan = private_api_tls._read_plan(path)
        rows = private_api_tls._owned(
            path, plan, self.runner, healthy=True, with_roles=True
        )
        self.provider.selected_tls = expected["plan"]
        return {
            "binding_sha256": contract.digest(observed),
            "container_ids": rows,
            "reservation": self.reservation.current(operation.intent),
        }

    def require_undispatched(self, operation):
        contract.require(not self.dispatched, "dispatched")
        contract.require(operation.read("tls-dispatch") is None, "dispatched")

    def _dispatch(self, operation, side):
        contract.require(operation.refresh() == ("pending", None), "phase")
        operation._side_admission(side)
        operation.ensure(
            "tls-dispatch",
            {
                "schema_version": 1,
                "operation_id": operation.operation_id,
                "intent_sha256": operation.sha256,
            },
        )
        self.dispatched = True

    def publish(self, operation, side):
        """Allow a bounded TLS gap; never auto-restore the old backend."""
        self.reservation.require_side(operation.intent, side)
        contract.require(
            self.reservation.released(operation.intent) is None,
            "already_released",
        )
        self._dispatch(operation, side)
        selected = operation.intent["sides"][side]["tls"]
        current = self._current_plan(operation)
        if current is not None:
            private_api_tls._operate_owned("stop", current, runner=self.runner)
        private_api_tls._operate_owned(
            "up",
            selected["plan"]["path"],
            runner=self.runner,
            cleanup_runner=self.cleanup_runner,
        )
        self.reservation.mark(operation.intent, "published")
        return self.observe(operation, side)

    def _current_plan(self, operation):
        expected = operation.intent["sides"]["predecessor"]["tls"]
        raw = private_api_tls._output(
            [
                "docker",
                "ps",
                "--all",
                "--no-trunc",
                "--filter",
                "label=com.docker.compose.project=" + expected["project"],
                "--format",
                "{{.ID}}",
            ],
            self.runner,
        )
        identifiers = raw.decode("ascii").splitlines()
        contract.require(
            len(identifiers) <= 2
            and len(set(identifiers)) == len(identifiers),
            "runtime_changed",
        )
        if not identifiers:
            return None
        paths = set()
        for identifier in identifiers:
            contract.hexadecimal(identifier)
            row = private_api_tls._one(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{json .Config.Labels}}",
                    identifier,
                ],
                self.runner,
            )
            paths.add(row.get("com.docker.compose.project.config_files"))
        admitted = {
            str(
                Path(binding["tls"]["plan"]["path"]).parent / "compose.json"
            ): binding["tls"]["plan"]["path"]
            for binding in operation.intent["sides"].values()
        }
        contract.require(
            len(paths) == 1 and next(iter(paths)) in admitted,
            "runtime_changed",
        )
        selected = admitted[next(iter(paths))]
        plan = private_api_tls._read_plan(Path(selected))
        observed = private_api_tls._owned(Path(selected), plan, self.runner)
        contract.require(set(observed) == set(identifiers), "runtime_changed")
        return selected

    def release(self, operation, decision):
        observation = self.observe(operation, decision["side"])
        return self.reservation.release(
            operation.intent, decision, observation
        )


class BackendOwner:
    """Own actual provider/catalog/model/TLS evidence for one operation."""

    def __init__(self, provider, tls, *, profile=None, candidate_store=None):
        self.provider, self.tls = provider, tls
        self.profile, self.candidate_store = profile, candidate_store
        self.operation = provider.operation
        self.proposed = None
        if (
            self.operation is not None
            and self.operation.intent["candidate"] is not None
        ):
            ref = self.operation.intent["candidate"]["backend"]
            record = authority.read_backend(provider.control, ref["sha256"])
            contract.require(reference(ref["path"]) == ref, "input_changed")
            self.proposed = runtime._backend(provider, record, ref["sha256"])

    def members(self):
        return [
            *self.provider.backends,
            *([] if self.proposed is None else [self.proposed]),
        ]

    def seal_candidate(self, profile_reference):
        """Use the original E admission before publishing immutable B facts."""
        contract.require(self.operation is None, "phase")
        record = candidate.current(self.candidate_store)
        contract.require(
            record is not None and record["phase"] == "running",
            "candidate_changed",
        )
        candidate.require_binding(self.provider.control, record)
        candidate.disposable(self.profile, record)
        contract.require(
            record == self.profile["_candidate_record"], "candidate_changed"
        )
        session = candidate.Session(
            self.profile,
            self.provider,
            self.provider.control,
            self.provider.selected_catalog(),
        )
        session.attach_record(record)
        session.verify(record["policy"]["policy"], record["compute_sha256"])
        snapshot = self.provider.observe(session.members(), session.catalogs())
        host = session.candidate
        value = self._candidate_backend(host, record, snapshot)
        name = self.provider.control.publish("backend", value)
        host.record, host.backend_sha256 = value, name
        host.namespace_sha256 = value["namespace_sha256"]
        self.proposed = host
        snapshot_name = (
            "candidate-snapshot-" + contract.digest(record) + ".json"
        )
        try:
            existing = self.candidate_store.read(snapshot_name)
        except FileNotFoundError:
            self.candidate_store.write(snapshot_name, record)
        else:
            contract.require(existing == record, "candidate_changed")
        return {
            "profile": copy.deepcopy(profile_reference),
            "journal_root": str(self.candidate_store.root),
            "record": reference(self.candidate_store.root / snapshot_name),
            "original_epoch": record["roles_epoch"],
            "original_roles_sha256": record["roles_sha256"],
            "backend": reference(
                self.provider.control.root / ("backend-" + name + ".json")
            ),
        }

    def _candidate_backend(self, host, record, snapshot):
        binding = host.image_binding
        return {
            "schema_version": 1,
            "kind": "backend",
            "source": {
                "root": str(host.source_root),
                "revision": record["revision"],
            },
            "configuration": reference(self.profile["catalog_config"]),
            "catalog_source": {
                "root": str(self.profile["catalog_source"]),
                "revision": host.context["source_revision"],
            },
            "build_state_root": str(
                candidate.build_root(self.profile, record["revision"])
            ),
            "compose": copy.deepcopy(host.compose),
            "lease": copy.deepcopy(host.lease),
            "runtime": copy.deepcopy(record["runtime"]),
            "image_proof": copy.deepcopy(host.image_proof),
            "image_authority_sha256": binding.authority_sha256,
            "catalog_binding": host.catalog.identity(),
            "owned_paths": [
                str(host.source_root),
                str(candidate.build_root(self.profile, record["revision"])),
            ],
            "provider_sha256": self.provider.provider_sha256,
            "namespace_sha256": runtime.namespace_digest(
                snapshot["groups"][host.compose["name"]]
            ),
        }

    def catalogs(self):
        return list(
            {
                host.catalog.context["plan"]["project"]: host.catalog
                for host in self.members()
            }.values()
        )

    def bind(self, operation):
        contract.require(
            self.provider.roles == operation.intent["predecessor_roles"],
            "cas_conflict",
        )
        self.operation = self.provider.operation = operation

    def prepare(self, operation):
        self.bind(operation)
        self.tls.prepare(operation)
        self._candidate_snapshot()

    def _candidate_snapshot(self):
        value = self.operation.intent["candidate"]
        if value is None:
            return
        record = read_reference(value["record"])
        contract.require(
            record["roles_epoch"] == value["original_epoch"]
            and record["roles_sha256"] == value["original_roles_sha256"]
            and record["provider_authority_sha256"]
            == self.provider.provider_sha256,
            "candidate_changed",
        )
        contract.require(
            record["container_ids"]
            == self.proposed.expected_runtime["container_ids"]
            and record["runtime"] == self.proposed.expected_runtime,
            "candidate_changed",
        )

    def observe(self, operation, side):
        self.bind(operation)
        operation._side_admission(side)
        self._candidate_snapshot()
        if self.provider.cleanup is None:
            snapshot = self.provider.observe(self.members(), self.catalogs())
        else:
            snapshot = self.provider.cleanup.observe()
            contract.require(
                self.provider.cleanup.proof()["runtime_lease_closed"],
                "cleanup_incomplete",
            )
        tls = self.tls.observe(operation, side)
        self._audit(snapshot)
        self.provider.check_models(self.provider.anchor["policy"]["policy"])
        self.provider.refresh()
        return {
            "provider_sha256": self.provider.provider_sha256,
            "gpu": snapshot["gpu"],
            "backend_proofs": snapshot["backend_proofs"],
            "catalog_snapshots_sha256": contract.digest(
                snapshot["catalog_snapshots"]
            ),
            "tls": tls,
            "cleanup": None
            if self.provider.cleanup is None
            else self.provider.cleanup.proof(),
        }

    def _audit(self, snapshot):
        for binding in self.catalogs():
            binding.audit(
                snapshot["catalog_snapshots"][
                    binding.context["plan"]["project"]
                ],
                timeout=self.provider.remaining(180),
            )

    def require_undispatched(self, operation):
        self.tls.require_undispatched(operation)
        contract.require(
            operation.read("discard") is None
            and operation.read("retire-dispatch") is None,
            "dispatched",
        )

    def release(self, operation, decision):
        return self.tls.release(operation, decision)


@contextmanager
def open_owner(
    control,
    *,
    source,
    tls_plan,
    deadline=None,
    operation=None,
    profile=None,
    candidate_store=None,
    commands=None,
):
    """Control → candidate → product states → catalogs → endpoint lock."""
    with ExitStack() as stack:
        provider = TransitionProvider(
            control,
            stack,
            time.monotonic() + contract.MAX_SECONDS
            if deadline is None
            else deadline,
            operation=operation,
            profile=profile,
            commands=commands,
        )
        provider.load()
        provider.refresh()
        root = enrollment.endpoint_root(Path(tls_plan["path"]))
        fd = stack.enter_context(enrollment.locked(root))
        config_ref = {
            "path": str(control.path),
            "sha256": control.config_sha256,
        }
        tls = TLSOwner(provider, root, fd, source, config_ref)
        owner = BackendOwner(
            provider, tls, profile=profile, candidate_store=candidate_store
        )
        yield owner
        provider.refresh()
