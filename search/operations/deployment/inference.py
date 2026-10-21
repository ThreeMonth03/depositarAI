"""Start or freshly prove one source-bound, private GPU inference plane.

The caller holds the product lock and outer operation receipt. There is no
automatic adoption, recovery, recreation, model download, public port, release
activation or generated-answer warm-up here. An uncertain start is quarantined
by its durable intent; the spawned Compose client may fail while daemon work
continues, so client termination is not proof of daemon cancellation.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import copy
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from scripts import prepare_search_models, search_model_source
from search.operations import doctor
from search.operations.config import ProductConfig
from search.operations.deployment import (
    composition,
    gpu_ownership,
    image_authority,
    image_build,
    inference_runtime,
    source_bundle,
    runtime_docker,
)
from search.operations.deployment.runtime_docker import (
    InferenceError,
)
from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision
from search.operations.state import StateStore


MAX_SLOTS = 16
MAX_MODEL_FILES = 32
MAX_MODEL_BYTES = 64 * 1024**3
SLOT = re.compile(r"inference-[0-9a-f]{12}\Z")


def _digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def _equal(actual, expected, code):
    try:
        same = private_io.canonical(actual) == private_io.canonical(expected)
    except (ValueError, TypeError, RecursionError, OverflowError):
        same = False
    if not same:
        raise InferenceError(code)


def _source_revision(repo_root, revision, runner):
    if require_clean_revision(repo_root, runner=runner) != revision:
        raise InferenceError("inference_invocation_source_changed")


def _images(docker, source_plan, authority):
    proof = authority["images"]["reranker"]
    reranker = docker.inspect(proof["image_id"])
    fresh = source_bundle.validate_compatible_built_image(
        source_plan, "reranker", authority["revision"], proof["image_id"], reranker
    )
    tagged = source_bundle.validate_compatible_built_image(
        source_plan,
        "reranker",
        authority["revision"],
        proof["image_id"],
        docker.inspect(image_build.TAGS["reranker"] + authority["revision"]),
    )
    _equal(fresh, proof, "inference_reranker_image_changed")
    _equal(tagged, proof, "inference_reranker_tag_changed")
    ollama = docker.inspect(composition.OLLAMA_IMAGE)
    image_id = ollama.get("Id")
    if (
        type(image_id) is not str
        or source_bundle.IMAGE_ID.fullmatch(image_id) is None
        or type(ollama.get("Config")) is not dict
        or ollama.get("Os") != "linux"
        or ollama.get("Architecture") != "amd64"
        or "ollama/ollama@" + composition.OLLAMA_IMAGE.split("@", 1)[1]
        not in (ollama.get("RepoDigests") or [])
    ):
        raise InferenceError("inference_ollama_registry_identity_changed")
    immutable = docker.inspect(image_id)
    _equal(immutable, ollama, "inference_ollama_immutable_identity_changed")
    return {"scope-model": ollama, "answer-model": ollama, "reranker": reranker}


def _authority(config, store, source_plan, compose):
    """Select only a sealed v2 slot's original build, never adopt live labels."""
    if "inference" not in os.listdir(store.fd):
        return image_authority.read_completed_images(config, store, source_plan)
    descriptor = private_io.open_child_directory(store.fd, "inference")
    try:
        names = set(os.listdir(descriptor))
        if len(names) > MAX_SLOTS or any(
            SLOT.fullmatch(name) is None for name in names
        ):
            raise InferenceError("inference_unknown_state")
        name = compose["name"].removeprefix("depositar-ai-")
        if name not in names:
            # Old app-SHA slots have a different identity; their persisted
            # evidence cannot silently grant authority over an existing plane.
            for existing in names:
                child = private_io.open_child_directory(descriptor, existing)
                try:
                    if "started.json" in os.listdir(child):
                        started = image_build._record(child, "started.json")
                        if type(started) is dict and started.get("schema_version") == 1:
                            raise InferenceError(
                                "inference_legacy_state_requires_transition"
                            )
                finally:
                    os.close(child)
            return image_authority.read_completed_images(config, store, source_plan)
        child = private_io.open_child_directory(descriptor, name)
        try:
            if set(os.listdir(child)) != {
                "started.json",
                "compose.json",
                "complete.json",
            }:
                raise InferenceError("inference_partial_state_recovery_required")
            started = image_build._record(child, "started.json")
            if (
                type(started) is not dict
                or type(started.get("schema_version")) is not int
                or started.get("schema_version") != 2
                or started.get("inference_source_sha256")
                != source_plan["services"]["reranker"]["source_sha256"]
            ):
                raise InferenceError("inference_start_identity_changed")
            _, authority = image_authority.read_completed_images_at_revision(
                config, store, source_plan, started["revision"]
            )
            return authority
        finally:
            os.close(child)
    finally:
        os.close(descriptor)


