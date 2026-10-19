"""Prepare an isolated public edge; never activate it or change the private trial.

APISIX owns browser OIDC and synchronous Redis admission counters. The small
rewrite adapter transfers verified issuer/subject identity to the website while
preserving the website's independent search-session Authorization header.
Credentials are file references, never rendered configuration values.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from search.operations.shared import private_io
from search.serialization import strict_json


APISIX_IMAGE = (
    "apache/apisix:3.19.0-debian@sha256:"
    "9a7e45dc943fbf10ec916d1232bae4cda9302ae28a4696cfb83c2da01d261141"
)
REDIS_IMAGE = (
    "redis:8.4.2-alpine@sha256:"
    "e1b6db24cb4fdd89f4bc9be09f671ea3bec92fbd7042554f76c34aa2be9b59ad"
)
PROJECT = "depositar-ai-public-entry"
MAX_CONFIG_BYTES = 16384
HEX = re.compile(r"[0-9a-f]{64}\Z")
ROOT = Path(__file__).resolve().parents[4]
REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "state_root",
        "public_origin",
        "oidc_issuer",
        "oidc_discovery",
        "oidc_client_id",
        "oidc_client_secret_file",
        "oidc_session_secret_file",
        "edge_secret_file",
        "redis_password_file",
    }
)
OPTIONAL_FIELDS = frozenset(
    {
        "loopback_port",
        "quota_per_account",
        "quota_global",
        "quota_window_seconds",
        "session_limit",
        "session_window_seconds",
        "trusted_proxy_cidrs",
        "oidc_ca_file",
        "network_subnets",
    }
)


class PublicEntryError(ValueError):
    """Closed diagnostics: never interpolate secret or operator input bytes."""


@dataclass(frozen=True)
class PublicEntryConfig:
    schema_version: int
    state_root: Path
    public_origin: str
    oidc_issuer: str
    oidc_discovery: str
    oidc_client_id: str
    oidc_client_secret_file: Path
    oidc_session_secret_file: Path
    edge_secret_file: Path
    redis_password_file: Path
    loopback_port: int = 18084
    quota_per_account: int = 10
    quota_global: int = 100
    quota_window_seconds: int = 86400
    session_limit: int = 10
    session_window_seconds: int = 3600
    trusted_proxy_cidrs: tuple[str, ...] = ()
    oidc_ca_file: Path | None = None
    network_subnets: dict[str, str] | None = None

    def to_dict(self):
        result = asdict(self)
        for key, value in result.items():
            if isinstance(value, Path):
                result[key] = str(value)
        result["trusted_proxy_cidrs"] = list(self.trusted_proxy_cidrs)
        if self.oidc_ca_file is None:
            result.pop("oidc_ca_file")
        if self.network_subnets is None:
            result.pop("network_subnets")
        return result


def _require(condition, code):
    if not condition:
        raise PublicEntryError("public_entry_" + code)


def _path(raw, repo_root):
    _require(
        type(raw) is str and raw and not any(c in raw for c in "\x00\r\n,"),
        "path_invalid",
    )
    path = Path(raw)
    _require(
        path.is_absolute() and ".." not in path.parts and path != Path("/"),
        "path_invalid",
    )
    _require(
        path != repo_root
        and repo_root not in path.parents
        and path not in repo_root.parents,
        "path_in_repository",
    )
    _require(
        all(not item.is_symlink() for item in (path, *path.parents)), "path_symlink"
    )
    return path


def _read_private(path, *, uid, maximum):
    parent = path.parent.lstat()
    _require(
        stat.S_ISDIR(parent.st_mode)
        and parent.st_uid == uid
        and stat.S_IMODE(parent.st_mode) == 0o700,
        "file_parent_unsafe",
    )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        _require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == uid
            and before.st_nlink == 1
            and stat.S_IMODE(before.st_mode) in (0o400, 0o600),
            "file_unsafe",
        )
        _require(1 <= before.st_size <= maximum, "file_size")
        encoded = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        _require(
            len(encoded) == before.st_size
            and private_io.identity(before) == private_io.identity(after)
            and private_io.identity(after) == private_io.identity(path.lstat()),
            "file_changed",
        )
        return encoded
    finally:
        os.close(fd)


def _url(raw, *, origin=False):
    _require(
        type(raw) is str
        and 1 <= len(raw) <= 2048
        and raw.isascii()
        and not any(c.isspace() or ord(c) < 32 for c in raw),
        "url_invalid",
    )
    value = urlsplit(raw)
    _require(
        value.scheme == "https"
        and value.hostname
        and value.username is None
        and value.password is None
        and not value.query
        and not value.fragment
        and "\\" not in raw,
        "url_invalid",
    )
    _require(value.port is None or 1 <= value.port <= 65535, "url_invalid")
    try:
        ipaddress.ip_address(value.hostname)
    except ValueError:
        _require(
            len(value.hostname) <= 253
            and all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in value.hostname.split(".")
            ),
            "url_host_invalid",
        )
    _require(
        raw == value.geturl() and value.netloc == value.netloc.lower(), "url_invalid"
    )
    if origin:
        _require(value.path == "" and value.port != 443, "origin_invalid")
    return raw


def validate_public_entry_config(value, *, repo_root=None, uid=None):
    """Validate existing private inputs, including bounded secret-file formats."""
    repo_root = ROOT if repo_root is None else Path(repo_root).resolve()
    uid = os.geteuid() if uid is None else uid
    try:
        _require(
            type(value) is dict
            and REQUIRED_FIELDS <= set(value)
            and not set(value) - REQUIRED_FIELDS - OPTIONAL_FIELDS,
            "fields",
        )
        _require(
            type(value["schema_version"]) is int and value["schema_version"] == 1,
            "schema",
        )
        parsed = dict(value)
        parsed["state_root"] = _path(value["state_root"], repo_root)
        root_info = parsed["state_root"].lstat()
        _require(
            stat.S_ISDIR(root_info.st_mode)
            and root_info.st_uid == uid
            and stat.S_IMODE(root_info.st_mode) == 0o700,
            "state_root_unsafe",
        )
        for field in ("public_origin", "oidc_issuer", "oidc_discovery"):
            parsed[field] = _url(value[field], origin=field == "public_origin")
        _require(
            type(value["oidc_client_id"]) is str
            and 1 <= len(value["oidc_client_id"]) <= 255
            and value["oidc_client_id"].isascii()
            and not any(ord(c) < 33 or ord(c) > 126 for c in value["oidc_client_id"]),
            "client_id_invalid",
        )
        for field in (
            "oidc_client_secret_file",
            "oidc_session_secret_file",
            "edge_secret_file",
            "redis_password_file",
        ):
            parsed[field] = _path(value[field], repo_root)
            encoded = _read_private(parsed[field], uid=uid, maximum=4097)
            if encoded.endswith(b"\n"):
                encoded = encoded[:-1]
            if field == "oidc_client_secret_file":
                _require(
                    1 <= len(encoded) <= 4096 and all(33 <= c <= 126 for c in encoded),
                    "client_secret_invalid",
                )
            else:
                _require(
                    HEX.fullmatch(encoded.decode("ascii")) is not None, "secret_invalid"
                )
        _require(
            len(
                {
                    parsed[field]
                    for field in (
                        "oidc_client_secret_file",
                        "oidc_session_secret_file",
                        "edge_secret_file",
                        "redis_password_file",
                    )
                }
            )
            == 4,
            "secret_paths_overlap",
        )
        bounds = {
            "loopback_port": (1024, 65535),
            "quota_per_account": (1, 10000),
            "quota_global": (1, 100000),
            "quota_window_seconds": (60, 604800),
            "session_limit": (1, 1000),
            "session_window_seconds": (60, 86400),
        }
        for name, (low, high) in bounds.items():
            if name in value:
                _require(
                    type(value[name]) is int and low <= value[name] <= high,
                    "limits_invalid",
                )
        _require(value.get("loopback_port", 18084) != 18083, "private_port_reserved")
        _require(
            value.get("quota_per_account", 10) <= value.get("quota_global", 100),
            "limits_invalid",
        )
        cidrs = value.get("trusted_proxy_cidrs", [])
        _require(type(cidrs) is list and len(cidrs) <= 16, "trusted_proxy_invalid")
        normalized = []
        for cidr in cidrs:
            _require(type(cidr) is str, "trusted_proxy_invalid")
            network = ipaddress.ip_network(cidr, strict=True)
            _require(
                network.prefixlen > 0 and not network.is_multicast,
                "trusted_proxy_invalid",
            )
            normalized.append(str(network))
        _require(len(set(normalized)) == len(normalized), "trusted_proxy_invalid")
        parsed["trusted_proxy_cidrs"] = tuple(normalized)
        if "network_subnets" in value:
            subnets = value["network_subnets"]
            _require(
                type(subnets) is dict
                and set(subnets) == {"website", "quota", "outbound"},
                "network_subnets_invalid",
            )
            networks = []
            normalized_subnets = {}
            private_ranges = [
                ipaddress.ip_network(item)
                for item in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            ]
            for name, raw in subnets.items():
                _require(type(raw) is str, "network_subnets_invalid")
                network = ipaddress.ip_network(raw, strict=True)
                _require(
                    network.version == 4
                    and 24 <= network.prefixlen <= 28
                    and any(network.subnet_of(private) for private in private_ranges),
                    "network_subnets_invalid",
                )
                _require(
                    all(not network.overlaps(previous) for previous in networks),
                    "network_subnets_overlap",
                )
                networks.append(network)
                normalized_subnets[name] = str(network)
            parsed["network_subnets"] = normalized_subnets
        if "oidc_ca_file" in value:
            parsed["oidc_ca_file"] = _path(value["oidc_ca_file"], repo_root)
            certificate = _read_private(
                parsed["oidc_ca_file"], uid=uid, maximum=256 * 1024
            )
            _require(
                b"-----BEGIN CERTIFICATE-----" in certificate
                and b"PRIVATE KEY" not in certificate,
                "ca_invalid",
            )
        return PublicEntryConfig(**parsed)
    except PublicEntryError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError):
        raise PublicEntryError("public_entry_configuration_invalid") from None


def load_public_entry_config(path, *, repo_root=None, uid=None):
    uid = os.geteuid() if uid is None else uid
    try:
        path = Path(path)
        _require(
            path.is_absolute()
            and all(not p.is_symlink() for p in (path, *path.parents)),
            "config_path_invalid",
        )
        raw = _read_private(path, uid=uid, maximum=MAX_CONFIG_BYTES)
        try:
            value = strict_json(raw)
        except ValueError as error:
            if error.args == ("duplicate JSON key",):
                raise PublicEntryError("public_entry_duplicate_key") from None
            raise
        return validate_public_entry_config(value, repo_root=repo_root, uid=uid)
    except PublicEntryError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError):
        raise PublicEntryError("public_entry_configuration_invalid") from None


def _json(value):
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    )


def _sanitize(config, *, api=False, mutating=False, cookie_auth=False):
    """Runs before OIDC: reject bad boundary inputs and stash the BFF token."""
    authority = urlsplit(config.public_origin).netloc
    return """return function(conf, ctx)
 local core = require('apisix.core')
 local h, err = ngx.req.get_headers(64)
 if err then return 400, {error={code='invalid_request'}} end
 for _, name in ipairs({'host','origin','authorization','content-length','content-type'}) do
  if type(h[name]) == 'table' then return 400, {error={code='invalid_request'}} end
 end
 if type(h.host) ~= 'string' or string.lower(h.host) ~= %s then
  return 421, {error={code='host_denied'}}
 end
 %s
 %s
 ctx.depositar_authorization = h.authorization
 for _, name in ipairs({'Authorization','X-Depositar-Subject','X-Depositar-Edge-Key',
  'X-Depositar-Quota-Bucket','X-ID-Token','X-Raw-ID-Token','X-Userinfo','X-Access-Token',
  'X-Refresh-Token','Forwarded','X-Forwarded-Host','X-Forwarded-Proto',
  'X-Forwarded-Port','X-Forwarded-For','X-Real-IP'}) do
  core.request.set_header(ctx, name, nil)
 end
 core.request.set_header(ctx, 'X-Forwarded-Proto', 'https')
 core.request.set_header(ctx, 'X-Forwarded-Host', %s)
 core.request.set_header(ctx, 'X-Forwarded-Port', %s)
 %s
