"""Explicit client-address admission for the standalone intranet HTTP entry."""

from ipaddress import IPv4Network
import os
from pathlib import Path
import tempfile

from search.web.origin import validate_origin


PRIVATE_NETWORKS = tuple(
    IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
ACCESS_TARGET = "/etc/nginx/intranet-allow.conf"


def intranet_settings(config):
    """Read only operator-owned origin/CIDRs; never use forwarding headers."""
    services = config["services"]
    environment = services["web"]["environment"]
    if environment.get("WEB_ACCESS_MODE") != "intranet-http":
        raise ValueError("explicit intranet HTTP mode required")
    origin = validate_origin(environment["WEB_ORIGIN"], access_mode="intranet-http")
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
    rules = "# Direct TCP peers only. Loopback is for container health checks.\n"
    rules += "allow 127.0.0.1;\nallow ::1;\n"
    rules += "".join(f"allow {network};\n" for network in networks)
    rules += "deny all;\n"
    return origin, path, rules


def prepare_access(config):
    """Write the dedicated admission input after validating the composition."""
    _, path, rules = intranet_settings(config)
    if not path.parent.is_dir() or path.is_symlink():
        raise ValueError("admission parent must exist and target cannot be a symlink")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="ascii", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(rules)
            stream.flush()
            # CIDRs are not credentials. The bind-mounted file must also work
            # when the container's non-root UID differs from the operator's.
            os.fchmod(stream.fileno(), 0o444)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify_access(config):
    _, path, rules = intranet_settings(config)
    if path.is_symlink() or path.stat().st_size > 4096 or path.read_text() != rules:
        raise ValueError("admission file differs from the approved client CIDRs")
