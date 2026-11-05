"""Validate the closed offline-payload manifest and source projections."""

from __future__ import annotations

import hashlib

from scripts import validate_catalog_compose
from search.catalog.bundle import ARTIFACT_NAMES, MANIFEST_NAME
from search.operations.catalog import execution as catalog_execution
from search.operations.deployment import composition, source_bundle
from search.operations.deployment.images import build as image_build
from search.operations.deployment.payload import contract
from search.operations.shared import private_io


CATALOG_ROLES = (
    "fess-search",
    "opensearch-search",
    "catalog-admin",
    "opensearch-hash",
)
BUILT_ROLES = (*image_build.SERVICES, *CATALOG_ROLES)
IMAGE_ROLES = (*BUILT_ROLES, "ollama")
MODEL_ROLES = frozenset({"api", "embedding", "reranker"})


def _files(
    value,
    *,
    names=None,
    maximum=contract.MAX_ASSET_BYTES,
    reviewed_hashes=None,
):
    if type(value) is not dict or not 1 <= len(value) <= 64:
        contract.fail("asset_inventory_invalid")
    if any(type(name) is not str for name in value):
        contract.fail("asset_name_invalid")
    if names is not None and set(value) != set(names):
        contract.fail("asset_inventory_incomplete")
    result = {}
    total = 0
    for name, entry in sorted(value.items()):
        contract.keys(entry, {"bytes", "sha256"}, "asset_record_invalid")
        # Reviewed tokenizer metadata can contain "tokens" in its filename.
        # Only the exact pinned model filename AND digest can bypass that
        # lexical secret-name check; all path checks remain unconditional.
        contract.relative(
            name,
            reviewed_model_file=(
                reviewed_hashes is not None
                and name in reviewed_hashes
                and entry["sha256"] == reviewed_hashes[name]
            ),
        )
        if (
            type(entry["bytes"]) is not int
            or not 1 <= entry["bytes"] <= maximum
        ):
            contract.fail("asset_size_invalid")
        total += entry["bytes"]
        result[name] = {
            "bytes": entry["bytes"],
            "sha256": contract.hash_value(entry["sha256"]),
        }
    if total > maximum:
        contract.fail("asset_size_invalid")
    return result


def source_names(revision):
    contract.hash_value(revision, source_bundle.REVISION)
    prefix = "depositar-ai-" + revision
    return (
        prefix + ".tar.gz",
        prefix + ".bundle",
        "SOURCE_REVISION",
        "SHA256SUMS",
    )


def _source_files(revision, source):
    source = _files(source, names=source_names(revision))
    revision_bytes = (revision + "\n").encode("ascii")
    revision_record = {
        "bytes": len(revision_bytes),
        "sha256": hashlib.sha256(revision_bytes).hexdigest(),
    }
    if source["SOURCE_REVISION"] != revision_record:
        contract.fail("source_revision_mismatch")
    sums = "".join(
        source[name]["sha256"] + "  " + name + "\n"
        for name in source_names(revision)[:-1]
    ).encode("ascii")
    if source["SHA256SUMS"] != {
        "bytes": len(sums),
        "sha256": hashlib.sha256(sums).hexdigest(),
    }:
        contract.fail("source_checksums_invalid")
    return source


def _metadata_catalog(metadata, catalog):
    contract.keys(
        metadata,
        {"bundle_id", "manifest_sha256", "document_count", "files"},
        "metadata_invalid",
    )
    contract.hash_value(metadata["bundle_id"])
    contract.hash_value(metadata["manifest_sha256"])
    if (
        type(metadata["document_count"]) is not int
        or not 1 <= metadata["document_count"] <= 10000
    ):
        contract.fail("metadata_count_invalid")
    metadata_files = _files(
        metadata["files"], names=(*ARTIFACT_NAMES, MANIFEST_NAME)
    )
    if metadata_files[MANIFEST_NAME]["sha256"] != metadata["manifest_sha256"]:
        contract.fail("metadata_manifest_mismatch")
    contract.keys(
        catalog,
        {
            "project",
            "revision",
            "bundle_id",
            "manifest_sha256",
            "compose_sha256",
            "image_receipt_sha256",
        },
        "catalog_invalid",
    )
    contract.hash_value(catalog["revision"], source_bundle.REVISION)
    expected_project = (
        "depositar-ai-catalog-"
        + metadata["bundle_id"][:12]
        + "-"
        + catalog["revision"][:12]
    )
    if catalog["project"] != expected_project or any(
        catalog[key] != metadata[key]
        for key in ("bundle_id", "manifest_sha256")
    ):
        contract.fail("catalog_identity_mismatch")
    for key in ("compose_sha256", "image_receipt_sha256"):
        contract.hash_value(catalog[key])
    return metadata_files


def _images(images, revision, catalog):
    contract.keys(images, IMAGE_ROLES, "image_inventory_incomplete")
    clean_images = {}
    for role in IMAGE_ROLES:
        fields = {"image_id", "image_environment_sha256"} | (
            {"reference"}
            if role == "ollama"
            else {"revision", "source_sha256"}
        )
        entry = images[role]
        contract.keys(entry, fields, "image_record_invalid")
        contract.hash_value(entry["image_id"], source_bundle.IMAGE_ID)
        contract.hash_value(entry["image_environment_sha256"])
        if role == "ollama":
            if entry["reference"] != composition.OLLAMA_IMAGE:
                contract.fail("registry_image_unreviewed")
        elif entry["revision"] != (
            catalog["revision"] if role in CATALOG_ROLES else revision
        ):
            contract.fail("image_source_mismatch")
        else:
            contract.hash_value(entry["source_sha256"])
        clean_images[role] = dict(entry)
    if len({entry["image_id"] for entry in clean_images.values()}) != len(
        IMAGE_ROLES
    ):
        contract.fail("image_role_overlap")
    return clean_images


