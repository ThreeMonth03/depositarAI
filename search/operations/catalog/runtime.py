"""Startup orchestration for exactly three private CPU catalog services."""

from __future__ import annotations

import copy

from search.operations.catalog import administration, commands, execution, volumes
from search.operations.catalog.administration import admin as execute_admin
from search.operations.catalog.policy import (
    CONTAINER_ID as CONTAINER_ID,
    IMAGE_ID as IMAGE_ID,
    LABEL_PREFIX as LABEL_PREFIX,
    SERVICES,
    RuntimeError as RuntimeError,
)


def _build_services(context):
    services = context["compose"]["services"]
    built = {}
    for name in ("opensearch-search", "fess-search"):
        files = execution.service_sources(name)
        build = services[name]["build"]
        dockerfile = (
            "Dockerfile" if name == "opensearch-search" else build["dockerfile"]
        )
        built[name] = execution.build_image(
            context, name, files, dockerfile, build["args"], network="default"
        )
    reference = services["ollama-embedding"]["image"]
    try:
        image = execution.inspect_image(context, reference)
    except commands.BackendError:
        commands.command_output(
            context["runner"], ["docker", "pull", reference], timeout=600
        )
        image = execution.inspect_image(context, reference)
    if execution.registry_digest(reference) not in (image.get("RepoDigests") or []):
        raise RuntimeError("catalog_ollama_registry_digest_changed")
    built["ollama-embedding"] = {
        "image_id": image["Id"],
        "reference": reference,
        "labels": {},
        "sources": {},
    }
    execution.write_record(
        context["root"],
        "images.json",
        {
            "schema_version": 1,
            "project": context["plan"]["project"],
            "revision": context["plan"]["revision"],
            "images": built,
        },
    )
    return built


def _require_staged_images(context):
    """Reverify both durable staging records and their local immutable images."""
    root = context["root"]
    if not all(
        (root / name).exists()
        for name in ("image-stage-started.json", "image-stage-complete.json")
    ):
        raise RuntimeError("catalog_image_stage_requires_explicit_recovery")
    identity = execution.catalog_identity(context)
    started = execution.read_record(root, "image-stage-started.json")
    if (
        type(started) is not dict
        or type(started.get("schema_version")) is not int
        or started.get("active_changed") is not False
        or started.get("services_started") is not False
        or started != {**identity, "status": "started", "services_started": False}
    ):
        raise RuntimeError("catalog_image_stage_receipt_changed")
    complete = execution.read_record(root, "image-stage-complete.json")
    if (
        type(complete) is not dict
        or type(complete.get("schema_version")) is not int
        or complete.get("active_changed") is not False
        or complete.get("services_started") is not False
    ):
        raise RuntimeError("catalog_image_stage_receipt_changed")
    images = execution.load_images(context)
    admin_image = administration._admin_image(context, read_only=True)
    expected = {
        **identity,
        "status": "images_staged",
        "services_started": False,
        "images": images,
        "admin_image": admin_image,
    }
    if complete != expected:
        raise RuntimeError("catalog_image_stage_receipt_changed")
    return complete


def stage_images(config, plan, *, repo_root, runner=None):
    """Build and seal catalog service/admin images without starting services.

    Only a fresh prepared slot or an exactly completed image stage is accepted.
    Partial stages and older runtime/build evidence need explicit recovery; no
    image is rebuilt or pulled during completed-stage reuse. ``services_started``
    describes this operation and does not report whether services are running.
    """
    context = execution.load_context(config, plan, repo_root=repo_root, runner=runner)
    with execution.operation_lock(context):
        root = context["root"]
        if (root / "image-stage-started.json").exists() or (
            root / "image-stage-complete.json"
        ).exists():
            return _require_staged_images(context)
        evidence = (
            "images.json",
            "admin-image.json",
            "image-lock.json",
            "runtime.json",
            "start-started.json",
            "start-failure.json",
        )
        if (
            any((root / name).exists() for name in evidence)
            or any(root.glob("build-*"))
            or any(root.glob("image-*.iid"))
            or execution.container_ids(context)
            or administration._admin_ids(context)
        ):
            raise RuntimeError("catalog_image_stage_requires_fresh_prepared_slot")
        identity = execution.catalog_identity(context)
        execution.write_record(
            root,
            "image-stage-started.json",
            {**identity, "status": "started", "services_started": False},
        )
        # A failed build intentionally leaves the durable intent. Retrying it
        # implicitly could overwrite image provenance or adopt partial state.
        _build_services(context)
        administration._admin_image(context)
        result = {
            **identity,
            "status": "images_staged",
            "services_started": False,
            "images": execution.load_images(context),
            "admin_image": administration._admin_image(context, read_only=True),
        }
        execution.write_record(root, "image-stage-complete.json", result)
        return result


