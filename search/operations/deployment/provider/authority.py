"""Retain a private namespace and immutable provider reference seals."""

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat

from search.operations.shared import namespace, private_io
from search.serialization import strict_json

from . import contract


def read_private(path, *, maximum=contract.MAX_BYTES):
    path = Path(contract.path(str(path)))
    namespace.check_directory(path.parent, private=True)
    fd = private_io.open_directory(path.parent)
    try:
        return private_io.read_file(
            fd, path.name, mode=0o600, max_bytes=maximum
        )
    finally:
        os.close(fd)


def load_config(path):
    return _control_configuration(path)[0]


def _control_configuration(path):
    try:
        raw = read_private(path, maximum=16384)
        value = strict_json(raw)
        contract.closed(value, {"schema_version", "state_root"})
        contract.require(
            type(value["schema_version"]) is int
            and value["schema_version"] == 1,
            "config_invalid",
        )
        root = Path(contract.path(value["state_root"]))
        contract.require(
            not any((p / ".git").exists() for p in (root, *root.parents)),
            "config_invalid",
        )
        return (
            {"schema_version": 1, "state_root": root},
            hashlib.sha256(raw).hexdigest(),
        )
    except (OSError, ValueError, TypeError, RecursionError):
        raise contract.ProviderError(
            "shared_backend_provider_config_invalid"
        ) from None