def create(
    revision, *, source, images, models, metadata, catalog, include_models=True
):
    """Validate trusted receipt projections without reading paths or using
    Docker.

    ``source`` is a filename-to-{bytes,sha256} map from source_artifact.py's
    reviewed output. ``metadata`` describes the four exact sealed bundle files.
    ``models`` maps api/embedding/reranker to complete relative-file
    inventories;
    their bytes are bundled by default. The explicit external profile records
    their complete required inventory without pretending those files are
    present.
    Application build proofs bind the current source; catalog images retain
    their original revision. The caller must freshly verify both source sets
    and their data compatibility, not accept arbitrary user JSON as evidence.
    """
    contract.hash_value(revision, source_bundle.REVISION)
    if type(include_models) is not bool:
        contract.fail("model_delivery_invalid")
    source = _source_files(revision, source)
    metadata_files = _metadata_catalog(metadata, catalog)
    clean_images = _images(images, revision, catalog)
    contract.keys(models, MODEL_ROLES, "model_inventory_incomplete")
    from search.reranker.model import MODEL_HASHES

    clean_models = {
        role: _files(
            models[role],
            maximum=contract.MAX_ARCHIVE_BYTES,
            reviewed_hashes=MODEL_HASHES if role == "reranker" else None,
        )
        for role in sorted(models)
    }
    return {
        "schema_version": 1,
        "kind": "depositar-ai-product-package",
        "revision": revision,
        "source": {"delivery": "bundled", "files": source},
        "images": clean_images,
        "models": {
            "delivery": "bundled" if include_models else "external",
            "files": clean_models,
        },
        "metadata": {
            **metadata,
            "delivery": "bundled",
            "files": metadata_files,
        },
        "catalog": dict(catalog),
        "credentials_included": False,
        "payload_self_contained": include_models,
        "install_complete": False,
        "runtime_install_verified": False,
        "registry_identity_restore_verified": False,
        "active_changed": False,
    }


def validate(plan):
    if type(plan) is not dict or any(
        type(plan.get(key)) is not dict
        for key in ("source", "models", "metadata", "images", "catalog")
    ):
        contract.fail("plan_invalid")
    try:
        expected = create(
            plan["revision"],
            source=plan["source"]["files"],
            images=plan["images"],
            models=plan["models"]["files"],
            catalog=plan["catalog"],
            include_models=plan["models"]["delivery"] == "bundled",
            metadata={
                key: value
                for key, value in plan["metadata"].items()
                if key != "delivery"
            },
        )
        if private_io.canonical(plan) != private_io.canonical(expected):
            contract.fail("plan_changed")
    except (KeyError, TypeError, RecursionError):
        contract.fail("plan_invalid")
    return expected


def catalog_images(plan, receipt):
    """Retain only the exact, nonsecret catalog prebuilt-image contract."""
    if receipt is None:
        return None
    contract.keys(
        receipt,
        {"schema_version", "project", "revision", "images"},
        "catalog_images_invalid",
    )
    if (
        type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["project"] != plan["catalog"]["project"]
        or receipt["revision"] != plan["catalog"]["revision"]
    ):
        contract.fail("catalog_images_identity_mismatch")
    contract.keys(
        receipt["images"],
        {"fess-search", "opensearch-search", "ollama-embedding"},
        "catalog_images_inventory_invalid",
    )
    bases = {
        "fess-search": validate_catalog_compose.FESS_BASE_IMAGE,
        "opensearch-search": validate_catalog_compose.OPENSEARCH_BASE_IMAGE,
    }
    clean = {
        "schema_version": 1,
        "project": plan["catalog"]["project"],
        "revision": plan["catalog"]["revision"],
        "images": {},
    }
    for service, entry in receipt["images"].items():
        contract.keys(
            entry,
            {"reference", "image_id", "labels", "sources"},
            "catalog_images_record_invalid",
        )
        role = "ollama" if service == "ollama-embedding" else service
        if entry["image_id"] != plan["images"][role]["image_id"]:
            contract.fail("catalog_images_image_mismatch")
        if service == "ollama-embedding":
            reference = composition.OLLAMA_IMAGE
            labels, sources = {}, {}
        else:
            reference = plan["catalog"]["project"] + "-" + service + ":1"
            contract.keys(
                entry["sources"],
                catalog_execution.service_sources(service),
                "catalog_images_sources_invalid",
            )
            sources = {
                name: contract.hash_value(digest)
                for name, digest in entry["sources"].items()
            }
            if (
                catalog_execution.digest_record(sources)
                != plan["images"][role]["source_sha256"]
            ):
                contract.fail("catalog_images_source_mismatch")
            labels = catalog_execution.image_labels(
                {"plan": plan["catalog"]}, service, sources, bases[service]
            )
        if (
            entry["reference"] != reference
            or type(entry["labels"]) is not dict
            or entry["labels"] != labels
            or type(entry["sources"]) is not dict
            or entry["sources"] != sources
        ):
            contract.fail("catalog_images_record_mismatch")
        clean["images"][service] = {
            "reference": reference,
            "image_id": entry["image_id"],
            "labels": labels,
            "sources": sources,
        }
    raw = private_io.canonical(clean)
    if (
        len(raw) > contract.MAX_RECORD_BYTES
        or hashlib.sha256(raw).hexdigest()
        != plan["catalog"]["image_receipt_sha256"]
    ):
        contract.fail("catalog_images_receipt_mismatch")
    return raw
