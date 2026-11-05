"""Fence enrolled backend writers and retain local release confirmations."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

from search.operations.shared import namespace, private_io
from search.serialization import strict_json

from . import contract


OWNER = "binding-owner.json"
RESERVATION = "binding-reservation.json"


def _read(fd, name):
    try:
        raw = private_io.read_file(
            fd, name, mode=0o600, max_bytes=contract.MAX_BYTES
        )
    except FileNotFoundError:
        return None
    value = strict_json(raw)
    contract.require(private_io.canonical(value) == raw, "record_invalid")
    return value


def _identity(fd, root):
    private_io.verify_directory(fd, root)
    row = os.fstat(fd)
    return contract.digest(
        {
            "root": str(root),
            "uid": row.st_uid,
            "gid": row.st_gid,
            "mode": row.st_mode,
            "device": row.st_dev,
            "inode": row.st_ino,
        }
    )


def validate_owner(value):
    contract.version(
        value,
        {
            "schema_version",
            "kind",
            "participant",
            "root",
            "source",
            "configuration",
            "namespace_sha256",
        },
        "backend-binding-owner",
    )
    contract.choice(value["participant"], {"frontend", "tls", "product"})
    contract.path(value["root"])
    contract.source(value["source"])
    contract.reference(value["configuration"])
    contract.hexadecimal(value["namespace_sha256"])
    return value


def owner(root):
    root = Path(root)
    namespace.check_directory(root, private=True)
    fd = private_io.open_directory(root)
    try:
        value = _read(fd, OWNER)
        if value is None:
            return None
        validate_owner(value)
        contract.require(
            value["root"] == str(root)
            and value["namespace_sha256"] == _identity(fd, root),
            "namespace_changed",
        )
        return value
    finally:
        os.close(fd)


def require_unmanaged(root):
    contract.require(owner(root) is None, "provider_managed")


def require_unreserved(root):
    root = Path(root)
    fd = private_io.open_directory(root)
    try:
        contract.require(_read(fd, RESERVATION) is None, "binding_reserved")
    finally:
        os.close(fd)


def endpoint_root(plan_path):
    """Find only the enrolled endpoint's bounded version-directory ancestry."""
    root = Path(plan_path).parent
    for _unused in range(3):
        row = root.stat()
        if stat.S_IMODE(row.st_mode) != 0o700 or row.st_uid != os.geteuid():
            return Path(plan_path).parent
        value = owner(root)
        if value is not None:
            contract.require(value["participant"] == "tls", "owner_invalid")
            return root
        root = root.parent
    return Path(plan_path).parent


