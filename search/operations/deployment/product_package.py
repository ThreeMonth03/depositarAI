"""Offline product payloads, with optional explicit external model dependencies.

The caller supplies trusted, same-source build/catalog receipts and previously
scanned source artifacts. This layer checks that narrow identity, freshly checks
immutable Docker images, and copies only the approved source/catalog files. It
does not authenticate a caller's build attestation, scan arbitrary image layers
for secrets, include credentials, install services, or activate a release.
"""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile

from search.catalog.bundle import ARTIFACT_NAMES, MANIFEST_NAME
from scripts import search_model_source, validate_catalog_compose
from search.operations.catalog import execution as catalog_execution
from search.operations.deployment import composition, source_bundle
from search.operations.deployment.images import build as image_build
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
MAX_RECORD_BYTES = 1024**2
MAX_ASSET_BYTES = 1024**3
MAX_ARCHIVE_BYTES = 64 * 1024**3
MAX_MEMBERS = 100000
SECRET_KEY = re.compile(
    r"(?:^|[_.-])(?:passwords?|secrets?|credentials?|tokens?|api[_-]?key|access[_-]?key|private[_-]?key)(?:$|[_.-])",
    re.I,
)


class ProductPackageError(ValueError):
    """Fixed diagnostic codes; never include source, paths, or Docker output."""


def _fail(code):
    raise ProductPackageError("product_package_" + code)


def _keys(value, keys, code):
    if type(value) is not dict or set(value) != set(keys):
        _fail(code)


def _hash(value, pattern=source_bundle.DIGEST):
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail("identity_invalid")
    return value


def _relative(value, *, reviewed_model_file=False):
    if (
        type(value) is not str
        or not value
        or len(value) > 256
        or any(character in value for character in "\\\x00\r\n")
        or PurePosixPath(value).is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(
            part.startswith(".")
            or (SECRET_KEY.search(part) and not reviewed_model_file)
            for part in value.split("/")
        )
    ):
        _fail("asset_name_invalid")
    return value


def _files(value, *, names=None, maximum=MAX_ASSET_BYTES, reviewed_hashes=None):
    if type(value) is not dict or not 1 <= len(value) <= 64:
        _fail("asset_inventory_invalid")
    if any(type(name) is not str for name in value):
        _fail("asset_name_invalid")
    if names is not None and set(value) != set(names):
        _fail("asset_inventory_incomplete")
    result = {}
    total = 0
    for name, entry in sorted(value.items()):
        _keys(entry, {"bytes", "sha256"}, "asset_record_invalid")
        # Reviewed tokenizer metadata can contain "tokens" in its filename.
        # Only the exact pinned model filename AND digest can bypass that
        # lexical secret-name check; all path checks remain unconditional.
        _relative(
            name,
            reviewed_model_file=(
                reviewed_hashes is not None
                and name in reviewed_hashes
                and entry["sha256"] == reviewed_hashes[name]
            ),
        )
        if type(entry["bytes"]) is not int or not 1 <= entry["bytes"] <= maximum:
            _fail("asset_size_invalid")
        total += entry["bytes"]
        result[name] = {"bytes": entry["bytes"], "sha256": _hash(entry["sha256"])}
    if total > maximum:
        _fail("asset_size_invalid")
    return result


def source_names(revision):
    _hash(revision, source_bundle.REVISION)
    prefix = "depositar-ai-" + revision
    return (prefix + ".tar.gz", prefix + ".bundle", "SOURCE_REVISION", "SHA256SUMS")


