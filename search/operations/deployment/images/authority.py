"""Authenticate completed source and image evidence for private operators.

This reader leaves staging, runtime mutation and GPU/model policy to callers.
The caller retains its product lock and independently committed source plan.
"""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import os

from search.operations.deployment import source_bundle
from search.operations.deployment.runtime_docker import InferenceError
from search.operations.shared import private_io
from . import build
from . import staging


def _digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def _equal(actual, expected, code):
    try:
        same = private_io.canonical(actual) == private_io.canonical(expected)
    except (ValueError, TypeError, RecursionError, OverflowError):
        same = False
    if not same:
        raise InferenceError(code)


def read_completed_images(config, store, source_plan):
    """Read closed completed source/image evidence, never attest its own source."""
    return _read_completed_images(config, store, source_plan, candidate_required=True)


def read_completed_images_at_revision(config, store, current_plan, origin_revision):
    """Authenticate an original build whose complete reranker source is current.

    Unrelated historical website recipes need not equal today's recipes. The
    original manifest, staged bytes and all build receipts remain sealed under
    their original revision; only the independently committed reranker closure
    is required to equal today's reviewed recipe and inputs. The global image
    candidate belongs to the new application and is deliberately not replaced.
    """
    current = source_bundle._verified_plan(current_plan)
    source_bundle._revision(origin_revision)
    parent = config.state_root / "image-builds"
    name = "source-" + origin_revision
    with ExitStack() as stack:
        parent_fd = private_io.open_directory(parent)
        stack.callback(os.close, parent_fd)
        staging._inventory(parent_fd)
        root_fd = private_io.open_child_directory(parent_fd, name)
        stack.callback(os.close, root_fd)
        origin = build._record(root_fd, "manifest.json")
        if (
            type(origin) is not dict
            or set(origin) != set(current)
            or type(origin.get("schema_version")) is not int
            or origin.get("schema_version") != 1
            or origin.get("revision") != origin_revision
            or origin.get("status") != "source_plan_only"
            or origin.get("runtime_verified") is not False
            or origin.get("images_built") is not False
            or type(origin.get("files")) is not dict
            or not 1 <= len(origin["files"]) <= source_bundle.MAX_SOURCE_FILES
            or type(origin.get("services")) is not dict
            or set(origin["services"]) != set(build.SERVICES)
        ):
            raise InferenceError("inference_origin_source_invalid")
        for path, record in origin["files"].items():
            source_bundle._path(path)
            source_bundle._record(record)
        total = sum(record["size"] for record in origin["files"].values())
        if (
            total > source_bundle.MAX_TOTAL_BYTES
            or type(origin.get("total_bytes")) is not int
            or origin["total_bytes"] != total
            or origin.get("source_map_sha256") != _digest(origin["files"])
        ):
            raise InferenceError("inference_origin_source_invalid")
        expected = current["services"]["reranker"]
        expected = {
            **expected,
            "build_arguments": {"SOURCE_REVISION": origin_revision},
            "required_labels": {
                **expected["required_labels"],
                "org.opencontainers.image.revision": origin_revision,
                source_bundle.LABEL_PREFIX + "revision": origin_revision,
            },
        }
        _equal(
            origin["services"]["reranker"],
            expected,
            "inference_origin_reranker_source_changed",
        )
        _equal(
            {path: origin["files"].get(path) for path in expected["source_map"]},
            expected["source_map"],
            "inference_origin_reranker_source_changed",
        )
        private_io.verify_directory(root_fd, parent / name)
        private_io.verify_directory(parent_fd, parent)
    return origin, _read_completed_images(
        config, store, origin, candidate_required=False
    )


def _read_completed_images(config, store, source_plan, *, candidate_required):
    revision = source_plan["revision"]
    parent = config.state_root / "image-builds"
    name = "source-" + revision
    slot = parent / name
    identity = staging._identity(source_plan, slot)
    if candidate_required:
        candidate = store.read("image-candidate.json")
    else:
        descriptor = private_io.open_directory(parent)
        try:
            candidate = build._record(descriptor, name + ".image-complete.json")
        finally:
            os.close(descriptor)
    images = staging._image_result(
        source_plan,
        {
            "schema_version": 1,
            "revision": revision,
            "images": candidate.get("images") if type(candidate) is dict else None,
            "services_started": False,
            "registry_published": False,
        },
    )
    expected = {
        **identity,
        "status": "images_staged",
        "images": images,
        "images_sha256": _digest(images),
        "services_started": False,
        "registry_published": False,
        "active_changed": False,
    }
    _equal(
        candidate,
        expected,
        "inference_image_candidate_changed"
        if candidate_required
        else "inference_source_receipt_changed",
    )
    with ExitStack() as stack:
        parent_fd = private_io.open_directory(parent)
        stack.callback(os.close, parent_fd)
        staging._inventory(parent_fd)
        for suffix, value in (
            ("stage-started", identity),
            ("stage-complete", {**identity, "status": "source_staged"}),
            ("image-complete", expected),
        ):
            _equal(
                build._record(parent_fd, name + "." + suffix + ".json"),
                value,
                "inference_source_receipt_changed",
            )
        root_fd = private_io.open_child_directory(parent_fd, name)
        stack.callback(os.close, root_fd)
        context_fd = private_io.open_child_directory(root_fd, "context")
        stack.callback(os.close, context_fd)
        build._verify_source(root_fd, context_fd, slot, source_plan)
        images_fd = private_io.open_child_directory(root_fd, "images")
        stack.callback(os.close, images_fd)
        completed = build._inventory(images_fd, source_plan)
        if set(completed) != set(build.SERVICES):
            raise InferenceError("inference_images_incomplete")
        for service in build.SERVICES:
            _equal(
                completed[service]["image"],
                images[service],
                "inference_built_image_receipt_changed",
            )
        build._verify_source(root_fd, context_fd, slot, source_plan)
        private_io.verify_directory(images_fd, slot / "images")
        private_io.verify_directory(parent_fd, parent)
    return expected
