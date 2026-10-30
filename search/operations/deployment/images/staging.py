"""Product-lock adapter for private source staging and four immutable images.

The lifecycle caller retains its product lock and operation receipt throughout
this call. This module does not nest product operations, activate releases,
start services, publish images or adopt legacy build artifacts. A crash/partial
source intent requires operator investigation; it is never removed or retried.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

from search.operations.config import ProductConfig
from search.operations.deployment import source_bundle
from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision
from search.operations.state import StateStore
from . import build


MAX_SOURCE_SLOTS = 16
SLOT = re.compile(r"source-[0-9a-f]{40}\Z")
RECORD = re.compile(
    r"source-[0-9a-f]{40}\.(stage-started|stage-complete|image-complete)\.json\Z"
)


class ImageStagingError(ValueError):
    """Fixed error codes without credentials, source bytes or command output."""


def _digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def _equal(actual, expected, error):
    try:
        same = private_io.canonical(actual) == private_io.canonical(expected)
    except (ValueError, TypeError, RecursionError, OverflowError):
        same = False
    if not same:
        raise ImageStagingError(error)


def _clean_revision(repo_root, source_revision, runner):
    if require_clean_revision(repo_root, runner=runner) != source_revision:
        raise ImageStagingError("image_staging_invocation_source_changed")


def _identity(plan, slot):
    return {
        "schema_version": 1,
        "revision": plan["revision"],
        "staged_root": str(slot),
        "source_manifest_sha256": _digest(plan),
        "source_map_sha256": plan["source_map_sha256"],
    }


def _inventory(descriptor):
    names = set(os.listdir(descriptor))
    if any(
        SLOT.fullmatch(name) is None and RECORD.fullmatch(name) is None
        for name in names
    ):
        raise ImageStagingError("image_staging_unknown_state")
    identities = {name[:47] for name in names}
    if len(identities) > MAX_SOURCE_SLOTS:
        raise ImageStagingError("image_staging_retention_limit")
    return names, identities


def _source_stage(
    parent_fd, parent, plan, *, repo_root, source_runner, source_revision
):
    name = "source-" + source_revision
    slot = parent / name
    identity = _identity(plan, slot)
    started = name + ".stage-started.json"
    complete = name + ".stage-complete.json"
    image_complete = name + ".image-complete.json"
    names, identities = _inventory(parent_fd)
    associated = {name, started, complete, image_complete} & names
    reused = bool(associated)
    if reused:
        if not {name, started, complete} <= associated:
            raise ImageStagingError("image_staging_partial_source_state")
        _equal(
            build._record(parent_fd, started),
            identity,
            "image_staging_source_intent_changed",
        )
        _equal(
            build._record(parent_fd, complete),
            {**identity, "status": "source_staged"},
            "image_staging_source_receipt_changed",
        )
        root_fd = private_io.open_child_directory(parent_fd, name)
        try:
            _equal(
                build._record(root_fd, "manifest.json"),
                plan,
                "image_staging_committed_manifest_changed",
            )
        finally:
            os.close(root_fd)
    else:
        if len(identities) >= MAX_SOURCE_SLOTS:
            raise ImageStagingError("image_staging_retention_limit")
        private_io.verify_directory(parent_fd, parent)
        private_io.write_json(
            parent_fd, started, identity, max_bytes=build.MAX_RECORD_BYTES
        )
        private_io.verify_directory(parent_fd, parent)
        staged = source_bundle.stage_source_bundle(
            repo_root, source_revision, slot, runner=source_runner
        )
        _equal(staged, plan, "image_staging_committed_source_changed")
        private_io.verify_directory(parent_fd, parent)
        private_io.write_json(
            parent_fd,
            complete,
            {**identity, "status": "source_staged"},
            max_bytes=build.MAX_RECORD_BYTES,
        )
    private_io.verify_directory(parent_fd, parent)
    return slot, identity, reused


def _image_result(plan, result):
    if type(result) is not dict or type(result.get("images")) is not dict:
        raise ImageStagingError("image_staging_builder_result_invalid")
    images = result["images"]
    if set(images) != set(build.SERVICES):
        raise ImageStagingError("image_staging_builder_result_invalid")
    ids = set()
    for service in build.SERVICES:
        proof = images[service]
        if type(proof) is not dict:
            raise ImageStagingError("image_staging_image_proof_invalid")
        image_id = proof.get("image_id")
        environment_digest = proof.get("image_environment_sha256")
        if (
            type(image_id) is not str
            or source_bundle.IMAGE_ID.fullmatch(image_id) is None
            or type(environment_digest) is not str
            or source_bundle.DIGEST.fullmatch(environment_digest) is None
        ):
            raise ImageStagingError("image_staging_image_proof_invalid")
        _equal(
            proof,
            {
                "schema_version": 1,
                "revision": plan["revision"],
                "service": service,
                "source_sha256": plan["services"][service]["source_sha256"],
                "image_id": image_id,
                "image_environment_sha256": environment_digest,
            },
            "image_staging_image_proof_changed",
        )
        ids.add(image_id)
    if len(ids) != len(build.SERVICES):
        raise ImageStagingError("image_staging_image_identity_overlap")
    _equal(
        result,
        {
            "schema_version": 1,
            "revision": plan["revision"],
            "images": images,
            "services_started": False,
            "registry_published": False,
        },
        "image_staging_builder_result_changed",
    )
    return images


def stage_product_images(
    config,
    store,
    source_revision,
    *,
    repo_root,
    source_runner=None,
    docker_runner=None,
):
    """Stage/build or freshly reverify one exact source SHA's private images.

    The caller must hold ``locked_state(config)`` and its outer operation
    receipt. Source SHA is independently checked against the clean checkout;
    existing stage receipts cannot attest their own manifest. Source-runner
    injection obeys Git bounds; Docker-runner injection obeys the existing
    builder's timeout/capture contract. Neither runner is a user API input.

    Completed roles may be reused only through the builder's real image/source
    reinspection. Ambiguous source or image started state is never retried or
    deleted. Successful image-candidate publication is not runtime readiness,
    model validation, activation or public website deployment.
    """
    if (
        not isinstance(config, ProductConfig)
        or not isinstance(store, StateStore)
        or store.root != config.state_root
    ):
        raise ImageStagingError("image_staging_product_state_invalid")
    source_bundle._revision(source_revision)
    repo_root = Path(repo_root)
    parent = config.state_root / "image-builds"
    parent_fd = None
    try:
        store._check_directory()
        _clean_revision(repo_root, source_revision, source_runner)
        plan = source_bundle.committed_source_plan(
            repo_root, source_revision, runner=source_runner
        )
        _clean_revision(repo_root, source_revision, source_runner)
        store._check_directory()
        parent_fd = private_io.open_child_directory(
            store.fd, "image-builds", create="image-builds" not in os.listdir(store.fd)
        )
        slot, identity, reused = _source_stage(
            parent_fd,
            parent,
            plan,
            repo_root=repo_root,
            source_runner=source_runner,
            source_revision=source_revision,
        )
        _clean_revision(repo_root, source_revision, source_runner)
        store._check_directory()
        private_io.verify_directory(parent_fd, parent)
        result = build.build_product_images(
            slot,
            source_revision,
            runner=docker_runner,
            expected_manifest_sha256=identity["source_manifest_sha256"],
        )
        images = _image_result(plan, result)
        _clean_revision(repo_root, source_revision, source_runner)
        store._check_directory()
        private_io.verify_directory(parent_fd, parent)
        candidate = {
            **identity,
            "status": "images_staged",
            "images": images,
            "images_sha256": _digest(images),
            "services_started": False,
            "registry_published": False,
            "active_changed": False,
        }
        complete = slot.name + ".image-complete.json"
        if complete in os.listdir(parent_fd):
            _equal(
                build._record(parent_fd, complete),
                candidate,
                "image_staging_completed_images_changed",
            )
        else:
            private_io.write_json(
                parent_fd, complete, candidate, max_bytes=build.MAX_RECORD_BYTES
            )
        private_io.verify_directory(parent_fd, parent)
        # Recheck persisted source and image receipts before publishing the
        # product pointer; a later role/runner must not silently alter evidence.
        _equal(
            build._record(parent_fd, slot.name + ".stage-started.json"),
            identity,
            "image_staging_source_intent_changed",
        )
        _equal(
            build._record(parent_fd, slot.name + ".stage-complete.json"),
            {**identity, "status": "source_staged"},
            "image_staging_source_receipt_changed",
        )
        _equal(
            build._record(parent_fd, complete),
            candidate,
            "image_staging_completed_images_changed",
        )
        _inventory(parent_fd)
        store._check_directory()
        store.write("image-candidate.json", candidate, replace=True)
        store._check_directory()
        return {**candidate, "source_staging_reused": reused, "runtime_verified": False}
    except (OSError, ValueError) as error:
        if isinstance(
            error,
            (
                ImageStagingError,
                build.ImageBuildError,
                source_bundle.SourceBundleError,
            ),
        ):
            raise
        raise ImageStagingError("image_staging_state_unsafe") from None
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