end""" % (
        json.dumps(authority),
        "if ngx.var.args then return 400, {error={code='invalid_request'}} end"
        if api
        else "",
        (
            "if h.origin ~= "
            + json.dumps(config.public_origin)
            + " then return 403, {error={code='origin_denied'}} end"
        )
        if mutating
        else (
            "if h.origin and h.origin ~= "
            + json.dumps(config.public_origin)
            + " then return 403, {error={code='origin_denied'}} end"
        )
        if api
        else "",
        json.dumps(authority),
        json.dumps(str(urlsplit(config.public_origin).port or 443)),
        "" if cookie_auth else "core.request.set_header(ctx, 'Cookie', nil)",
    )


def _identity(config, *, redirect=False):
    bucket = hashlib.sha256(config.public_origin.encode("ascii")).hexdigest()
    return """return function(conf, ctx)
 local core = require('apisix.core')
 local encoded = core.request.header(ctx, 'X-ID-Token')
 if type(encoded) ~= 'string' or #encoded > 32768 then return 401, {error={code='authentication_required'}} end
 local decoded = ngx.decode_base64(encoded)
 local claims = decoded and core.json.decode(decoded)
 if type(claims) ~= 'table' or claims.iss ~= %s or type(claims.sub) ~= 'string'
  or #claims.sub < 1 or #claims.sub > 255 or string.find(claims.sub, '[%%z%%c]') then
  return 401, {error={code='authentication_required'}}
 end
 local client_id = %s
 local audience_ok = false
 local audience_count = 1
 if type(claims.aud) == 'string' then
  audience_ok = claims.aud == client_id
 elseif type(claims.aud) == 'table' then
  audience_count = #claims.aud
  if audience_count < 1 or audience_count > 16 then return 401, {error={code='authentication_required'}} end
  local entries = 0
  for key, value in pairs(claims.aud) do
   if type(key) ~= 'number' or key ~= math.floor(key) or key < 1 or key > audience_count
    or type(value) ~= 'string' or #value < 1 or #value > 255 then
    return 401, {error={code='authentication_required'}}
   end
   entries = entries + 1
   if value == client_id then audience_ok = true end
  end
  if entries ~= audience_count then return 401, {error={code='authentication_required'}} end
 end
 if not audience_ok or (claims.azp ~= nil and claims.azp ~= client_id)
  or (audience_count > 1 and claims.azp ~= client_id) then
  return 401, {error={code='authentication_required'}}
 end
 local sha = require('resty.sha256'):new()
 sha:update(claims.iss); sha:update(string.char(0)); sha:update(claims.sub)
 local subject = require('resty.string').to_hex(sha:final())
 local edge_key = os.getenv('DEPOSITAR_EDGE_KEY')
 if type(edge_key) ~= 'string' or #edge_key ~= 64 or string.find(edge_key, '[^0-9a-f]') then
  return 503, {error={code='service_unavailable'}}
 end
 for _, name in ipairs({'X-ID-Token','X-Raw-ID-Token','X-Userinfo','X-Access-Token','X-Refresh-Token','Cookie'}) do
  core.request.set_header(ctx, name, nil)
 end
 core.request.set_header(ctx, 'Authorization', ctx.depositar_authorization)
 core.request.set_header(ctx, 'X-Depositar-Subject', subject)
 core.request.set_header(ctx, 'X-Depositar-Edge-Key', edge_key)
 core.request.set_header(ctx, 'X-Depositar-Quota-Bucket', %s)
 ctx.var.http_x_depositar_subject = nil
 ctx.var.http_x_depositar_quota_bucket = nil
 %s
