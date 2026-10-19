"""Serialized CPU website updates; the admission plane and Redis stay pinned.

Compose owns containers. Private snapshots and a durable transaction record
provide explicit recovery around its non-atomic operations. No update migrates
quota configuration, credentials, networks, the private release or GPU services.
"""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid

from search.serialization import strict_json
from search.operations.deployment.public_entry import host, profile as public_entry
from search.operations.deployment.public_entry import (
    preflight as public_entry_preflight,
)
from search.operations.shared import private_io


SERVICES = frozenset({"public-entry", "quota-redis", "public-web"})
ERROR_CODES = frozenset(
    "public_entry_" + code
    for code in (
        "operation_locked",
        "lock_unsafe",
        "pending_operation",
        "not_started",
        "unknown_runtime",
        "unknown_redis_data",
        "snapshot_invalid",
        "preparation_changed",
        "material_changed",
        "profile_changed",
        "runtime_changed",
        "recovery_required",
        "no_previous_version",
    )
)


class PublicEntryUpdateError(ValueError):
    """Fixed diagnostics only; no command output or secret material."""


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


def _rows(raw):
    if not raw.strip():
        return []
    try:
        value = strict_json(raw)
    except ValueError:
        value = [strict_json(line) for line in raw.splitlines() if line.strip()]
    return value if type(value) is list else [value]


def _project_present(snapshot, runner):
    return bool(
        _rows(
            _compose(
                snapshot, runner, "ps", "--all", "--no-trunc", "--format", "json"
            ).stdout
        )
    )


def _container_ids(snapshot, runner):
    rows = _rows(
        _compose(
            snapshot, runner, "ps", "--all", "--no-trunc", "--format", "json"
        ).stdout
    )
    _require(
        all(type(row) is dict and row.get("Service") in SERVICES for row in rows)
        and len({row["Service"] for row in rows}) == len(rows),
        "runtime_changed",
    )
    identifiers = {row["Service"]: row.get("ID", "") for row in rows}
    _require(
        all(
            type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in identifiers.values()
        ),
        "runtime_changed",
    )
    return identifiers


def _inspect_containers(snapshot, runner, identifiers, *, healthy=False):
    """Use the same profile checks before first start and after admission opens."""
    inspected = _rows(_run(runner, ["docker", "inspect", *identifiers.values()]).stdout)
    by_id = {row.get("Id"): row for row in inspected}
    profile = snapshot["compose"]
    for role, identifier in identifiers.items():
        row, service = by_id.get(identifier, {}), profile["services"][role]
        image = (
            _run(
                runner,
                ["docker", "image", "inspect", "--format", "{{.Id}}", service["image"]],
            )
            .stdout.decode()
            .strip()
        )
        labels = row.get("Config", {}).get("Labels") or {}
        state = row.get("State", {})
        actual_mounts = sorted(
            (mount.get("Source"), mount.get("Destination"), mount.get("RW"))
            for mount in row.get("Mounts", [])
            if mount.get("Type") == "bind"
        )
        expected_mounts = sorted(
            (mount["source"], mount["target"], not mount["read_only"])
            for mount in service["volumes"]
        )
        expected_networks = {
            profile["networks"][name].get("name", profile["name"] + "_" + name)
            for name in service["networks"]
        }
        _require(
            row.get("Image") == image
            and labels.get("com.docker.compose.project") == profile["name"]
            and labels.get("com.docker.compose.service") == role
            and actual_mounts == expected_mounts
            and set(row.get("NetworkSettings", {}).get("Networks", {}))
            == expected_networks,
            "runtime_changed",
        )
        if healthy:
            _require(
                state.get("Running") is True
                and state.get("Paused") is False
                and state.get("Restarting") is False
                and state.get("Dead") is False
                and state.get("Health", {}).get("Status") == "healthy",
                "runtime_changed",
            )
    return by_id


def _observe(snapshot, runner):
    """Check scoped container identity, images, bind mounts, networks and health."""
    identifiers = _container_ids(snapshot, runner)
    _require(set(identifiers) == SERVICES, "runtime_changed")
    _inspect_containers(snapshot, runner, identifiers, healthy=True)
    return identifiers


