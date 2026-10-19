"""Two stateless TLS proxies bound to an existing, unchanged private API.

The inner process shares the exact API container network namespace. The host
listener verifies its TLS identity before forwarding service credentials. Neither
proxy mounts an API key, Docker socket, or CA signing key.
"""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess

from search.serialization import strict_json
from search.operations.deployment import composition, image_build, source_bundle
from search.operations.deployment.public_entry import preflight, profile
from search.operations.shared import private_io

NGINX_IMAGE = source_bundle.NGINX_BASE
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
TLS_FIELDS = (
    "ca_file",
    "outer_certificate_file",
    "outer_key_file",
    "inner_certificate_file",
    "inner_key_file",
)
CONFIG_FIELDS = frozenset(TLS_FIELDS) | {
    "schema_version",
    "release_compose",
    "api_container_id",
    "nginx_image",
    "bind_address",
    "listen_port",
    "allowed_source",
    "outer_server_name",
    "inner_server_name",
}
DETAIL_FORMAT = (
    '{"Id":{{json .Id}},"Pid":{{json .State.Pid}},"StartedAt":{{json .State.StartedAt}},'
    '"SandboxID":{{json .NetworkSettings.SandboxID}},"Labels":{{json .Config.Labels}},'
    '"Networks":{{json .NetworkSettings.Networks}},"Mounts":{{json .Mounts}}}'
)


class EndpointError(ValueError):
    """Fixed diagnostics only, never subprocess output or private material."""


def _require(value, code="invalid"):
    if not value:
        raise EndpointError("private_api_tls_" + code)


def _path(value):
    return profile._path(value, profile.ROOT)


def _read(path):
    return profile._read_private(Path(path), uid=os.geteuid(), maximum=256 * 1024)


def load_config(path):
    value = strict_json(_read(_path(str(path))))
    return validate_config(value)


def _server_name(value):
    _require(type(value) is str and 1 <= len(value) <= 253)
    labels = value.split(".")
    _require(len(labels) >= 2 and all(DNS_LABEL.fullmatch(label) for label in labels))
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise EndpointError("private_api_tls_invalid")


def validate_config(value):
    _require(type(value) is dict and set(value) == CONFIG_FIELDS)
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    _require(
        type(value["api_container_id"]) is str
        and preflight.IDENTIFIER.fullmatch(value["api_container_id"])
    )
    image = value["nginx_image"]
    _require(
        type(image) is str
        and re.fullmatch(
            r"(?:sha256:|[a-z0-9][a-z0-9./_:-]*@sha256:)[0-9a-f]{64}", image
        )
    )
    _require(type(value["listen_port"]) is int and 1 <= value["listen_port"] <= 65535)
    _require(
        type(value["bind_address"]) is str and type(value["allowed_source"]) is str
    )
    address = ipaddress.IPv4Address(value["bind_address"])
    source = ipaddress.IPv4Network(value["allowed_source"], strict=True)
    _require(
        not address.is_multicast
        and not address.is_unspecified
        and source.prefixlen == 32
    )
    for name in ("outer_server_name", "inner_server_name"):
        _server_name(value[name])
    _require(value["outer_server_name"] != value["inner_server_name"])
    for name in (*TLS_FIELDS, "release_compose"):
        _path(value[name])
    _require(value["outer_key_file"] != value["inner_key_file"])
    return value


def _output(argv, runner=subprocess.run):
    return preflight._output(runner, argv)


def _compose_output(argv, runner):
    if runner is subprocess.run:
        result = image_build._run(argv, timeout=60, environment=dict(os.environ))
    else:
        result = runner(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=True,
        )
    _require(result.returncode == 0, "operation_failed")
    return result.stdout


def _one(argv, runner):
    rows = preflight._objects(_output(argv, runner))
    _require(len(rows) == 1, "inspection_failed")
    return rows[0]


