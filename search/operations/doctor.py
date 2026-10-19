"""Bounded, read-only product preflight; never install, pull, or repair.

Diagnostic codes are fixed. Command output, credentials, environment values,
model contents, and host filesystem paths never enter the returned report.
The Docker visibility probe uses only reviewed existing directories, no GPU,
no network, a read-only container and mounts, and an already-local pinned image.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess

from .config import ProductConfig, ROOT, validate_config, validate_gpu_inventory
from .shared.source_identity import SOURCE_PATHS as SOURCE_PATHS, require_clean_revision


PROBE_IMAGE = (
    "python:3.13.7-slim-bookworm@sha256:"
    "adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d"
)
MAX_COMMAND_OUTPUT = 64 * 1024
MAX_MANIFEST_BYTES = 128 * 1024


def subprocess_runner(argv, *, timeout):
    """No shell, stdin, daemon changes or command-supplied environment."""
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _command(runner, argv, timeout=15) -> str:
    result = runner(argv, timeout=timeout)
    if (
        result.returncode != 0
        or type(result.stdout) is not str
        or len(result.stdout.encode("utf-8")) > MAX_COMMAND_OUTPUT
    ):
        raise ValueError("command_failed")
    return result.stdout.strip()


def _file_info(path: Path, *, size=None):
    # Do not follow an attacker-controlled ancestor or read device/FIFO files.
    if path.resolve() != path:
        raise ValueError("model_path_unsafe")
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid not in {0, os.geteuid()}
        or info.st_mode & 0o022
        or not 0 < info.st_size <= 32 * 1024**3
        or (size is not None and info.st_size != size)
    ):
        raise ValueError("model_file_unsafe")
    return info


def _ollama_files(directory: Path, pins: dict[str, str]) -> dict:
    """Hash small pinned manifests; check all referenced artifacts and sizes."""
    from scripts.prepare_search_models import verified_bytes
    from search.serialization import strict_json

    files = {}
    for model, expected in pins.items():
        relative = Path("manifests/registry.ollama.ai/library") / model.replace(
            ":", "/"
        )
        manifest = directory / relative
        info = _file_info(manifest)
        if info.st_size > MAX_MANIFEST_BYTES:
            raise ValueError("model_manifest_size")
        raw = verified_bytes(manifest, expected, max_bytes=MAX_MANIFEST_BYTES)
        value = strict_json(raw)
        if (
            type(value) is not dict
            or type(value.get("schemaVersion")) is not int
            or value["schemaVersion"] != 2
            or type(value.get("config")) is not dict
            or type(value.get("layers")) is not list
            or not 1 <= len(value["layers"]) <= 16
        ):
            raise ValueError("model_manifest_schema")
        files[str(relative)] = {"sha256": expected, "bytes": info.st_size}
        for descriptor in [value["config"], *value["layers"]]:
            if type(descriptor) is not dict:
                raise ValueError("model_descriptor_invalid")
            digest, count = descriptor.get("digest"), descriptor.get("size")
            if (
                type(digest) is not str
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                or type(count) is not int
                or not 0 < count <= 32 * 1024**3
            ):
                raise ValueError("model_descriptor_invalid")
            name = "blobs/" + digest.replace(":", "-")
            record = {"sha256": digest[7:], "bytes": count}
            if name in files and files[name] != record:
                raise ValueError("model_descriptor_conflict")
            _file_info(directory / name, size=count)
            files[name] = record
    return files


def _api_models(directory: Path, deep: bool) -> None:
    from scripts.prepare_search_models import checked_file, plan

    # Production pin authority remains the adapters used by the existing helper.
    files = plan(directory)
    for relative, item in files.items():
        _file_info(directory / relative, size=item["bytes"])
        if deep:
            checked_file(directory / relative, item["sha256"], item["bytes"])


def _embedding_model(directory: Path, deep: bool) -> None:
    from scripts.prepare_search_models import checked_file
    from search.catalog.embedding import MODEL, MODEL_DIGEST

    files = _ollama_files(directory, {MODEL: MODEL_DIGEST})
    if deep:
        for relative, item in files.items():
            checked_file(directory / relative, item["sha256"], item["bytes"])


def _reranker_model(directory: Path, deep: bool) -> None:
    from scripts.prepare_search_models import checked_file
    from search.reranker.model import MODEL_HASHES

    for name, expected in MODEL_HASHES.items():
        path = directory / name
        info = _file_info(path)
        if name != "model.safetensors":
            if info.st_size > 32 * 1024**2:
                raise ValueError("reranker_config_size")
            checked_file(path, expected, info.st_size)
        elif deep:
            checked_file(path, expected, info.st_size)


def _hardware_gpus(raw: str, config: ProductConfig) -> list[dict]:
    inventory, records = [], []
    roles = {
        config.gpus.scope: "scope",
        config.gpus.planner[0]: "planner",
        config.gpus.planner[1]: "planner",
        config.gpus.reranker: "reranker",
    }
    for line in raw.splitlines():
        if not line.strip():
            continue
        fields = [part.strip() for part in line.split(",")]
        if (
            len(fields) != 5
            or not fields[3].isdigit()
            or not 0 < int(fields[3]) <= 1024 * 1024
            or re.fullmatch(r"[A-Za-z0-9 ._+()-]{1,100}", fields[2]) is None
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", fields[4]) is None
        ):
            raise ValueError("gpu_hardware_invalid")
        identifier = "GPU-" + fields[0][4:].lower()
        inventory.append(f"{fields[0]},{fields[1]}")
        records.append(
            {
                "uuid": identifier,
                "index": int(fields[1]),
                "name": fields[2],
                "memory_mib": int(fields[3]),
                "driver_version": fields[4],
                "role": roles.get(identifier, "unassigned"),
            }
        )
    validate_gpu_inventory(config, "\n".join(inventory))
    return records


def _storage(runner, path: Path, *, writable: bool) -> dict:
    if not os.access(path, os.R_OK | os.X_OK | (os.W_OK if writable else 0)):
        raise ValueError("storage_not_accessible")
    raw = _command(
        runner,
        ["findmnt", "--json", "--target", str(path), "--output", "FSTYPE,OPTIONS"],
    )
    value = json.loads(raw)
    mounts = value.get("filesystems")
    if type(mounts) is not list or len(mounts) != 1:
        raise ValueError("storage_mount_unknown")
    filesystem, options = mounts[0].get("fstype"), mounts[0].get("options")
    if (
        type(filesystem) is not str
        or re.fullmatch(r"[A-Za-z0-9._+-]{1,32}", filesystem) is None
        or type(options) is not str
        or (writable and "rw" not in options.split(","))
    ):
        raise ValueError("storage_mount_unavailable")
    capacity = os.statvfs(path)
    return {
        "filesystem": filesystem,
        "available_bytes": capacity.f_bavail * capacity.f_frsize,
    }


def _probe(runner, path: Path, uid: int) -> None:
    _command(
        runner,
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--user",
            f"{uid}:{os.getegid()}",
            "--memory=128m",
            "--cpus=1",
            "--pids-limit=32",
            "--env",
            "NVIDIA_VISIBLE_DEVICES=void",
            "--mount",
            f"type=bind,source={path},target=/probe,readonly",
            PROBE_IMAGE,
            "python",
            "-B",
            "-c",
            "import os; assert os.path.isdir('/probe'); os.listdir('/probe')",
        ],
        timeout=30,
    )


def run_doctor(
    config: ProductConfig,
    *,
    repo_root: Path = ROOT,
    runner=None,
    deep_verify: bool = False,
    uid: int | None = None,
) -> dict:
    """Return sanitized readiness evidence; success does not deploy anything.

    Light mode hashes only manifests and small reranker configuration/code;
    the multi-GB weight files are hashed only when deep_verify is explicit.
    This is a prerequisites check, not an inference or GPU-performance test.
    """
    runner = subprocess_runner if runner is None else runner
    uid = os.geteuid() if uid is None else uid
    report = {
        "schema_version": 1,
        "status": "blocked",
        "source_revision": "unknown",
        "verification": "deep" if deep_verify else "light",
        "hardware": {"cpu_count": os.cpu_count(), "ram_bytes": None, "gpus": []},
        "checks": [],
    }
    try:
        report["hardware"]["ram_bytes"] = os.sysconf("SC_PHYS_PAGES") * os.sysconf(
            "SC_PAGE_SIZE"
        )
    except (OSError, ValueError):
        pass

    def check(name, action, success="ok"):
        try:
            action()
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            report["checks"].append(
                {"name": name, "status": "blocked", "code": f"{name}_failed"}
            )
            return False
        report["checks"].append({"name": name, "status": "ok", "code": success})
        return True

    if uid == 0:
        report["checks"].append(
            {"name": "operator", "status": "blocked", "code": "non_root_required"}
        )
        return report
    hostname = socket.gethostname()
    report["hardware"]["hostname"] = (
        hostname if re.fullmatch(r"[A-Za-z0-9._-]{1,253}", hostname) else "unknown"
    )
    report["hardware"]["uid"] = uid
    report["checks"].append({"name": "operator", "status": "ok", "code": "ok"})
    if not check(
        "configuration",
        lambda: validate_config(config.to_dict(), repo_root=repo_root, uid=uid),
    ):
        return report

    def source():
        report["source_revision"] = require_clean_revision(repo_root, runner=runner)

    check("source", source)

    def docker():
        server = json.loads(
            _command(runner, ["docker", "version", "--format", "{{json .Server}}"])
        )
        if type(server) is not dict or type(server.get("Version")) is not str:
            raise ValueError("docker_server_missing")

    docker_ok = check("docker", docker)

    def nvidia_runtime():
        runtimes = json.loads(
            _command(runner, ["docker", "info", "--format", "{{json .Runtimes}}"])
        )
        if type(runtimes) is not dict or type(runtimes.get("nvidia")) is not dict:
            raise ValueError("nvidia_runtime_missing")

    if docker_ok:
        check("nvidia_runtime", nvidia_runtime, "registered_not_exercised")

    def compose():
        version = _command(runner, ["docker", "compose", "version", "--short"])
        match = re.fullmatch(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)", version)
        if match is None or tuple(map(int, match.groups())) < (2, 38, 0):
            raise ValueError("compose_version_unsupported")

    check("compose", compose)

    def gpu():
        raw = _command(
            runner,
            [
                "nvidia-smi",
                "--query-gpu=uuid,index,name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
        )
        report["hardware"]["gpus"] = _hardware_gpus(raw, config)

    check("gpus", gpu)

    for name, action in (
        ("api_models", lambda: _api_models(config.api_model_root, deep_verify)),
        (
            "embedding_model",
            lambda: _embedding_model(config.embedding_model_root, deep_verify),
        ),
        (
            "reranker_model",
            lambda: _reranker_model(config.reranker_model_root, deep_verify),
        ),
    ):
        check(name, action)

    # Never mount a broad ancestor just because state has not been initialized.
    roots = [
        ("api_model_storage", config.api_model_root, False),
        ("embedding_model_storage", config.embedding_model_root, False),
        ("reranker_model_storage", config.reranker_model_root, False),
        (
            "state_storage",
            config.state_root
            if config.state_root.exists()
            else config.state_root.parent,
            True,
        ),
    ]
    for name, path, writable in roots:
        details = {}

        def storage(path=path, writable=writable, details=details):
            details.update(_storage(runner, path, writable=writable))

        if check(name, storage):
            report["checks"][-1]["storage"] = details
    if docker_ok:

        def probe_image():
            identifier = _command(
                runner,
                ["docker", "image", "inspect", PROBE_IMAGE, "--format", "{{.Id}}"],
            )
            if re.fullmatch(r"sha256:[0-9a-f]{64}", identifier) is None:
                raise ValueError("probe_image_invalid")

        image_ok = check("storage_probe_image", probe_image)
        if image_ok:
            probe_roots = roots[:3]
            if config.state_root.exists():
                probe_roots.append(("state_storage", config.state_root, True))
            for name, path, _writable in probe_roots:
                check(f"{name}_docker", lambda path=path: _probe(runner, path, uid))
    if all(item["status"] == "ok" for item in report["checks"]):
        report["status"] = "ok"
    return report