def _refresh_authority(config, store, source_plan, authority):
    if authority["revision"] == source_plan["revision"]:
        return image_authority.read_completed_images(config, store, source_plan)
    return image_authority.read_completed_images_at_revision(
        config, store, source_plan, authority["revision"]
    )[1]


def _model_plan(config):
    from search.reranker.model import MODEL_HASHES

    api = doctor._ollama_files(config.api_model_root, prepare_search_models.PINS)
    reranker = {}
    for name, expected in MODEL_HASHES.items():
        info = doctor._file_info(config.reranker_model_root / name)
        if name != "model.safetensors" and info.st_size > 32 * 1024**2:
            raise InferenceError("inference_reranker_config_limit")
        reranker[name] = {"sha256": expected, "bytes": info.st_size}
    plans = {"api": api, "reranker": reranker}
    if (
        sum(map(len, plans.values())) > MAX_MODEL_FILES
        or sum(item["bytes"] for files in plans.values() for item in files.values())
        > MAX_MODEL_BYTES
    ):
        raise InferenceError("inference_model_inventory_limit")
    return plans


@contextmanager
def _verified_models(config, *, deadline=None, include_embedding=False):
    """Deep verify fixed pin authority via retained no-follow ancestry/file FDs."""
    with ExitStack() as stack:
        roots = {
            "api": stack.enter_context(
                search_model_source.open_source_root(config.api_model_root)
            ),
            "reranker": stack.enter_context(
                search_model_source.open_source_root(config.reranker_model_root)
            ),
        }
        plans = _model_plan(config)
        if include_embedding:
            from search.catalog.embedding import MODEL, MODEL_DIGEST

            roots["embedding"] = stack.enter_context(
                search_model_source.open_source_root(config.embedding_model_root)
            )
            plans["embedding"] = doctor._ollama_files(
                config.embedding_model_root, {MODEL: MODEL_DIGEST}
            )
        if (
            sum(map(len, plans.values())) > MAX_MODEL_FILES
            or sum(item["bytes"] for files in plans.values() for item in files.values())
            > MAX_MODEL_BYTES
        ):
            raise InferenceError("inference_model_inventory_limit")
        records = []
        for role, files in plans.items():
            root = roots[role]
            for relative, expected in sorted(files.items()):
                parent, name = root.relative_parent(relative)
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                search_model_source._source_file_policy(
                    named, root.uid, expected["bytes"]
                )
                descriptor = os.open(
                    name, search_model_source.READ_FLAGS, dir_fd=parent
                )
                stack.callback(os.close, descriptor)
                opened = os.fstat(descriptor)
                search_model_source._source_file_policy(
                    opened, root.uid, expected["bytes"]
                )
                if search_model_source._file_identity(
                    opened
                ) != search_model_source._file_identity(named):
                    raise InferenceError("inference_model_file_changed")
                digest, size = hashlib.sha256(), 0
                while size <= expected["bytes"]:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise InferenceError("inference_model_verification_deadline")
                    block = os.read(
                        descriptor,
                        min(
                            search_model_source.CHUNK_BYTES,
                            expected["bytes"] + 1 - size,
                        ),
                    )
                    if not block:
                        break
                    size += len(block)
                    digest.update(block)
                if (
                    size != expected["bytes"]
                    or digest.hexdigest() != expected["sha256"]
                ):
                    raise InferenceError("inference_model_digest_changed")
                records.append(
                    (
                        descriptor,
                        parent,
                        name,
                        search_model_source._file_identity(opened),
                    )
                )

        def verify():
            for root in roots.values():
                root.verify()
            for descriptor, parent, name, expected in records:
                if (
                    search_model_source._file_identity(os.fstat(descriptor)) != expected
                    or search_model_source._file_identity(
                        os.stat(name, dir_fd=parent, follow_symlinks=False)
                    )
                    != expected
                ):
                    raise InferenceError("inference_model_file_changed")

        verify()
        yield plans, verify
        verify()


