"""Collect sealed product assets for the offline payload exporter.

The lifecycle caller retains the product lock. This facade retains the catalog
lock while checking its source/image receipts, scans and roundtrips the source
with the existing handoff tool, and exports only reviewed payload inventories.
It never starts services, includes runtime credentials, or activates a release.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile

from scripts.ci import source_artifact
from search.catalog.bundle import ARTIFACT_NAMES, MANIFEST_NAME
from search.operations import doctor
from search.operations.catalog import backend, execution, runtime
from search.operations.deployment import (
    composition,
    image_authority as image_evidence,
    inference,
    product_package,
    source_bundle,
)
from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision


def _digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def _source(repo_root, revision, runner):
    if require_clean_revision(repo_root, runner=runner) != revision:
        raise ValueError("product_package_source_changed")


def _destination(config, repo_root, destination):
    destination = Path(destination)
    if not destination.is_absolute() or ".." in destination.parts:
        raise ValueError("product_package_destination_invalid")
    roots = (
        Path(repo_root),
        config.state_root,
        config.api_model_root,
        config.embedding_model_root,
        config.reranker_model_root,
    )
    if any(
        destination == root
        or root in destination.parents
        or destination in root.parents
        for root in roots
    ):
        raise ValueError("product_package_destination_overlaps_input")
    if destination.exists() or destination.is_symlink():
        raise ValueError("product_package_destination_exists")
    return destination


def _asset_records(paths, *, maximum):
    """Reuse the source tool's stable no-follow streaming reader."""
    result = {}
    for name, path in paths.items():
        digest = source_artifact._read_file(path, maximum=maximum, digest_only=True)
        result[name] = {"bytes": path.stat().st_size, "sha256": digest}
    return result


def _image_summary(revision, sources, inspected):
    return {
        "revision": revision,
        "source_sha256": _digest(sources),
        "image_id": inspected["Id"],
        "image_environment_sha256": _digest(
            source_bundle.parse_image_environment(inspected["Config"].get("Env"))
        ),
    }


def _catalog_images(context):
    """Authenticate exact-project receipts, including the prepare-time helper."""
    staged = runtime._require_staged_images(context)
    images, admin = staged["images"], staged["admin_image"]
    helper = execution.read_record(context["root"], "hash-image.json")
    revision = context["plan"]["revision"]
    sources = {
        name: context["plan"]["source_sha256"][name]
        for name in execution.service_sources("opensearch-search")
    }
    if (
        type(helper) is not dict
        or set(helper) != {"image_id", "reference", "revision", "sources", "base"}
        or helper["reference"] != backend.HASH_IMAGE_PREFIX + revision
        or helper["revision"] != revision
        or helper["sources"] != sources
        or helper["base"] != backend.APPROVED_OPENSEARCH_BASE
    ):
        raise ValueError("product_package_hash_image_receipt_changed")
    result = {}
    for role, entry in (
        ("fess-search", images["fess-search"]),
        ("opensearch-search", images["opensearch-search"]),
        ("catalog-admin", admin),
        ("opensearch-hash", helper),
    ):
        labels = (
            {"org.opencontainers.image.revision": revision}
            if role == "opensearch-hash"
            else entry["labels"]
        )
        inspected = execution.verify_image(
            context, entry["reference"], entry["image_id"], labels
        )
        result[role] = _image_summary(revision, entry["sources"], inspected)
    ollama = execution.inspect_image(context, images["ollama-embedding"]["image_id"])
    result["ollama"] = {
        "image_id": ollama["Id"],
        "reference": composition.OLLAMA_IMAGE,
        "image_environment_sha256": _digest(
            source_bundle.parse_image_environment(ollama["Config"].get("Env"))
        ),
    }
    return result


def _models(config):
    plans = inference._model_plan(config)
    from search.catalog.embedding import MODEL, MODEL_DIGEST

    plans["embedding"] = doctor._ollama_files(
        config.embedding_model_root, {MODEL: MODEL_DIGEST}
    )
    roots = {
        "api": config.api_model_root,
        "embedding": config.embedding_model_root,
        "reranker": config.reranker_model_root,
    }
    return plans, {
        role: {name: roots[role] / name for name in files}
        for role, files in plans.items()
    }


def package_product(
    config,
    store,
    revision,
    catalog_plan,
    *,
    repo_root,
    destination,
    scanner,
    include_models=True,
    source_runner=None,
    docker_runner=None,
    catalog_runner=None,
):
    """Export current application assets and a compatible catalog's own images.

    Missing receipts fail before source scanning/export. Models are copied and
    deep-hashed by default; external mode records the exact required inventories.
    Full runtime installation/registry-digest restoration are separate gates.
    """
    destination = _destination(config, repo_root, destination)
    _source(repo_root, revision, source_runner)
    source_plan = source_bundle.committed_source_plan(
        repo_root, revision, runner=source_runner
    )
    authority = image_evidence.read_completed_images(config, store, source_plan)
    context = execution.load_context(
        config, catalog_plan, repo_root=repo_root, runner=catalog_runner
    )
    with execution.operation_lock(context):
        catalog_images = _catalog_images(context)
        images = {
            role: {
                key: proof[key]
                for key in (
                    "revision",
                    "source_sha256",
                    "image_id",
                    "image_environment_sha256",
                )
            }
            for role, proof in authority["images"].items()
        }
        with tempfile.TemporaryDirectory(
            prefix="depositar-product-input-"
        ) as temporary:
            temporary = Path(temporary)
            source_root = temporary / "source"
            source_artifact.package(source_root, revision, scanner, repo_root=repo_root)
            # Use the existing exact-tree/Git-bundle/CLI roundtrip before handing
            # source files to the product exporter. This creates no Docker runtime.
            source_artifact.restore(source_root, revision, temporary / "restored")
            source_files = {
                name: source_root / name
                for name in product_package.source_names(revision)
            }
            metadata_files = {
                name: Path(catalog_plan["bundle_path"]) / name
                for name in (*ARTIFACT_NAMES, MANIFEST_NAME)
            }
            models, model_files = _models(config)
            receipt = execution.read_record(context["root"], "images.json")
            plan = product_package.plan_product_package(
                revision,
                source=_asset_records(
                    source_files, maximum=source_artifact.MAX_ARTIFACT_BYTES
                ),
                images={**images, **catalog_images},
                models=models,
                include_models=include_models,
                metadata={
                    "bundle_id": context["bundle"].bundle_id,
                    "manifest_sha256": catalog_plan["manifest_sha256"],
                    "document_count": catalog_plan["document_count"],
                    "files": _asset_records(metadata_files, maximum=1024**3),
                },
                catalog={
                    "project": catalog_plan["project"],
                    "revision": catalog_plan["revision"],
                    "bundle_id": context["bundle"].bundle_id,
                    "manifest_sha256": catalog_plan["manifest_sha256"],
                    "compose_sha256": catalog_plan["compose_sha256"],
                    "image_receipt_sha256": _digest(receipt),
                },
            )
            _source(repo_root, revision, source_runner)

            def before_publish():
                _source(repo_root, revision, source_runner)
                if catalog_images != _catalog_images(context):
                    raise ValueError("product_package_catalog_images_changed")
                if authority != image_evidence.read_completed_images(
                    config, store, source_plan
                ):
                    raise ValueError("product_package_image_authority_changed")

            result = product_package.export_product_package(
                plan,
                destination,
                source_files=source_files,
                metadata_files=metadata_files,
                model_files=model_files if include_models else None,
                runner=docker_runner,
                before_publish=before_publish,
                catalog_images=receipt,
            )
    return result
