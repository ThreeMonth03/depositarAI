"""Durable, private product-operation receipts; never infer deployment success."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from search.operations.config import ProductConfig
from search.operations.shared import private_io
from search.serialization import strict_json


MAX_BYTES = 1024 * 1024
MAX_OPERATIONS = 128
OPERATIONS = frozenset(
    {
        "sync",
        "prepare-index",
        "start-index",
        "import-index",
        "reconcile-index",
        "build-vectors",
        "build-images",
        "build-catalog-images",
        "package-product",
        "prepare-query",
        "prepare-secrets",
        "start-inference",
        "start-release",
        "prepare-gateway",
        "activate-gateway",
    }
)


class ActivationOutcomeError(ValueError):
    """An activation changed, or may have changed, before persistence failed."""

    def __init__(self, *, active_changed):
        if active_changed is not True and active_changed is not None:
            raise ValueError("product_activation_outcome_invalid")
        super().__init__("product_activation_outcome_unconfirmed")
        self.active_changed = active_changed


def _canonical(value):
    return private_io.canonical(value)


def _identity(info):
    return private_io.identity(info)


def _owned_file(info):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise ValueError("product_state_file_unsafe")


def _directory(path: Path, *, create=False):
    return private_io.open_directory(path, create=create)


class StateStore:
    """One directory FD retained while the caller holds its operation lock."""

    def __init__(self, root: Path, fd: int):
        self.root, self.fd = root, fd
        # This invocation's write evidence, never an observation of live services.
        self._activation_changed = False

    def _check_directory(self):
        private_io.verify_directory(self.fd, self.root)

    @staticmethod
    def _name(name):
        if (
            type(name) is not str
            or not name.endswith(".json")
            or not name.replace("-", "").replace(".", "").isalnum()
            or len(name) > 100
        ):
            raise ValueError("product_state_name_invalid")

    def read(self, name):
        self._name(name)
        self._check_directory()
        data = private_io.read_file(self.fd, name, mode=0o600, max_bytes=MAX_BYTES)
        try:
            return strict_json(data)
        except ValueError as error:
            if error.args == ("duplicate JSON key",):
                raise ValueError("product_state_duplicate_key") from None
            if error.args == ("invalid JSON number",):
                raise ValueError("product_state_nonfinite") from None
            raise

    def write(self, name, value, *, replace=False):
        self._name(name)
        self._check_directory()
        if replace:
            try:
                self.read(name)
            except FileNotFoundError:
                pass
        if name == "active.json":
            # A link/rename may succeed before fsync reports an IO failure.
            self._activation_changed = None
        private_io.write_json(
            self.fd, name, value, replace=replace, max_bytes=MAX_BYTES
        )
        if name == "active.json":
            self._activation_changed = True


def _marker(config):
    identity = config.to_dict()
    # A freshness policy may change without relabelling GPU or storage ownership.
    identity.pop("cache_ttl_seconds")
    return {
        "schema_version": 1,
        "kind": "depositar-ai-product",
        "configuration_sha256": hashlib.sha256(_canonical(identity)).hexdigest(),
    }


def operation_receipts(store):
    """Strict receipt validation before interpreting a possibly interrupted run."""
    values = []
    base = {
        "schema_version",
        "operation",
        "source_revision",
        "status",
        "active_changed",
    }
    for name in sorted(os.listdir(store.fd)):
        if not name.startswith("op-"):
            continue
        if re.fullmatch(r"op-[0-9a-f]{32}\.json", name) is None:
            raise ValueError("product_operation_receipt_invalid")
        value = store.read(name)
        if (
            type(value) is not dict
            or not base <= set(value)
            or set(value) - base - {"bundle_id", "manifest_sha256", "error_type"}
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or type(value["operation"]) is not str
            or value["operation"] not in OPERATIONS
            or type(value["source_revision"]) is not str
            or re.fullmatch(r"[0-9a-f]{40}", value["source_revision"]) is None
            or type(value["status"]) is not str
            or value["status"] not in {"started", "staged", "failed"}
            or value["active_changed"]
            is not (
                value["operation"] == "activate-gateway" and value["status"] == "staged"
            )
        ):
            raise ValueError("product_operation_receipt_invalid")
        for key in ("bundle_id", "manifest_sha256"):
            if key in value and (
                type(value[key]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None
            ):
                raise ValueError("product_operation_receipt_invalid")
        if "error_type" in value and (
            type(value["error_type"]) is not str
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,80}", value["error_type"]) is None
        ):
            raise ValueError("product_operation_receipt_invalid")
        values.append(value)
    return values


@contextmanager
def locked_state(config: ProductConfig, *, create=False):
    """Reject unmarked nonempty directories and concurrent product operations."""
    root_fd = _directory(config.state_root, create=create)
    lock_fd = None
    try:
        try:
            fd = os.open("operations.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=root_fd)
        except FileNotFoundError:
            if not create:
                raise
            if os.listdir(root_fd):
                raise ValueError("product_state_unmarked_nonempty") from None
            fd = os.open(
                "operations.lock",
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=root_fd,
            )
            os.fsync(root_fd)
        lock_fd = fd
        before = os.fstat(fd)
        _owned_file(before)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("product_operation_busy") from None
        if _identity(before) != _identity(
            os.stat("operations.lock", dir_fd=root_fd, follow_symlinks=False)
        ):
            raise ValueError("product_state_lock_changed")
        store = StateStore(config.state_root, root_fd)
        try:
            marker = store.read("product.json")
        except FileNotFoundError:
            if not create or set(os.listdir(root_fd)) != {"operations.lock"}:
                raise ValueError("product_state_unmarked_nonempty") from None
            store.write("product.json", _marker(config))
        else:
            if (
                marker != _marker(config)
                or type(marker.get("schema_version")) is not int
            ):
                raise ValueError("product_state_configuration_mismatch")
        yield store
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(root_fd)


@contextmanager
def operation(store: StateStore, name: str, revision: str):
    """Leave a started receipt on SIGKILL; failures never become success receipts."""
    if name not in OPERATIONS:
        raise ValueError("product_operation_invalid")
    receipts = operation_receipts(store)
    if len(receipts) >= MAX_OPERATIONS:
        raise ValueError("product_operation_retention_limit")
    for receipt in receipts:
        if receipt["status"] == "started":
            raise ValueError("product_operation_recovery_required")
    receipt = "op-" + uuid.uuid4().hex + ".json"
    value = {
        "schema_version": 1,
        "operation": name,
        "source_revision": revision,
        "status": "started",
        "active_changed": False,
    }
    if name == "activate-gateway":
        store._activation_changed = False
    store.write(receipt, value)
    outcome = {}
    try:
        yield outcome
    except Exception as error:
        if name == "activate-gateway" and store._activation_changed is not False:
            # Retain the started intent: a failed/unchanged receipt would lie,
            # and another write may mask the original storage failure.
            raise ActivationOutcomeError(
                active_changed=store._activation_changed
            ) from error
        store.write(
            receipt,
            {**value, "status": "failed", "error_type": type(error).__name__},
            replace=True,
        )
        raise
    else:
        try:
            store.write(receipt, {**value, "status": "staged", **outcome}, replace=True)
        except Exception as error:
            if name == "activate-gateway" and outcome.get("active_changed") is True:
                raise ActivationOutcomeError(active_changed=True) from error
            raise
