"""Small Compose release workflow for one frontend/BFF image pair.

Updates briefly close admission, preserve Redis and private inference/catalog,
and retain the last release for explicit operator rollback. Compose owns runtime
reconciliation; no automatic recovery controller or container adoption exists.
"""

from contextlib import contextmanager
import copy
import hashlib
import http.client
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid
from urllib.parse import urlsplit

from search.serialization import strict_json
from search.operations.deployment.public_entry import host, profile as public_entry
from search.operations.deployment.public_entry import preflight
from search.operations.shared import private_io

SERVICES = frozenset({"public-entry", "quota-redis", "public-web", "public-frontend"})
ANTIBOT_SERVICES = frozenset({"public-edge", "antibot"})


def _entry_services(snapshot):
    return (
        ["public-edge", "public-entry"]
        if "public-edge" in snapshot["compose"]["services"]
        else ["public-entry"]
    )


ERROR_CODES = frozenset(
    "public_entry_" + code
    for code in (
        "operation_locked",
        "lock_unsafe",
        "not_started",
        "snapshot_invalid",
        "preparation_changed",
        "material_changed",
        "profile_changed",
        "no_previous_version",
        "pending_operation",
        "runtime_changed",
    )
)


class PublicEntryUpdateError(ValueError):
    """Closed diagnostics; Compose output and secrets never enter API reports."""


def _require(condition, code):
    if not condition:
        raise PublicEntryUpdateError("public_entry_" + code)


def _json(path):
    return strict_json(host._read(path))


def _write(root, name, value):
    descriptor = private_io.open_directory(root)
    try:
        private_io.write_json(
            descriptor, name, value, replace=True, max_bytes=2 * 1024**2
        )
    finally:
        os.close(descriptor)


