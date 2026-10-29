"""Read-only standalone frontend and authenticated TLS version verification.

No inference, lifecycle command, host credential read, redirect or proxy is used.
Expected deployment identities belong to the operator receipt, not this checkout.
"""

import argparse
from html.parser import HTMLParser
import http.client
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from urllib.parse import urlsplit

from search.serialization import strict_json
from search.transport.version import CONVERSATION_CONTRACT_VERSION
from . import frontend_policy
from search.operations.shared.bounded_http import AbortableConnection
from .intranet_access import ACCESS_TARGET, intranet_settings
from .public_entry import profile
from .runtime_docker import Docker


ROOT = Path(__file__).resolve().parents[3]
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
FIELDS = frozenset(
    (
        "schema_version",
        "frontend_env_file",
        "frontend_revision",
        "deployment_mode",
        "conversation_contract_version",
        "web_platform_manifest",
        "web_nginx_platform_manifest",
        "api_revision",
        "catalog_manifest_sha256",
    )
)


class FrontendCheckError(ValueError):
    """Fixed diagnostics only; no daemon output, response or credential bytes."""


def _require(condition, code):
    if not condition:
        raise FrontendCheckError("frontend_check_" + code)


def load_config(path):
    path = profile._path(str(path), ROOT)
    value = strict_json(profile._read_private(path, uid=os.geteuid(), maximum=16384))
    _require(type(value) is dict and set(value) == FIELDS, "config_invalid")
    _require(
        type(value["schema_version"]) is int and value["schema_version"] == 3,
        "config_invalid",
    )
    _require(
        type(value["deployment_mode"]) is str
        and value["deployment_mode"] in frontend_policy.PROJECTS,
        "config_invalid",
    )
    _require(
        type(value["conversation_contract_version"]) is int
        and value["conversation_contract_version"] == CONVERSATION_CONTRACT_VERSION,
        "config_invalid",
    )
    for name, pattern in (
        ("frontend_revision", HEX40),
        ("api_revision", HEX40),
        ("catalog_manifest_sha256", HEX64),
        ("web_platform_manifest", IMAGE),
        ("web_nginx_platform_manifest", IMAGE),
    ):
        _require(
            type(value[name]) is str and pattern.fullmatch(value[name]),
            "config_invalid",
        )
    env = profile._path(value["frontend_env_file"], ROOT)
    # Compose owns dotenv interpolation; this read authenticates its input only.
    profile._read_private(env, uid=os.geteuid(), maximum=16384)
    return value


def _policy(composition, config):
    try:
        frontend_policy.validate(
            composition, intranet=config["deployment_mode"] == "intranet-http"
        )
    except (ValueError, KeyError, TypeError, OSError):
        raise FrontendCheckError("frontend_check_profile_invalid") from None


def _compose(client, config):
    mode = config["deployment_mode"]
    command = [
        "compose",
        "--project-name",
        frontend_policy.PROJECTS[mode],
        "--env-file",
        config["frontend_env_file"],
        "-f",
        str(ROOT / "deploy/compose/compose.search-web.yaml"),
    ]
    if mode == "intranet-http":
        command += ["-f", str(ROOT / "deploy/compose/compose.search-web.intranet.yaml")]
    first = strict_json(client.call(command + ["config", "--format", "json"]))
    ca = first["services"]["web"]["environment"].get("WEB_UPSTREAM_CA_FILE")
    if ca:
        _require(
            type(ca) is str
            and Path(ca).is_absolute()
            and not any(c in ca for c in "\0\r\n"),
            "profile_invalid",
        )
        command += ["-f", str(ROOT / "deploy/compose/compose.search-web.ca.yaml")]
        first = strict_json(client.call(command + ["config", "--format", "json"]))
    _policy(first, config)
    return first


def _environment(values):
    _require(
        type(values) is list and all(type(v) is str and "=" in v for v in values),
        "runtime_invalid",
    )
    pairs = [v.split("=", 1) for v in values]
    _require(len({p[0] for p in pairs}) == len(pairs), "runtime_invalid")
    return dict(pairs)


def _resource_integer(value):
    # Compose versions emit byte counts as either JSON integers or decimal
    # strings. Normalize only those representations, never units or floats.
    if type(value) is str:
        _require(re.fullmatch(r"[1-9][0-9]{0,18}", value), "profile_invalid")
        value = int(value)
    _require(type(value) is int and 0 < value <= 2**63 - 1, "profile_invalid")
    return value