def plan_product_package(
    revision, *, source, images, models, metadata, catalog, include_models=True
):
    """Validate trusted receipt projections without reading paths or using Docker.

    ``source`` is a filename-to-{bytes,sha256} map from source_artifact.py's
    reviewed output. ``metadata`` describes the four exact sealed bundle files.
    ``models`` maps api/embedding/reranker to complete relative-file inventories;
    their bytes are bundled by default. The explicit external profile records
    their complete required inventory without pretending those files are present.
    Application build proofs bind the current source; catalog images retain
    their original revision. The caller must freshly verify both source sets
    and their data compatibility, not accept arbitrary user JSON as evidence.
    """
    _hash(revision, source_bundle.REVISION)
    if type(include_models) is not bool:
        _fail("model_delivery_invalid")
    source = _files(source, names=source_names(revision))
    revision_bytes = (revision + "\n").encode("ascii")
    revision_record = {
        "bytes": len(revision_bytes),
        "sha256": hashlib.sha256(revision_bytes).hexdigest(),
    }
    if source["SOURCE_REVISION"] != revision_record:
        _fail("source_revision_mismatch")
    sums = "".join(
        source[name]["sha256"] + "  " + name + "\n"
        for name in source_names(revision)[:-1]
    ).encode("ascii")
    if source["SHA256SUMS"] != {
        "bytes": len(sums),
        "sha256": hashlib.sha256(sums).hexdigest(),
    }:
        _fail("source_checksums_invalid")
    _keys(
        metadata,
        {"bundle_id", "manifest_sha256", "document_count", "files"},
        "metadata_invalid",
    )
    _hash(metadata["bundle_id"])
    _hash(metadata["manifest_sha256"])
    if (
        type(metadata["document_count"]) is not int
        or not 1 <= metadata["document_count"] <= 10000
    ):
        _fail("metadata_count_invalid")
    metadata_files = _files(metadata["files"], names=(*ARTIFACT_NAMES, MANIFEST_NAME))
    if metadata_files[MANIFEST_NAME]["sha256"] != metadata["manifest_sha256"]:
        _fail("metadata_manifest_mismatch")
    _keys(
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
    _hash(catalog["revision"], source_bundle.REVISION)
    expected_project = (
        "depositar-ai-catalog-"
        + metadata["bundle_id"][:12]
        + "-"
        + catalog["revision"][:12]
    )
    if catalog["project"] != expected_project or any(
        catalog[key] != metadata[key] for key in ("bundle_id", "manifest_sha256")
    ):
        _fail("catalog_identity_mismatch")
    for key in ("compose_sha256", "image_receipt_sha256"):
        _hash(catalog[key])
    _keys(images, IMAGE_ROLES, "image_inventory_incomplete")
    clean_images = {}
    for role in IMAGE_ROLES:
        fields = {"image_id", "image_environment_sha256"} | (
            {"reference"} if role == "ollama" else {"revision", "source_sha256"}
        )
        entry = images[role]
        _keys(entry, fields, "image_record_invalid")
        _hash(entry["image_id"], source_bundle.IMAGE_ID)
        _hash(entry["image_environment_sha256"])
        if role == "ollama":
            if entry["reference"] != composition.OLLAMA_IMAGE:
                _fail("registry_image_unreviewed")
        elif entry["revision"] != (
            catalog["revision"] if role in CATALOG_ROLES else revision
        ):
            _fail("image_source_mismatch")
        else:
            _hash(entry["source_sha256"])
        clean_images[role] = dict(entry)
    if len({entry["image_id"] for entry in clean_images.values()}) != len(IMAGE_ROLES):
        _fail("image_role_overlap")
    _keys(models, MODEL_ROLES, "model_inventory_incomplete")
    from search.reranker.model import MODEL_HASHES

    clean_models = {
        role: _files(
            models[role],
            maximum=MAX_ARCHIVE_BYTES,
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
        "metadata": {**metadata, "delivery": "bundled", "files": metadata_files},
        "catalog": dict(catalog),
        "credentials_included": False,
        "payload_self_contained": include_models,
        "install_complete": False,
        "runtime_install_verified": False,
        "registry_identity_restore_verified": False,
        "active_changed": False,
    }


def _validate_plan(plan):
    if type(plan) is not dict or any(
        type(plan.get(key)) is not dict
        for key in ("source", "models", "metadata", "images", "catalog")
    ):
        _fail("plan_invalid")
    try:
        expected = plan_product_package(
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
            _fail("plan_changed")
    except (KeyError, TypeError, RecursionError):
        _fail("plan_invalid")
    return expected


def _catalog_images(plan, receipt):
    """Retain only the exact, nonsecret catalog prebuilt-image contract."""
    if receipt is None:
        return None
    _keys(
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
        _fail("catalog_images_identity_mismatch")
    _keys(
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
        _keys(
            entry,
            {"reference", "image_id", "labels", "sources"},
            "catalog_images_record_invalid",
        )
        role = "ollama" if service == "ollama-embedding" else service
        if entry["image_id"] != plan["images"][role]["image_id"]:
            _fail("catalog_images_image_mismatch")
        if service == "ollama-embedding":
            reference = composition.OLLAMA_IMAGE
            labels, sources = {}, {}
        else:
            reference = plan["catalog"]["project"] + "-" + service + ":1"
            _keys(
                entry["sources"],
                catalog_execution.service_sources(service),
                "catalog_images_sources_invalid",
            )
            sources = {name: _hash(digest) for name, digest in entry["sources"].items()}
            if (
                catalog_execution.digest_record(sources)
                != plan["images"][role]["source_sha256"]
            ):
                _fail("catalog_images_source_mismatch")
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
            _fail("catalog_images_record_mismatch")
        clean["images"][service] = {
            "reference": reference,
            "image_id": entry["image_id"],
            "labels": labels,
            "sources": sources,
        }
    raw = private_io.canonical(clean)
    if (
        len(raw) > MAX_RECORD_BYTES
        or hashlib.sha256(raw).hexdigest() != plan["catalog"]["image_receipt_sha256"]
    ):
        _fail("catalog_images_receipt_mismatch")
    return raw


def _file_info(fd):
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) not in {0o600, 0o644}
    ):
        _fail("file_unsafe")
    return info


def _copy_files(inputs, expected, destination, *, sealed_metadata=False):
    _keys(inputs, expected, "input_inventory_incomplete")
    identities = {}
    with search_model_source.open_private_root(destination) as target:
        for relative, record in expected.items():
            path = Path(inputs[relative])
            parts = PurePosixPath(relative).parts
            if tuple(path.parts[-len(parts) :]) != parts:
                _fail("input_filename_mismatch")
            parent, name = target.relative_parent(relative, private=True, create=True)
            output_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            os.close(output_fd)
            with ExitStack() as sources:
                source_root = path.parents[len(parts) - 1]
                source_relative = relative
                if sealed_metadata:
                    # CatalogBundle roots are sealed 0500. Anchor the existing
                    # copy helper at their private 0700 parent so its allocated
                    # ancestor policy stays intact; the sealed child and file
                    # are still opened no-follow and verified through its FDs.
                    sources.enter_context(
                        search_model_source.open_private_root(source_root.parent)
                    )
                    source_relative = source_root.name + "/" + relative
                    source_root = source_root.parent
                search_model_source.copy_verified_file(
                    source_root,
                    source_relative,
                    destination / relative,
                    record["sha256"],
                    record["bytes"],
                )
            target.verify()
            identities[relative] = private_io.identity(
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            )
            os.fsync(parent)
        target.verify()
        os.fsync(target.fd)
    return identities


def _verify_copies(destination, copies):
    for relative, identities in copies.items():
        with search_model_source.open_private_root(destination / relative) as root:
            for name, expected in identities.items():
                parent, leaf = root.relative_parent(name, private=True)
                if (
                    private_io.identity(
                        os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                    )
                    != expected
                ):
                    _fail("copied_asset_changed")
            root.verify()


def _image_environment(config):
    if type(config) is not dict:
        _fail("image_config_invalid")
    environment = source_bundle.parse_image_environment(config.get("Env"))
    labels = config.get("Labels") or {}
    if type(labels) is not dict:
        _fail("image_labels_invalid")
    for key, value in (*environment.items(), *labels.items()):
        if type(key) is not str or type(value) is not str:
            _fail("image_config_invalid")
        if value and (
            SECRET_KEY.search(key)
            or "PRIVATE KEY-----" in value
            or re.search(r"https?://[^/\s]+:[^/\s]+@", value)
        ):
            _fail("image_secret_configuration")
    return environment, labels


def _check_image(role, entry, inspected, plan):
    if (
        type(inspected) is not dict
        or inspected.get("Id") != entry["image_id"]
        or inspected.get("Os") != "linux"
        or inspected.get("Architecture") != "amd64"
    ):
        _fail("image_identity_mismatch")
    environment, labels = _image_environment(inspected.get("Config"))
    if (
        hashlib.sha256(private_io.canonical(environment)).hexdigest()
        != entry["image_environment_sha256"]
    ):
        _fail("image_environment_changed")
    if role == "ollama":
        digest = "ollama/ollama@" + composition.OLLAMA_IMAGE.split("@", 1)[1]
        repo_digests = inspected.get("RepoDigests")
        if (
            type(repo_digests) is not list
            or any(type(value) is not str for value in repo_digests)
            or digest not in repo_digests
        ):
            _fail("registry_digest_mismatch")
        return
    if labels.get("org.opencontainers.image.revision") != entry["revision"]:
        _fail("image_source_mismatch")
    if role in image_build.SERVICES:
        if labels.get(source_bundle.LABEL_PREFIX + "sha256") != entry["source_sha256"]:
            _fail("image_source_digest_mismatch")
    elif role != "opensearch-hash":
        prefix = "io.depositar.ai.catalog."
        if (
            labels.get(prefix + "project") != plan["catalog"]["project"]
            or labels.get(prefix + "source-sha256") != entry["source_sha256"]
        ):
            _fail("catalog_image_mismatch")


def _check_archive(fd, images):
    """Read Docker save's manifest/config/layers without extracting any path."""
    digests, small, sizes, total = {}, {}, {}, 0
    os.lseek(fd, 0, os.SEEK_SET)
    with (
        os.fdopen(os.dup(fd), "rb") as stream,
        tarfile.open(fileobj=stream, mode="r|") as archive,
    ):
        for member in archive:
            if len(digests) >= MAX_MEMBERS:
                _fail("archive_member_limit")
            name = _relative(member.name.rstrip("/"))
            if name in digests or not (member.isfile() or member.isdir()):
                _fail("archive_member_invalid")
            if member.isdir():
                digests[name] = None
                continue
            total += member.size
            sizes[name] = member.size
            if member.size < 0 or total > MAX_ARCHIVE_BYTES:
                _fail("archive_size_limit")
            digest, retained = hashlib.sha256(), []
            source = archive.extractfile(member)
            with source:
                while part := source.read(1024**2):
                    digest.update(part)
                    if member.size <= MAX_RECORD_BYTES:
                        retained.append(part)
            digests[name] = digest.hexdigest()
            if retained:
                small[name] = b"".join(retained)
                if sum(map(len, small.values())) > 32 * MAX_RECORD_BYTES:
                    _fail("archive_metadata_limit")
    if "index.json" in small or "oci-layout" in small:
        return _check_oci_archive(digests, sizes, small, images)
    manifest = image_build._json(small.get("manifest.json"))
    if type(manifest) is not list or len(manifest) != len(images):
        _fail("archive_inventory_incomplete")
    expected = {entry["image_id"] for entry in images.values()}
    found = set()
    for row in manifest:
        _keys(row, {"Config", "RepoTags", "Layers"}, "archive_manifest_invalid")
        config_name = _relative(row["Config"])
        image_id = "sha256:" + (digests.get(config_name) or "")
        if image_id not in expected or image_id in found:
            _fail("archive_image_mismatch")
        config = image_build._json(small.get(config_name))
        _image_environment(config.get("config") if type(config) is dict else None)
        rootfs = config.get("rootfs", {})
        diff_ids = rootfs.get("diff_ids") if type(rootfs) is dict else None
        layers = row["Layers"]
        if (
            type(layers) is not list
            or type(diff_ids) is not list
            or len(layers) != len(diff_ids)
        ):
            _fail("archive_layers_invalid")
        for layer, digest in zip(layers, diff_ids):
            _hash(digest, source_bundle.IMAGE_ID)
            if digests.get(_relative(layer)) != digest[7:]:
                _fail("archive_layer_digest_mismatch")
        found.add(image_id)
    if found != expected:
        _fail("archive_inventory_incomplete")
    return "docker-save-classic"


def _check_oci_archive(digests, sizes, small, images):
    """Verify containerd Docker-save roots, selecting the required amd64 graph.

    Docker 29 may inspect an OCI index/manifest ID rather than the config digest.
    Its save archive includes gzip layer blobs, and indexes can retain references
    to platforms not cached locally. Only linux/amd64 is a supported payload;
    unavailable other-platform references never count as available images.
    """
    if image_build._json(small.get("oci-layout")) != {"imageLayoutVersion": "1.0.0"}:
        _fail("archive_layout_invalid")
    indexes = {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
    manifests = {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
    layer_types = {
        "application/vnd.oci.image.layer.v1.tar",
        "application/vnd.oci.image.layer.v1.tar+gzip",
        "application/vnd.oci.image.layer.v1.tar+zstd",
        "application/vnd.docker.image.rootfs.diff.tar",
        "application/vnd.docker.image.rootfs.diff.tar.gzip",
    }
    for name, digest in digests.items():
        if (
            name.startswith("blobs/sha256/")
            and digest is not None
            and name != "blobs/sha256/" + digest
        ):
            _fail("archive_blob_digest_mismatch")

    def blob(descriptor, *, json_value=False):
        if type(descriptor) is not dict or not {"digest", "size", "mediaType"} <= set(
            descriptor
        ):
            _fail("archive_descriptor_invalid")
        identifier = _hash(descriptor["digest"], source_bundle.IMAGE_ID)
        name = "blobs/sha256/" + identifier[7:]
        if (
            type(descriptor["size"]) is not int
            or descriptor["size"] <= 0
            or digests.get(name) != identifier[7:]
            or sizes.get(name) != descriptor["size"]
        ):
            _fail("archive_descriptor_missing")
        return image_build._json(small.get(name)) if json_value else name

    index = image_build._json(small.get("index.json"))
    if (
        type(index) is not dict
        or index.get("schemaVersion") != 2
        or type(index.get("manifests")) is not list
    ):
        _fail("archive_index_invalid")
    expected = {entry["image_id"]: entry for entry in images.values()}
    descriptors = index["manifests"]
    if len(descriptors) != len(expected) or any(
        type(entry) is not dict for entry in descriptors
    ):
        _fail("archive_inventory_incomplete")
    identifiers = [entry.get("digest") for entry in descriptors]
    if (
        any(type(identifier) is not str for identifier in identifiers)
        or set(identifiers) != set(expected)
        or len(set(identifiers)) != len(identifiers)
    ):
        _fail("archive_image_mismatch")
    selected = set()
    for root in descriptors:
        descriptor = root
        for _ in range(8):
            value = blob(descriptor, json_value=True)
            if type(value) is not dict or value.get("schemaVersion") != 2:
                _fail("archive_descriptor_invalid")
            if descriptor["mediaType"] in manifests:
                break
            if (
                descriptor["mediaType"] not in indexes
                or type(value.get("manifests")) is not list
            ):
                _fail("archive_media_type_unsupported")
            eligible = [
                child
                for child in value["manifests"]
                if type(child) is dict
                and type(child.get("platform")) is dict
                and child["platform"].get("os") == "linux"
                and child["platform"].get("architecture") == "amd64"
            ]
            if len(eligible) != 1:
                _fail("archive_platform_incomplete")
            descriptor = eligible[0]
        else:
            _fail("archive_descriptor_depth")
        config_descriptor = value.get("config")
        config = blob(config_descriptor, json_value=True)
        if (
            type(config) is not dict
            or config.get("os") != "linux"
            or config.get("architecture") != "amd64"
        ):
            _fail("archive_platform_incomplete")
        environment, _ = _image_environment(config.get("config"))
        if (
            hashlib.sha256(private_io.canonical(environment)).hexdigest()
            != expected[root["digest"]]["image_environment_sha256"]
        ):
            _fail("archive_environment_mismatch")
        layers = value.get("layers")
        rootfs = config.get("rootfs")
        if (
            type(layers) is not list
            or type(rootfs) is not dict
            or type(rootfs.get("diff_ids")) is not list
            or len(layers) != len(rootfs["diff_ids"])
        ):
            _fail("archive_layers_invalid")
        for layer, diff_id in zip(layers, rootfs["diff_ids"]):
            _hash(diff_id, source_bundle.IMAGE_ID)
            blob(layer)
            if layer["mediaType"] not in layer_types:
                _fail("archive_layer_type_unsupported")
            if (
                layer["mediaType"]
                in {
                    "application/vnd.oci.image.layer.v1.tar",
                    "application/vnd.docker.image.rootfs.diff.tar",
                }
                and layer["digest"] != diff_id
            ):
                _fail("archive_layer_digest_mismatch")
        selected.add(
            (
                config_descriptor["digest"][7:],
                tuple(layer["digest"][7:] for layer in layers),
            )
        )
    # The classic compatibility manifest must describe those same selected
    # config/layer graphs. It cannot silently load a different extra image.
    compatibility = image_build._json(small.get("manifest.json"))
    if type(compatibility) is not list or len(compatibility) != len(selected):
        _fail("archive_compatibility_mismatch")
    actual = set()
    for row in compatibility:
        if (
            type(row) is not dict
            or not {"Config", "Layers", "RepoTags"} <= set(row)
            or type(row["Layers"]) is not list
        ):
            _fail("archive_compatibility_mismatch")
        actual.add(
            (
                digests.get(_relative(row["Config"])),
                tuple(digests.get(_relative(name)) for name in row["Layers"]),
            )
        )
    if actual != selected or len(actual) != len(compatibility):
        _fail("archive_compatibility_mismatch")
    return "docker-save-oci-linux-amd64"


def export_product_package(
    plan,
    destination,
    *,
    source_files,
    metadata_files,
    model_files=None,
    catalog_images=None,
    runner=None,
    before_publish=None,
):
    """Copy approved payloads and Docker-save immutable IDs into a new directory.

    The runner follows image_build._run(argv, timeout, environment)'s contract.
    No Docker mutation besides image save occurs. Failed output is preserved;
    package.json is published only after payload checks and fresh post-inspection.
    The caller retains its product lock and owns durable outer operation state.
    ``before_publish`` lets that trusted caller reverify original source/catalog/
    build authority after copying and exporting, before any completion marker.
    ``catalog_images`` optionally includes the verified exact-project prebuilt
    receipt accepted by start-index; arbitrary receipt fields are rejected.
    """
    plan = _validate_plan(plan)
    destination = Path(destination)
    parent_fd = root_fd = archive_fd = None
    runner = image_build._run if runner is None else runner
    if not callable(runner):
        _fail("runner_invalid")
    if before_publish is not None and not callable(before_publish):
        _fail("publication_callback_invalid")
    catalog_raw = _catalog_images(plan, catalog_images)
    if plan["models"]["delivery"] == "bundled":
        _keys(model_files, MODEL_ROLES, "model_input_inventory_incomplete")
    elif model_files is not None:
        _fail("external_model_inputs_forbidden")
    try:
        if (
            not destination.is_absolute()
            or destination.name in {"", ".", ".."}
            or ".." in destination.parts
            or destination != destination.resolve()
        ):
            _fail("destination_unsafe")
        parent_fd = private_io.open_directory(destination.parent)
        root_fd = private_io.open_child_directory(
            parent_fd, destination.name, create=True
        )
        copies = {}
        if catalog_raw is not None:
            private_io.write_file(
                root_fd, "catalog-images.json", catalog_raw, max_bytes=MAX_RECORD_BYTES
            )
            copies[""] = {
                "catalog-images.json": private_io.identity(
                    os.stat(
                        "catalog-images.json", dir_fd=root_fd, follow_symlinks=False
                    )
                )
            }
        assets = [
            ("source", source_files, plan["source"]["files"]),
            ("metadata", metadata_files, plan["metadata"]["files"]),
        ]
        if plan["models"]["delivery"] == "bundled":
            models_fd = private_io.open_child_directory(root_fd, "models", create=True)
            os.close(models_fd)
            assets.extend(
                ("models/" + role, model_files[role], files)
                for role, files in plan["models"]["files"].items()
            )
        for name, inputs, expected in assets:
            parent = root_fd
            model_fd = None
            if name.startswith("models/"):
                model_fd = private_io.open_child_directory(root_fd, "models")
                parent = model_fd
            child = private_io.open_child_directory(
                parent, name.split("/")[-1], create=True
            )
            try:
                copies[name] = _copy_files(
                    inputs,
                    expected,
                    destination / name,
                    sealed_metadata=name == "metadata",
                )
                private_io.verify_directory(child, destination / name)
            finally:
                os.close(child)
                if model_fd is not None:
                    os.close(model_fd)
        with tempfile.TemporaryDirectory(prefix="depositar-package-docker-") as client:
            prefix = ["docker", "--host", image_build.DOCKER_HOST, "--config", client]
            environment = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }

            def command(arguments, timeout=30):
                result = runner(
                    prefix + arguments, timeout=timeout, environment=dict(environment)
                )
                if type(result.returncode) is not int or result.returncode != 0:
                    _fail("docker_command_failed")
                raw = (
                    result.stdout.encode()
                    if type(result.stdout) is str
                    else result.stdout
                )
                error = (
                    result.stderr.encode()
                    if type(result.stderr) is str
                    else result.stderr
                )
                if (
                    type(raw) is not bytes
                    or type(error) is not bytes
                    or len(raw) + len(error) > image_build.MAX_OUTPUT_BYTES
                ):
                    _fail("docker_output_invalid")
                return raw

            def inspect():
                for role, entry in plan["images"].items():
                    raw = command(
                        [
                            "image",
                            "inspect",
                            "--format",
                            "{{json .}}",
                            entry["image_id"],
                        ]
                    )
                    _check_image(role, entry, image_build._json(raw), plan)

            inspect()
            private_io.verify_directory(root_fd, destination)
            if "images.tar" in os.listdir(root_fd):
                _fail("archive_already_exists")
            # Docker 29 uses atomicwriter: /proc/.../fd symlinks are refused and
            # the client renames a temporary inode. Save only inside this new
            # private directory, then open/recheck the resulting regular file.
            output = str(destination / "images.tar")
            command(
                [
                    "image",
                    "save",
                    "--output",
                    output,
                    *sorted(entry["image_id"] for entry in plan["images"].values()),
                ],
                timeout=1800,
            )
            private_io.verify_directory(root_fd, destination)
            archive_fd = os.open(
                "images.tar", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd
            )
            _file_info(archive_fd)
            os.fchmod(archive_fd, 0o600)
            os.fsync(archive_fd)
            before = _file_info(archive_fd)
            if not 1 <= before.st_size <= MAX_ARCHIVE_BYTES:
                _fail("archive_size_limit")
            archive_format = _check_archive(archive_fd, plan["images"])
            digest = hashlib.sha256()
            os.lseek(archive_fd, 0, os.SEEK_SET)
            while part := os.read(archive_fd, 1024**2):
                digest.update(part)
            if private_io.identity(before) != private_io.identity(
                os.fstat(archive_fd)
            ) or private_io.identity(before) != private_io.identity(
                os.stat("images.tar", dir_fd=root_fd, follow_symlinks=False)
            ):
                _fail("archive_changed")
            inspect()
        private_io.verify_directory(parent_fd, destination.parent)
        private_io.verify_directory(root_fd, destination)
        _verify_copies(destination, copies)
        if before_publish is not None:
            try:
                before_publish()
            except Exception:
                _fail("publication_authority_failed")
        private_io.verify_directory(root_fd, destination)
        _verify_copies(destination, copies)
        if private_io.identity(before) != private_io.identity(
            os.fstat(archive_fd)
        ) or private_io.identity(before) != private_io.identity(
            os.stat("images.tar", dir_fd=root_fd, follow_symlinks=False)
        ):
            _fail("archive_changed")
        result = {
            **plan,
            "status": "packaged",
            "image_archive": {
                "path": "images.tar",
                "bytes": before.st_size,
                "sha256": digest.hexdigest(),
                "format": archive_format,
            },
            "catalog_image_receipt": {
                "path": "catalog-images.json",
                "sha256": plan["catalog"]["image_receipt_sha256"],
            }
            if catalog_raw is not None
            else None,
        }
        checksums = {
            "images.tar": digest.hexdigest(),
            "package.json": hashlib.sha256(private_io.canonical(result)).hexdigest(),
        }
        if catalog_raw is not None:
            checksums["catalog-images.json"] = plan["catalog"]["image_receipt_sha256"]
        for directory in ("source", "metadata"):
            checksums.update(
                {
                    directory + "/" + name: record["sha256"]
                    for name, record in plan[directory]["files"].items()
                }
            )
        if plan["models"]["delivery"] == "bundled":
            for role, files in plan["models"]["files"].items():
                checksums.update(
                    {
                        "models/" + role + "/" + name: record["sha256"]
                        for name, record in files.items()
                    }
                )
        encoded = "".join(
            value + "  " + name + "\n" for name, value in sorted(checksums.items())
        ).encode("ascii")
        private_io.write_file(
            root_fd, "SHA256SUMS", encoded, max_bytes=MAX_RECORD_BYTES
        )
        private_io.verify_directory(root_fd, destination)
        private_io.write_json(
            root_fd, "package.json", result, max_bytes=MAX_RECORD_BYTES
        )
        return result
    except (
        OSError,
        ValueError,
        TypeError,
        subprocess.SubprocessError,
        tarfile.TarError,
    ) as error:
        if isinstance(error, ProductPackageError):
            raise
        raise ProductPackageError("product_package_export_failed") from None
    finally:
        for descriptor in (archive_fd, root_fd, parent_fd):
            if descriptor is not None:
                os.close(descriptor)