def _lock(fd, *, create):
    flags = os.O_RDWR | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT
    lock = os.open(
        "operations.lock",
        flags,
        0o600,
        dir_fd=fd,
    )
    try:
        row = os.fstat(lock)
        contract.require(
            stat.S_ISREG(row.st_mode)
            and row.st_uid == os.geteuid()
            and stat.S_IMODE(row.st_mode) == 0o600
            and row.st_nlink == 1,
            "namespace_unsafe",
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise contract.ProviderError(
                "shared_backend_provider_busy"
            ) from None
        named = os.stat("operations.lock", dir_fd=fd, follow_symlinks=False)
        contract.require(
            private_io.identity(row) == private_io.identity(named),
            "namespace_unsafe",
        )
        return lock
    except BaseException:
        os.close(lock)
        raise


class Control:
    def __init__(
        self, path, config, fd, lock, config_sha256, *, allow_empty=False
    ):
        self.path, self.config, self.fd = Path(path), config, fd
        self.root = config["state_root"]
        self.lock = lock
        self.config_sha256 = config_sha256
        self.uncertain = False
        self._roles = None
        self.refresh()
        try:
            self._roles = self.read("roles.json", contract.roles)
        except FileNotFoundError:
            contract.require(allow_empty, "not_adopted")

    @property
    def roles(self):
        contract.require(self._roles is not None, "not_adopted")
        return self._roles

    @property
    def provider_sha256(self):
        return self.roles["provider_sha256"]

    @property
    def roles_sha256(self):
        return contract.digest(self.roles)

    @property
    def provider(self):
        return read_provider(self)

    def read(self, name, validate=None, *, expected=None):
        self._namespace()
        raw = private_io.read_file(
            self.fd, name, mode=0o600, max_bytes=contract.MAX_BYTES
        )
        value = strict_json(raw)
        contract.require(private_io.canonical(value) == raw)
        if expected is not None:
            contract.require(
                hashlib.sha256(raw).hexdigest() == expected,
                "authority_changed",
            )
        return value if validate is None else validate(value)

    def _namespace(self):
        namespace.check_directory(self.root, private=True)
        private_io.verify_directory(self.fd, self.root)
        named = os.stat(
            "operations.lock", dir_fd=self.fd, follow_symlinks=False
        )
        contract.require(
            private_io.identity(os.fstat(self.lock))
            == private_io.identity(named),
            "namespace_unsafe",
        )

    def refresh(self):
        contract.require(not self.uncertain, "outcome_unconfirmed")
        self._namespace()
        current = hashlib.sha256(read_private(self.path)).hexdigest()
        contract.require(current == self.config_sha256, "authority_changed")
        if self._roles is not None:
            current = self.read("roles.json", contract.roles)
            contract.require(current == self._roles, "cas_conflict")

    def publish(self, kind, value):
        contract.require(kind in {"provider", "backend"})
        validate = {
            "provider": contract.provider,
            "backend": contract.backend,
        }[kind]
        validate(value)
        self.refresh()
        digest = contract.digest(value)
        self._write(kind + "-" + digest + ".json", value)
        return digest

    def _write(self, name, value, *, replace=False):
        try:
            private_io.write_json(
                self.fd,
                name,
                value,
                replace=replace,
                max_bytes=contract.MAX_BYTES,
            )
        except BaseException as error:
            self.uncertain = True
            if not isinstance(error, OSError):
                raise
            raise contract.ProviderError(
                "shared_backend_provider_outcome_unconfirmed"
            ) from None

    def adopt_roles(self, value):
        contract.roles(value)
        contract.require(self._roles is None, "already_adopted")
        contract.require(
            value["epoch"] == 1
            and value["rollback_backend_sha256"] is None
            and value["pending_operation_sha256"] is None
        )
        self.refresh()
        self._role_references(value)
        self._write("roles.json", value)
        self._roles = value

    def _compare_and_swap_roles(
        self, value, *, expected_epoch, expected_sha256
    ):
        self.refresh()
        contract.require(
            type(expected_epoch) is int and expected_epoch > 0, "cas_conflict"
        )
        contract.hexadecimal(expected_sha256)
        contract.roles(value)
        contract.require(
            self.roles["epoch"] == expected_epoch
            and self.roles_sha256 == expected_sha256,
            "cas_conflict",
        )
        contract.require(value["epoch"] == expected_epoch + 1, "cas_conflict")
        pending = value["pending_operation_sha256"]
        if pending is not None:
            intent = self.read("intent-" + pending + ".json", expected=pending)
            contract.require(
                type(intent) is dict and bool(intent), "intent_invalid"
            )
        self._role_references(value)
        self._write("roles.json", value, replace=True)
        self._roles = value

    def _role_references(self, value):
        identifier = value["provider_sha256"]
        self.read(
            "provider-" + identifier + ".json",
            contract.provider,
            expected=identifier,
        )
        for key in ("active_backend_sha256", "rollback_backend_sha256"):
            identifier = value[key]
            if identifier is not None:
                record = self.read(
                    "backend-" + identifier + ".json",
                    contract.backend,
                    expected=identifier,
                )
                contract.require(
                    record["provider_sha256"] == value["provider_sha256"],
                    "authority_changed",
                )

    def require_disposable(self, resources):
        self.refresh()
        protected = [
            _provider_resources(self.provider),
            {
                "container_ids": [],
                "network_ids": [],
                "owned_paths": sorted(
                    {
                        str(self.root),
                        str(self.path),
                        self.roles["tls_plan_ref"]["path"],
                    }
                ),
            },
        ]
        for backend in read_backends(self):
            protected.append(backend_resources(backend))
        contract.require_disposable(self.roles, resources, protected)


@contextmanager
def _locked_control(path, *, create=False):
    config, config_sha256 = _control_configuration(path)
    root = config["state_root"]
    namespace.check_directory(root.parent)
    try:
        fd = private_io.open_directory(root, create=create)
    except FileNotFoundError:
        raise contract.ProviderError(
            "shared_backend_provider_not_adopted"
        ) from None
    lock = None
    try:
        namespace.check_directory(root, private=True)
        try:
            lock = _lock(fd, create=create)
        except FileNotFoundError:
            raise contract.ProviderError(
                "shared_backend_provider_not_adopted"
            ) from None
        control = Control(
            path, config, fd, lock, config_sha256, allow_empty=create
        )
        yield control
        control.refresh()
    finally:
        if lock is not None:
            os.close(lock)
        os.close(fd)


@contextmanager
def open_control(path, *, create=False):
    """Admit normal operations only while no transition is reserved."""
    with _locked_control(path, create=create) as control:
        if control._roles is not None:
            contract.require(
                control.roles["pending_operation_sha256"] is None,
                "recovery_required",
            )
            try:
                control.read("operation-hold.json")
            except FileNotFoundError:
                pass
            else:
                raise contract.ProviderError(
                    "shared_backend_provider_recovery_required"
                )
        yield control


def read_roles(control):
    control.refresh()
    return control.roles


def read_provider(control):
    return control.read(
        "provider-" + control.provider_sha256 + ".json",
        contract.provider,
        expected=control.provider_sha256,
    )


def read_backend(control, backend_sha256):
    contract.hexadecimal(backend_sha256)
    value = control.read(
        "backend-" + backend_sha256 + ".json",
        contract.backend,
        expected=backend_sha256,
    )
    contract.require(
        value["provider_sha256"] == control.provider_sha256,
        "authority_changed",
    )
    return value


def read_backends(control):
    names = [
        control.roles["active_backend_sha256"],
        control.roles["rollback_backend_sha256"],
    ]
    return [read_backend(control, name) for name in names if name is not None]


def backend_resources(value):
    config = _configuration(
        value["configuration"], value["catalog_source"]["root"]
    )
    owned = {
        *value["owned_paths"],
        value["source"]["root"],
        value["build_state_root"],
        value["catalog_source"]["root"],
        value["configuration"]["path"],
        str(config.state_root),
    }
    return {
        "container_ids": list(value["runtime"]["container_ids"].values()),
        "network_ids": list(value["runtime"]["network_ids"].values()),
        "owned_paths": sorted(owned),
    }


def _configuration(reference, root):
    from search.operations.config import validate_config

    raw = read_private(reference["path"])
    contract.require(
        hashlib.sha256(raw).hexdigest() == reference["sha256"],
        "authority_changed",
    )
    return validate_config(strict_json(raw), repo_root=Path(root))


def _provider_resources(value):
    config = _configuration(value["configuration"], value["source"]["root"])
    raw = read_private(config.state_root / "inference-candidate.json")
    contract.require(
        hashlib.sha256(raw).hexdigest() == value["inference_complete_sha256"],
        "authority_changed",
    )
    native = strict_json(raw)
    return {
        "container_ids": list(native["container_ids"].values()),
        "network_ids": list(native["network_ids"].values()),
        "owned_paths": [
            value["source"]["root"],
            value["configuration"]["path"],
            str(config.state_root),
            str(config.api_model_root),
            str(config.embedding_model_root),
            str(config.reranker_model_root),
        ],
    }