def _backend(config, runner):
    release = strict_json(_read(config["release_compose"]))
    network_name = release["networks"]["website"]["name"]
    key_mount = next(
        m
        for m in release["services"]["conversation-api"]["volumes"]
        if m["target"] == "/run/secrets/api_key"
    )
    bound = {
        "services": {"public-web": {"volumes": [key_mount]}},
        "networks": {"existing-release": {"external": True, "name": network_name}},
    }
    preflight.verify_private_backend(bound, release, runner=runner)
    network = _one(
        [
            "docker",
            "network",
            "inspect",
            "--format",
            preflight.NETWORK_FORMAT,
            network_name,
        ],
        runner,
    )
    members = network["Containers"]
    _require(len(members) == 4, "peers_changed")
    rows = preflight._objects(
        _output(
            ["docker", "inspect", "--format", DETAIL_FORMAT, *sorted(members)], runner
        )
    )
    _require(
        len(rows) == 4 and {r["Id"] for r in rows} == set(members), "peers_changed"
    )
    expected = {
        (release["name"], role)
        for role in ("web", "conversation-api", "filtered-embedding")
    } | {(composition.GATEWAY_PROJECT, "web-nginx")}
    actual = {
        (
            r["Labels"].get("com.docker.compose.project"),
            r["Labels"].get("com.docker.compose.service"),
        )
        for r in rows
    }
    _require(actual == expected, "peers_changed")
    api = next(r for r in rows if r["Id"] == config["api_container_id"])
    _require(
        api["Labels"].get("com.docker.compose.service") == "conversation-api",
        "backend_changed",
    )
    _require(
        type(api["Pid"]) is int
        and api["Pid"] > 0
        and api["StartedAt"]
        and preflight.IDENTIFIER.fullmatch(api["SandboxID"]),
        "backend_changed",
    )
    ip = str(ipaddress.IPv4Address(api["Networks"][network_name]["IPAddress"]))
    gateway = next(
        r for r in rows if r["Labels"].get("com.docker.compose.service") == "web-nginx"
    )
    route_mount = next(
        m
        for m in gateway["Mounts"]
        if m["Destination"] == "/etc/nginx/routes"
        and m["Type"] == "bind"
        and m["RW"] is False
    )
    route = str(_path(str(Path(route_mount["Source"]) / "active.conf")))
    return {
        "api_container_id": api["Id"],
        "api_image": release["services"]["conversation-api"]["image"],
        "api_pid": api["Pid"],
        "api_started_at": api["StartedAt"],
        "api_sandbox_id": api["SandboxID"],
        "website_network": network_name,
        "website_network_id": network["Id"],
        "api_address": ip,
        "peer_ids": sorted(members),
        "active_route": route,
        "active_route_sha256": hashlib.sha256(_read(route)).hexdigest(),
    }


def _materials(config, runner):
    digests = {}
    for name in TLS_FIELDS:
        digests[name] = hashlib.sha256(_read(config[name])).hexdigest()
    for prefix in ("outer", "inner"):
        hostname = config[prefix + "_server_name"]
        cert, key = config[prefix + "_certificate_file"], config[prefix + "_key_file"]
        _output(
            [
                "openssl",
                "verify",
                "-CAfile",
                config["ca_file"],
                "-purpose",
                "sslserver",
                "-verify_hostname",
                hostname,
                cert,
            ],
            runner,
        )
        san = _output(
            ["openssl", "x509", "-in", cert, "-noout", "-ext", "subjectAltName"],
            runner,
        ).decode("ascii")
        _require(
            "DNS:" + hostname in {name.strip() for name in re.split(r"[,\n]", san)},
            "certificate_invalid",
        )
        _output(
            ["openssl", "x509", "-in", cert, "-noout", "-checkend", "86400"], runner
        )
        cert_public = _output(
            ["openssl", "x509", "-in", cert, "-pubkey", "-noout"], runner
        )
        key_public = _output(
            ["openssl", "pkey", "-in", key, "-passin", "pass:", "-pubout"], runner
        )
        _require(cert_public == key_public, "certificate_invalid")
    return hashlib.sha256(private_io.canonical(digests)).hexdigest()