def _inspect_runtime(docker, compose, images):
    ids, names = runtime_docker.project_inventory(docker, compose["name"])
    expected = {entry["name"] for entry in compose["networks"].values()}
    if len(ids) != 3 or names != expected:
        raise InferenceError("inference_runtime_inventory_changed")
    containers = image_build._json(docker.call(["inspect", *sorted(ids)]))
    networks = image_build._json(docker.call(["network", "inspect", *sorted(names)]))
    return inference_runtime.validate_runtime(compose, images, containers, networks)


def _gpu(config, allowed, runner):
    result = gpu_ownership.run_gpu_ownership(config, allowed=allowed, runner=runner)
    if (
        type(result) is not dict
        or result.get("schema_version") != 1
        or type(result.get("schema_version")) is not int
        or result.get("status") != "ok"
        or result.get("code") != "gpu_ownership_verified"
        or result.get("snapshot_only") is not True
        or result.get("exclusive_reservation") is not False
        or result.get("runtime_modified") is not False
        or type(result.get("sealed_gpu_container_count")) is not int
        or result.get("sealed_gpu_container_count") != len(allowed)
        or type(result.get("gpu_count")) is not int
        or result.get("gpu_count") != 4
    ):
        raise InferenceError("inference_gpu_ownership_blocked")


def _runtime_identity(compose, model_plan, authority, images):
    return {
        "schema_version": 2,
        # Preserve original build provenance across unrelated application
        # commits. No new app revision is ever attached to an existing image.
        "revision": authority["revision"],
        "inference_source_sha256": compose["x-depositar-plan"][
            "inference_source_sha256"
        ],
        "project": compose["name"],
        "compose_sha256": _digest(compose),
        "model_manifest_sha256": _digest(model_plan),
        "source_manifest_sha256": authority["source_manifest_sha256"],
        "image_ids": {name: value["Id"] for name, value in images.items()},
        "image_config_sha256": {
            name: _digest(value["Config"]) for name, value in images.items()
        },
        "active_changed": False,
    }