def _quota_identity(root, snapshot, runner, expected, *, healthy=False):
    """An unbound transaction may adopt only a Redis that has never started."""
    identifiers = _container_ids(snapshot, runner)
    identifier = identifiers.get("quota-redis")
    if expected is None:
        _require(set(identifiers) <= {"quota-redis"}, "runtime_changed")
        _require(not os.listdir(root / "redis-data"), "unknown_redis_data")
        if identifier is None:
            return None
    else:
        _require(identifier == expected, "runtime_changed")
    row = _inspect_containers(
        snapshot, runner, {"quota-redis": identifier}, healthy=healthy
    )[identifier]
    state = row.get("State", {})
    _require(
        state.get("Paused") is False
        and state.get("Restarting") is False
        and state.get("Dead") is False
        and not row.get("HostConfig", {}).get("PortBindings")
        and not any((row.get("NetworkSettings", {}).get("Ports") or {}).values()),
        "runtime_changed",
    )
    if expected is None:
        _require(
            state.get("Status") == "created"
            and state.get("Running") is False
            and state.get("StartedAt") == "0001-01-01T00:00:00Z"
            and row.get("RestartCount") == 0,
            "runtime_changed",
        )
    return identifier


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
        },
        "preparation_changed",
    )
    for name, digest in files.items():
        _require(
            hashlib.sha256(host._read(root / name)).hexdigest() == digest,
            "preparation_changed",
        )
    profile = _json(root / "compose.json")
    _require(set(profile.get("services", {})) == SERVICES, "profile_changed")
    return profile, files


def _material(config, profile):
    values = {"config": config.to_dict(), "files": {}}
    paths = [
        config.oidc_client_secret_file,
        config.oidc_session_secret_file,
        config.edge_secret_file,
        config.redis_password_file,
    ]
    if config.oidc_ca_file is not None:
        paths.append(config.oidc_ca_file)
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


def _same_profile_except_web_image(profile, base):
    comparison = strict_json(private_io.canonical(profile))
    comparison["services"]["public-web"]["image"] = base["services"]["public-web"][
        "image"
    ]
    return comparison == base


def _verify(config, root, snapshot, inspector):
    base, files = _base(root)
    _require(snapshot["asset_sha256"] == files, "preparation_changed")
    _require(snapshot["material_sha256"] == _material(config, base), "material_changed")
    profile = snapshot["compose"]
    _require(
        _same_profile_except_web_image(profile, base)
        and profile["services"]["public-web"]["image"] == snapshot["web_image"],
        "profile_changed",
    )
    proof = inspector(snapshot["web_image"])
    _require(
        type(proof) is dict
        and proof.get("Id") == snapshot["web_image"]
        and (proof.get("Labels") or {}).get(host.CAPABILITY) == "1",
        "profile_changed",
    )


def _active(root):
    _require((root / "active.json").exists(), "not_started")
    return _json(root / "active.json")


def _report(status, state, *, changed=False):
    return {
        "schema_version": 1,
        "status": status,
        "generation": state["generation"],
        "previous_generation": state.get("previous_generation"),
        "web_image": state["web_image"],
        "compose_file": state["compose_file"],
        "deployment_performed": changed,
        "public_entry_changed": changed,
        "active_changed": False,
        "public_readiness_verified": False,
    }


def _commit(root, snapshot, previous, runtime):
    state = {
        "schema_version": 1,
        "generation": snapshot["generation"],
        "previous_generation": previous,
        "web_image": snapshot["web_image"],
        "compose_file": snapshot["compose_file"],
        "runtime_ids": runtime,
        "material_sha256": snapshot["material_sha256"],
    }
    _write(root, "active.json", state)
    _remove(root, "transaction.json")
    return state


def _pin_runtime(runtime, expected):
    _require(
        all(
            runtime.get(role) == expected.get(role)
            for role in ("public-entry", "quota-redis")
        ),
        "runtime_changed",
    )


def _stop(snapshot, runner):
    _compose(snapshot, runner, "stop", "--timeout", "10", "public-entry", timeout=30)


def _replace_web(snapshot, runner):
    _compose(
        snapshot,
        runner,
        "up",
        "--detach",
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "--wait",
        "--wait-timeout",
        "60",
        "public-web",
        timeout=90,
    )
    public_entry_preflight.verify_bound_backend(snapshot["compose"], runner=runner)
    _compose(
        snapshot,
        runner,
        "start",
        "--wait",
        "--wait-timeout",
        "60",
        "public-entry",
        timeout=90,
    )