def _nginx(config, backend, *, inner):
    port = 8443 if inner else config["listen_port"]
    address = backend["api_address"] if inner else config["bind_address"]
    prefix = "inner" if inner else "outer"
    hostname = config[prefix + "_server_name"]
    inner_hostname = config["inner_server_name"]
    acl = "" if inner else f"allow {config['allowed_source']}; deny all;"
    upstream = (
        "http://127.0.0.1:8000" if inner else f"https://{backend['api_address']}:8443"
    )
    tls = (
        ""
        if inner
        else f"proxy_ssl_server_name on; proxy_ssl_name {inner_hostname}; proxy_ssl_verify on; proxy_ssl_verify_depth 2; proxy_ssl_trusted_certificate /run/tls/ca.pem; proxy_ssl_session_reuse off;"
    )
    locations = []
    for path, method in (("/v1/turn", "POST"), ("/v1/version", "GET")):
        locations.append(f'''location = {path} {{
            if ($request_uri != "{path}") {{ return 400; }}
            if ($request_method != {method}) {{ return 405; }}
            proxy_pass {upstream}; {tls}
            proxy_set_header Host {inner_hostname if not inner else "conversation-api"};
            proxy_set_header Connection close;
            proxy_http_version 1.1;
            proxy_next_upstream off;
            proxy_connect_timeout 3s; proxy_read_timeout 125s; proxy_send_timeout 5s;
            proxy_buffering off;
        }}''')
    user = "" if inner else "user nginx;"
    return f"""{user}
worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr crit;
events {{ worker_connections 128; }}
http {{
    access_log off; server_tokens off;
    client_body_temp_path /tmp/client; proxy_temp_path /tmp/proxy;
    fastcgi_temp_path /tmp/fastcgi; uwsgi_temp_path /tmp/uwsgi; scgi_temp_path /tmp/scgi;
    client_max_body_size 8192;
    client_header_timeout 5s; client_body_timeout 5s; send_timeout 5s;
    keepalive_timeout 5s; large_client_header_buffers 2 8k;
    server {{
        listen {address}:{port} ssl;
        server_name {hostname};
        ssl_certificate /run/tls/{prefix}.crt;
        ssl_certificate_key /run/tls/{prefix}.key;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_tickets off;
        {acl}
        if ($host != {hostname}) {{ return 421; }}
        {"".join(locations)}
        location / {{ return 404; }}
    }}
}}
"""


def render(config, backend, output, *, project="depositar-private-api-tls"):
    _require(re.fullmatch(r"depositar-private-api-tls(?:-[a-z0-9-]{1,40})?", project))
    uid, gid = os.geteuid(), os.getegid()
    services, assets = {}, {}
    for role in ("inner", "outer"):
        assets[role + ".conf"] = _nginx(config, backend, inner=role == "inner")
        volumes = [
            composition._mount(output / (role + ".conf"), "/run/endpoint.conf"),
            composition._mount(
                Path(config[role + "_certificate_file"]), "/run/tls/" + role + ".crt"
            ),
            composition._mount(
                Path(config[role + "_key_file"]), "/run/tls/" + role + ".key"
            ),
        ]
        if role == "outer":
            volumes.append(
                composition._mount(Path(config["ca_file"]), "/run/tls/ca.pem")
            )
        service = {
            **composition._runtime(uid, gid, cpus=0.5, memory=64 * 1024**2, pids=32),
            "image": config["nginx_image"],
            "entrypoint": ["nginx"],
            "command": ["-c", "/run/endpoint.conf", "-g", "daemon off;"],
            "environment": {"NVIDIA_VISIBLE_DEVICES": "void"},
            "network_mode": "host"
            if role == "outer"
            else "container:" + backend["api_container_id"],
            "volumes": volumes,
            "labels": {
                "io.depositar.private-api-tls.binding": hashlib.sha256(
                    private_io.canonical(backend)
                ).hexdigest()
            },
            "healthcheck": {
                "test": ["CMD", "nginx", "-t", "-q", "-c", "/run/endpoint.conf"],
                "interval": "2s",
                "timeout": "3s",
                "retries": 10,
            },
        }
        if role == "outer":
            # Docker does not retain effective capabilities through non-root
            # exec. Keep the standard nginx master and drop its workers.
            service["user"] = "0:0"
            service["cap_add"] = ["SETUID", "SETGID", "DAC_OVERRIDE", "CHOWN"]
            if config["listen_port"] < 1024:
                service["cap_add"].append("NET_BIND_SERVICE")
        services[role] = service
    return {"name": project, "services": services}, assets


def prepare(
    config_path, output, *, runner=subprocess.run, project="depositar-private-api-tls"
):
    config = load_config(config_path)
    output = _path(str(output))
    _require(not os.path.lexists(output), "output_exists")
    material = _materials(config, runner)
    backend = _backend(config, runner)
    _output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", config["nginx_image"]],
        runner,
    )
    compose, assets = render(config, backend, output, project=project)
    assets["compose.json"] = private_io.canonical(compose).decode()
    plan = {
        "schema_version": 1,
        "config": config,
        "backend": backend,
        "material_sha256": material,
        "files": {
            name: hashlib.sha256(raw.encode()).hexdigest()
            for name, raw in assets.items()
        },
    }
    descriptor = private_io.open_directory(output, create=True)
    try:
        for name, raw in assets.items():
            private_io.write_file(descriptor, name, raw.encode(), max_bytes=256 * 1024)
        private_io.write_json(descriptor, "plan.json", plan, max_bytes=256 * 1024)
    finally:
        os.close(descriptor)
    return {
        "status": "private_api_tls_prepared",
        "plan": str(output / "plan.json"),
        "deployment_performed": False,
    }