def start_private_inference(
    config,
    store,
    source_revision,
    *,
    repo_root,
    source_runner=None,
    docker_runner=None,
    gpu_runner=None,
):
    """Start exactly scope/planner/reranker, or freshly prove sealed reuse.

    ``locked_state(config)`` and an outer durable operation are caller duties.
    Fresh starts require an empty exact project/network slot and globally free
    configured GPUs. Completed reuse verifies all running IDs, model bytes,
    immutable images, resource/mount policies and host GPU PID ownership again.
    No missing image is pulled or built. There is no blind retry after a partial
    start and no automatic stop/delete or release-client network adoption.
    """
    parent_fd = slot_fd = None
    deadline = time.monotonic() + runtime_docker.TOTAL_SECONDS
    try:
        if (
            not isinstance(config, ProductConfig)
            or not isinstance(store, StateStore)
            or store.root != config.state_root
            or os.geteuid() == 0
        ):
            raise InferenceError("inference_product_state_invalid")
        composition._configuration(config)
        source_bundle._revision(source_revision)
        repo_root = Path(repo_root)
        _source_revision(repo_root, source_revision, source_runner)
        source_plan = source_bundle.committed_source_plan(
            repo_root, source_revision, runner=source_runner
        )
        compose = composition._inference(
            config,
            source_revision,
            os.geteuid(),
            os.getegid(),
            inference_source_sha256=source_plan["services"]["reranker"][
                "source_sha256"
            ],
        )
        authority = _authority(config, store, source_plan, compose)
        with (
            _verified_models(config, deadline=deadline) as (model_plan, verify_models),
            tempfile.TemporaryDirectory(
                prefix="depositar-inference-client-"
            ) as temporary,
        ):
            client = Path(temporary)
            client_fd = private_io.open_directory(client)
            try:
                if os.listdir(client_fd):
                    raise InferenceError("inference_client_config_not_empty")
                docker = runtime_docker.Docker(client, docker_runner, deadline=deadline)
                if (
                    image_build._json(
                        docker.call(["info", "--format", "{{json .OSType}}"])
                    )
                    != "linux"
                ):
                    raise InferenceError("inference_daemon_unsupported")
                images = _images(docker, source_plan, authority)
                compose = copy.deepcopy(compose)
                for name, image in images.items():
                    compose["services"][name]["image"] = image["Id"]
                if b"$" in private_io.canonical(compose):
                    raise InferenceError("inference_compose_interpolation_forbidden")
                identity = _runtime_identity(compose, model_plan, authority, images)
                parent = config.state_root / "inference"
                store._check_directory()
                parent_fd = private_io.open_child_directory(
                    store.fd,
                    "inference",
                    create="inference" not in os.listdir(store.fd),
                )
                names = set(os.listdir(parent_fd))
                if (
                    any(SLOT.fullmatch(name) is None for name in names)
                    or len(names) > MAX_SLOTS
                ):
                    raise InferenceError("inference_unknown_state")
                name = compose["name"].removeprefix("depositar-ai-")
                reused = name in names
                if not reused and len(names) >= MAX_SLOTS:
                    raise InferenceError("inference_retention_limit")
                slot = parent / name
                if reused:
                    slot_fd = private_io.open_child_directory(parent_fd, name)
                    if set(os.listdir(slot_fd)) != {
                        "started.json",
                        "compose.json",
                        "complete.json",
                    }:
                        raise InferenceError(
                            "inference_partial_state_recovery_required"
                        )
                    _equal(
                        image_build._record(slot_fd, "started.json"),
                        identity,
                        "inference_start_identity_changed",
                    )
                    _equal(
                        image_build._record(slot_fd, "compose.json"),
                        compose,
                        "inference_compose_changed",
                    )
                    complete = image_build._record(slot_fd, "complete.json")
                else:
                    ids, network_names = runtime_docker.project_inventory(
                        docker, compose["name"]
                    )
                    if ids or network_names:
                        raise InferenceError("inference_legacy_runtime_not_adopted")
                    _gpu(config, (), gpu_runner)
                    if any(runtime_docker.project_inventory(docker, compose["name"])):
                        raise InferenceError("inference_legacy_runtime_not_adopted")
                    private_io.verify_directory(parent_fd, parent)
                    slot_fd = private_io.open_child_directory(
                        parent_fd, name, create=True
                    )
                    private_io.write_json(
                        slot_fd,
                        "started.json",
                        identity,
                        max_bytes=image_build.MAX_RECORD_BYTES,
                    )
                    private_io.write_json(
                        slot_fd,
                        "compose.json",
                        compose,
                        max_bytes=image_build.MAX_RECORD_BYTES,
                    )
                    _source_revision(repo_root, source_revision, source_runner)
                    _equal(
                        image_authority.read_completed_images(
                            config, store, source_plan
                        ),
                        authority,
                        "inference_source_authority_changed",
                    )
                    _equal(
                        _images(docker, source_plan, authority),
                        images,
                        "inference_images_changed",
                    )
                    verify_models()
                    private_io.verify_directory(slot_fd, slot)
                    prefix = [
                        "compose",
                        "--env-file",
                        "/dev/null",
                        "--project-directory",
                        str(slot),
                        "--project-name",
                        compose["name"],
                        "--file",
                        str(slot / "compose.json"),
                    ]
                    docker.call([*prefix, "config", "--quiet"])
                    _equal(
                        image_build._record(slot_fd, "compose.json"),
                        compose,
                        "inference_compose_changed",
                    )
                    _equal(
                        image_build._record(slot_fd, "started.json"),
                        identity,
                        "inference_start_identity_changed",
                    )
                    _gpu(config, (), gpu_runner)
                    if any(runtime_docker.project_inventory(docker, compose["name"])):
                        raise InferenceError("inference_legacy_runtime_not_adopted")
                    private_io.verify_directory(parent_fd, parent)
                    private_io.verify_directory(slot_fd, slot)
                    docker.call(
                        [
                            *prefix,
                            "up",
                            "--detach",
                            "--no-build",
                            "--pull",
                            "never",
                            "--wait",
                            "--wait-timeout",
                            "600",
                            *sorted(inference_runtime.SERVICES),
                        ],
                        timeout=660,
                    )
                    complete = None
                proof = _inspect_runtime(docker, compose, images)
                result = {
                    **identity,
                    "status": "runtime_running",
                    **proof,
                    "runtime_verified": True,
                    "models_verified": True,
                    "gpu_ownership_snapshot_verified": True,
                    "model_warmup_verified": False,
                    "website_started": False,
                }
                if reused:
                    _equal(complete, result, "inference_completed_runtime_changed")
                _gpu(
                    config,
                    inference_runtime.ownership_proofs(compose, proof, images),
                    gpu_runner,
                )
                _equal(
                    _inspect_runtime(docker, compose, images),
                    proof,
                    "inference_runtime_changed_during_verification",
                )
                _equal(
                    _images(docker, source_plan, authority),
                    images,
                    "inference_images_changed",
                )
                verify_models()
                _source_revision(repo_root, source_revision, source_runner)
                _equal(
                    _refresh_authority(config, store, source_plan, authority),
                    authority,
                    "inference_source_authority_changed",
                )
                store._check_directory()
                private_io.verify_directory(parent_fd, parent)
                private_io.verify_directory(slot_fd, slot)
                _equal(
                    image_build._record(slot_fd, "started.json"),
                    identity,
                    "inference_start_identity_changed",
                )
                _equal(
                    image_build._record(slot_fd, "compose.json"),
                    compose,
                    "inference_compose_changed",
                )
                if reused:
                    _equal(
                        image_build._record(slot_fd, "complete.json"),
                        result,
                        "inference_completed_runtime_changed",
                    )
            finally:
                os.close(client_fd)
        # Every fallible model/ancestry context exit and temporary-client
        # cleanup finishes before a durable success receipt is published.
        if time.monotonic() >= deadline:
            raise InferenceError("inference_operation_deadline")
        store._check_directory()
        private_io.verify_directory(parent_fd, parent)
        private_io.verify_directory(slot_fd, slot)
        _equal(
            image_build._record(slot_fd, "started.json"),
            identity,
            "inference_start_identity_changed",
        )
        _equal(
            image_build._record(slot_fd, "compose.json"),
            compose,
            "inference_compose_changed",
        )
        if reused:
            _equal(
                image_build._record(slot_fd, "complete.json"),
                result,
                "inference_completed_runtime_changed",
            )
        else:
            private_io.write_json(
                slot_fd, "complete.json", result, max_bytes=image_build.MAX_RECORD_BYTES
            )
        store.write("inference-candidate.json", result, replace=True)
        return {**result, "runtime_reused": reused}
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        subprocess.SubprocessError,
    ) as error:
        if isinstance(error, (InferenceError, inference_runtime.InferenceRuntimeError)):
            raise
        raise InferenceError("inference_operation_failed") from None
    finally:
        for descriptor in (slot_fd, parent_fd):
            if descriptor is not None:
                os.close(descriptor)