def _stop_project(runner):
    """Close only this entrance even when credentials or version files are damaged."""
    command = [
        "docker",
        "ps",
        "--no-trunc",
        "--filter",
        "label=com.docker.compose.project=" + public_entry.PROJECT,
        "--filter",
        "label=com.docker.compose.service=public-entry",
        "--format",
        "{{.ID}}",
    ]
    identifiers = _run(runner, command).stdout.decode("ascii").split()
    _require(
        all(re.fullmatch(r"[0-9a-f]{64}", item) for item in identifiers),
        "runtime_changed",
    )
    if identifiers:
        _run(runner, ["docker", "stop", "--time", "10", *identifiers], timeout=30)
    return not _run(runner, command).stdout.strip()


def _mark_recovery(root, transaction, snapshot, runner):
    stopped = False
    try:
        stopped = _stop_project(runner)
    except Exception:
        pass
    if transaction is not None:
        # A durable pin may have been published before its writer reported an IO
        # failure. Never replace that receipt with a stale in-memory null pin.
        try:
            durable = _transaction(root)
            _require(
                all(
                    durable[key] == transaction[key]
                    for key in ("action", "candidate", "previous")
                ),
                "snapshot_invalid",
            )
        except FileNotFoundError:
            # Commit may unlink the journal before its directory fsync fails.
            # Restore our latest receipt only when no raw receipt remains.
            durable = (
                None if os.path.lexists(root / "transaction.json") else transaction
            )
        except Exception:
            durable = None
        transaction = durable
    if transaction is not None:
        _write(
            root,
            "transaction.json",
            {**transaction, "status": "recovery_required", "entrance_stopped": stopped},
        )
    state = snapshot or {"generation": None, "web_image": None, "compose_file": None}
    return {
        **_report("recovery_required", state, changed=True),
        "entrance_stopped": stopped,
    }


def _bootstrap_runtime(root, snapshot, transaction, runner, *, pull):
    """Pin first-boot Redis durably before any service can start."""
    identifier = transaction["quota_container_id"]
    if identifier is None:
        identifier = _quota_identity(root, snapshot, runner, None)
        if identifier is None:
            _require(
                not snapshot["compose"]["services"]["quota-redis"].get("depends_on"),
                "profile_changed",
            )
            _compose(
                snapshot,
                runner,
                "create",
                "--no-recreate",
                "--no-build",
                "--pull",
                pull,
                "quota-redis",
                timeout=180,
            )
            identifier = _quota_identity(root, snapshot, runner, None)
            _require(identifier is not None, "runtime_changed")
        pinned = {**transaction, "quota_container_id": identifier}
        _write(root, "transaction.json", pinned)
        transaction.update(pinned)
    _quota_identity(root, snapshot, runner, identifier)
    _compose(
        snapshot,
        runner,
        "start",
        "--wait",
        "--wait-timeout",
        "90",
        "quota-redis",
        timeout=180,
    )
    _quota_identity(root, snapshot, runner, identifier, healthy=True)
    public_entry_preflight.verify_bound_backend(snapshot["compose"], runner=runner)
    _quota_identity(root, snapshot, runner, identifier, healthy=True)
    _compose(
        snapshot,
        runner,
        "up",
        "--detach",
        "--no-recreate",
        "--no-deps",
        "--no-build",
        "--pull",
        pull,
        "--wait",
        "--wait-timeout",
        "90",
        "public-web",
        "public-entry",
        timeout=180,
    )
    _quota_identity(root, snapshot, runner, identifier, healthy=True)
    runtime = _observe(snapshot, runner)
    _require(runtime["quota-redis"] == identifier, "runtime_changed")
    return runtime