def _read_plan(path):
    plan = strict_json(_read(path))
    _require(
        type(plan) is dict
        and set(plan)
        == {"schema_version", "config", "backend", "material_sha256", "files"}
        and type(plan["schema_version"]) is int
        and plan["schema_version"] == 1
        and type(plan["files"]) is dict
        and set(plan["files"]) == {"inner.conf", "outer.conf", "compose.json"}
    )
    validate_config(plan["config"])
    _require(plan["backend"]["api_container_id"] == plan["config"]["api_container_id"])
    _require(type(plan["backend"]["api_address"]) is str)
    _require(
        str(ipaddress.IPv4Address(plan["backend"]["api_address"]))
        == plan["backend"]["api_address"]
    )
    _require(
        all(
            type(v) is str and preflight.IDENTIFIER.fullmatch(v)
            for v in (*plan["files"].values(), plan["material_sha256"])
        )
    )
    return plan


def check(plan_path, *, runner=subprocess.run):
    plan_path = _path(str(plan_path))
    plan = _read_plan(plan_path)
    for name, digest in plan["files"].items():
        _require(
            hashlib.sha256(_read(plan_path.parent / name)).hexdigest() == digest,
            "material_changed",
        )
    config = validate_config(plan["config"])
    _require(_materials(config, runner) == plan["material_sha256"], "material_changed")
    _require(_backend(config, runner) == plan["backend"], "backend_changed")
    compose, assets = render(
        config,
        plan["backend"],
        plan_path.parent,
        project=strict_json(_read(plan_path.parent / "compose.json"))["name"],
    )
    _require(
        private_io.canonical(compose) == _read(plan_path.parent / "compose.json"),
        "material_changed",
    )
    _require(
        all(assets[name].encode() == _read(plan_path.parent / name) for name in assets),
        "material_changed",
    )
    return plan


