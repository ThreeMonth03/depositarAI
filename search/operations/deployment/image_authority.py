"""Authenticate completed source and image evidence for private operators.

This reader leaves staging, runtime mutation and GPU/model policy to callers.
The caller retains its product lock and independently committed source plan.
"""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import os

from search.operations.deployment import image_build, image_staging
from search.operations.deployment.runtime_docker import InferenceError
from search.operations.shared import private_io


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
    revision = source_plan["revision"]
    parent = config.state_root / "image-builds"
    name = "source-" + revision
    slot = parent / name
    identity = image_staging._identity(source_plan, slot)
    candidate = store.read("image-candidate.json")
    images = image_staging._image_result(
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
    _equal(candidate, expected, "inference_image_candidate_changed")
    with ExitStack() as stack:
        parent_fd = private_io.open_directory(parent)
        stack.callback(os.close, parent_fd)
        image_staging._inventory(parent_fd)
        for suffix, value in (
            ("stage-started", identity),
            ("stage-complete", {**identity, "status": "source_staged"}),
            ("image-complete", expected),
        ):
            _equal(
                image_build._record(parent_fd, name + "." + suffix + ".json"),
                value,
                "inference_source_receipt_changed",
            )
        root_fd = private_io.open_child_directory(parent_fd, name)
        stack.callback(os.close, root_fd)
        context_fd = private_io.open_child_directory(root_fd, "context")
        stack.callback(os.close, context_fd)
        image_build._verify_source(root_fd, context_fd, slot, source_plan)
        images_fd = private_io.open_child_directory(root_fd, "images")
        stack.callback(os.close, images_fd)
        completed = image_build._inventory(images_fd, source_plan)
        if set(completed) != set(image_build.SERVICES):
            raise InferenceError("inference_images_incomplete")
        for service in image_build.SERVICES:
            _equal(
                completed[service]["image"],
                images[service],
                "inference_built_image_receipt_changed",
            )
        image_build._verify_source(root_fd, context_fd, slot, source_plan)
        private_io.verify_directory(images_fd, slot / "images")
        private_io.verify_directory(parent_fd, parent)
    return expected