def bootstrap_public_entry(
    config_path,
    release_compose,
    web_image,
    *,
    runner=subprocess.run,
    image_inspector=host.inspect_web_image,
):
    config = public_entry.load_public_entry_config(Path(config_path))
    with _lock(config.state_root) as root:
        _require(not os.path.lexists(root / "transaction.json"), "pending_operation")
        if (root / "active.json").exists():
            state = _active(root)
            current = _read_snapshot(root, state["generation"])
            _verify(config, root, current, image_inspector)
            _require(current["web_image"] == web_image, "profile_changed")
            requested = host.bind_public_web(
                config,
                public_entry.render_public_entry(config),
                strict_json(host._read(release_compose)),
                web_image,
                image_inspector(web_image),
            )["compose"]
            _require(requested == current["compose"], "profile_changed")
            public_entry_preflight.verify_bound_backend(
                current["compose"], runner=runner
            )
            _require(
                _observe(current, runner) == state["runtime_ids"], "runtime_changed"
            )
            return _report("public_entry_started", state)
        host.prepare_public_entry(
            config_path, release_compose, web_image, image_inspector=image_inspector
        )
        _require(
            public_entry.load_public_entry_config(Path(config_path)) == config,
            "material_changed",
        )
        profile, files = _base(root)
        public_entry_preflight.verify_bound_backend(profile, runner=runner)
        snapshot = _snapshot(root, profile, files, _material(config, profile))
        _require(
            not _project_present(
                {**snapshot, "compose_file": str(root / "compose.json")}, runner
            ),
            "unknown_runtime",
        )
        _require(not os.listdir(root / "redis-data"), "unknown_redis_data")
        _save_snapshot(root, snapshot)
        transaction = {
            "schema_version": 1,
            "status": "pending",
            "action": "bootstrap",
            "previous": None,
            "candidate": snapshot["generation"],
            "quota_container_id": None,
        }
        _write(root, "transaction.json", transaction)
        try:
            runtime = _bootstrap_runtime(
                root, snapshot, transaction, runner, pull="missing"
            )
            _verify(config, root, snapshot, image_inspector)
            public_entry_preflight.verify_bound_backend(
                snapshot["compose"], runner=runner
            )
            _quota_identity(
                root, snapshot, runner, transaction["quota_container_id"], healthy=True
            )
            state = _commit(root, snapshot, None, runtime)
            return _report("public_entry_started", state, changed=True)
        except Exception:
            return _mark_recovery(root, transaction, snapshot, runner)


def _change(config, root, state, target, runner, inspector, *, action):
    old = _read_snapshot(root, state["generation"])
    _verify(config, root, old, inspector)
    _verify(config, root, target, inspector)
    public_entry_preflight.verify_bound_backend(old["compose"], runner=runner)
    _require(_observe(old, runner) == state["runtime_ids"], "runtime_changed")
    if target["generation"] == old["generation"]:
        return _report("public_entry_unchanged", state)
    _save_snapshot(root, target)
    transaction = {
        "schema_version": 1,
        "status": "pending",
        "action": action,
        "previous": state,
        "candidate": target["generation"],
    }
    _write(root, "transaction.json", transaction)
    try:
        _stop(old, runner)
        _replace_web(target, runner)
        runtime = _observe(target, runner)
        _pin_runtime(runtime, state["runtime_ids"])
        _verify(config, root, target, inspector)
        public_entry_preflight.verify_bound_backend(target["compose"], runner=runner)
        current = _commit(root, target, old["generation"], runtime)
        return _report(
            "public_entry_updated"
            if action == "update"
            else "public_entry_rolled_back",
            current,
            changed=True,
        )
    except Exception:
        try:
            _verify(config, root, old, inspector)
            _stop(old, runner)
            _replace_web(old, runner)
            runtime = _observe(old, runner)
            _pin_runtime(runtime, state["runtime_ids"])
            _verify(config, root, old, inspector)
            public_entry_preflight.verify_bound_backend(old["compose"], runner=runner)
            _commit(root, old, state.get("previous_generation"), runtime)
            return _report("update_failed_rolled_back", state, changed=True)
        except Exception:
            return _mark_recovery(root, transaction, old, runner)


def update_public_entry(
    config_path,
    release_compose,
    web_image,
    *,
    runner=subprocess.run,
    image_inspector=host.inspect_web_image,
):
    config = public_entry.load_public_entry_config(Path(config_path))
    with _lock(config.state_root) as root:
        _require(not os.path.lexists(root / "transaction.json"), "pending_operation")
        state = _active(root)
        base, files = _base(root)
        candidate = host.bind_public_web(
            config,
            public_entry.render_public_entry(config),
            strict_json(host._read(release_compose)),
            web_image,
            image_inspector(web_image),
        )["compose"]
        _require(_same_profile_except_web_image(candidate, base), "profile_changed")
        rendered = public_entry.render_public_entry(config)
        for name, content in rendered["assets"].items():
            _require(
                hashlib.sha256(content.encode()).hexdigest() == files.get(name),
                "preparation_changed",
            )
        target = _snapshot(root, candidate, files, _material(config, base))
        return _change(
            config, root, state, target, runner, image_inspector, action="update"
        )