def _owned(path, plan, runner, *, healthy=False):
    compose = strict_json(_read(path.parent / "compose.json"))
    expected, assets = render(
        validate_config(plan["config"]),
        plan["backend"],
        path.parent,
        project=compose["name"],
    )
    _require(compose == expected, "material_changed")
    _require(
        all(
            hashlib.sha256(_read(path.parent / name)).hexdigest() == digest
            for name, digest in plan["files"].items()
        ),
        "material_changed",
    )
    raw = _output(
        [
            "docker",
            "compose",
            "-f",
            str(path.parent / "compose.json"),
            "ps",
            "--all",
            "--no-trunc",
            "--format",
            "json",
        ],
        runner,
    )
    rows = (
        strict_json(raw)
        if raw.strip().startswith(b"[")
        else [strict_json(line) for line in raw.splitlines() if line.strip()]
    )
    _require(
        type(rows) is list
        and len(rows) <= 2
        and len({r["Service"] for r in rows}) == len(rows),
        "runtime_changed",
    )
    _require(
        all(
            r["Service"] in ("inner", "outer")
            and preflight.IDENTIFIER.fullmatch(r["ID"])
            for r in rows
        ),
        "runtime_changed",
    )
    if healthy:
        _require({r["Service"] for r in rows} == {"inner", "outer"}, "runtime_changed")
    image_id = (
        _output(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                plan["config"]["nginx_image"],
            ],
            runner,
        )
        .decode()
        .strip()
    )
    result = []
    fmt = (
        '{"Id":{{json .Id}},"Image":{{json .Image}},"Labels":{{json .Config.Labels}},"Mounts":{{json .Mounts}},'
        '"NetworkMode":{{json .HostConfig.NetworkMode}},"Running":{{json .State.Running}},'
        '"Command":{{json .Config.Cmd}},"Entrypoint":{{json .Config.Entrypoint}},"User":{{json .Config.User}},'
        '"ReadonlyRootfs":{{json .HostConfig.ReadonlyRootfs}},"Privileged":{{json .HostConfig.Privileged}},'
        '"CapAdd":{{json .HostConfig.CapAdd}},"CapDrop":{{json .HostConfig.CapDrop}},'
        '"SecurityOpt":{{json .HostConfig.SecurityOpt}},"PortBindings":{{json .HostConfig.PortBindings}},'
        '"Runtime":{{json .HostConfig.Runtime}},"PidMode":{{json .HostConfig.PidMode}},'
        '"Healthy":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}}}'
    )
    for row in rows:
        observed = _one(["docker", "inspect", "--format", fmt, row["ID"]], runner)
        service = compose["services"][row["Service"]]
        labels = observed["Labels"]
        mounts = sorted(
            (m["Source"], m["Destination"], m["RW"])
            for m in observed["Mounts"]
            if m["Type"] == "bind"
        )
        wanted = sorted(
            (m["source"], m["target"], not m["read_only"]) for m in service["volumes"]
        )
        _require(
            observed["Id"] == row["ID"]
            and observed["Image"] == image_id
            and observed["NetworkMode"] == service["network_mode"]
            and mounts == wanted
            and labels.get("com.docker.compose.project") == compose["name"]
            and labels.get("com.docker.compose.service") == row["Service"]
            and labels.get("com.docker.compose.project.config_files")
            == str(path.parent / "compose.json")
            and labels.get("io.depositar.private-api-tls.binding")
            == service["labels"]["io.depositar.private-api-tls.binding"],
            "runtime_changed",
        )
        _require(
            observed.get("Command") == service["command"]
            and observed.get("Entrypoint") == service["entrypoint"]
            and observed.get("User") == service["user"]
            and observed.get("ReadonlyRootfs") is True
            and observed.get("Privileged") is False
            and observed.get("Runtime") == "runc"
            and observed.get("PidMode") in (None, "")
            and set(observed.get("SecurityOpt") or []) == set(service["security_opt"])
            and {c.removeprefix("CAP_") for c in (observed.get("CapDrop") or [])}
            == {"ALL"}
            and {c.removeprefix("CAP_") for c in (observed.get("CapAdd") or [])}
            == set(service.get("cap_add", []))
            and observed.get("PortBindings") in (None, {}),
            "runtime_changed",
        )
        if healthy:
            _require(
                observed["Running"] is True and observed["Healthy"] == "healthy",
                "runtime_changed",
            )
        result.append(row["ID"])
    return result


def _stop_owned(path, plan, runner):
    identifiers = _owned(path, plan, runner)
    if identifiers:
        _compose_output(["docker", "stop", "--time", "5", *identifiers], runner)
        _compose_output(["docker", "rm", *identifiers], runner)


def operate(action, plan_path, *, runner=subprocess.run):
    _require(action in ("check", "up", "stop"))
    path = _path(str(plan_path))
    # Stop requires intact, authenticated assets, but not a healthy backend or
    # unexpired certificate: it must still work when recovery is necessary.
    plan = check(path, runner=runner) if action != "stop" else _read_plan(path)
    validate_config(plan["config"])
    compose_path = path.parent / "compose.json"
    _require(
        hashlib.sha256(_read(compose_path)).hexdigest()
        == plan["files"]["compose.json"],
        "material_changed",
    )
    command = ["docker", "compose", "-f", str(compose_path)]
    _owned(path, plan, runner)
    if action == "up":
        try:
            _compose_output(
                command
                + [
                    "up",
                    "--detach",
                    "--no-build",
                    "--pull",
                    "never",
                    "--wait",
                    "--wait-timeout",
                    "30",
                    "inner",
                    "outer",
                ],
                runner,
            )
            _owned(path, plan, runner, healthy=True)
            check(path, runner=runner)
        except Exception:
            _stop_owned(path, plan, runner)
            raise
    elif action == "stop":
        _stop_owned(path, plan, runner)
    return {
        "status": "private_api_tls_"
        + {"up": "started", "stop": "stopped", "check": "checked"}[action],
        "deployment_performed": action == "up",
        "public_readiness_verified": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "check", "up", "stop"))
    parser.add_argument("--config")
    parser.add_argument("--output")
    parser.add_argument("--plan")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            _require(args.config is not None and args.output is not None)
            value = prepare(args.config, Path(args.output))
        else:
            _require(args.plan is not None)
            value = operate(args.action, args.plan)
        print(json.dumps(value, sort_keys=True))
        return 0
    except (
        ValueError,
        OSError,
        KeyError,
        StopIteration,
        TypeError,
        subprocess.SubprocessError,
    ):
        print(json.dumps({"error": "private_api_tls_failed"}))
        return 1