INSPECT = (
    '{"Id":{{json .Id}},"Image":{{json .Image}},"Config":{{json .Config}},'
    '"State":{{json .State}},"HostConfig":{{json .HostConfig}},'
    '"Mounts":{{json .Mounts}},"NetworkSettings":{{json .NetworkSettings}},'
    '"ImageManifestDescriptor":{{json .ImageManifestDescriptor}}}'
)


def _descriptor(value, manifest):
    _require(
        type(value) is dict
        and value.get("digest") == manifest
        and value.get("mediaType")
        in {
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        }
        and value.get("platform") == {"architecture": "amd64", "os": "linux"},
        "image_changed",
    )


def _admission(client, runtime, composition, config):
    if config["deployment_mode"] == "intranet-http":
        _, _, rules = intranet_settings(composition)
        raw = client.call(
            ["exec", runtime["web-nginx"]["container_id"], "cat", ACCESS_TARGET],
            timeout=5,
        )
        _require(raw == rules.encode("ascii"), "admission_changed")


def inspect_frontend_image(client, reference, manifest, revision, role):
    """Inspect an exact platform image; a tag is only its locator."""
    image = client.inspect(reference, platform="linux/amd64")
    _descriptor(image.get("Descriptor"), manifest)
    _require(
        image.get("Id") == manifest
        and image.get("Os") == "linux"
        and image.get("Architecture") == "amd64"
        and type(image.get("Config")) is dict,
        "image_changed",
    )
    try:
        frontend_policy.validate_image_labels(
            image["Config"].get("Labels"), revision, role
        )
    except ValueError:
        raise FrontendCheckError("frontend_check_image_changed") from None
    return image


def _runtime(client, expected, config):
    project = expected["name"]
    raw = client.call(
        [
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            "label=com.docker.compose.project=" + project,
            "--format",
            "{{.ID}}",
        ]
    )
    identifiers = raw.decode("ascii").splitlines()
    _require(
        len(identifiers) == 2
        and len(set(identifiers)) == 2
        and all(HEX64.fullmatch(v) for v in identifiers),
        "runtime_invalid",
    )
    rows = [
        strict_json(client.call(["inspect", "--format", INSPECT, identifier]))
        for identifier in identifiers
    ]
    _require(
        all(
            type(row) is dict
            and all(
                type(row.get(key)) is dict
                for key in ("Config", "State", "HostConfig", "NetworkSettings")
            )
            and type(row["Config"].get("Labels")) is dict
            and type(row["NetworkSettings"].get("Ports")) is dict
            and type(row["NetworkSettings"].get("Networks")) is dict
            for row in rows
        ),
        "runtime_invalid",
    )
    _require(
        {r["Config"]["Labels"].get("com.docker.compose.service") for r in rows}
        == {"web", "web-nginx"},
        "runtime_invalid",
    )
    result = {}
    for row in rows:
        actual, host, state = row["Config"], row["HostConfig"], row["State"]
        role = actual["Labels"]["com.docker.compose.service"]
        service = expected["services"][role]
        manifest = config[
            "web_platform_manifest" if role == "web" else "web_nginx_platform_manifest"
        ]
        locator = service["image"]
        image = inspect_frontend_image(
            client, locator, manifest, config["frontend_revision"], role
        )
        _require(
            row["Id"] in identifiers
            and type(row.get("Image")) is str
            and IMAGE.fullmatch(row["Image"]),
            "image_changed",
        )
        _descriptor(row.get("ImageManifestDescriptor"), manifest)
        wanted_env = _environment(image["Config"].get("Env") or [])
        wanted_env.update(
            {k: str(v) for k, v in service.get("environment", {}).items()}
        )
        _require(
            actual["Labels"].get("com.docker.compose.project") == project
            and actual["Labels"].get("org.opencontainers.image.revision")
            == config["frontend_revision"]
            and actual["User"] == service["user"]
            and re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", actual["User"])
            and actual["Cmd"] == image["Config"].get("Cmd")
            and actual.get("Entrypoint") == image["Config"].get("Entrypoint")
            and actual.get("WorkingDir") == image["Config"].get("WorkingDir")
            and _environment(actual["Env"]) == wanted_env,
            "runtime_invalid",
        )
        _require(
            state["Running"] is True
            and not any(state.get(k) for k in ("Paused", "Restarting", "Dead")),
            "runtime_unhealthy",
        )
        _require(
            state.get("Health", {}).get("Status") == "healthy", "runtime_unhealthy"
        )
        if role == "web":
            _require(
                actual["WorkingDir"] == "/app"
                and wanted_env.get("SOURCE_REVISION") == config["frontend_revision"],
                "runtime_invalid",
            )
        _require(
            host["ReadonlyRootfs"] is True
            and host["Privileged"] is False
            and {v.removeprefix("CAP_") for v in host.get("CapDrop") or []} == {"ALL"}
            and not host.get("CapAdd")
            and set(host.get("SecurityOpt") or []) == {"no-new-privileges:true"}
            and host.get("PidMode") in (None, "")
            and host.get("IpcMode") != "host"
            and host.get("Runtime") == "runc"
            and not host.get("Devices")
            and not host.get("DeviceRequests")
            and host["Memory"] == _resource_integer(service["mem_limit"])
            and host["MemorySwap"] == _resource_integer(service["memswap_limit"])
            and host["PidsLimit"] == _resource_integer(service["pids_limit"])
            and host["NanoCpus"] == int(float(service["cpus"]) * 1_000_000_000),
            "runtime_invalid",
        )
        wanted_mounts = sorted(
            (m["source"], m["target"], False) for m in service.get("volumes", [])
        )
        actual_mounts = sorted(
            (m["Source"], m["Destination"], m["RW"])
            for m in row["Mounts"]
            if m["Type"] == "bind"
        )
        _require(
            actual_mounts == wanted_mounts
            and all(m["Type"] in ("bind", "tmpfs") for m in row["Mounts"]),
            "runtime_invalid",
        )
        _require(
            host.get("Tmpfs") == {"/tmp": service["tmpfs"][0].split(":", 1)[1]},
            "runtime_invalid",
        )
        ports = {}
        if role == "web-nginx":
            binding = service["ports"][0]
            ports = {
                "8080/tcp": [
                    {
                        "HostIp": binding["host_ip"],
                        "HostPort": str(binding["published"]),
                    }
                ]
            }

        _require((host.get("PortBindings") or {}) == ports, "ports_exposed")
        observed_ports = {k: v for k, v in row["NetworkSettings"]["Ports"].items() if v}
        _require(observed_ports == ports, "ports_exposed")
        networks = {expected["networks"][name]["name"] for name in service["networks"]}
        _require(
            set(row["NetworkSettings"]["Networks"]) == networks
            and host["NetworkMode"] in networks,
            "network_changed",
        )
        result[role] = {
            "container_id": row["Id"],
            "container_image_index": row["Image"],
            "platform_manifest": manifest,
        }
    for name, value in expected["networks"].items():
        network = strict_json(
            client.call(["network", "inspect", "--format", "{{json .}}", value["name"]])
        )
        _require(
            network["Name"] == value["name"]
            and network["Driver"] == "bridge"
            and network["Internal"] is (name == "website")
            and network["Labels"].get("com.docker.compose.project") == project
            and network["Labels"].get("com.docker.compose.network") == name,
            "network_changed",
        )
        wanted = {
            result[role]["container_id"]
            for role in result
            if name in expected["services"][role]["networks"]
        }
        _require(set(network["Containers"]) == wanted, "network_changed")
        for row in rows:
            attachment = row["NetworkSettings"]["Networks"].get(value["name"])
            if attachment:
                _require(attachment["NetworkID"] == network["Id"], "network_changed")
    return result