def rollback_public_entry(
    config_path, *, runner=subprocess.run, image_inspector=host.inspect_web_image
):
    config = public_entry.load_public_entry_config(Path(config_path))
    with _lock(config.state_root) as root:
        _require(not os.path.lexists(root / "transaction.json"), "pending_operation")
        state = _active(root)
        _require(state.get("previous_generation") is not None, "no_previous_version")
        target = _read_snapshot(root, state["previous_generation"])
        return _change(
            config, root, state, target, runner, image_inspector, action="rollback"
        )


def _recovery_state_root(config_path):
    """Locate private state without requiring intact secrets to close an entrance."""
    path = public_entry._path(str(config_path), public_entry.ROOT)
    value = strict_json(
        public_entry._read_private(
            path, uid=os.geteuid(), maximum=public_entry.MAX_CONFIG_BYTES
        )
    )
    _require(type(value) is dict, "snapshot_invalid")
    return public_entry._path(value.get("state_root"), public_entry.ROOT)


def _transaction(root):
    value = _json(root / "transaction.json")
    _require(
        type(value) is dict
        and value.get("schema_version") == 1
        and value.get("status") in ("pending", "recovery_required")
        and value.get("action") in ("bootstrap", "update", "rollback")
        and type(value.get("candidate")) is str
        and re.fullmatch(r"[0-9a-f]{64}", value["candidate"]),
        "snapshot_invalid",
    )
    previous = value.get("previous")
    _require("previous" in value, "snapshot_invalid")
    if value["action"] == "bootstrap":
        _require(
            previous is None
            and "quota_container_id" in value
            and (
                value["quota_container_id"] is None
                or (
                    type(value["quota_container_id"]) is str
                    and re.fullmatch(r"[0-9a-f]{64}", value["quota_container_id"])
                )
            ),
            "snapshot_invalid",
        )
    else:
        _require(type(previous) is dict, "snapshot_invalid")
        _require(
            type(previous.get("generation")) is str
            and re.fullmatch(r"[0-9a-f]{64}", previous["generation"])
            and type(previous.get("runtime_ids")) is dict
            and set(previous["runtime_ids"]) == SERVICES
            and all(
                type(item) is str and re.fullmatch(r"[0-9a-f]{64}", item)
                for item in previous["runtime_ids"].values()
            ),
            "snapshot_invalid",
        )
    return value


def recover_public_entry(
    config_path, *, runner=subprocess.run, image_inspector=host.inspect_web_image
):
    state_root = _recovery_state_root(config_path)
    with _lock(state_root) as root:
        if not os.path.lexists(root / "transaction.json"):
            config = public_entry.load_public_entry_config(Path(config_path))
            state = _active(root)
            snapshot = _read_snapshot(root, state["generation"])
            _verify(config, root, snapshot, image_inspector)
            public_entry_preflight.verify_bound_backend(
                snapshot["compose"], runner=runner
            )
            _require(
                _observe(snapshot, runner) == state["runtime_ids"], "runtime_changed"
            )
            return _report("public_entry_unchanged", state)
        transaction = target = None
        try:
            transaction = _transaction(root)
            config = public_entry.load_public_entry_config(Path(config_path))
            previous = transaction["previous"]
            target = _read_snapshot(
                root, previous["generation"] if previous else transaction["candidate"]
            )
            _verify(config, root, target, image_inspector)
            _stop(target, runner)
            public_entry_preflight.verify_bound_backend(
                target["compose"], runner=runner
            )
            if previous:
                _replace_web(target, runner)
            else:
                _bootstrap_runtime(root, target, transaction, runner, pull="never")
            runtime = _observe(target, runner)
            if previous:
                _pin_runtime(runtime, previous["runtime_ids"])
            _verify(config, root, target, image_inspector)
            public_entry_preflight.verify_bound_backend(
                target["compose"], runner=runner
            )
            if previous is None:
                _quota_identity(
                    root,
                    target,
                    runner,
                    transaction["quota_container_id"],
                    healthy=True,
                )
            state = _commit(
                root,
                target,
                previous.get("previous_generation") if previous else None,
                runtime,
            )
            return _report("public_entry_recovered", state, changed=True)
        except Exception:
            return _mark_recovery(root, transaction, target, runner)
