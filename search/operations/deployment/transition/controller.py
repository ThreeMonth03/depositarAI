"""Run one complete backend operation through its retained actual owners."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import copy
import json
import os
from pathlib import Path
import sys
import time
import uuid
from functools import wraps

from search.operations.deployment.candidate import controller as candidate
from search.operations.deployment.provider import authority
from search.operations.shared.source_identity import require_clean_revision

from . import backend, contract, enrollment, journal, retirement, transport


CONFIG_FIELDS = {
    "schema_version",
    "kind",
    "operator",
    "control_config",
    "frontend_transport",
    "backend_peer_sha256",
}


class Settings:
    def __init__(self, filename, *, source_runner=None, source_root=None):
        self.reference = backend.reference(filename)
        self.value = backend.read_reference(self.reference)
        contract.version(
            self.value, CONFIG_FIELDS, "backend-binding-controller"
        )
        contract.source(self.value["operator"])
        loaded = (
            Path(__file__).resolve().parents[4]
            if source_root is None
            else Path(source_root).resolve()
        )
        contract.require(
            Path(self.value["operator"]["root"]) == loaded, "source_changed"
        )
        for key in ("control_config", "frontend_transport"):
            contract.reference(self.value[key])
        contract.hexadecimal(self.value["backend_peer_sha256"])
        self.source_runner = source_runner
        self.commands = backend.OwnerCommands(
            time.monotonic() + contract.MAX_SECONDS
        )
        self._used, self._active = False, False
        self.refresh()

    @contextmanager
    def invocation(self):
        contract.require(not self._active, "invocation_busy")
        if self._used:
            self.commands = backend.OwnerCommands(
                time.monotonic() + contract.MAX_SECONDS
            )
        self._active, self._used = True, True
        try:
            self.refresh()
            yield
        finally:
            self._active = False

    def _source_call(self, argv, *, timeout):
        if self.source_runner is None:
            return self.commands.git_text(argv, timeout=timeout)
        timeout = self.commands.remaining("operator_source", timeout)
        return self.source_runner(argv, timeout=timeout)

    def refresh(self):
        contract.require(
            backend.read_reference(self.reference) == self.value,
            "configuration_changed",
        )
        source = self.value["operator"]
        contract.require(
            require_clean_revision(Path(source["root"]), self._source_call)
            == source["revision"],
            "source_changed",
        )
        contract.require(
            backend.reference(self.value["control_config"]["path"])
            == self.value["control_config"],
            "configuration_changed",
        )

    @property
    def control_path(self):
        return self.value["control_config"]["path"]

    def peer(self):
        self.refresh()
        return transport.load_profile(self.value["frontend_transport"])


def _invocation(phase=None):
    def decorate(function):
        @wraps(function)
        def run(settings, *args, **kwargs):
            with settings.invocation(), ExitStack() as stack:
                if phase is not None:
                    stack.enter_context(settings.commands.phase(phase))
                return function(settings, *args, **kwargs)

        return run

    return decorate


def _frontend_target(before, revision):
    target = copy.deepcopy(before)
    target["profile"]["api_revision"] = revision
    target["current"]["profile"] = copy.deepcopy(target["profile"])
    target["canonical_profile"]["sha256"] = contract.digest(target["profile"])
    target["state"]["sha256"] = contract.digest(
        {
            "schema_version": 1,
            "current": target["current"],
            "previous": target["previous"],
        }
    )
    return target


def _backend_reference(control, name):
    return backend.reference(control.root / ("backend-" + name + ".json"))


def _exact(control, epoch, roles_sha256):
    contract.integer(epoch)
    contract.hexadecimal(roles_sha256)
    control.refresh()
    contract.require(
        control.roles["epoch"] == epoch
        and control.roles_sha256 == roles_sha256,
        "cas_conflict",
    )


def _profile(reference, control):
    if reference is None:
        return None
    value = candidate.load_config(reference["path"])
    contract.require(
        backend.reference(reference["path"]) == reference
        and value["control_config"] == control.path,
        "configuration_changed",
    )
    return {**value, "_control": control}


@contextmanager
def _candidate_store(stack, profile):
    if profile is None:
        yield None
        return
    store = stack.enter_context(candidate.journal(profile["state_root"]))
    yield store


def _enrolled_products(owner, settings):
    for root in owner.provider.stores:
        enrolled = enrollment.owner(root)
        contract.require(enrolled is not None, "enrollment_required")
        contract.require(
            enrolled["participant"] == "product"
            and enrolled["source"] == settings.value["operator"]
            and enrolled["configuration"] == settings.value["control_config"],
            "owner_changed",
        )


def _retirement_plan(owner, control, operation_id):
    host = owner.provider.backends[1]
    snapshot = owner.provider.observe(owner.members(), owner.catalogs())
    resources = retirement.disposal(owner, host, snapshot)
    retained = {
        "paths": resources["retain_paths"],
        "network_ids": sorted(
            {
                identifier
                for member in owner.members()
                for identifier in member.expected_runtime[
                    "network_ids"
                ].values()
                if identifier not in resources["exclusive_network_ids"]
            }
        ),
        "records": [
            _backend_reference(control, member.backend_sha256)
            for member in owner.members()
        ],
    }
    value = {
        "schema_version": 1,
        "operation_id": operation_id,
        "predecessor_roles_sha256": control.roles_sha256,
        "backend": _backend_reference(control, host.backend_sha256),
        "dispose": resources,
        "retain": retained,
    }
    filename = "retirement-plan-" + operation_id + ".json"
    control._write(filename, value)
    return {
        "decision": backend.reference(control.root / filename),
        **{key: value[key] for key in ("backend", "dispose", "retain")},
    }


def _intent(
    settings,
    control,
    owner,
    peer,
    remote_before,
    *,
    kind,
    operation_id,
    candidate_binding,
    target_tls,
):
    before = remote_before["projection"]
    active_ref = _backend_reference(
        control, control.roles["active_backend_sha256"]
    )
    active = authority.read_backend(control, active_ref["sha256"])
    target_ref = active_ref
    if kind == "backend-code-update":
        target_ref = candidate_binding["backend"]
    elif kind == "backend-code-rollback":
        target_ref = _backend_reference(
            control, control.roles["rollback_backend_sha256"]
        )
    target_backend = authority.read_backend(control, target_ref["sha256"])
    predecessor_tls = backend.tls_projection(
        control.roles["tls_plan_ref"], runner=owner.tls.runner
    )
    selected_tls = (
        predecessor_tls
        if target_tls is None
        else backend.tls_projection(target_tls, runner=owner.tls.runner)
    )
    contract.require(
        predecessor_tls["backend"]["api_container_id"]
        == active["lease"]["api_container_id"]
        and selected_tls["backend"]["api_container_id"]
        == target_backend["lease"]["api_container_id"],
        "binding_changed",
    )
    target_frontend = (
        before
        if kind == "backend-code-retire"
        else _frontend_target(before, target_backend["source"]["revision"])
    )
    return {
        "schema_version": 1,
        "kind": kind,
        "operation_id": operation_id,
        "operator": copy.deepcopy(settings.value["operator"]),
        "authority": {
            "control_config": copy.deepcopy(settings.value["control_config"]),
            "control_root": str(control.root),
            "owner_uid": os.geteuid(),
            "namespace_sha256": journal.namespace_identity(control),
            "backend_peer_sha256": settings.value["backend_peer_sha256"],
        },
        "predecessor_roles": copy.deepcopy(control.roles),
        "predecessor_roles_sha256": control.roles_sha256,
        "candidate": candidate_binding,
        "sides": {
            "predecessor": {
                "backend": active_ref,
                "tls": predecessor_tls,
                "frontend": before,
            },
            "target": {
                "backend": target_ref,
                "tls": selected_tls,
                "frontend": target_frontend,
            },
        },
        "frontend_owner": {
            "transport_profile": copy.deepcopy(
                settings.value["frontend_transport"]
            ),
            "participant_config": peer["participant_config"],
            "source": peer["participant_source"],
            "entry_sha256": peer["entry_sha256"],
            "peer_sha256": peer["peer_sha256"],
        },
        "limits": dict(contract.LIMITS),
        "retirement": _retirement_plan(owner, control, operation_id)
        if kind == "backend-code-retire"
        else None,
    }


def _summary(operation):
    phase, side = operation.refresh()
    return {
        "status": "backend_transition_" + phase,
        "operation_id": operation.operation_id,
        "intent_sha256": operation.sha256,
        "epoch": operation.control.roles["epoch"],
        "roles_sha256": operation.control.roles_sha256,
        "side": side,
        "released": operation.read("released") is not None,
    }


@_invocation("prepare")
def prepare(
    settings,
    *,
    kind,
    expected_epoch,
    expected_roles_sha256,
    target_tls=None,
    candidate_profile=None,
    session_factory=transport.FrontendSession,
):
    """Seal actual inputs and reserve both owners; do not dispatch effects."""
    contract.choice(kind, contract.KINDS)
    with authority.open_control(settings.control_path) as control:
        _exact(control, expected_epoch, expected_roles_sha256)
        _prepare_admission(control, kind, target_tls, candidate_profile)
        profile = _profile(candidate_profile, control)
        with ExitStack() as stack:
            store = stack.enter_context(_candidate_store(stack, profile))
            if profile is not None:
                profile["_candidate_record"] = candidate.current(store)
                contract.require(
                    profile["_candidate_record"] is not None,
                    "candidate_changed",
                )
            owner = stack.enter_context(
                backend.open_owner(
                    control,
                    source=settings.value["operator"],
                    tls_plan=control.roles["tls_plan_ref"],
                    profile=profile,
                    candidate_store=store,
                    commands=settings.commands,
                    deadline=settings.commands.deadline,
                )
            )
            _enrolled_products(owner, settings)
            owner.tls.preflight()
            proposed = (
                None
                if profile is None
                else owner.seal_candidate(candidate_profile)
            )
            peer = settings.peer()
            owner.provider.commands.remaining("ssh_transport", 1)
            frontend = session_factory(peer, deadline=owner.provider.deadline)
            stack.callback(frontend.close)
            operation_id = uuid.uuid4().hex
            remote = frontend.preflight(operation_id)
            value = _intent(
                settings,
                control,
                owner,
                peer,
                remote,
                kind=kind,
                operation_id=operation_id,
                candidate_binding=proposed,
                target_tls=target_tls,
            )
            settings.refresh()
            operation = journal.create(control, value)
            owner.prepare(operation)
            frontend.prepare(operation)
            return _summary(operation)


def _prepare_admission(control, kind, target_tls, profile):
    if kind == "backend-code-update":
        contract.require(
            control.roles["rollback_backend_sha256"] is None, "capacity"
        )
        contract.require(
            profile is not None and target_tls is not None, "arguments_invalid"
        )
    else:
        contract.require(
            control.roles["rollback_backend_sha256"] is not None, "no_rollback"
        )
        contract.require(profile is None, "arguments_invalid")
    if kind == "backend-code-rollback":
        contract.require(target_tls is not None, "arguments_invalid")
    if kind == "backend-code-retire":
        contract.require(target_tls is None, "arguments_invalid")


def _open_inputs(settings, operation, stack, *, session_factory):
    operation.require_claim()
    settings.refresh()
    contract.require(
        operation.intent["operator"] == settings.value["operator"]
        and operation.intent["frontend_owner"]["transport_profile"]
        == settings.value["frontend_transport"],
        "owner_changed",
    )
    proposed = operation.intent["candidate"]
    profile = _profile(
        None if proposed is None else proposed["profile"], operation.control
    )
    store = stack.enter_context(_candidate_store(stack, profile))
    if profile is not None:
        profile["_candidate_record"] = backend.read_reference(
            proposed["record"]
        )
    owner = stack.enter_context(
        backend.open_owner(
            operation.control,
            source=settings.value["operator"],
            tls_plan=operation.intent["sides"]["predecessor"]["tls"]["plan"],
            operation=operation,
            profile=profile,
            candidate_store=store,
            commands=settings.commands,
            deadline=settings.commands.deadline,
        )
    )
    _enrolled_products(owner, settings)
    peer = settings.peer()
    contract.require(
        peer["participant_config"]
        == operation.intent["frontend_owner"]["participant_config"]
        and peer["entry_sha256"]
        == operation.intent["frontend_owner"]["entry_sha256"],
        "owner_changed",
    )
    owner.provider.commands.remaining("ssh_transport", 1)
    frontend = session_factory(peer, deadline=owner.provider.deadline)
    stack.callback(frontend.close)
    owner.prepare(operation)
    frontend.prepare(operation)
    _resume_cleanup(owner, operation)
    return owner, frontend


@contextmanager
def _opened(settings, operation, *, session_factory):
    with ExitStack() as stack:
        with settings.commands.phase("open"):
            owner, frontend = _open_inputs(
                settings, operation, stack, session_factory=session_factory
            )
        yield owner, frontend
        settings.refresh()


def _resume_cleanup(owner, operation):
    if (
        operation.read("discard") is not None
        or operation.read("retire-dispatch") is not None
    ):
        owner.provider.cleanup = retirement.Cleanup(owner, operation)


def _discard(owner, frontend, operation):
    if operation.read("discard") is None:
        owner.tls.publish(operation, "predecessor")
        frontend.reconcile(operation, "predecessor")
        baseline = retirement.seal(owner, operation, owner.proposed)
        decision = operation.lock_predecessor(baseline["resources"], frontend)
        owner.tls.reservation.lock_predecessor(operation.intent, decision)
    else:
        decision = operation.read("discard")
        frontend.reconcile(operation, "predecessor", decision=decision)
        owner.tls.reservation.lock_predecessor(operation.intent, decision)
    owner.provider.cleanup = retirement.Cleanup(owner, operation)
    owner.provider.cleanup.finish()


def _retire(owner, operation):
    if operation.read("retire-dispatch") is None:
        host = owner.provider.backends[1]
        retirement.seal(owner, operation, host)
        operation.dispatch_retirement()
    owner.provider.cleanup = retirement.Cleanup(owner, operation)
    owner.provider.cleanup.finish()


def _complete_pending(owner, frontend, operation, side):
    operation._side_admission(side)
    kind = operation.intent["kind"]
    if kind == "backend-code-retire":
        contract.require(side == "target", "use_no_effect")
        with owner.provider.commands.phase("cleanup"):
            _retire(owner, operation)
    elif kind == "backend-code-update" and side == "predecessor":
        with owner.provider.commands.phase("cleanup"):
            _discard(owner, frontend, operation)
    else:
        with owner.provider.commands.phase("terminal"):
            owner.tls.publish(operation, side)
            frontend.reconcile(operation, side)
    reason = "target_verified" if side == "target" else "verified_return"
    with owner.provider.commands.phase("terminal"):
        return operation.resolve(owner, frontend, side, reason=reason)


def _handoff(owner, operation, decision):
    proposed = operation.intent["candidate"]
    if proposed is None or decision["mode"] != "terminal":
        return
    store = owner.candidate_store
    original = backend.read_reference(proposed["record"])
    current = candidate.current(store)
    if decision["resolution"] == "no_effect":
        contract.require(
            decision["side"] == "predecessor" and current == original,
            "candidate_changed",
        )
        return
    if decision["side"] == "predecessor":
        cleanup = owner.provider.cleanup
        contract.require(
            type(cleanup) is retirement.Cleanup
            and cleanup.proof()["runtime_lease_closed"] is True,
            "cleanup_incomplete",
        )
    name = "handoff-" + operation.sha256 + ".json"
    value = {
        "schema_version": 1,
        "intent_sha256": operation.sha256,
        "candidate_snapshot": proposed["record"],
        "roles_sha256": decision["roles_sha256"],
        "side": decision["side"],
        "backend": proposed["backend"],
    }
    try:
        previous = store.read(name)
    except FileNotFoundError:
        contract.require(current == original, "candidate_changed")
        store.write(name, value)
    else:
        contract.require(previous == value, "candidate_changed")
    if decision["side"] == "target":
        if current is not None:
            contract.require(current == original, "candidate_changed")
            os.unlink("current.json", dir_fd=store.fd)
            os.fsync(store.fd)
    elif current != {**original, "phase": "stopped"}:
        contract.require(current == original, "candidate_changed")
        candidate.save(store, copy.deepcopy(original), "stopped")


@_invocation()
def operate(
    settings,
    *,
    action,
    intent_sha256,
    expected_epoch,
    expected_roles_sha256,
    side="target",
    session_factory=transport.FrontendSession,
):
    contract.choice(action, {"apply", "cancel", "reconcile", "no-effect"})
    contract.choice(side, contract.SIDES)
    with journal.open_operation(
        settings.control_path,
        intent_sha256=intent_sha256,
        expected_epoch=expected_epoch,
        expected_roles_sha256=expected_roles_sha256,
    ) as operation:
        with _opened(settings, operation, session_factory=session_factory) as (
            owner,
            frontend,
        ):
            phase, terminal_side = operation.refresh()
            if phase == "prepared":
                if operation.read("resolution") is not None:
                    contract.require(
                        action in {"cancel", "reconcile"}, "phase"
                    )
                    with settings.commands.phase("terminal"):
                        decision = operation.recover_cancelled(owner, frontend)
                elif action == "cancel":
                    with settings.commands.phase("terminal"):
                        decision = operation.cancel(owner, frontend)
                else:
                    contract.require(action == "apply", "phase")
                    with settings.commands.phase("begin"):
                        operation.begin(owner, frontend)
                    decision = _complete_pending(
                        owner, frontend, operation, side
                    )
            elif phase == "pending":
                decision = _pending(action, owner, frontend, operation, side)
            else:
                contract.require(side == terminal_side, "outcome_fixed")
                with settings.commands.phase("terminal"):
                    decision = operation.recover_terminal(owner, frontend)
            _handoff(owner, operation, decision)
            with settings.commands.phase("release"):
                operation.release(owner, frontend)
            return _summary(operation)


def _pending(action, owner, frontend, operation, side):
    if action == "no-effect":
        contract.require(side == "predecessor", "side_invalid")
        with owner.provider.commands.phase("terminal"):
            return operation.resolve(owner, frontend, side, reason="no_effect")
    contract.require(action == "reconcile", "explicit_reconcile_required")
    return _complete_pending(owner, frontend, operation, side)


@_invocation()
def status(settings, *, intent_sha256, expected_epoch, expected_roles_sha256):
    settings.refresh()
    with journal.open_operation(
        settings.control_path,
        intent_sha256=intent_sha256,
        expected_epoch=expected_epoch,
        expected_roles_sha256=expected_roles_sha256,
    ) as operation:
        operation.require_claim()
        return _summary(operation)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise contract.TransitionError("backend_transition_arguments_invalid")


def _arguments(argv):
    parser = _Parser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "apply",
            "cancel",
            "reconcile",
            "no-effect",
            "status",
        ),
    )
    parser.add_argument("--kind", choices=sorted(contract.KINDS))
    parser.add_argument("--expected-epoch", required=True, type=int)
    parser.add_argument("--expected-roles-sha256", required=True)
    parser.add_argument("--intent-sha256")
    parser.add_argument(
        "--side", choices=sorted(contract.SIDES), default="target"
    )
    for name in ("candidate-profile", "target-tls"):
        parser.add_argument("--" + name)
        parser.add_argument("--" + name + "-sha256")
    return parser.parse_args(argv)


def _optional_reference(path, sha256):
    if path is None and sha256 is None:
        return None
    value = {"path": path, "sha256": sha256}
    contract.reference(value)
    return value


def main(argv=None):
    try:
        args = _arguments(argv)
        settings = Settings(args.config)
        common = {
            "expected_epoch": args.expected_epoch,
            "expected_roles_sha256": args.expected_roles_sha256,
        }
        if args.action == "prepare":
            contract.require(
                args.kind is not None and args.intent_sha256 is None,
                "arguments_invalid",
            )
            result = prepare(
                settings,
                kind=args.kind,
                candidate_profile=_optional_reference(
                    args.candidate_profile, args.candidate_profile_sha256
                ),
                target_tls=_optional_reference(
                    args.target_tls, args.target_tls_sha256
                ),
                **common,
            )
        else:
            contract.require(
                args.kind is None
                and args.candidate_profile is None
                and args.candidate_profile_sha256 is None
                and args.target_tls is None
                and args.target_tls_sha256 is None,
                "arguments_invalid",
            )
            contract.hexadecimal(args.intent_sha256)
            common["intent_sha256"] = args.intent_sha256
            result = (
                status(settings, **common)
                if args.action == "status"
                else operate(
                    settings, action=args.action, side=args.side, **common
                )
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        sys.stderr.write("backend_transition_interrupted\n")
        return 130
    except (ValueError, OSError, TypeError, KeyError, RecursionError):
        sys.stderr.write("backend_transition_recovery_required\n")
        return 1
