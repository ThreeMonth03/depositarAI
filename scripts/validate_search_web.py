"""Guard the standalone CPU frontend composition."""

import json
from pathlib import PurePosixPath
import re
import sys
from urllib.parse import urlsplit


def _origin(value, *, upstream=False):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("a fixed origin is required")
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("only a bare origin is permitted")
    # Accessing port rejects malformed and out-of-range values.
    parsed.port
    if parsed.scheme != "https" and not (
        not upstream and parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
    ):
        raise ValueError("remote upstream requires an approved HTTPS origin")


def validate(config):
    services = config["services"]
    if config.get("name") != "depositar-search-web":
        raise ValueError("unexpected frontend project")
    if set(services) != {"web", "web-nginx"}:
        raise ValueError("remote frontend may contain only web and web-nginx")
    for name, service in services.items():
        if (
            not service.get("read_only")
            or service.get("cap_drop") != ["ALL"]
            or service.get("security_opt") != ["no-new-privileges:true"]
        ):
            raise ValueError("read-only unprivileged runtime required")
        if (
            service.get("privileged")
            or service.get("devices")
            or service.get("gpus")
            or service.get("deploy")
            or service.get("network_mode")
        ):
            raise ValueError(
                "website services must not allocate GPUs or host namespaces"
            )
        if not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", str(service.get("user", ""))):
            raise ValueError("non-root numeric user required")
        tmp = service.get("tmpfs", [])
        if len(tmp) != 1 or not tmp[0].startswith("/tmp:rw,noexec,nosuid,nodev,size="):
            raise ValueError("one bounded /tmp mount required")
        if (
            not service.get("mem_limit")
            or not service.get("cpus")
            or not service.get("pids_limit")
        ):
            raise ValueError("resource limits required")
        for mount in service.get("volumes", []):
            if (
                mount.get("type") != "bind"
                or not mount.get("read_only")
                or mount.get("bind", {}).get("create_host_path")
            ):
                raise ValueError("only existing read-only bind inputs permitted")
            if "docker.sock" in mount.get("source", ""):
                raise ValueError("no Docker socket in website")
        ports = service.get("ports", [])
        if name == "web-nginx":
            if (
                len(ports) != 1
                or ports[0].get("host_ip") != "127.0.0.1"
                or str(ports[0].get("published")) != "18081"
                or ports[0].get("target") != 8080
            ):
                raise ValueError("only the frontend loopback port permitted")
        elif ports:
            raise ValueError("private backend cannot publish a port")
    if not config["networks"]["website"].get("internal"):
        raise ValueError("website backend network must be internal")
    web = services["web"]
    _origin(web["environment"].get("WEB_ORIGIN"))
    _origin(web["environment"].get("WEB_UPSTREAM"), upstream=True)
    if (
        set(config["networks"]) != {"website", "upstream-route", "ingress"}
        or set(web["networks"]) != {"website", "upstream-route"}
        or set(services["web-nginx"]["networks"]) != {"website", "ingress"}
        or any(
            config["networks"][name].get("internal")
            or config["networks"][name].get("external")
            for name in ("upstream-route", "ingress")
        )
    ):
        raise ValueError("remote frontend network policy drift")
    mounts = web.get("volumes", [])
    ca_file = web["environment"].get("WEB_UPSTREAM_CA_FILE", "")
    expected_targets = {"/run/secrets/api_key"}
    if ca_file:
        if ca_file != "/run/certs/upstream-ca.pem":
            raise ValueError("private upstream CA requires its fixed read-only mount")
        expected_targets.add(ca_file)
    if (
        len(mounts) != len(expected_targets)
        or {mount.get("target") for mount in mounts} != expected_targets
        or any(
            not PurePosixPath(mount.get("source", "")).is_absolute() for mount in mounts
        )
        or services["web-nginx"].get("volumes")
    ):
        raise ValueError("remote frontend receives only its API key and optional CA")


if __name__ == "__main__":
    try:
        validate(json.load(sys.stdin))
    except (ValueError, KeyError, TypeError):
        print("Private website deployment policy failed.", file=sys.stderr)
        raise SystemExit(1) from None
    print("Private website deployment policy passed.")
