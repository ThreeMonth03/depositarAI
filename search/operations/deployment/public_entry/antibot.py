"""Optional browser challenges before the public gateway; no runtime mutation.

The HTTP entry rejects missing challenges as JSON on expensive APIs. Only GET
/verify renders HTML; successful verification returns home, never replays a POST.
OIDC callbacks retain the gateway's own state, nonce and body processing.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from search.operations.deployment.source_bundle import NGINX_BASE


ANUBIS_IMAGE = (
    "ghcr.io/techarohq/anubis:v1.28.1@sha256:"
    "1744c03a918160a28b0d2e8c1656de6585f4071b30b4c3b0d51d02282c9b7c25"
)
ASSET_ROOT = Path(__file__).resolve().parents[4] / "deploy/config/public-entry/antibot"
NETWORK = "antibot-gateway"


def _mount(source, target, *, read_only=True):
    return {
        "type": "bind",
        "source": str(source),
        "target": target,
        "read_only": read_only,
        "bind": {"create_host_path": False},
    }


def render_antibot(config, *, uid=None, gid=None):
    """Return assets and additions for a validated PublicEntryConfig.

    The caller owns configuration and secret validation. It must apply the
    returned APISIX overrides, stage state_directories with mode 0700 and retain
    the complete asset set in its deployment/upgrade integrity receipt.
    """
    if not getattr(config, "antibot_enabled", False):
        return {
            "assets": {},
            "services": {},
            "networks": {},
            "state_directories": [],
            "entry_service": "public-entry",
            "apisix_overrides": {},
        }
    uid = os.geteuid() if uid is None else uid
    gid = os.getegid() if gid is None else gid
    if any(type(v) is not int or not 1 <= v <= 2**31 - 1 for v in (uid, gid)):
        raise ValueError("public_entry_antibot_operator_invalid")
    signing_key = getattr(config, "antibot_signing_key_file", None)
    if not isinstance(signing_key, Path) or not signing_key.is_absolute():
        raise ValueError("public_entry_antibot_key_required")
    origin = urlsplit(config.public_origin)
    if origin.scheme != "https" or not origin.hostname:
        raise ValueError("public_entry_antibot_origin_invalid")
    for cidr in config.trusted_proxy_cidrs:
        # Never interpolate unparsed configuration into nginx directives.
        ipaddress.ip_network(cidr, strict=True)
    trusted = "\n".join(
        "    set_real_ip_from " + cidr + ";" for cidr in config.trusted_proxy_cidrs
    )
    template = (ASSET_ROOT / "nginx.conf.template").read_text(encoding="utf-8")
    for key, value in {
        "HOST": origin.hostname,
        "AUTHORITY": origin.netloc,
        "PORT": str(origin.port or 443),
        "TRUSTED_PROXIES": trusted,
    }.items():
        template = template.replace("@@" + key + "@@", value)
    root = config.state_root / "public-entry"
    runtime = {
        "user": f"{uid}:{gid}",
        "runtime": "runc",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "unless-stopped",
        "environment": {"NVIDIA_VISIBLE_DEVICES": "void"},
        "logging": {
            "driver": "json-file",
            "options": {"max-size": "5m", "max-file": "2"},
        },
        "networks": [NETWORK],
    }
    return {
        "assets": {
            "antibot-nginx.conf": template,
            "antibot-errors.conf": (ASSET_ROOT / "errors.conf").read_text(
                encoding="utf-8"
            ),
            "antibot-policy.yaml": (ASSET_ROOT / "policy.json").read_text(
                encoding="utf-8"
            ),
        },
        "services": {
            "public-edge": {
                **runtime,
                "image": NGINX_BASE,
                "entrypoint": ["nginx"],
                "command": ["-g", "daemon off;"],
                "cpus": 0.5,
                "mem_limit": "64m",
                "pids_limit": 32,
                "tmpfs": [f"/tmp:rw,noexec,nosuid,size=16m,uid={uid},gid={gid}"],
                "volumes": [
                    _mount(root / "antibot-nginx.conf", "/etc/nginx/nginx.conf"),
                    _mount(
                        root / "antibot-errors.conf", "/etc/nginx/antibot-errors.conf"
                    ),
                ],
                "ports": [
                    {
                        "target": 8080,
                        "published": str(config.loopback_port),
                        "host_ip": "127.0.0.1",
                        "protocol": "tcp",
                    }
                ],
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        "wget -q -O - --header='Host: "
                        + origin.netloc
                        + '\' http://127.0.0.1:8080/_edge_healthz | grep -q \'"status":"ok"\'',
                    ],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 6,
                },
            },
            "antibot": {
                **runtime,
                "image": ANUBIS_IMAGE,
                "cpus": 0.5,
                "mem_limit": "128m",
                "pids_limit": 32,
                "environment": {
                    "NVIDIA_VISIBLE_DEVICES": "void",
                    "BIND": ":8923",
                    "TARGET": " ",
                    "PUBLIC_URL": "",
                    "POLICY_FNAME": "/run/antibot/policy.yaml",
                    "ED25519_PRIVATE_KEY_HEX_FILE": "/run/secrets/antibot-signing-key",
                    "COOKIE_EXPIRATION_TIME": "24h",
                    "COOKIE_SECURE": "true",
                    "COOKIE_HTTP_ONLY": "true",
                    "COOKIE_SAME_SITE": "Lax",
                    "COOKIE_PARTITIONED": "false",
                    "COOKIE_DYNAMIC_DOMAIN": "false",
                    "JWT_RESTRICTION_HEADER": "X-Real-IP",
                    "REDIRECT_DOMAINS": origin.netloc,
                    "SLOG_LEVEL": "ERROR",
                    "METRICS_BIND": "127.0.0.1:9090",
                    "GOMAXPROCS": "1",
                },
                "volumes": [
                    _mount(root / "antibot-policy.yaml", "/run/antibot/policy.yaml"),
                    _mount(signing_key, "/run/secrets/antibot-signing-key"),
                    _mount(root / "antibot-data", "/data", read_only=False),
                ],
                "healthcheck": {
                    "test": ["CMD", "/ko-app/anubis", "-healthcheck"],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 6,
                },
            },
        },
        "networks": {NETWORK: {"internal": True}},
        "state_directories": ["antibot-data"],
        "entry_service": "public-edge",
        "apisix_overrides": {
            "ports": [],
            "add_networks": [NETWORK],
            "depends_on": {"public-edge": {"condition": "service_healthy"}},
            "real_ip_from": ["public-edge"],
        },
    }