end""" % (
        json.dumps(config.oidc_issuer),
        json.dumps(config.oidc_client_id),
        json.dumps(bucket),
        "ngx.header['Location'] = '/'; return 302" if redirect else "",
    )


def _oidc(config, *, login=False):
    return {
        "client_id": config.oidc_client_id,
        "client_secret": "$ENV://DEPOSITAR_OIDC_CLIENT_SECRET",
        "discovery": config.oidc_discovery,
        "redirect_uri": config.public_origin + "/auth/callback",
        "logout_path": "/auth/logout",
        "post_logout_redirect_uri": config.public_origin + "/",
        "scope": "openid",
        "bearer_only": False,
        "use_jwks": False,
        "ssl_verify": True,
        "timeout": 5,
        "use_pkce": True,
        "use_nonce": True,
        "accept_none_alg": False,
        "accept_unsupported_alg": False,
        "unauth_action": "auth" if login else "deny",
        "set_access_token_header": False,
        "set_id_token_header": True,
        "set_userinfo_header": False,
        "set_refresh_token_header": False,
        "set_raw_id_token_header": False,
        "session_contents": {
            "id_token": True,
            "access_token": True,
            "enc_id_token": True,
        },
        "session": {
            "secret": "$ENV://DEPOSITAR_OIDC_SESSION_SECRET",
            "cookie_name": "__Host-depositar-login",
            "cookie_path": "/",
            "cookie_secure": True,
            "cookie_http_only": True,
            "cookie_same_site": "Lax",
            "absolute_timeout": 86400,
            "idling_timeout": 1800,
            "rolling_timeout": 3600,
        },
        "claim_validator": {
            "issuer": {"valid_issuers": [config.oidc_issuer]},
            "audience": {"required": True, "match_with_client_id": True},
        },
        "claim_schema": {
            "type": "object",
            "required": ["id_token"],
            "properties": {
                "id_token": {
                    "type": "object",
                    "required": ["iss", "sub"],
                    "properties": {
                        "iss": {"type": "string", "enum": [config.oidc_issuer]},
                        "sub": {"type": "string", "minLength": 1, "maxLength": 255},
                    },
                }
            },
        },
        "_meta": {"error_response": {"error": {"code": "authentication_required"}}},
    }


def _counter(config, *, session=False):
    return {
        "policy": "redis",
        "redis_host": "quota-redis",
        "redis_port": 6379,
        "redis_username": "gateway",
        "redis_password": "$ENV://DEPOSITAR_REDIS_PASSWORD",
        "redis_database": 0,
        "redis_timeout": 1000,
        "allow_degradation": False,
        "sync_interval": -1,
        "window_type": "fixed",
        "rejected_code": 429,
        "rejected_msg": "quota_exceeded",
        "show_limit_quota_header": True,
        # Stable route IDs/configuration preserve keys across gateway restarts.
        # APISIX rejects combining group with rules. Changing this plugin's
        # configuration creates a new namespace; operators must drain quotas
        # before changing quota/backend settings.
        "rules": [
            {
                "count": config.session_limit if session else config.quota_per_account,
                "time_window": config.session_window_seconds
                if session
                else config.quota_window_seconds,
                "key": "account:$http_x_depositar_subject",
                "header_prefix": "Account",
            },
            {
                "count": 100 if session else config.quota_global,
                "time_window": config.session_window_seconds
                if session
                else config.quota_window_seconds,
                "key": "global:$http_x_depositar_quota_bucket",
                "header_prefix": "Global",
            },
        ],
        "_meta": {"error_response": {"error": {"code": "quota_exceeded"}}},
    }


APISIX_START = """#!/bin/sh
set -eu
umask 077
DEPOSITAR_OIDC_CLIENT_SECRET=$(cat /run/secrets/oidc-client-secret)
DEPOSITAR_OIDC_SESSION_SECRET=$(cat /run/secrets/oidc-session-secret)
DEPOSITAR_EDGE_KEY=$(cat /run/secrets/edge-key)
DEPOSITAR_REDIS_PASSWORD=$(cat /run/secrets/redis-password)
for secret in "$DEPOSITAR_OIDC_SESSION_SECRET" "$DEPOSITAR_EDGE_KEY" "$DEPOSITAR_REDIS_PASSWORD"; do
 case "$secret" in *[!0-9a-f]*|'') exit 1;; esac
 test "${#secret}" -eq 64