def start(config, plan, *, repo_root, runner=None, prebuilt_images=None):
    """Start one private CPU slot, optionally from an explicit images receipt.

    The default retains reviewed local builds. A supplied exact-project/source
    ``images.json`` receipt must verify before startup intent and selects only
    local immutable images: no build, pull or fallback is performed. Completed
    runtime reuse must still match that explicit choice and all existing proofs.
    """
    context = execution.load_context(config, plan, repo_root=repo_root, runner=runner)
    runner = context["runner"]
    with execution.operation_lock(context):
        root = context["root"]
        if (root / "start-failure.json").exists():
            raise RuntimeError("catalog_start_is_quarantined")
        images = (
            None
            if prebuilt_images is None
            else execution.verify_images(context, prebuilt_images)
        )
        staged = None
        if (root / "image-stage-started.json").exists() or (
            root / "image-stage-complete.json"
        ).exists():
            staged = _require_staged_images(context)
            if images is not None and images != staged["images"]:
                raise RuntimeError("catalog_prebuilt_images_differ_from_stage")
            images = staged["images"]
        if (root / "runtime.json").exists():
            receipt, running_images = execution.require_runtime(context)
            if images is not None and images != running_images:
                raise RuntimeError("catalog_prebuilt_images_differ_from_runtime")
            return receipt
        if (root / "start-started.json").exists() or execution.container_ids(context):
            raise RuntimeError("catalog_start_requires_explicit_recovery")
        identity = execution.catalog_identity(context)
        execution.write_record(
            root, "start-started.json", {**identity, "status": "started"}
        )
        try:
            if images is None:
                images = _build_services(context)
            elif staged is None:
                execution.write_record(
                    root,
                    "images.json",
                    {
                        "schema_version": 1,
                        "project": plan["project"],
                        "revision": plan["revision"],
                        "images": images,
                    },
                )
            overlay = {
                "services": {
                    name: {"image": entry["image_id"]} for name, entry in images.items()
                }
            }
            execution.write_record(root, "image-lock.json", overlay)
            rendered = execution.run_json(
                runner,
                execution.compose_command(
                    context, "config", "--format", "json", overlay=True
                ),
            )
            expected = copy.deepcopy(context["compose"])
            for name, entry in images.items():
                expected["services"][name]["image"] = entry["image_id"]
            if rendered != expected:
                raise RuntimeError("catalog_image_override_changed_other_policy")
            execution.load_images(
                context
            )  # Repeat immutable ID/source proof immediately before startup.
            if volumes.enabled(context):
                volumes.ensure(context)
            commands.command_output(
                runner,
                execution.compose_command(
                    context,
                    "up",
                    "--detach",
                    "--no-build",
                    "--pull",
                    "never",
                    "--wait",
                    "--wait-timeout",
                    "600",
                    *sorted(SERVICES),
                    overlay=True,
                ),
                timeout=660,
            )
            images = execution.load_images(
                context
            )  # Startup cannot silently accept retagged/replaced images.
            proof = execution.inspect_runtime(context, images)
            receipt = {
                **identity,
                "status": "running",
                **proof,
                "image_ids": {
                    name: entry["image_id"] for name, entry in images.items()
                },
                "image_lock_sha256": execution.digest_record(overlay),
                "images_sha256": execution.digest_record(
                    execution.read_record(root, "images.json")
                ),
            }
            execution.write_record(root, "runtime.json", receipt)
            return receipt
        except Exception as error:
            execution.write_record(
                root,
                "start-failure.json",
                {
                    **identity,
                    "status": "quarantined",
                    "error_type": type(error).__name__,
                },
            )
            raise


def admin(config, plan, *, repo_root, operation, runner=None):
    """Execute one bounded import or explicit read-only reconciliation."""
    return execute_admin(
        config, plan, repo_root=repo_root, operation=operation, runner=runner
    )
