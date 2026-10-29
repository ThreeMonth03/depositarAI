"""Operator-selected browser origins, including an explicit intranet HTTP mode."""

from ipaddress import IPv4Address, IPv4Network
from urllib.parse import SplitResult, urlsplit


_INTRANET_NETWORKS = tuple(
    IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def validate_origin(value: str, *, access_mode: str = "private") -> SplitResult:
    """Validate a fixed browser origin without granting any network access.

    The deployment's client-address ACL remains responsible for admission.
    Intranet HTTP requires an explicit mode, RFC1918 IPv4 and a dedicated port.
    """
    if access_mode not in ("private", "intranet-http"):
        raise ValueError("unknown website access mode")
    if (
        not isinstance(value, str)
        or not value.isascii()
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
        or "\\" in value
    ):
        raise ValueError("origin must be a bare origin")
    parsed = urlsplit(value)
    # Accessing port also rejects malformed and out-of-range values.
    port = parsed.port
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value != f"{parsed.scheme}://{parsed.netloc}"
    ):
        raise ValueError("origin must be a bare origin")
    if access_mode == "intranet-http":
        address = IPv4Address(parsed.hostname)
        if (
            parsed.scheme != "http"
            or not any(address in network for network in _INTRANET_NETWORKS)
            or port is None
            or not 1024 <= port <= 65535
            or value != f"http://{address}:{port}"
        ):
            raise ValueError("intranet HTTP requires RFC1918 IPv4 and a dedicated port")
    elif parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
    ):
        raise ValueError(
            "HTTP requires an explicit intranet mode or local private demo"
        )
    return parsed
