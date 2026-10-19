"""Render three private planes, without reading files or touching Docker.

This is not an installer or a runtime proof. Built-image source tags are only
plans: the host must build committed bytes, pin immutable image IDs, provision
query identities, verify actual GPU ownership, and seal catalog client leases.
The release has no ports and never starts another copy of the GPU services.
The separate singleton gateway publishes only 127.0.0.1:18083.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from search.operations.config import GPU_UUID, GPUConfig, ProductConfig


OLLAMA_IMAGE = (
    "ollama/ollama:0.32.14@sha256:"
    "9d30908e41144b1f1da89b9d8e33c07e4aeb43ff41a8660241b1686e2cc330ad"
)
LOOPBACK_PORT = 18083
GATEWAY_PROJECT = "depositar-ai-gateway"
REVISION = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "project",
        "work_root",
        "data_root",
        "tooling_root",
        "compose_file",
        "bundle_path",
        "manifest_sha256",
        "revision",
        "document_count",
        "status",
        "source_sha256",
        "compose_sha256",
        "security_material_sha256",
    }
)


@dataclass(frozen=True)
class QueryBindingPaths:
    """Closed input paths; their contents are never read by the renderer."""

    api_key: Path
    index_password: Path
    binding: Path
    root_ca: Path

    @classmethod
    def for_catalog(cls, config, plan):
        """Construct canonical paths; render_composition validates the plan."""
        query_root = Path(plan["work_root"]) / "query-access"
        return cls(
            config.state_root / "secrets/api_key",
            query_root / "index-query-password",
            query_root / "binding.json",
            Path(plan["data_root"]) / "opensearch/config/root-ca.pem",
        )


def _path(value):
    if (
        not isinstance(value, Path)
        or not value.is_absolute()
        or value == Path("/")
        or ".." in value.parts
        or any(character in str(value) for character in "\x00\r\n,")
    ):
        raise ValueError("composition_path_invalid")
    return value


def _configuration(config):
    if (
        not isinstance(config, ProductConfig)
        or type(config.schema_version) is not int
        or config.schema_version not in (1, 2)
        or type(config.catalog_storage) is not str
        or config.catalog_storage
        != ("docker-volume" if config.schema_version == 2 else "bind")
    ):
        raise ValueError("composition_config_invalid")
    roots = (
        config.state_root,
        config.api_model_root,
        config.embedding_model_root,
        config.reranker_model_root,
    )
    for index, root in enumerate(roots):
        _path(root)
        if any(
            root == other or root in other.parents or other in root.parents
            for other in roots[:index]
        ):
            raise ValueError("composition_roots_overlap")
    if (
        not isinstance(config.gpus, GPUConfig)
        or not isinstance(config.gpus.planner, tuple)
        or len(config.gpus.planner) != 2
    ):
        raise ValueError("composition_gpu_allocation_invalid")
    identifiers = config.gpus.all_uuids()
    if (
        any(
            type(identifier) is not str or GPU_UUID.fullmatch(identifier) is None
            for identifier in identifiers
        )
        or len({identifier.lower() for identifier in identifiers}) != 4
    ):
        raise ValueError("composition_gpu_allocation_invalid")


def _catalog(config, plan, source_revision):
    if (
        type(source_revision) is not str
        or REVISION.fullmatch(source_revision) is None
        or type(plan) is not dict
        or set(plan) != PLAN_FIELDS
        or type(plan["schema_version"]) is not int
        or plan["schema_version"] != 1
        or plan["status"] != "prepared"
        or type(plan["revision"]) is not str
        or REVISION.fullmatch(plan["revision"]) is None
        or type(plan["document_count"]) is not int
        or not 1 <= plan["document_count"] <= 10000
    ):
        raise ValueError("composition_catalog_identity_invalid")
    for name in (
        "manifest_sha256",
        "compose_sha256",
        "security_material_sha256",
    ):
        if type(plan[name]) is not str or DIGEST.fullmatch(plan[name]) is None:
            raise ValueError("composition_catalog_digest_invalid")
    source_hashes = plan["source_sha256"]
    if (
        type(source_hashes) is not dict
        or not source_hashes
        or any(
            type(name) is not str
            or not name
            or Path(name).is_absolute()
            or ".." in Path(name).parts
            or any(character in name for character in "\x00\r\n,")
            or type(digest) is not str
            or DIGEST.fullmatch(digest) is None
            for name, digest in source_hashes.items()
        )
    ):
        raise ValueError("composition_catalog_source_invalid")
    paths = {}
    for name in (
        "work_root",
        "data_root",
        "tooling_root",
        "compose_file",
        "bundle_path",
    ):
        if type(plan[name]) is not str:
            raise ValueError("composition_catalog_path_invalid")
        paths[name] = _path(Path(plan[name]))
        if str(paths[name]) != plan[name]:
            raise ValueError("composition_catalog_path_invalid")
    bundle_name = paths["bundle_path"].name
    if (
        not bundle_name.startswith("sha256-")
        or DIGEST.fullmatch(bundle_name[7:]) is None
    ):
        raise ValueError("composition_catalog_bundle_invalid")
    bundle_id = bundle_name[7:]
    work_name = bundle_id[:12] + "-" + plan["revision"][:12]
    work_root = config.state_root / "catalogs" / work_name
    if (
        plan["project"] != "depositar-ai-catalog-" + work_name
        or paths["work_root"] != work_root
        or paths["data_root"] != work_root / "data"
        or paths["tooling_root"] != work_root / "tooling"
        or paths["compose_file"] != work_root / "compose.json"
        or paths["bundle_path"]
        != config.state_root / "cache/bundles" / ("sha256-" + bundle_id)
    ):
        raise ValueError("composition_catalog_path_binding_invalid")
    return bundle_id


def _runtime(uid, gid, *, cpus, memory, pids, gpu=False):
    return {
        "init": True,
        "restart": "unless-stopped",
        "runtime": "nvidia" if gpu else "runc",
        "user": f"{uid}:{gid}",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "cpus": cpus,
        "mem_limit": memory,
        "memswap_limit": memory,
        "pids_limit": pids,
        "logging": {
            "driver": "json-file",
            "options": {"max-size": "5m", "max-file": "3"},
        },
        "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777"],
    }


def _mount(source, target):
    return {
        "type": "bind",
        "source": str(source),
        "target": str(target),
        "read_only": True,
        "bind": {"create_host_path": False},
    }


def _gpu_request(identifiers):
    return {
        "resources": {
            "reservations": {
                "devices": [
                    {
                        "driver": "nvidia",
                        "device_ids": list(identifiers),
                        "capabilities": ["gpu"],
                    }
                ]
            }
        }
    }


def _health(command, *, start_period="0s"):
    return {
        "test": command,
        "interval": "20s",
        "timeout": "5s",
        "retries": 5,
        "start_period": start_period,
    }


def _http_health(port):
    return _health(
        [
            "CMD",
            "python",
            "-c",
            "import urllib.request; urllib.request.urlopen("
            f"'http://127.0.0.1:{port}/healthz', timeout=2).close()",
        ]
    )


def _plan(source_revision, **fields):
    return {
        "schema_version": 1,
        "status": "plan_only",
        "revision": source_revision,
        "runtime_verified": False,
        **fields,
    }


def _inference(config, source_revision, uid, gid):
    from search.model_policy import (
        PLANNER_MODEL,
        PLANNER_MODEL_DIGEST,
        SCOPE_MODEL,
        SCOPE_MODEL_DIGEST,
    )
    from search.reranker.contract import MODEL_ID, MODEL_REVISION
    from search.reranker.model import MODEL_HASHES

    # No metadata identity is included: a same-commit catalog update shares this
    # one plane instead of starting duplicate processes on the same UUIDs.
    identity = {
        "revision": source_revision,
        "model_root": str(config.api_model_root),
        "reranker_root": str(config.reranker_model_root),
        "gpus": config.to_dict()["gpus"],
        "uid": uid,
        "gid": gid,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    project = "depositar-ai-inference-" + digest[:12]
    services = {}
    for name, identifiers in (
        ("scope-model", (config.gpus.scope,)),
        ("answer-model", config.gpus.planner),
    ):
        services[name] = {
            **_runtime(uid, gid, cpus=8, memory=16 * 1024**3, pids=512, gpu=True),
            "image": OLLAMA_IMAGE,
            "command": ["serve"],
            "networks": ["inference"],
            "environment": {
                "HOME": "/tmp/ollama-home",
                "OLLAMA_HOST": "0.0.0.0:11434",
                "OLLAMA_MODELS": "/models",
                "OLLAMA_NO_CLOUD": "1",
                "OLLAMA_NUM_PARALLEL": "1",
                "OLLAMA_MAX_LOADED_MODELS": "1",
                "OLLAMA_MAX_QUEUE": "1",
                "OLLAMA_KEEP_ALIVE": "-1",
                "NVIDIA_VISIBLE_DEVICES": ",".join(identifiers),
                "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
            },
            "volumes": [_mount(config.api_model_root, "/models")],
            "deploy": _gpu_request(identifiers),
            "healthcheck": _health(["CMD", "ollama", "list"]),
        }
    services["reranker"] = {
        **_runtime(uid, gid, cpus=8, memory=20 * 1024**3, pids=256, gpu=True),
        "image": "depositar-search-reranker:" + source_revision,
        "networks": ["ranking"],
        "environment": {
            "NVIDIA_VISIBLE_DEVICES": config.gpus.reranker,
            "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
        },
        "volumes": [_mount(config.reranker_model_root, "/model")],
        "deploy": _gpu_request((config.gpus.reranker,)),
        "healthcheck": _health(
            [
                "CMD",
                "/opt/conda/bin/python",
                "-c",
                "import urllib.request; urllib.request.urlopen("
                "'http://127.0.0.1:8001/healthz', timeout=2).close()",
            ],
            start_period="60s",
        ),
    }
    # Torch/Python must execute their existing local temporary helper libraries.
    services["reranker"]["tmpfs"] = [
        f"/tmp:rw,nosuid,nodev,size=512m,uid={uid},gid={gid},mode=0700"
    ]
    return {
        "name": project,
        "services": services,
        "networks": {
            name: {
                "name": project + "_" + name,
                "internal": True,
                "attachable": False,
            }
            for name in ("inference", "ranking")
        },
        "x-depositar-plan": _plan(
            source_revision,
            ownership_verified=False,
            model_warmup_required=True,
            immutable_built_images_required=True,
            models={
                "scope": {"model": SCOPE_MODEL, "manifest_sha256": SCOPE_MODEL_DIGEST},
                "planner": {
                    "model": PLANNER_MODEL,
                    "manifest_sha256": PLANNER_MODEL_DIGEST,
                },
                "reranker": {
                    "model": MODEL_ID,
                    "revision": MODEL_REVISION,
                    "files": dict(MODEL_HASHES),
                },
            },
        ),
    }


def _route_text(alias):
    # The caller cannot supply an origin, URL, arbitrary server or route text.
    return f"""worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr crit;