class _Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        path = (
            attrs.get("src")
            if tag == "script"
            else attrs.get("href")
            if tag == "link" and attrs.get("rel") == "stylesheet"
            else None
        )
        if path is not None:
            _require(
                re.fullmatch(r"/assets/[A-Za-z0-9_-]+\.(?:js|css)", path),
                "website_invalid",
            )
            self.paths.add(path)


class _Endpoint(AbortableConnection, http.client.HTTPConnection):
    def __init__(self, host, port):
        http.client.HTTPConnection.__init__(self, host, port, timeout=3)
        self._init_abortable()

    def connect(self):
        self._publish_channel(
            socket.create_connection((self.host, self.port), timeout=3)
        )


def _homepage(composition):
    origin = urlsplit(composition["services"]["web"]["environment"]["WEB_ORIGIN"])
    binding = composition["services"]["web-nginx"]["ports"][0]

    def get(path, maximum):
        connection = _Endpoint(binding["host_ip"], int(binding["published"]))
        watchdog = threading.Timer(4, connection.abort)
        watchdog.daemon = True
        watchdog.start()
        response = None
        try:
            connection.request(
                "GET",
                path,
                headers={"Host": origin.netloc, "Connection": "close"},
            )
            response = connection.getresponse()
            raw = response.read(maximum + 1)
            _require(
                response.status == 200 and 0 < len(raw) <= maximum, "website_invalid"
            )
            return raw
        finally:
            if response is not None:
                response.close()
            watchdog.cancel()
            connection.finish()

    parser = _Assets()
    parser.feed(get("/", 128 * 1024).decode("utf-8"))
    _require(
        1 <= len(parser.paths) <= 8 and any(p.endswith(".js") for p in parser.paths),
        "website_invalid",
    )
    for path in sorted(parser.paths):
        get(path, 1024 * 1024)
    return len(parser.paths)


