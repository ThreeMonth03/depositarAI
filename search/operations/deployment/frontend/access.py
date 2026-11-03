"""Explicit client-address admission for the standalone intranet HTTP entry."""

from ipaddress import IPv4Network
import errno
import os
from pathlib import Path
import uuid

from search.operations.shared import private_io
from search.operations.shared.namespace import check_directory
from search.web.origin import validate_origin


PRIVATE_NETWORKS = tuple(
    IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
ACCESS_TARGET = "/etc/nginx/intranet-allow.conf"
MAX_POLICY_BYTES = 4096


def intranet_settings(config):
    """Read only operator-owned origin/CIDRs; never use forwarding headers."""
    services = config["services"]
    environment = services["web"]["environment"]
    if environment.get("WEB_ACCESS_MODE") != "intranet-http":
        raise ValueError("explicit intranet HTTP mode required")
    origin = validate_origin(
        environment["WEB_ORIGIN"], access_mode="intranet-http"
    )
    raw = services["web-nginx"].get("environment", {}).get("WEB_ALLOWED_CIDRS")
    if not isinstance(raw, str) or not raw or len(raw) > 2048:
        raise ValueError("explicit intranet client CIDRs required")
    values = raw.split(",")
    if len(values) > 32 or len(set(values)) != len(values):
        raise ValueError("bounded unique client CIDRs required")
    networks = []
    for value in values:
        network = IPv4Network(value, strict=True)
        if str(network) != value or not any(
            network.subnet_of(private) for private in PRIVATE_NETWORKS
        ):
            raise ValueError("only explicit RFC1918 client CIDRs permitted")
        networks.append(network)
    mounts = services["web-nginx"].get("volumes", [])
    if (
        len(mounts) != 1
        or mounts[0].get("target") != ACCESS_TARGET
        or mounts[0].get("type") != "bind"
        or mounts[0].get("read_only") is not True
        or mounts[0].get("bind", {}).get("create_host_path") is not False
    ):
        raise ValueError("one fixed read-only admission file required")
    path = Path(mounts[0]["source"])
    if not path.is_absolute() or path.name != "intranet-allow.conf":
        raise ValueError("absolute intranet-allow.conf path required")
    rules = (
        "# Direct TCP peers only. Loopback is for container health checks.\n"
    )
    rules += "allow 127.0.0.1;\nallow ::1;\n"
    rules += "".join(f"allow {network};\n" for network in networks)
    rules += "deny all;\n"
    return origin, path, rules


def prepare_access(config):
    """Write the dedicated admission input after validating the composition."""
    _, path, rules = intranet_settings(config)
    encoded = rules.encode("ascii")
    check_directory(path.parent, private=True)
    parent = private_io.open_directory(path.parent)
    temporary = ".intranet-" + uuid.uuid4().hex
    created = False
    try:
        try:
            existing = _read_file(parent, path.name)
        except FileNotFoundError:
            pass
        else:
            if existing == encoded:
                private_io.verify_directory(parent, path.parent)
                return
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            # CIDRs are not credentials. The bind-mounted file must also work
            # when the container's non-root UID differs from the operator's.
            os.fchmod(stream.fileno(), 0o444)
            os.fsync(stream.fileno())
        private_io.verify_directory(parent, path.parent)
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        created = False
        os.fsync(parent)
        _verify_file(parent, path, encoded)
    finally:
        try:
            if created:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent)


def _read_file(parent, name):
    try:
        return private_io.read_file(
            parent, name, mode=0o444, max_bytes=MAX_POLICY_BYTES
        )
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("admission file cannot be a symlink") from None
        raise


def _verify_file(parent, path, encoded):
    actual = _read_file(parent, path.name)
    private_io.verify_directory(parent, path.parent)
    if actual != encoded:
        raise ValueError(
            "admission file differs from the approved client CIDRs"
        )


def verify_access(config):
    _, path, rules = intranet_settings(config)
    check_directory(path.parent, private=True)
    parent = private_io.open_directory(path.parent)
    try:
        _verify_file(parent, path, rules.encode("ascii"))
    finally:
        os.close(parent)