events {{ worker_connections 128; }}
http {{
    include /etc/nginx/mime.types;
    server_tokens off;
    log_format safe '$request_id $request_method $status $request_time';
    access_log /dev/stdout safe;
    client_body_temp_path /tmp/client;
    proxy_temp_path /tmp/proxy;
    fastcgi_temp_path /tmp/fastcgi;
    uwsgi_temp_path /tmp/uwsgi;
    scgi_temp_path /tmp/scgi;
    client_max_body_size 4k;
    client_body_timeout 5s;
    client_header_timeout 5s;
    keepalive_timeout 5s;
    send_timeout 10s;
    resolver 127.0.0.11 ipv6=off valid=10s;
    upstream website {{
        zone website 64k;
        server {alias}:8000 resolve;
        keepalive 4;
    }}
    limit_req_zone $binary_remote_addr zone=web_rate:1m rate=3r/s;
    server {{
        listen 8080;
        limit_req zone=web_rate burst=12 nodelay;
        limit_req_status 429;
        location / {{
            proxy_pass http://website;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For "";
            proxy_set_header X-Forwarded-Host "";
            proxy_set_header X-Forwarded-Proto "";
            proxy_connect_timeout 5s;
            proxy_send_timeout 5s;
            proxy_read_timeout 130s;
            proxy_buffering off;
        }}
        location /v1/ {{ return 404; }}
    }}
}}
"""


def render_composition(config, catalog_plan, query_paths, source_revision, *, uid, gid):
    """Return catalog reference, shared inference, CPU release and gateway plans.

    Only validated, private paths enter mounts. Provisioning/readiness/source
    proofs, including compatibility of an older catalog, are caller
    responsibilities, not inferred from this rendering. The
    gateway route text must be safely published before starting the singleton.
    A candidate release can be started alone; it neither publishes 18083 nor
    allocates GPUs. Catalog network access still requires a sealed client lease.
    """
    if any(
        type(value) is not int or not 1 <= value <= 2**31 - 1 for value in (uid, gid)
    ):
        raise ValueError("composition_operator_invalid")
    _configuration(config)
    bundle_id = _catalog(config, catalog_plan, source_revision)
    if not isinstance(query_paths, QueryBindingPaths) or query_paths != (
        QueryBindingPaths.for_catalog(config, catalog_plan)
    ):
        raise ValueError("composition_query_paths_invalid")
    for path in vars(query_paths).values():
        _path(path)
    from search.catalog.embedding import MODEL, MODEL_DIGEST

    inference = _inference(config, source_revision, uid, gid)
    project = "depositar-ai-release-" + bundle_id[:12] + "-" + source_revision[:12]
    alias = project + "-web"
    bundle_target = "/catalog/sha256-" + bundle_id
    services = {
        "web": {
            **_runtime(uid, gid, cpus=1, memory=512 * 1024**2, pids=64),
            "image": "depositar-search-web:" + source_revision,
            "networks": {"website": {"aliases": [alias]}},
            "environment": {
                "WEB_ORIGIN": f"http://127.0.0.1:{LOOPBACK_PORT}",
                "WEB_UPSTREAM": "http://conversation-api:8000",
                "NVIDIA_VISIBLE_DEVICES": "void",
            },
            "volumes": [_mount(query_paths.api_key, "/run/secrets/api_key")],
            "healthcheck": _http_health(8000),
        },
        "conversation-api": {
            **_runtime(uid, gid, cpus=2, memory=2 * 1024**3, pids=96),
            "image": "depositar-search-conversation:" + source_revision,
            "networks": ["website", "existing-inference", "catalog", "ranking"],
            "environment": {
                "SEARCH_API_MANIFEST_SHA256": catalog_plan["manifest_sha256"],
                "SEARCH_API_BUNDLE_PATH": bundle_target,
                "SEARCH_API_RERANK_ENABLED": "1",
                "SEARCH_API_RERANK_CANDIDATES": "20",
                "NVIDIA_VISIBLE_DEVICES": "void",
            },
            "volumes": [
                _mount(catalog_plan["bundle_path"], bundle_target),
                _mount(query_paths.api_key, "/run/secrets/api_key"),
                _mount(query_paths.index_password, "/run/secrets/index_query_password"),
                _mount(query_paths.binding, "/run/index/binding.json"),
                _mount(query_paths.root_ca, "/run/index/root-ca.pem"),
            ],
            "healthcheck": _http_health(8000),
        },
        "filtered-embedding": {
            **_runtime(uid, gid, cpus=8, memory=8 * 1024**3, pids=96),
            "image": OLLAMA_IMAGE,
            "command": ["serve"],
            "networks": ["website"],
            "environment": {
                "OLLAMA_HOST": "0.0.0.0:11434",
                "OLLAMA_MODELS": "/models",
                "OLLAMA_NUM_PARALLEL": "1",
                "OLLAMA_MAX_LOADED_MODELS": "1",
                "OLLAMA_MAX_QUEUE": "1",
                "OLLAMA_NO_CLOUD": "1",
                "CUDA_VISIBLE_DEVICES": "-1",
                "NVIDIA_VISIBLE_DEVICES": "void",
            },
            "volumes": [_mount(config.embedding_model_root, "/models")],
            "tmpfs": [
                "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
                f"/.ollama:rw,noexec,nosuid,nodev,size=16m,mode=0700,uid={uid},gid={gid}",
            ],
            "healthcheck": _health(["CMD", "ollama", "list"]),
        },
    }
    release = {
        "name": project,
        "services": services,
        "networks": {
            "website": {
                "name": project + "_website",
                "internal": True,
                "attachable": False,
            },
            "existing-inference": {
                "external": True,
                "name": inference["networks"]["inference"]["name"],
            },
            "catalog": {
                "external": True,
                "name": catalog_plan["project"] + "_search_backend",
            },
            "ranking": {
                "external": True,
                "name": inference["networks"]["ranking"]["name"],
            },
        },
        "x-depositar-plan": _plan(
            source_revision,
            **(
                {"catalog_revision": catalog_plan["revision"]}
                if catalog_plan["revision"] != source_revision
                else {}
            ),
            manifest_sha256=catalog_plan["manifest_sha256"],
            bundle_id=bundle_id,
            catalog_client_lease_required=True,
            query_provisioning_required=True,
            immutable_built_images_required=True,
            generated_answers=False,
            embedding={
                "model": MODEL,
                "manifest_sha256": MODEL_DIGEST,
                "dimensions": 768,
            },
        ),
    }
    gateway = {
        "name": GATEWAY_PROJECT,
        "services": {
            "web-nginx": {
                **_runtime(uid, gid, cpus=0.5, memory=64 * 1024**2, pids=32),
                "image": "depositar-search-web-nginx:" + source_revision,
                "environment": {"NVIDIA_VISIBLE_DEVICES": "void"},
                "command": [
                    "-c",
                    "/etc/nginx/routes/active.conf",
                    "-g",
                    "daemon off;",
                ],
                "networks": ["route", "ingress"],
                "ports": [
                    {
                        "target": 8080,
                        "published": str(LOOPBACK_PORT),
                        "host_ip": "127.0.0.1",
                        "protocol": "tcp",
                    }
                ],
                "volumes": [
                    _mount(config.state_root / "gateway/routes", "/etc/nginx/routes")
                ],
                "healthcheck": _health(
                    [
                        "CMD",
                        "wget",
                        "-q",
                        "-O",
                        "/dev/null",
                        "http://127.0.0.1:8080/healthz",
                    ]
                ),
            }
        },
        "networks": {
            "route": {"external": True, "name": project + "_website"},
            "ingress": {
                "name": GATEWAY_PROJECT + "_ingress",
                "internal": False,
                "attachable": False,
            },
        },
        "x-depositar-plan": _plan(
            source_revision,
            singleton=True,
            route_publication_required=True,
            immutable_built_images_required=True,
            approved_upstream=alias + ":8000",
            route_text=_route_text(alias),
        ),
    }
    reference = {
        "schema_version": 1,
        "status": "sealed_reference_only",
        "project": catalog_plan["project"],
        "revision": catalog_plan["revision"],
        "bundle_id": bundle_id,
        "manifest_sha256": catalog_plan["manifest_sha256"],
        "document_count": catalog_plan["document_count"],
        "compose_sha256": catalog_plan["compose_sha256"],
        "compose_file": catalog_plan["compose_file"],
        "security_material_sha256": catalog_plan["security_material_sha256"],
        "network_name": catalog_plan["project"] + "_search_backend",
        "runtime_verified": False,
        "client_lease_required": True,
    }
    return {
        "catalog_reference": reference,
        "inference": inference,
        "release": release,
        "gateway": gateway,
    }
