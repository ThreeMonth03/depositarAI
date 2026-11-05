"""Retain operation evidence; the provider remains the sole Roles writer."""

from __future__ import annotations

from contextlib import contextmanager
import copy
import os

from search.operations.deployment.provider import authority as provider
from search.operations.shared import private_io

from . import contract


HOLD = "operation-hold.json"


def namespace_identity(control):
    control._namespace()
    root, lock = os.fstat(control.fd), os.fstat(control.lock)
    return contract.digest(
        {
            "root": str(control.root),
            "configuration": control.config_sha256,
            "directory": [
                root.st_dev,
                root.st_ino,
                root.st_uid,
                root.st_gid,
                root.st_mode,
            ],
            "lock": list(private_io.identity(lock)),
            "uid": os.geteuid(),
        }
    )


def _optional(control, name, *, expected=None):
    try:
        return control.read(name, expected=expected)
    except FileNotFoundError:
        return None


class Operation:
    """Bind recovery to one immutable intent and exact Roles snapshots."""

    def __init__(self, control, value):
        contract.intent(value)
        self.control = control
        self.intent = copy.deepcopy(value)
        self.sha256 = contract.digest(value)
        self.operation_id = value["operation_id"]
        self.namespace_sha256 = namespace_identity(control)
        self._owner_admission()
        self.refresh()

    def _owner_admission(self):
        owner = self.intent["authority"]
        contract.require(
            owner["control_config"]
            == {
                "path": str(self.control.path),
                "sha256": self.control.config_sha256,
            }
            and owner["control_root"] == str(self.control.root)
            and owner["owner_uid"] == os.geteuid()
            and owner["namespace_sha256"] == self.namespace_sha256,
            "authority_changed",
        )
        contract.require(
            self.control.provider_sha256
            == self.intent["predecessor_roles"]["provider_sha256"],
            "authority_changed",
        )

    @property
    def phase(self):
        return contract.phase(self.intent, self.control.roles)

    def name(self, kind):
        return kind + "-" + self.sha256 + ".json"

    def read(self, kind):
        self.refresh()
        return _optional(self.control, self.name(kind))

    def refresh(self):
        self.control.refresh()
        contract.require(
            namespace_identity(self.control) == self.namespace_sha256,
            "authority_changed",
        )
        contract.require(
            self.control.read(self.name("intent"), expected=self.sha256)
            == self.intent,
            "intent_changed",
        )
        contract.phase(self.intent, self.control.roles)
        hold = _optional(self.control, HOLD)
        if hold is not None:
            contract.require(hold == self.hold(), "operation_busy")
        return self.phase

    def hold(self):
        return {
            "schema_version": 1,
            "kind": "operation-hold",
            "operation_id": self.operation_id,
            "intent_sha256": self.sha256,
            "namespace_sha256": self.namespace_sha256,
        }

    def require_claim(self):
        """An intent alone never authorizes participant reservation."""
        self.refresh()
        if _optional(self.control, HOLD) is not None:
            return
        released = _optional(self.control, self.name("released"))
        contract.closed(
            released,
            {
                "schema_version",
                "operation_id",
                "intent_sha256",
                "decision_sha256",
                "participants",
            },
            "hold",
        )
        contract.require(
            type(released["schema_version"]) is int
            and released["schema_version"] == 1
            and released["operation_id"] == self.operation_id
            and released["intent_sha256"] == self.sha256,
            "hold",
        )
        decision = _optional(self.control, self.name("resolution"))
        contract.resolution(decision, self.intent)
        digest = contract.digest(decision)
        contract.require(
            released["decision_sha256"] == digest
            and self.control.roles == decision["roles"],
            "hold",
        )
        contract.closed(
            released["participants"], {"frontend", "backend"}, "hold"
        )
        for value in released["participants"].values():
            contract.require(
                type(value) is dict
                and value.get("operation_id") == self.operation_id
                and value.get("intent_sha256") == self.sha256
                and value.get("phase") == "released"
                and value.get("decision_sha256") == digest,
                "hold",
            )

    def ensure(self, kind, value):
        self.refresh()
        old = _optional(self.control, self.name(kind))
        if old is not None:
            contract.require(old == value, "record_changed")
            return value
        contract.bounded(value)
        self.control._write(self.name(kind), value)
        return value

    def begin(self, backend, frontend):
        """Recheck both prepared owners immediately before the sole CAS."""
        contract.require(self.refresh() == ("prepared", None), "phase")
        contract.require(self.read("resolution") is None, "already_resolved")
        contract.require(_optional(self.control, HOLD) == self.hold(), "hold")
        backend.prepare(self)
        frontend.prepare(self)
        backend.observe(self, "predecessor")
        frontend.observe(self, "predecessor")
        self.refresh()
        original = self.intent["predecessor_roles"]
        self.control._compare_and_swap_roles(
            contract.pending_roles(self.intent),
            expected_epoch=original["epoch"],
            expected_sha256=contract.digest(original),
        )
        return self.phase

    def lock_predecessor(self, resources, frontend):
        contract.require(self.refresh() == ("pending", None), "phase")
        contract.require(self.intent["kind"] == "backend-code-update")
        decision = {
            "schema_version": 1,
            "kind": "update-return-discard",
            "operation_id": self.operation_id,
            "intent_sha256": self.sha256,
            "expected_pending_epoch": self.control.roles["epoch"],
            "expected_pending_roles_sha256": self.control.roles_sha256,
            "candidate_snapshot_sha256": self.intent["candidate"]["record"][
                "sha256"
            ],
            "candidate_backend_sha256": self.intent["candidate"]["backend"][
                "sha256"
            ],
            "outcome": "predecessor",
            "resources": copy.deepcopy(resources),
            "owner_source": copy.deepcopy(self.intent["operator"]),
        }
        contract.discard(decision, self.intent)
        self.ensure("discard", decision)
        frontend.reconcile(self, "predecessor", decision=decision)
        return decision

    def dispatch_retirement(self):
        contract.require(self.refresh() == ("pending", None), "phase")
        contract.require(self.intent["kind"] == "backend-code-retire")
        return self.ensure(
            "retire-dispatch",
            {
                "schema_version": 1,
                "kind": "retire-dispatch",
                "operation_id": self.operation_id,
                "intent_sha256": self.sha256,
                "rollback_backend_sha256": self.intent["predecessor_roles"][
                    "rollback_backend_sha256"
                ],
            },
        )

    def _side_admission(self, side):
        contract.require(side in contract.SIDES)
        if self.read("discard") is not None:
            contract.require(side == "predecessor", "outcome_fixed")
        if self.read("retire-dispatch") is not None:
            contract.require(side == "target", "outcome_fixed")

    def _observations(self, backend, frontend, side):
        self._side_admission(side)
        first = backend.observe(self, side)
        remote = frontend.observe(self, side)
        second = backend.observe(self, side)
        contract.require(first == second, "runtime_changed")
        return {"backend": second, "frontend": remote}

    def resolve(self, backend, frontend, side, *, reason):
        """Call retained owners; never accept a caller's serialized proof."""
        contract.require(self.refresh() == ("pending", None), "phase")
        if reason == "no_effect":
            contract.require(self.read("discard") is None, "dispatched")
            backend.require_undispatched(self)
            frontend.require_undispatched(self)
        observations = self._observations(backend, frontend, side)
        target = contract.terminal_roles(self.intent, side)
        epoch, digest = self.control.roles["epoch"], self.control.roles_sha256
        self.control._compare_and_swap_roles(
            target, expected_epoch=epoch, expected_sha256=digest
        )
        return self._decision("terminal", side, reason, observations)

    def cancel(self, backend, frontend):
        contract.require(self.refresh() == ("prepared", None), "phase")
        contract.require(self.read("discard") is None, "dispatched")
        contract.require(self.read("retire-dispatch") is None, "dispatched")
        backend.require_undispatched(self)
        frontend.require_undispatched(self)
        observations = self._observations(backend, frontend, "predecessor")
        return self._decision(
            "cancelled_before_begin",
            "predecessor",
            "no_dispatch",
            observations,
        )

    def recover_terminal(self, backend, frontend):
        phase, side = self.refresh()
        contract.require(phase == "terminal", "phase")
        observations = self._observations(backend, frontend, side)
        existing = self.read("resolution")
        if existing is not None:
            contract.resolution(existing, self.intent)
            return existing
        reason = "target_verified" if side == "target" else "verified_return"
        if side == "predecessor" and self.read("tls-dispatch") is None:
            # An E+2 CAS can precede the resolution file. Reclassify only
            # after both actual owners again prove that no dispatch occurred.
            backend.require_undispatched(self)
            frontend.require_undispatched(self)
            reason = "no_effect"
        return self._decision("terminal", side, reason, observations)

    def recover_cancelled(self, backend, frontend):
        contract.require(self.refresh() == ("prepared", None), "phase")
        existing = self.read("resolution")
        contract.require(existing is not None, "resolution_missing")
        contract.resolution(existing, self.intent)
        contract.require(existing["mode"] == "cancelled_before_begin", "phase")
        self._observations(backend, frontend, "predecessor")
        return existing

    def _decision(self, mode, side, reason, observations):
        value = {
            "schema_version": 1,
            "mode": mode,
            "operation_id": self.operation_id,
            "intent_sha256": self.sha256,
            "roles": copy.deepcopy(self.control.roles),
            "roles_sha256": self.control.roles_sha256,
            "side": side,
            "resolution": reason,
            "observations": observations,
        }
        contract.resolution(value, self.intent)
        return self.ensure("resolution", value)

    def release(self, backend, frontend):
        decision = self.read("resolution")
        contract.require(decision is not None, "resolution_missing")
        contract.resolution(decision, self.intent)
        contract.require(
            self.control.roles == decision["roles"], "cas_conflict"
        )
        self._observations(backend, frontend, decision["side"])
        remote = frontend.release(self, decision)
        local = backend.release(self, decision)
        self.ensure(
            "released",
            {
                "schema_version": 1,
                "operation_id": self.operation_id,
                "intent_sha256": self.sha256,
                "decision_sha256": contract.digest(decision),
                "participants": {"frontend": remote, "backend": local},
            },
        )
        self._remove_hold()
        return decision

    def _remove_hold(self):
        self.refresh()
        value = _optional(self.control, HOLD)
        if value is None:
            contract.require(self.read("released") is not None, "hold")
            return
        contract.require(value == self.hold(), "operation_busy")
        try:
            os.unlink(HOLD, dir_fd=self.control.fd)
            os.fsync(self.control.fd)
            self.control._namespace()
        except BaseException:
            self.control.uncertain = True
            raise