def _remove(root, name):
    descriptor = private_io.open_directory(root)
    try:
        os.unlink(name, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _lock(state_root):
    parent = private_io.open_directory(state_root)
    descriptor = None
    try:
        descriptor = os.open(
            ".public-entry-operation.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        info = os.fstat(descriptor)
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600,
            "lock_unsafe",
        )
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublicEntryUpdateError("public_entry_operation_locked") from None
        yield state_root / "public-entry"
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _run(runner, command, *, timeout=30):
    return runner(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=True,
    )


def _compose(snapshot, runner, *arguments, timeout=90):
    return _run(
        runner,
        ["docker", "compose", "-f", snapshot["compose_file"], *arguments],
        timeout=timeout,
    )


def _base(root):
    report = _json(root / "prepared.json")
    files = report.get("files", {})
    _require(
        type(files) is dict
        and "compose.json" in files
        and set(files)
        <= {
            "compose.json",
            "config.yaml",
            "apisix.yaml",
            "redis.conf",
            "apisix-start.sh",
            "redis-start.sh",
            "depositar_identity.lua",
            "depositar-quota-response.lua",
            "antibot-nginx.conf",
            "antibot-errors.conf",
            "antibot-policy.yaml",
        },
        "preparation_changed",
    )
    for name, digest in files.items():
        _require(
            hashlib.sha256(host._read(root / name)).hexdigest() == digest,
            "preparation_changed",
        )
    profile = _json(root / "compose.json")
    expected = SERVICES | (
        ANTIBOT_SERVICES
        if profile.get("x-depositar-plan", {}).get("antibot_enabled") is True
        else frozenset()
    )
    _require(set(profile.get("services", {})) == expected, "profile_changed")
    return profile, files


def _material(config, profile):
    values = {"config": config.to_dict(), "files": {}}
    paths = [
        config.oidc_client_secret_file,
        config.oidc_session_secret_file,
        config.edge_secret_file,
        config.redis_password_file,
    ]
    for path in (
        config.oidc_ca_file,
        config.guest_session_secret_file,
        config.antibot_signing_key_file,
    ):
        if path is not None:
            paths.append(path)
    paths.extend(
        Path(mount["source"]) for mount in profile["services"]["public-web"]["volumes"]
    )
    for path in paths:
        raw = public_entry._read_private(path, uid=os.geteuid(), maximum=256 * 1024)
        values["files"][str(path)] = hashlib.sha256(raw).hexdigest()
    return hashlib.sha256(private_io.canonical(values)).hexdigest()


def _snapshot(root, profile, files, material):
    value = {
        "schema_version": 1,
        "compose": profile,
        "web_image": profile["services"]["public-web"]["image"],
        "web_nginx_image": profile["services"]["public-frontend"]["image"],
        "revision": profile["x-depositar-plan"]["revision"],
        "asset_sha256": files,
        "material_sha256": material,
    }
    generation = hashlib.sha256(private_io.canonical(value)).hexdigest()
    value.update(
        generation=generation,
        compose_file=str(root / "versions" / generation / "compose.json"),
    )
    return value


def _save_snapshot(root, snapshot):
    versions = private_io.open_directory(root / "versions", create=True)
    try:
        generation = snapshot["generation"]
        if os.path.lexists(root / "versions" / generation):
            existing = _read_snapshot(root, generation)
            _require(existing == snapshot, "snapshot_invalid")
            return
        temporary = ".staging-" + uuid.uuid4().hex
        directory = private_io.open_child_directory(versions, temporary, create=True)
        try:
            private_io.write_json(
                directory, "compose.json", snapshot["compose"], max_bytes=2 * 1024**2
            )
            private_io.write_json(
                directory, "snapshot.json", snapshot, max_bytes=2 * 1024**2
            )
        finally:
            os.close(directory)
        private_io.verify_directory(versions, root / "versions")
        _require(
            not os.path.lexists(root / "versions" / generation), "snapshot_invalid"
        )
        os.rename(temporary, generation, src_dir_fd=versions, dst_dir_fd=versions)
        os.fsync(versions)
    finally:
        os.close(versions)


def _read_snapshot(root, generation):
    _require(
        type(generation) is str and re.fullmatch(r"[0-9a-f]{64}", generation),
        "snapshot_invalid",
    )
    directory = root / "versions" / generation
    value = _json(directory / "snapshot.json")
    fields = {
        key: value[key]
        for key in (
            "schema_version",
            "compose",
            "web_image",
            "web_nginx_image",
            "revision",
            "asset_sha256",
            "material_sha256",
        )
    }
    _require(
        hashlib.sha256(private_io.canonical(fields)).hexdigest() == generation
        and value["generation"] == generation
        and value["compose_file"] == str(directory / "compose.json")
        and _json(directory / "compose.json") == value["compose"],
        "snapshot_invalid",
    )
    return value


def _same_profile_except_images(profile, base):
    comparison = copy.deepcopy(profile)
    for role in ("public-web", "public-frontend"):
        comparison["services"][role]["image"] = base["services"][role]["image"]
    comparison["x-depositar-plan"]["revision"] = base["x-depositar-plan"]["revision"]
    return comparison == base


def _verify(config, root, snapshot, inspector):
    base, files = _base(root)
    _require(snapshot["asset_sha256"] == files, "preparation_changed")
    _require(snapshot["material_sha256"] == _material(config, base), "material_changed")
    profile = snapshot["compose"]
    _require(_same_profile_except_images(profile, base), "profile_changed")
    for field, role, capability in (
        ("web_image", "public-web", host.CAPABILITY),
        ("web_nginx_image", "public-frontend", host.FRONTEND_CAPABILITY),
    ):
        image = snapshot[field]
        proof = inspector(image)
        labels = proof.get("Labels") if type(proof) is dict else None
        _require(
            type(image) is str
            and host.IMAGE.fullmatch(image)
            and type(proof) is dict
            and proof.get("Id") == image
            and type(labels) is dict
            and profile["services"][role]["image"] == image
            and labels.get(capability)
            == ("2" if capability == host.CAPABILITY else "1")
            and labels.get(host.CAPABILITY) == "2"
            and labels.get("org.opencontainers.image.revision") == snapshot["revision"],
            "profile_changed",
        )
        if (
            role == "public-web"
            and profile["x-depositar-plan"]["private_backend"].get("kind") == "https"
        ):
            _require(
                labels.get("io.depositar.ai.conversation.version") == "3",
                "profile_changed",
            )
    _require(
        profile["x-depositar-plan"]["revision"] == snapshot["revision"],
        "profile_changed",
    )


def _report(status, state, *, changed=False):
    return {
        "schema_version": 1,
        "status": status,
        "generation": state["generation"],
        "previous_generation": state.get("previous_generation"),
        "web_image": state["web_image"],
        "web_nginx_image": state["web_nginx_image"],
        "revision": state["revision"],
        "compose_file": state["compose_file"],
        "deployment_performed": changed,
        "public_entry_changed": changed,
        "active_changed": False,
        "public_readiness_verified": False,
    }


def _stop(snapshot, runner):
    _compose(
        snapshot,
        runner,
        "stop",
        "--timeout",
        "10",
        *_entry_services(snapshot),
        timeout=30,
    )


def _smoke(config):
    """Read only this loopback entrance; never follow redirects or call AI."""
    connection = http.client.HTTPConnection(
        "127.0.0.1", config.loopback_port, timeout=5
    )
    try:
        paths = ["/healthz", "/"]
        for path in paths:
            connection.request(
                "GET", path, headers={"Host": urlsplit(config.public_origin).netloc}
            )
            response = connection.getresponse()
            body = response.read(256 * 1024 + 1)
            if response.status != 200 or len(body) > 256 * 1024:
                raise ValueError("public_entry_http_acceptance_failed")
            if path == "/" and b'<div id="root"></div>' not in body:
                raise ValueError("public_entry_http_acceptance_failed")
            if path == "/healthz" and strict_json(body) != {"status": "ok"}:
                raise ValueError("public_entry_http_acceptance_failed")
            if path.endswith((".js", ".css")):
                expected = (
                    {"text/css"}
                    if path.endswith(".css")
                    else {"text/javascript", "application/javascript"}
                )
                content_type = response.getheader("Content-Type", "").split(";", 1)[0]
                if not body or content_type not in expected:
                    raise ValueError("public_entry_http_acceptance_failed")
            if path == "/":
                assets = sorted(
                    set(
                        re.findall(
                            rb'(?:src|href)="(/assets/[A-Za-z0-9_-]+\.(?:js|css))"',
                            body,
                        )
                    )
                )
                if not any(asset.endswith(b".js") for asset in assets) or not any(
                    asset.endswith(b".css") for asset in assets
                ):
                    raise ValueError("public_entry_http_acceptance_failed")
                paths.extend(asset.decode("ascii") for asset in assets)
    finally:
        connection.close()


def _candidate(
    config,
    release_compose,
    web_image,
    web_nginx_image,
    inspector,
    *,
    backend_config=None,
):
    return host.bind_configured_web(
        config,
        public_entry.render_public_entry(config),
        release_compose,
        web_image,
        inspector(web_image),
        web_nginx_image,
        inspector(web_nginx_image),
        backend_config=backend_config,
    )["compose"]


def _active(root):
    _require((root / "active.json").exists(), "not_started")
    state = _json(root / "active.json")
    snapshot = _read_snapshot(root, state.get("generation"))
    _require(
        state.get("web_image") == snapshot["web_image"]
        and state.get("web_nginx_image") == snapshot["web_nginx_image"]
        and state.get("revision") == snapshot["revision"],
        "snapshot_invalid",
    )
    return state, snapshot


def _deploy(config, root, target, old, runner, inspector, smoke, *, bootstrap=False):
    """A short maintenance window; failed candidates require explicit rollback."""
    _verify(config, root, target, inspector)
    preflight.verify_bound_backend(target["compose"], runner=runner)
    _save_snapshot(root, target)
    # This small intent only selects the rollback target after interruption. It
    # never drives an automatic retry, runtime adoption or recovery controller.
    _write(
        root,
        "pending.json",
        {
            "schema_version": 1,
            "candidate": target["generation"],
            "previous": None if old is None else old["generation"],
        },
    )
    try:
        _stop(target, runner)
        if bootstrap:
            _compose(
                target,
                runner,
                "up",
                "--detach",
                "--no-recreate",
                "--no-deps",
                "--no-build",
                "--pull",
                "missing",
                "--wait",
                "--wait-timeout",
                "90",
                "quota-redis",
                timeout=180,
            )
        _compose(
            target,
            runner,
            "up",
            "--detach",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "90",
            "public-web",
            "public-frontend",
            timeout=180,
        )
        preflight.verify_bound_backend(
            target["compose"], runner=runner, compose_file=target["compose_file"]
        )
        if "public-edge" in target["compose"]["services"]:
            _compose(
                target,
                runner,
                "up",
                "--detach",
                "--no-deps",
                "--no-build",
                "--pull",
                "missing" if bootstrap else "never",
                "--wait",
                "--wait-timeout",
                "90",
                "antibot",
                "public-edge",
                timeout=180,
            )
        _compose(
            target,
            runner,
            "up",
            "--detach",
            "--no-deps",
            "--no-build",
            "--pull",
            "missing" if bootstrap else "never",
            "--wait",
            "--wait-timeout",
            "90",
            "public-entry",
            timeout=180,
        )
        smoke(config)
        _verify(config, root, target, inspector)
        state = {
            key: target[key]
            for key in (
                "schema_version",
                "generation",
                "web_image",
                "web_nginx_image",
                "revision",
                "compose_file",
            )
        }
        state["previous_generation"] = None if old is None else old["generation"]
        _write(root, "active.json", state)
        _remove(root, "pending.json")
        return _report(
            "public_entry_started" if bootstrap else "public_entry_updated",
            state,
            changed=True,
        )
    except BaseException as error:
        stopped = False
        try:
            _stop(target, runner)
            stopped = True
        except Exception:
            pass
        if not isinstance(error, Exception):
            raise
        return {
            **_report("deployment_failed", target, changed=True),
            "entrance_stop_requested": stopped,
            "manual_rollback_required": old is not None,
        }


def bootstrap_public_entry(
    config_path,
    release_compose,
    web_image,
    web_nginx_image,
    *,
    runner=subprocess.run,
    image_inspector=host.inspect_web_image,
    smoke_probe=_smoke,
    backend_config=None,
):
    config = public_entry.load_public_entry_config(Path(config_path))
    with _lock(config.state_root) as root:
        if (root / "pending.json").exists():
            pending = _json(root / "pending.json")
            _require(
                set(pending) == {"schema_version", "candidate", "previous"}
                and pending["schema_version"] == 1
                and pending["previous"] is None,
                "pending_operation",
            )
            if (root / "active.json").exists():
                state, current = _active(root)
                _require(
                    current["generation"] == pending["candidate"]
                    and state.get("previous_generation") is None,
                    "pending_operation",
                )
            target = _read_snapshot(root, pending["candidate"])
            requested = _candidate(
                config,
                release_compose,
                web_image,
                web_nginx_image,
                image_inspector,
                backend_config=backend_config,
            )
            _require(requested == target["compose"], "profile_changed")
            return _deploy(
                config,
                root,
                target,
                None,
                runner,
                image_inspector,
                smoke_probe,
                bootstrap=True,
            )
        if (root / "active.json").exists():
            state, current = _active(root)
            requested = _candidate(
                config,
                release_compose,
                web_image,
                web_nginx_image,
                image_inspector,
                backend_config=backend_config,
            )
            _require(requested == current["compose"], "profile_changed")
            _verify(config, root, current, image_inspector)
            if state.get("previous_generation") is None:
                # Explicit up also handles first-boot cleanup failing after the
                # pending unlink. Compose preserves the existing Redis rather
                # than depending on an IO failure marker being durable.
                return _deploy(
                    config,
                    root,
                    current,
                    None,
                    runner,
                    image_inspector,
                    smoke_probe,
                    bootstrap=True,
                )
            preflight.verify_bound_backend(
                current["compose"], runner=runner, compose_file=current["compose_file"]
            )
            smoke_probe(config)
            return _report("public_entry_unchanged", state)
        host.prepare_public_entry(
            config_path,
            release_compose,
            web_image,
            web_nginx_image,
            image_inspector=image_inspector,
            backend_config=backend_config,
        )
        profile, files = _base(root)
        rows = _compose(
            {"compose_file": str(root / "compose.json")},
            runner,
            "ps",
            "--all",
            "--format",
            "json",
        ).stdout.strip()
        _require(rows in (b"", b"[]"), "runtime_changed")
        for name in ("redis-data", "antibot-data"):
            if (root / name).exists():
                _require(not os.listdir(root / name), "runtime_changed")
        target = _snapshot(root, profile, files, _material(config, profile))
        return _deploy(
            config,
            root,
            target,
            None,
            runner,
            image_inspector,
            smoke_probe,
            bootstrap=True,
        )


def update_public_entry(
    config_path,
    release_compose,
    web_image,
    web_nginx_image,
    *,
    runner=subprocess.run,
    image_inspector=host.inspect_web_image,
    smoke_probe=_smoke,
    backend_config=None,
):
    config = public_entry.load_public_entry_config(Path(config_path))
    with _lock(config.state_root) as root:
        _require(not (root / "pending.json").exists(), "pending_operation")
        state, old = _active(root)
        _verify(config, root, old, image_inspector)
        profile, files = _base(root)
        candidate = _candidate(
            config,
            release_compose,
            web_image,
            web_nginx_image,
            image_inspector,
            backend_config=backend_config,
        )
        _require(_same_profile_except_images(candidate, profile), "profile_changed")
        target = _snapshot(root, candidate, files, _material(config, profile))
        if target["generation"] == old["generation"]:
            preflight.verify_bound_backend(
                old["compose"], runner=runner, compose_file=old["compose_file"]
            )
            smoke_probe(config)
            return _report("public_entry_unchanged", state)
        return _deploy(config, root, target, old, runner, image_inspector, smoke_probe)


def rollback_public_entry(
    config_path,
    *,
    runner=subprocess.run,
    image_inspector=host.inspect_web_image,
    smoke_probe=_smoke,
):
    config = public_entry.load_public_entry_config(Path(config_path))
    with _lock(config.state_root) as root:
        state, current = _active(root)
        previous = state.get("previous_generation")
        if (root / "pending.json").exists():
            pending = _json(root / "pending.json")
            _require(
                set(pending) == {"schema_version", "candidate", "previous"}
                and pending["schema_version"] == 1,
                "snapshot_invalid",
            )
            previous = pending["previous"]
            # active.json is published before pending.json is removed. Both
            # sides of that small interruption window retain the same rollback
            # target; unrelated active/pending generations are never adopted.
            _require(
                previous == current["generation"]
                or (
                    pending["candidate"] == current["generation"]
                    and state.get("previous_generation") == previous
                ),
                "snapshot_invalid",
            )
        _require(previous is not None, "no_previous_version")
        target = _read_snapshot(root, previous)
        result = _deploy(
            config, root, target, current, runner, image_inspector, smoke_probe
        )
        if result["status"] == "public_entry_updated":
            result["status"] = "public_entry_rolled_back"
        return result