done
test -n "$DEPOSITAR_OIDC_CLIENT_SECRET"
test "${#DEPOSITAR_OIDC_CLIENT_SECRET}" -le 4096
export DEPOSITAR_OIDC_CLIENT_SECRET DEPOSITAR_OIDC_SESSION_SECRET DEPOSITAR_EDGE_KEY DEPOSITAR_REDIS_PASSWORD
export APISIX_STAND_ALONE=true
cp /usr/local/openresty/nginx/conf/mime.types /usr/local/apisix/conf/mime.types
exec /docker-entrypoint.sh docker-start
"""

REDIS_START = """#!/bin/sh
set -eu
umask 077
password=$(cat /run/secrets/redis-password)
case "$password" in *[!0-9a-f]*|'') exit 1;; esac
test "${#password}" -eq 64
digest=$(printf '%s' "$password" | sha256sum)
digest=${digest%% *}
printf 'user default off\\nuser gateway on #%s ~plugin-limit-count:v1:/routes/turn:* ~plugin-limit-count:v1:/routes/session-create:* -@all +auth +ping +eval +evalsha +pttl +set +incrby\\n' "$digest" > /tmp/users.acl
unset password digest
exec redis-server /etc/redis/redis.conf
"""

REDIS_CONFIG = """bind 0.0.0.0
port 6379
protected-mode yes
aclfile /tmp/users.acl
dir /data
appendonly yes
appendfsync always
no-appendfsync-on-rewrite no
save ""
maxmemory 64mb
maxmemory-policy noeviction
maxclients 128
timeout 30
tcp-keepalive 30
loglevel warning
logfile ""
"""


def _mount(source, target, *, read_only=True):
    return {
        "type": "bind",
        "source": str(source),
        "target": target,
        "read_only": read_only,
        "bind": {"create_host_path": False},
    }


def render_public_entry(config, *, uid=None, gid=None):
    """Return deployment assets without creating files or touching Docker.

    The caller must add an authenticated public-web image and bind it to the
    existing private conversation network before treating this as deployable.
    """
    uid = os.geteuid() if uid is None else uid
    gid = os.getegid() if gid is None else gid
    _require(isinstance(config, PublicEntryConfig), "config_invalid")
    _require(
        all(type(v) is int and 1 <= v <= 2**31 - 1 for v in (uid, gid)),
        "operator_invalid",
    )
    config = validate_public_entry_config(config.to_dict(), uid=uid)
    root = config.state_root / "public-entry"
    origin_host = urlsplit(config.public_origin).hostname
    upstream = {
        "type": "roundrobin",
        "nodes": {"public-web:8000": 1},
        "pass_host": "rewrite",
        "upstream_host": urlsplit(config.public_origin).netloc,
        "timeout": {"connect": 3, "send": 5, "read": 130},
        "scheme": "http",
    }
    routes = []

    def route(
        identifier,
        uri,
        methods,
        *,
        api=False,
        authenticated=False,
        login=False,
        session=False,
        turn=False,
    ):
        plugins = {
            "serverless-pre-function": {
                "phase": "rewrite",
                "functions": [
                    _sanitize(
                        config,
                        api=api,
                        mutating=api and methods != ["GET"],
                        cookie_auth=authenticated,
                    )
                ],
            },
            "limit-req": {
                "rate": 3,
                "burst": 12,
                "key": "remote_addr",
                "nodelay": True,
                "allow_degradation": False,
                "rejected_code": 429,
                "_meta": {"error_response": {"error": {"code": "rate_limited"}}},
            },
            "response-rewrite": {
                "headers": {
                    "set": {
                        "Cache-Control": "no-store",
                        "Referrer-Policy": "no-referrer",
                        "X-Content-Type-Options": "nosniff",
                        "X-Frame-Options": "DENY",
                    }
                }
            },
        }
        if authenticated:
            plugins["openid-connect"] = _oidc(config, login=login)
            plugins["serverless-post-function"] = {
                "phase": "rewrite",
                "functions": [_identity(config, redirect=login)],
            }
        if turn or session:
            plugins["limit-count"] = _counter(config, session=session)
        routes.append(
            {
                "id": identifier,
                "uri": uri,
                "methods": methods,
                "host": origin_host,
                "plugins": plugins,
                "upstream": upstream,
            }
        )

    route("public-home", "/", ["GET", "HEAD"])
    route("public-assets", "/assets/*", ["GET", "HEAD"])
    route("public-favicon", "/favicon.ico", ["GET", "HEAD"])
    route("public-health", "/healthz", ["GET"])
    route("access", "/api/access", ["GET"], api=True, authenticated=True)
    route(
        "session-create",
        "/api/session",
        ["POST"],
        api=True,
        authenticated=True,
        session=True,
    )
    route("session-delete", "/api/session", ["DELETE"], api=True, authenticated=True)
    route("turn", "/api/turn", ["POST"], api=True, authenticated=True, turn=True)
    route("login", "/auth/login", ["GET"], authenticated=True, login=True)
    route("callback", "/auth/callback", ["GET"], authenticated=True, login=True)
    route("logout", "/auth/logout", ["GET"], authenticated=True, login=True)
    apisix_config = {
        "apisix": {
            "node_listen": 9080,
            "enable_admin": False,
            "enable_control": False,
            "enable_server_tokens": False,
            "ssl": {"enable": False},
        },
        "deployment": {
            "role": "data_plane",
            "role_data_plane": {"config_provider": "yaml"},
        },
        "plugins": [
            "serverless-pre-function",
            "openid-connect",
            "serverless-post-function",
            "limit-count",
            "limit-req",
            "response-rewrite",
        ],
        "nginx_config": {
            "worker_processes": 1,
            "error_log": "/dev/stderr",
            "error_log_level": "crit",
            "http_configuration_snippet": "client_body_temp_path /tmp/client_body;\nproxy_temp_path /tmp/proxy;\nfastcgi_temp_path /tmp/fastcgi;\nuwsgi_temp_path /tmp/uwsgi;\nscgi_temp_path /tmp/scgi;",
            "envs": [
                "DEPOSITAR_OIDC_CLIENT_SECRET",
                "DEPOSITAR_OIDC_SESSION_SECRET",
                "DEPOSITAR_EDGE_KEY",
                "DEPOSITAR_REDIS_PASSWORD",
            ],
            "http": {
                "enable_access_log": False,
                "client_max_body_size": "4k",
                "client_header_timeout": "5s",
                "client_body_timeout": "5s",
                "keepalive_timeout": "5s",
                "real_ip_header": "X-Real-IP",
                "real_ip_recursive": "off",
                "real_ip_from": list(config.trusted_proxy_cidrs),
            },
        },
    }
    if config.oidc_ca_file is not None:
        apisix_config["apisix"]["ssl"]["ssl_trusted_certificate"] = (
            "/run/secrets/oidc-ca.pem"
        )
    routes_config = {"routes": routes}
    assets = {
        "config.yaml": _json(apisix_config),
        "apisix.yaml": _json(routes_config) + "#END\n",
        "redis.conf": REDIS_CONFIG,
        "apisix-start.sh": APISIX_START,
        "redis-start.sh": REDIS_START,
    }
    runtime = {
        "user": f"{uid}:{gid}",
        "runtime": "runc",
        "environment": {"NVIDIA_VISIBLE_DEVICES": "void"},
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "unless-stopped",
        "logging": {
            "driver": "json-file",
            "options": {"max-size": "5m", "max-file": "2"},
        },
    }
    compose = {
        "name": PROJECT,
        "services": {
            "public-entry": {
                **runtime,
                "image": APISIX_IMAGE,
                "entrypoint": ["/bin/sh", "/run/entry/apisix-start.sh"],
                "cpus": 1,
                "mem_limit": "512m",
                "pids_limit": 64,
                "ports": [
                    {
                        "target": 9080,
                        "published": str(config.loopback_port),
                        "host_ip": "127.0.0.1",
                        "protocol": "tcp",
                    }
                ],
                "networks": ["website", "quota", "outbound"],
                "depends_on": {"quota-redis": {"condition": "service_healthy"}},
                "healthcheck": {
                    "test": [
                        "CMD",
                        "/bin/bash",
                        "-c",
                        "exec 3<>/dev/tcp/127.0.0.1/9080; printf 'GET /healthz HTTP/1.1\\r\\nHost: "
                        + urlsplit(config.public_origin).netloc
                        + "\\r\\nConnection: close\\r\\n\\r\\n' >&3; IFS=' ' read -r protocol status message <&3; test \"$$status\" = 200",
                    ],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 12,
                },
                "tmpfs": [
                    f"/tmp:rw,noexec,nosuid,size=16m,uid={uid},gid={gid}",
                    f"/usr/local/apisix/conf:rw,noexec,nosuid,size=16m,uid={uid},gid={gid}",
                    f"/usr/local/apisix/logs:rw,noexec,nosuid,size=16m,uid={uid},gid={gid}",
                ],
                "volumes": [
                    _mount(root / "config.yaml", "/usr/local/apisix/conf/config.yaml"),
                    _mount(root / "apisix.yaml", "/usr/local/apisix/conf/apisix.yaml"),
                    _mount(root / "apisix-start.sh", "/run/entry/apisix-start.sh"),
                    _mount(
                        config.oidc_client_secret_file,
                        "/run/secrets/oidc-client-secret",
                    ),
                    _mount(
                        config.oidc_session_secret_file,
                        "/run/secrets/oidc-session-secret",
                    ),
                    _mount(config.edge_secret_file, "/run/secrets/edge-key"),
                    _mount(config.redis_password_file, "/run/secrets/redis-password"),
                ],
            },
            "quota-redis": {
                **runtime,
                "image": REDIS_IMAGE,
                "entrypoint": ["/bin/sh", "/run/entry/redis-start.sh"],
                "cpus": 0.5,
                "mem_limit": "128m",
                "pids_limit": 32,
                "networks": ["quota"],
                "tmpfs": [f"/tmp:rw,noexec,nosuid,size=4m,uid={uid},gid={gid}"],
                "volumes": [
                    _mount(root / "redis-start.sh", "/run/entry/redis-start.sh"),
                    _mount(root / "redis.conf", "/etc/redis/redis.conf"),
                    _mount(config.redis_password_file, "/run/secrets/redis-password"),
                    _mount(root / "redis-data", "/data", read_only=False),
                ],
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        'REDISCLI_AUTH="$(cat /run/secrets/redis-password)" redis-cli --no-auth-warning --user gateway ping | grep -qx PONG',
                    ],
                    "interval": "5s",
                    "timeout": "3s",
                    "retries": 6,
                },
            },
        },
        "networks": {
            "website": {"internal": True},
            "quota": {"internal": True},
            "outbound": {"internal": False},
        },
        "x-depositar-plan": {
            "schema_version": 1,
            "status": "public_entry_prepared_requires_web_binding",
            "public_origin": config.public_origin,
            "runtime_verified": False,
            "upstream": "public-web:8000",
            "external_https_proxy_required": True,
            "quota_counts": "admitted_http_requests_not_gpu_calls",
            "anonymous_ai": False,
        },
    }
    if config.oidc_ca_file is not None:
        compose["services"]["public-entry"]["volumes"].append(
            _mount(config.oidc_ca_file, "/run/secrets/oidc-ca.pem")
        )
    if config.network_subnets is not None:
        for name, subnet in config.network_subnets.items():
            compose["networks"][name]["ipam"] = {"config": [{"subnet": subnet}]}
    return {
        "compose": compose,
        "apisix_config": apisix_config,
        "apisix_routes": routes_config,
        "redis_config": REDIS_CONFIG,
        "assets": assets,
        "status": "public_entry_prepared_requires_web_binding",
    }


def write_public_entry(config, *, uid=None, gid=None):
    """Stage a new private edge directory; refuse any existing destination."""
    plan = render_public_entry(config, uid=uid, gid=gid)
    fd = private_io.open_directory(config.state_root)
    output = None
    try:
        output = private_io.open_child_directory(fd, "public-entry", create=True)
        for name, content in plan["assets"].items():
            private_io.write_file(
                output, name, content.encode("utf-8"), max_bytes=256 * 1024
            )
        private_io.write_json(
            output, "compose.json", plan["compose"], max_bytes=256 * 1024
        )
        data = private_io.open_child_directory(output, "redis-data", create=True)
        os.close(data)
        private_io.verify_directory(fd, config.state_root)
    finally:
        if output is not None:
            os.close(output)
        os.close(fd)
    return plan