def create(control, value):
    contract.intent(value)
    contract.require(
        control.roles == value["predecessor_roles"], "cas_conflict"
    )
    identifier = contract.digest(value)
    contract.require(_optional(control, HOLD) is None, "operation_busy")
    name = "intent-" + identifier + ".json"
    existing = _optional(control, name, expected=identifier)
    if existing is None:
        control._write(name, value)
    else:
        contract.require(existing == value, "intent_changed")
    operation = Operation(control, value)
    contract.require(operation.read("resolution") is None, "already_resolved")
    control._write(HOLD, operation.hold())
    return operation


@contextmanager
def open_operation(
    path, *, intent_sha256, expected_epoch, expected_roles_sha256
):
    """Reconcile only this sealed operation at an exact observed snapshot."""
    contract.hexadecimal(intent_sha256)
    contract.hexadecimal(expected_roles_sha256)
    contract.integer(expected_epoch)
    with provider._locked_control(path, create=False) as control:
        contract.require(
            control.roles["epoch"] == expected_epoch
            and control.roles_sha256 == expected_roles_sha256,
            "cas_conflict",
        )
        value = control.read(
            "intent-" + intent_sha256 + ".json", expected=intent_sha256
        )
        operation = Operation(control, value)
        yield operation
        operation.refresh()
