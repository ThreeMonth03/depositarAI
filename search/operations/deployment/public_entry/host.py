"""Bind a separate authenticated CPU website to an existing private release.

Preparation never changes the catalog, models, native gateway or active release.
Docker inspection verifies the opt-in website image contract; actual OIDC and
upstream HTTPS readiness still require operator configuration and acceptance.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid

from search.serialization import strict_json
from search.operations.deployment import composition
from search.operations.deployment.public_entry import profile as public_entry
from search.operations.shared import private_io


IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
RELEASE = re.compile(r"depositar-ai-release-[0-9a-f]{12}-[0-9a-f]{12}\Z")
CAPABILITY = "io.depositar.ai.public-entry.version"
FRONTEND_CAPABILITY = "io.depositar.ai.frontend.version"


def _read(path, limit=2 * 1024**2):
    path = Path(path)
    descriptor = private_io.open_directory(path.parent)
    try:
        return private_io.read_file(descriptor, path.name, mode=0o600, max_bytes=limit)
    finally:
        os.close(descriptor)


def inspect_web_image(image):
    if type(image) is not str or IMAGE.fullmatch(image) is None:
        raise ValueError("public_entry_web_image_invalid")
    result = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{"Id":{{json .Id}},"Labels":{{json .Config.Labels}}}',
            image,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=True,
    )
    if len(result.stdout) > 64 * 1024:
        raise ValueError("public_entry_web_image_invalid")
    return strict_json(result.stdout)


def _web_revision(image, image_proof, frontend_image, frontend_proof):
    labels = image_proof.get("Labels") if type(image_proof) is dict else None
    revision = (
        labels.get("org.opencontainers.image.revision")
        if type(labels) is dict
        else None
    )
    frontend_labels = (
        frontend_proof.get("Labels") if type(frontend_proof) is dict else None
    )
    if (
        type(image) is not str
        or IMAGE.fullmatch(image) is None
        or type(image_proof) is not dict
        or image_proof.get("Id") != image
        or type(labels) is not dict
        or labels.get(CAPABILITY) != "2"
        or type(revision) is not str
        or re.fullmatch(r"[0-9a-f]{40}", revision) is None
    ):
        raise ValueError("public_entry_web_image_invalid")
    if (
        type(frontend_image) is not str
        or IMAGE.fullmatch(frontend_image) is None
        or type(frontend_proof) is not dict
        or frontend_proof.get("Id") != frontend_image
        or type(frontend_labels) is not dict
        or frontend_labels.get(FRONTEND_CAPABILITY) != "1"
        or frontend_labels.get(CAPABILITY) != "2"
        or frontend_labels.get("org.opencontainers.image.revision") != revision
    ):
        raise ValueError("public_entry_frontend_image_invalid")
    return revision


def bind_public_web(
    config, rendered, release, image, image_proof, frontend_image, frontend_proof
):
    """Read-only same-host binding; never change the private release."""
    revision = _web_revision(image, image_proof, frontend_image, frontend_proof)
    if (
        type(release) is not dict
        or type(release.get("name")) is not str
        or RELEASE.fullmatch(release["name"]) is None
    ):
        raise ValueError("public_entry_release_invalid")
    try:
        network = release["networks"]["website"]["name"]
        website = release["services"]["web"]
        api = release["services"]["conversation-api"]
        if type(website) is not dict or type(api) is not dict:
            raise ValueError("public_entry_release_invalid")
        key_mounts = [
            mount
            for mount in website["volumes"]
            if mount["target"] == "/run/secrets/api_key"
        ]
    except (KeyError, TypeError):
        raise ValueError("public_entry_release_invalid") from None
    if (
        network != release["name"] + "_website"
        or website.get("environment", {}).get("WEB_UPSTREAM")
        != "http://conversation-api:8000"
        or len(key_mounts) != 1
        or key_mounts[0].get("read_only") is not True
        or key_mounts[0].get("type") != "bind"
        or type(api.get("image")) is not str
        or IMAGE.fullmatch(api["image"]) is None
        or api.get("ports")
        or website.get("ports")
    ):
        raise ValueError("public_entry_release_invalid")
    key_path = Path(key_mounts[0]["source"])
    raw_key = _read(key_path, 65)
    if re.fullmatch(rb"[0-9a-f]{64}\n?", raw_key) is None:
        raise ValueError("public_entry_api_key_invalid")
    result = _bind_website(config, rendered, image, frontend_image, revision, key_path)
    compose = result["compose"]
    compose["networks"]["existing-release"] = {"external": True, "name": network}
    compose["services"]["public-web"]["networks"]["existing-release"] = {}
    compose["x-depositar-plan"]["private_backend"] = {
        "schema_version": 1,
        "release_project": release["name"],
        "network_name": network,
        "api_image": api["image"],
    }
    return result


def _bind_website(config, rendered, image, frontend_image, revision, key_path):
    uid, gid = os.geteuid(), os.getegid()
    result = copy.deepcopy(rendered)
    compose = result["compose"]
    if "public-web" in compose["services"] or "existing-release" in compose["networks"]:
        raise ValueError("public_entry_binding_exists")
    compose["services"]["public-web"] = {
        **composition._runtime(uid, gid, cpus=1, memory=512 * 1024**2, pids=64),
        "image": image,
        "networks": {"website": {"aliases": ["web"]}},
        "environment": {
            "WEB_ORIGIN": config.public_origin,
            "WEB_UPSTREAM": "http://conversation-api:8000",
            "WEB_EDGE_SECRET_FILE": "/run/secrets/public_edge_key",
            "NVIDIA_VISIBLE_DEVICES": "void",
        },
        "volumes": [
            composition._mount(key_path, "/run/secrets/api_key"),
            composition._mount(config.edge_secret_file, "/run/secrets/public_edge_key"),
        ],
        "healthcheck": composition._http_health(8000),
    }
    compose["services"]["public-frontend"] = {
        **composition._runtime(uid, gid, cpus=0.5, memory=64 * 1024**2, pids=32),
        "image": frontend_image,
        "networks": ["website"],
        "environment": {"NVIDIA_VISIBLE_DEVICES": "void"},
        "depends_on": {"public-web": {"condition": "service_healthy"}},
        "healthcheck": {
            "test": [
                "CMD-SHELL",
                "wget -q -O /dev/null http://127.0.0.1:8080/healthz && wget -q -O - http://127.0.0.1:8080/ | grep -Fq '<div id=\"root\"></div>'",
            ],
            "interval": "5s",
            "timeout": "3s",
            "retries": 12,
        },
    }
    compose["services"]["public-entry"]["depends_on"]["public-frontend"] = {
        "condition": "service_healthy"
    }
    result["status"] = "public_entry_prepared"
    plan = compose.setdefault("x-depositar-plan", {})
    plan["status"] = "public_entry_prepared"
    plan["revision"] = revision
    return result


def bind_remote_web(
    config, rendered, backend, image, image_proof, frontend_image, frontend_proof
):
    """Bind only the CPU BFF to a fixed authenticated HTTPS origin."""
    revision = _web_revision(image, image_proof, frontend_image, frontend_proof)
    if image_proof["Labels"].get("io.depositar.ai.conversation.version") != "3":
        raise ValueError("public_entry_remote_contract_invalid")
    result = _bind_website(
        config, rendered, image, frontend_image, revision, backend.api_key_file
    )
    compose = result["compose"]
    if compose["networks"].get("outbound", {}).get("internal") is not False:
        raise ValueError("public_entry_remote_network_invalid")
    web = compose["services"]["public-web"]
    web["networks"]["outbound"] = {}
    web["environment"].update(
        WEB_UPSTREAM=backend.origin,
        WEB_UPSTREAM_CA_FILE="/run/certs/upstream-ca.pem",
    )
    web["volumes"].append(
        composition._mount(backend.ca_file, "/run/certs/upstream-ca.pem")
    )
    compose["x-depositar-plan"]["private_backend"] = backend.to_plan()
    return result


def bind_configured_web(
    config,
    rendered,
    release_compose,
    image,
    image_proof,
    frontend_image,
    frontend_proof,
    *,
    backend_config=None,
):
    if (release_compose is None) == (backend_config is None):
        raise ValueError("public_entry_backend_source_invalid")
    if backend_config is not None:
        from .remote import load_remote_backend

        return bind_remote_web(
            config,
            rendered,
            load_remote_backend(backend_config),
            image,
            image_proof,
            frontend_image,
            frontend_proof,
        )
    return bind_public_web(
        config,
        rendered,
        strict_json(_read(release_compose)),
        image,
        image_proof,
        frontend_image,
        frontend_proof,
    )


def _publish_prepared(root, assets, report, state_directories=("redis-data",)):
    """Publish a complete preparation; failed staging never replaces live state."""
    parent = private_io.open_directory(root)
    lock = descriptor = None
    try:
        lock = os.open(
            ".public-entry-prepare.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=parent,
        )
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("public_entry_lock_unsafe")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            os.stat("public-entry", dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            name = ".public-entry-staging-" + uuid.uuid4().hex
            descriptor = private_io.open_child_directory(parent, name, create=True)
            for directory in state_directories:
                data = private_io.open_child_directory(
                    descriptor, directory, create=True
                )
                os.close(data)
            for filename, raw in assets.items():
                private_io.write_file(descriptor, filename, raw, max_bytes=2 * 1024**2)
            private_io.write_json(descriptor, "prepared.json", report, max_bytes=65536)
            private_io.verify_directory(parent, root)
            # Other preparers hold this same lock. Refuse even an empty directory
            # created by a separate operator during staging.
            if os.path.lexists(root / "public-entry"):
                raise ValueError("public_entry_destination_exists")
            os.rename(name, "public-entry", src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        else:
            descriptor = private_io.open_child_directory(parent, "public-entry")
            saved = strict_json(
                private_io.read_file(
                    descriptor, "prepared.json", mode=0o600, max_bytes=65536
                )
            )
            if saved != report:
                raise ValueError("public_entry_preparation_changed")
            for filename, raw in assets.items():
                if (
                    private_io.read_file(
                        descriptor, filename, mode=0o600, max_bytes=2 * 1024**2
                    )
                    != raw
                ):
                    raise ValueError("public_entry_preparation_changed")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if lock is not None:
            os.close(lock)
        os.close(parent)


def prepare_public_entry(
    config_path,
    release_compose,
    web_image,
    web_nginx_image,
    *,
    image_inspector=inspect_web_image,
    backend_config=None,
):
    config = public_entry.load_public_entry_config(Path(config_path))
    rendered = bind_configured_web(
        config,
        public_entry.render_public_entry(config),
        release_compose,
        web_image,
        image_inspector(web_image),
        web_nginx_image,
        image_inspector(web_nginx_image),
        backend_config=backend_config,
    )
    assets = {name: value.encode("utf-8") for name, value in rendered["assets"].items()}
    assets["compose.json"] = private_io.canonical(rendered["compose"])
    report = {
        "schema_version": 1,
        "status": "public_entry_prepared",
        "compose_file": str(config.state_root / "public-entry/compose.json"),
        "web_image": web_image,
        "web_nginx_image": web_nginx_image,
        "revision": rendered["compose"]["x-depositar-plan"]["revision"],
        "files": {
            name: hashlib.sha256(raw).hexdigest() for name, raw in assets.items()
        },
        "deployment_performed": False,
        "public_readiness_verified": False,
        "active_changed": False,
    }
    _publish_prepared(config.state_root, assets, report, rendered["state_directories"])
    return report


def up_public_entry(
    config_path,
    release_compose,
    web_image,
    web_nginx_image,
    *,
    runner=subprocess.run,
    image_inspector=inspect_web_image,
    backend_config=None,
):
    from .updates import bootstrap_public_entry

    return bootstrap_public_entry(
        config_path,
        release_compose,
        web_image,
        web_nginx_image,
        runner=runner,
        image_inspector=image_inspector,
        backend_config=backend_config,
    )