@contextmanager
def locked(root):
    root = Path(root)
    namespace.check_directory(root, private=True)
    fd = private_io.open_directory(root)
    lock = None
    try:
        lock = os.open(
            ".binding.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=fd,
        )
        row = os.fstat(lock)
        contract.require(
            stat.S_ISREG(row.st_mode)
            and row.st_uid == os.geteuid()
            and stat.S_IMODE(row.st_mode) == 0o600
            and row.st_nlink == 1,
            "namespace_changed",
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise contract.TransitionError("backend_transition_busy") from None
        private_io.verify_directory(fd, root)
        contract.require(
            private_io.identity(row)
            == private_io.identity(
                os.stat(".binding.lock", dir_fd=fd, follow_symlinks=False)
            ),
            "namespace_changed",
        )
        yield fd
    finally:
        if lock is not None:
            os.close(lock)
        os.close(fd)


class BindingReservation:
    """One owner's durable fence; never an authority for changing Roles."""

    def __init__(self, root, fd, enrolled):
        validate_owner(enrolled)
        self.root, self.fd = Path(root), fd
        self.owner = enrolled
        self.namespace_sha256 = _identity(fd, self.root)
        self.uncertain = False
        contract.require(
            enrolled["namespace_sha256"] == self.namespace_sha256,
            "namespace_changed",
        )

    def refresh(self):
        contract.require(not self.uncertain, "outcome_unconfirmed")
        namespace.check_directory(self.root, private=True)
        contract.require(
            _identity(self.fd, self.root) == self.namespace_sha256,
            "namespace_changed",
        )
        contract.require(_read(self.fd, OWNER) == self.owner, "owner_changed")

    def _write(self, name, value, *, replace=False):
        self.refresh()
        try:
            private_io.write_json(
                self.fd,
                name,
                value,
                replace=replace,
                max_bytes=contract.MAX_BYTES,
            )
        except BaseException:
            self.uncertain = True
            raise

    def _released_name(self, operation):
        return (
            "released-"
            + operation["operation_id"]
            + "-"
            + contract.digest(operation)
            + ".json"
        )

    def _base(self, operation):
        return {
            "schema_version": 1,
            "operation_id": operation["operation_id"],
            "intent_sha256": contract.digest(operation),
            "participant": self.owner["participant"],
            "owner_source": self.owner["source"],
            "owner_namespace_sha256": self.namespace_sha256,
            "config_sha256": self.owner["configuration"]["sha256"],
        }

    def prepare(self, operation):
        self.refresh()
        contract.intent(operation)
        contract.require(
            _read(self.fd, self._released_name(operation)) is None,
            "already_released",
        )
        expected = {
            **self._base(operation),
            "phase": "prepared",
            "cleanup_decision_sha256": None,
        }
        current = _read(self.fd, RESERVATION)
        if current is None:
            self._write(RESERVATION, expected)
            return expected
        # Reattachment must not downgrade a published/frozen reservation or
        # erase its irreversible cleanup decision after a lost acknowledgement.
        return self.current(operation)

    def current(self, operation):
        self.refresh()
        current = _read(self.fd, RESERVATION)
        if current is None:
            return None
        base = self._base(operation)
        contract.closed(current, {*base, "phase", "cleanup_decision_sha256"})
        contract.require(
            all(current[key] == value for key, value in base.items()),
            "binding_reserved",
        )
        contract.choice(current["phase"], {"prepared", "published", "frozen"})
        if current["cleanup_decision_sha256"] is not None:
            contract.require(operation["kind"] == "backend-code-update")
            contract.hexadecimal(current["cleanup_decision_sha256"])
        return current

    def lock_predecessor(self, operation, decision):
        current = self.current(operation)
        contract.require(current is not None, "reservation_missing")
        contract.discard(decision, operation)
        digest = contract.digest(decision)
        old = current["cleanup_decision_sha256"]
        contract.require(old in (None, digest), "outcome_fixed")
        if old is None:
            self._write(
                RESERVATION,
                {**current, "cleanup_decision_sha256": digest},
                replace=True,
            )
        return self.current(operation)

    def require_side(self, operation, side):
        contract.require(side in contract.SIDES)
        current = self.current(operation)
        if (
            current is not None
            and current["cleanup_decision_sha256"] is not None
        ):
            contract.require(side == "predecessor", "outcome_fixed")
        return current

    def mark(self, operation, phase):
        current = self.current(operation)
        contract.require(current is not None, "reservation_missing")
        contract.require(phase in {"published", "frozen"})
        self._write(RESERVATION, {**current, "phase": phase}, replace=True)
        return self.current(operation)

    def released(self, operation):
        self.refresh()
        value = _read(self.fd, self._released_name(operation))
        if value is not None:
            self._validate_released(operation, value)
        return value

    def _validate_released(self, operation, value):
        base = self._base(operation)
        fields = {
            *base,
            "phase",
            "decision_mode",
            "decision_sha256",
            "resolved_roles_sha256",
            "resolved_epoch",
            "side",
            "last_observation_sha256",
            "reservation_sha256",
        }
        contract.closed(value, fields)
        contract.require(all(value[key] == item for key, item in base.items()))
        contract.require(value["phase"] == "released")
        contract.choice(value["side"], contract.SIDES)
        contract.integer(value["resolved_epoch"])
        for key in (
            "decision_sha256",
            "resolved_roles_sha256",
            "last_observation_sha256",
            "reservation_sha256",
        ):
            contract.hexadecimal(value[key])
        self._released_outcome(operation, value)

    def _released_outcome(self, operation, value):
        contract.choice(
            value["decision_mode"], {"cancelled_before_begin", "terminal"}
        )
        if value["decision_mode"] == "cancelled_before_begin":
            contract.require(value["side"] == "predecessor")
            expected = operation["predecessor_roles"]
        else:
            expected = contract.terminal_roles(operation, value["side"])
        contract.require(
            value["resolved_epoch"] == expected["epoch"]
            and value["resolved_roles_sha256"] == contract.digest(expected),
            "decision_changed",
        )

    def release(self, operation, decision, observation):
        """Durably confirm release before removing the matching fence."""
        self.refresh()
        contract.resolution(decision, operation)
        existing = self.released(operation)
        if existing is None:
            current = self.current(operation)
            contract.require(
                current is not None
                or decision["mode"] == "cancelled_before_begin",
                "reservation_missing",
            )
            self.require_side(operation, decision["side"])
            existing = self._release_record(
                operation, decision, observation, current
            )
            self._write(self._released_name(operation), existing)
        contract.require(
            existing["decision_sha256"] == contract.digest(decision),
            "decision_changed",
        )
        self._remove_reservation(operation)
        return existing

    def _release_record(self, operation, decision, observation, current):
        return {
            **self._base(operation),
            "phase": "released",
            "decision_mode": decision["mode"],
            "decision_sha256": contract.digest(decision),
            "resolved_roles_sha256": decision["roles_sha256"],
            "resolved_epoch": decision["roles"]["epoch"],
            "side": decision["side"],
            "last_observation_sha256": contract.digest(observation),
            "reservation_sha256": contract.digest(current),
        }

    def _remove_reservation(self, operation):
        current = _read(self.fd, RESERVATION)
        if current is None:
            return
        # A historical acknowledgement must never remove a newer operation.
        if current.get("intent_sha256") != contract.digest(operation):
            return
        self.current(operation)
        try:
            os.unlink(RESERVATION, dir_fd=self.fd)
            os.fsync(self.fd)
            private_io.verify_directory(self.fd, self.root)
        except BaseException:
            self.uncertain = True
            raise


def enroll(root, *, participant, source, configuration):
    """Create an explicit owner record while its normal writer lock is held."""
    root = Path(root)
    namespace.check_directory(root, private=True)
    fd = private_io.open_directory(root)
    try:
        value = {
            "schema_version": 1,
            "kind": "backend-binding-owner",
            "participant": participant,
            "root": str(root),
            "source": source,
            "configuration": configuration,
            "namespace_sha256": _identity(fd, root),
        }
        validate_owner(value)
        previous = _read(fd, OWNER)
        if previous is None:
            private_io.write_json(
                fd, OWNER, value, max_bytes=contract.MAX_BYTES
            )
        else:
            contract.require(previous == value, "owner_changed")
        return value
    finally:
        os.close(fd)
