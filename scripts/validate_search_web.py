"""Guard the standalone CPU frontend composition."""

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import sys


def _origin(value, *, upstream=False, access_mode="private"):
    from search.web.origin import validate_origin

    parsed = validate_origin(value, access_mode=access_mode)
    if upstream and parsed.scheme != "https":
        raise ValueError("remote upstream requires an approved HTTPS origin")
    return parsed


def validate(config, *, intranet=False, prepare=False):
    if prepare and not intranet:
        raise ValueError("admission preparation requires intranet mode")
    services = config["services"]
    expected_project = (
        "depositar-search-intranet" if intranet else "depositar-search-web"
    )
    if config.get("name") != expected_project:
        raise ValueError("unexpected frontend project")
    if set(services) != {"web", "web-nginx"}:
        raise ValueError("remote frontend may contain only web and web-nginx")
    web = services["web"]
    access_mode = "intranet-http" if intranet else "private"
    if web["environment"].get("WEB_ACCESS_MODE", "private") != access_mode:
        raise ValueError("website access mode differs from deployment mode")
    origin = _origin(web["environment"].get("WEB_ORIGIN"), access_mode=access_mode)
    expected_host = origin.hostname if intranet else "127.0.0.1"
    expected_port = str(origin.port) if intranet else "18081"
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
                or ports[0].get("host_ip") != expected_host
                or str(ports[0].get("published")) != expected_port
                or ports[0].get("target") != 8080
                or ports[0].get("protocol", "tcp") != "tcp"
            ):
                raise ValueError(
                    "only the configured frontend address and port permitted"
                )
        elif ports:
            raise ValueError("private backend cannot publish a port")
    if not config["networks"]["website"].get("internal"):
        raise ValueError("website backend network must be internal")
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
        or (not intranet and services["web-nginx"].get("volumes"))
    ):
        raise ValueError("remote frontend receives only its API key and optional CA")
    if intranet:
        from search.operations.deployment.intranet_access import (
            intranet_settings,
            prepare_access,
            verify_access,
        )

        intranet_settings(config)
        if prepare:
            prepare_access(config)
        else:
            verify_access(config)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intranet", action="store_true")
    parser.add_argument("--prepare-access", action="store_true")
    args = parser.parse_args()
    description = "Intranet" if args.intranet else "Private"
    try:
        validate(
            json.load(sys.stdin), intranet=args.intranet, prepare=args.prepare_access
        )
    except (ValueError, KeyError, TypeError, OSError):
        print(f"{description} website deployment policy failed.", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"{description} website deployment policy passed.")