PROBE = r"""
import http.client, json, os, ssl, sys, threading
from pathlib import Path
from search.secrets import read_secret
from search.serialization import strict_json
from search.web.origin import validate_origin
def abort():
    os.write(1, b'{"error":"frontend_check_upstream_failed"}\n')
    os._exit(1)
watchdog = threading.Timer(10, abort)
watchdog.daemon = True
watchdog.start()
try:
    origin = os.environ["WEB_UPSTREAM"]
    url = validate_origin(origin)
    if url.scheme != "https":
        raise ValueError()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    defaults = ssl.get_default_verify_paths()
    context.load_verify_locations(cafile=defaults.openssl_cafile, capath=defaults.openssl_capath)
    ca = os.environ.get("WEB_UPSTREAM_CA_FILE")
    if ca:
        if not Path(ca).is_absolute():
            raise ValueError()
        context.load_verify_locations(cafile=ca)
    def request(token=None):
        connection = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=3, context=context)
        try:
            headers = {"Connection": "close"}
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            connection.request("GET", "/v1/version", headers=headers)
            response = connection.getresponse()
            raw = response.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            return response.status, raw
        finally:
            connection.close()
    if request()[0] != 401:
        raise ValueError()
    status, raw = request(read_secret(Path(KEY_FILE)))
    value = strict_json(raw)
    if (status != 200 or type(value) is not dict or value.get("revision") != sys.argv[1]
        or value.get("catalog_manifest_sha256") != sys.argv[2]
        or type(value.get("conversation_contract_version")) is not int
        or value["conversation_contract_version"] != EXPECTED_CONTRACT_VERSION):
        raise ValueError()
    print('{"authenticated_version_status":200,"backend_identity_verified":true,"unauthenticated_version_status":401,"upstream_tls_verified":true}')
    watchdog.cancel()
except Exception:
    print('{"error":"frontend_check_upstream_failed"}')
    sys.exit(1)
"""


def _probe_source(
    *, key_file="/run/secrets/api_key", contract_version=CONVERSATION_CONTRACT_VERSION
):
    _require(
        type(contract_version) is int
        and contract_version == CONVERSATION_CONTRACT_VERSION,
        "config_invalid",
    )
    return (
        "KEY_FILE="
        + repr(key_file)
        + "\nEXPECTED_CONTRACT_VERSION="
        + repr(contract_version)
        + "\n"
        + PROBE
    )


def check(config_path, *, runner=None):
    config = load_config(config_path)
    with tempfile.TemporaryDirectory(prefix="depositar-frontend-check-") as temporary:
        client = Docker(
            temporary, runner, deadline=time.monotonic() + 90, max_commands=24
        )
        client.environment["SOURCE_REVISION"] = config["frontend_revision"]
        composition = _compose(client, config)
        runtime = _runtime(client, composition, config)
        _admission(client, runtime, composition, config)
        assets = _homepage(composition)
        raw = client.call(
            [
                "exec",
                runtime["web"]["container_id"],
                "python",
                "-E",
                "-s",
                "-B",
                "-c",
                _probe_source(contract_version=config["conversation_contract_version"]),
                config["api_revision"],
                config["catalog_manifest_sha256"],
            ],
            timeout=15,
        )
        proof = strict_json(raw)
        _require(
            proof
            == {
                "authenticated_version_status": 200,
                "backend_identity_verified": True,
                "unauthenticated_version_status": 401,
                "upstream_tls_verified": True,
            },
            "upstream_failed",
        )
        _require(_runtime(client, composition, config) == runtime, "runtime_changed")
        _policy(composition, config)
        _admission(client, runtime, composition, config)
    return {
        "schema_version": config["schema_version"],
        "deployment_mode": config["deployment_mode"],
        "project": composition["name"],
        "origin": composition["services"]["web"]["environment"]["WEB_ORIGIN"],
        "conversation_contract_version": config["conversation_contract_version"],
        "status": "frontend_checked",
        "frontend_revision": config["frontend_revision"],
        "api_revision": config["api_revision"],
        "catalog_manifest_sha256": config["catalog_manifest_sha256"],
        "runtime": runtime,
        "homepage_verified": True,
        "assets_verified": assets,
        **proof,
        "deployment_performed": False,
        "public_readiness_verified": False,
        "model_inference_verified": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        result = check(args.config)
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        UnicodeError,
        http.client.HTTPException,
    ):
        print('{"error":"frontend_check_failed"}')
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0
