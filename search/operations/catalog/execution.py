"""Shared sealed context, image execution and receipt proof for private catalogs.

Startup and bounded administration depend on this layer, never on each other.
It validates operator-owned state and exact Git source; no generic retries,
active-index writes, live promotion or global Docker changes are provided.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat

from search.catalog.bundle import CatalogBundle
from search.operations.catalog import backend, commands
from search.operations.catalog.policy import (
    CONTAINER_ID,
    IMAGE_ID,
    LABEL_PREFIX,
    SERVICES,
    RuntimeError as RuntimeError,
    validate_runtime,
)
from search.operations.metadata import source as metadata_source, storage
from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision


def digest_record(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def registry_digest(reference):
    """Docker RepoDigests is canonical repo@digest, not tag@digest."""
    match = re.fullmatch(
        r"ollama/ollama:[0-9][0-9A-Za-z_.-]*@(sha256:[0-9a-f]{64})", reference
    )
    if match is None:
        raise RuntimeError("catalog_registry_reference_not_reviewed")
    return "ollama/ollama@" + match[1]


def run_json(runner, argv, *, timeout=30):
    return metadata_source.strict_json(
        commands.command_output(runner, argv, timeout=timeout).encode("utf-8")
    )


def read_record(root, name):
    descriptor = private_io.open_directory(root)
    try:
        return storage.read_record(descriptor, name)
    finally:
        os.close(descriptor)


def write_record(root, name, value, *, replace=False):
    descriptor = private_io.open_directory(root)
    try:
        private_io.verify_directory(descriptor, root)
        storage.write_record(descriptor, name, value, replace=replace)
    finally:
        os.close(descriptor)


def load_context(config, plan, *, repo_root, runner=None):
    # Select the default once for both sealed preparation and later Docker
    # observations. Returning the original None silently broke new facades
    # whose callers intentionally omitted a test/operator runner.
    if runner is None:
        runner = commands.default_runner(repo_root)
    if not callable(runner):
        raise RuntimeError("catalog_runner_invalid")
    if (
        type(plan) is not dict
        or type(plan.get("schema_version")) is not int
        or type(plan.get("document_count")) is not int
        or type(plan.get("bundle_path")) is not str
        or type(plan.get("manifest_sha256")) is not str
        or type(plan.get("revision")) is not str
    ):
        raise RuntimeError("catalog_plan_invalid")
    revision = require_clean_revision(repo_root, runner=runner)
    options = {}
    if revision != plan["revision"]:
        options["catalog_revision"] = plan["revision"]
    loaded = backend.load_prepared(
        config,
        repo_root=repo_root,
        bundle_path=Path(plan["bundle_path"]),
        manifest_sha256=plan["manifest_sha256"],
        revision=revision,
        runner=runner,
        **options,
    )
    if plan != loaded:
        raise RuntimeError("catalog_plan_differs_from_sealed_record")
    bundle = CatalogBundle.load(
        Path(plan["bundle_path"]),
        expected_manifest_sha256=plan["manifest_sha256"],
    )
    compose = read_record(Path(plan["work_root"]), "compose.json")
    if require_clean_revision(repo_root, runner=runner) != revision:
        raise RuntimeError("catalog_source_revision_changed")
    return {
        "plan": loaded,
        "bundle": bundle,
        "root": Path(plan["work_root"]),
        "data": Path(plan["data_root"]),
        "compose": compose,
        "repo": Path(repo_root),
        "runner": runner,
        "source_revision": revision,
    }


@contextmanager
def operation_lock(context):
    state = context["data"] / "state"
    directory = private_io.open_directory(state)
    lock = None
    try:
        lock = os.open(
            "operations.lock",
            os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise RuntimeError("catalog_operation_lock_unsafe")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        named = os.stat("operations.lock", dir_fd=directory, follow_symlinks=False)
        if private_io.identity(info) != private_io.identity(named):
            raise RuntimeError("catalog_operation_lock_changed")
        private_io.verify_directory(directory, state)
        yield
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def compose_command(context, *arguments, overlay=False):
    plan = context["plan"]
    argv = [
        "docker",
        "compose",
        "--project-name",
        plan["project"],
        "--file",
        plan["compose_file"],
    ]
    if overlay:
        argv.extend(["--file", str(context["root"] / "image-lock.json")])
    return [*argv, *arguments]


def container_ids(context):
    raw = commands.command_output(
        context["runner"],
        [
            "docker",
            "ps",
            "--all",
            "--no-trunc",
            "--quiet",
            "--filter",
            "label=com.docker.compose.project=" + context["plan"]["project"],
        ],
    )
    ids = raw.split()
    if len(ids) != len(set(ids)) or any(
        CONTAINER_ID.fullmatch(value) is None for value in ids
    ):
        raise RuntimeError("catalog_container_inventory_invalid")
    return ids


def inspect_image(context, reference):
    rows = run_json(context["runner"], ["docker", "image", "inspect", reference])
    if (
        type(rows) is not list
        or len(rows) != 1
        or type(rows[0]) is not dict
        or type(rows[0].get("Id")) is not str
        or IMAGE_ID.fullmatch(rows[0]["Id"]) is None
    ):
        raise RuntimeError("catalog_image_identity_invalid")
    return rows[0]


def image_labels(context, service, sources, base):
    return {
        "org.opencontainers.image.revision": context["plan"]["revision"],
        LABEL_PREFIX + "project": context["plan"]["project"],
        LABEL_PREFIX + "revision": context["plan"]["revision"],
        LABEL_PREFIX + "service": service,
        LABEL_PREFIX + "source-sha256": digest_record(sources),
        LABEL_PREFIX + "base": base,
    }


def verify_image(context, reference, image_id, labels):
    if type(image_id) is not str or IMAGE_ID.fullmatch(image_id) is None:
        raise RuntimeError("catalog_image_id_not_immutable")
    image = inspect_image(context, reference)
    if image["Id"] != image_id or inspect_image(context, image_id)["Id"] != image_id:
        raise RuntimeError("catalog_image_identity_changed")
    actual = image.get("Config", {}).get("Labels") or {}
    if any(actual.get(name) != value for name, value in labels.items()):
        raise RuntimeError("catalog_image_source_labels_changed")
    return image


def sealed_source(context, relative):
    raw = commands.command_output(
        context["runner"],
        [
            "git",
            "-C",
            str(context["repo"]),
            "show",
            context["plan"]["revision"] + ":" + relative,
        ],
    ).encode("utf-8")
    if backend._source_bytes(context["repo"] / relative) != raw:
        raise RuntimeError("catalog_build_source_is_dirty")
    return raw


def _read_iid(root, name):
    """Read only Docker's nonsecret IID artifact, never receipts or secrets.

    Docker/buildx can recreate this file as 0644 even with a private child
    umask. Its parent must remain operator-owned 0700; both allowed file modes
    still require the same bounded, single-link and exact-identity proof.
    """
    if (
        type(name) is not str
        or re.fullmatch(
            r"image-(?:fess-search|opensearch-search|catalog-admin)\.iid", name
        )
        is None
    ):
        raise RuntimeError("catalog_image_id_file_name_invalid")
    descriptor = private_io.open_directory(root)
    try:
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or mode not in {0o600, 0o644}
            or before.st_size not in {71, 72}
        ):
            raise RuntimeError("catalog_image_id_file_unsafe")
        raw = private_io.read_file(descriptor, name, mode=mode, max_bytes=72)
        after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if private_io.identity(before) != private_io.identity(after):
            raise RuntimeError("catalog_image_id_file_changed")
        private_io.verify_directory(descriptor, root)
        try:
            image_id = raw.removesuffix(b"\n").decode("ascii")
        except UnicodeDecodeError as error:
            raise RuntimeError("catalog_image_id_not_immutable") from error
        if IMAGE_ID.fullmatch(image_id) is None:
            raise RuntimeError("catalog_image_id_not_immutable")
        return image_id
    finally:
        os.close(descriptor)


def build_image(context, service, files, dockerfile, arguments, *, network):
    raw_sources = {relative: sealed_source(context, relative) for relative in files}
    sources = {
        name: hashlib.sha256(raw).hexdigest() for name, raw in raw_sources.items()
    }
    base = next(
        value for name, value in arguments.items() if name.endswith("BASE_IMAGE")
    )
    labels = image_labels(context, service, sources, base)
    directory = context["root"] / ("build-" + service)
    os.close(private_io.open_directory(directory, create=True))
    if list(directory.iterdir()):
        raise RuntimeError("catalog_image_build_context_already_used")
    for relative, raw in raw_sources.items():
        target = relative
        if service == "catalog-admin":
            target = (
                ".dockerignore"
                if relative == files[1]
                else ("Dockerfile" if relative == files[0] else relative)
            )
        elif service == "opensearch-search":
            target = Path(relative).name
        backend._write_file(directory / target, raw)
    iid = context["root"] / ("image-" + service + ".iid")
    backend._write_file(iid, b"not-built\n")
    tag = context["plan"]["project"] + "-" + service + ":1"
    argv = [
        "docker",
        "build",
        "--pull",
        "--network",
        network,
        "--file",
        str(directory / dockerfile),
        "--tag",
        tag,
        "--iidfile",
        str(iid),
    ]
    for name, value in sorted(arguments.items()):
        argv.extend(["--build-arg", name + "=" + value])
    for name, value in sorted(labels.items()):
        argv.extend(["--label", name + "=" + value])
    argv.append(str(directory))
    commands.command_output(context["runner"], argv, timeout=900)
    image_id = _read_iid(context["root"], iid.name)
    verify_image(context, tag, image_id, labels)
    return {
        "image_id": image_id,
        "reference": tag,
        "labels": labels,
        "sources": sources,
    }


def service_sources(name):
    return tuple(
        relative
        for relative in backend.BUILD_FILES
        if (
            relative.startswith("deploy/docker/opensearch/")
            if name == "opensearch-search"
            else relative.startswith(("deploy/docker/fess/", "deploy/config/fess/"))
        )
    )


def verify_security_material(context):
    if (
        backend.security_material_digest(context["data"])
        != context["plan"]["security_material_sha256"]
    ):
        raise RuntimeError("catalog_security_material_changed")


def _verify_images_receipt(context, receipt):
    # Retain a private snapshot across Docker callbacks. A caller's mutable
    # product manifest cannot change the image choice after this proof.
    receipt = copy.deepcopy(receipt)
    if (
        type(receipt) is not dict
        or set(receipt) != {"schema_version", "project", "revision", "images"}
        or type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["project"] != context["plan"]["project"]
        or receipt["revision"] != context["plan"]["revision"]
        or type(receipt["images"]) is not dict
        or set(receipt["images"]) != SERVICES
    ):
        raise RuntimeError("catalog_image_receipt_invalid")
    for name, entry in receipt["images"].items():
        if (
            type(entry) is not dict
            or set(entry)
            != {
                "image_id",
                "reference",
                "labels",
                "sources",
            }
            or type(entry["reference"]) is not str
            or type(entry["labels"]) is not dict
            or type(entry["sources"]) is not dict
        ):
            raise RuntimeError("catalog_image_receipt_invalid")
        verify_image(context, entry["reference"], entry["image_id"], entry["labels"])
        if name == "ollama-embedding":
            image = inspect_image(context, entry["image_id"])
            if (
                entry["reference"] != context["compose"]["services"][name]["image"]
                or registry_digest(entry["reference"])
                not in (image.get("RepoDigests") or [])
                or entry["labels"]
                or entry["sources"]
            ):
                raise RuntimeError("catalog_ollama_registry_digest_changed")
        else:
            base = next(
                value
                for key, value in context["compose"]["services"][name]["build"][
                    "args"
                ].items()
                if key.endswith("BASE_IMAGE")
            )
            expected = image_labels(context, name, entry["sources"], base)
            if (
                entry["labels"] != expected
                or entry["reference"] != context["plan"]["project"] + "-" + name + ":1"
                or set(entry["sources"]) != set(service_sources(name))
            ):
                raise RuntimeError("catalog_image_source_receipt_changed")
            for relative, digest in entry["sources"].items():
                if context["plan"]["source_sha256"].get(relative) != digest:
                    raise RuntimeError("catalog_image_source_receipt_changed")
    return receipt["images"]


def verify_images(context, receipt):
    """Prove an explicit exact-project image receipt against sealed source.

    This observes only existing local images; it never builds, pulls, retags or
    writes a receipt. The caller must authenticate the package/source that
    supplied these immutable IDs: image labels alone are not build attestation.
    """
    verify_security_material(context)
    return _verify_images_receipt(context, receipt)


def load_images(context):
    verify_security_material(context)
    return _verify_images_receipt(context, read_record(context["root"], "images.json"))


def inspect_runtime(context, images, *, include_snapshot=False):
    """Validate one Docker snapshot; optionally retain its exact peer evidence.

    Operator transports must select IPs from this same validated snapshot,
    rather than resolving a service name or trusting a second loose inspect.
    The default proof-only return keeps the existing catalog contract intact.
    """
    if type(include_snapshot) is not bool:
        raise RuntimeError("catalog_runtime_snapshot_option_invalid")
    storage_proof = require_storage(context)
    ids = container_ids(context)
    if len(ids) != 3:
        raise RuntimeError("catalog_requires_exactly_three_services")
    containers = run_json(context["runner"], ["docker", "inspect", *ids])
    validate_storage_mounts(storage_proof, containers)
    networks = run_json(
        context["runner"],
        [
            "docker",
            "network",
            "inspect",
            context["plan"]["project"] + "_search_backend",
        ],
    )
    if type(networks) is not list or len(networks) != 1:
        raise RuntimeError("catalog_runtime_network_inventory_changed")
    for row in containers:
        image = inspect_image(context, row["Image"])
        for name in ("Cmd", "Entrypoint"):
            if row["Config"].get(name) != image.get("Config", {}).get(name):
                raise RuntimeError("catalog_runtime_command_changed")
    proof = validate_runtime(
        context["plan"], context["compose"], images, containers, networks[0]
    )
    return (proof, containers, networks[0]) if include_snapshot else proof


def require_storage(context):
    """Observe the sealed local volume without changing a running service."""
    from search.operations.catalog import volume_policy

    identity = volume_policy.identity(
        catalog_identity(context),
        compose=context["compose"],
        work_root=context["root"],
        data_root=context["data"],
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    root = context["root"]
    image = read_record(root, "admin-image.json").get("image_id")
    if type(image) is not str or IMAGE_ID.fullmatch(image) is None:
        raise RuntimeError("catalog_volume_helper_identity_changed")
    started = read_record(root, "volume-started.json")
    if digest_record(started) != digest_record(volume_policy.started(identity, image)):
        raise RuntimeError("catalog_volume_receipt_changed")
    receipt = read_record(root, "volume.json")
    metadata = volume_policy.inspect_metadata(
        run_json(
            context["runner"],
            [
                "docker",
                "volume",
                "inspect",
                "--format",
                volume_policy.INSPECT_FORMAT,
                identity["name"],
            ],
        ),
        identity,
    )
    expected = volume_policy.expected_receipt(identity, image, metadata)
    if digest_record(receipt) != digest_record(expected):
        raise RuntimeError("catalog_volume_receipt_changed")
    return receipt


def validate_storage_mounts(storage_proof, containers):
    """Bind the sealed volume to the same unfiltered Docker container snapshot."""
    if type(storage_proof) is not dict:
        raise RuntimeError("catalog_runtime_volume_binding_changed")
    matches = []
    for row in containers:
        role = row.get("Config", {}).get("Labels", {}).get("com.docker.compose.service")
        for mount in row.get("Mounts") or []:
            if mount.get("Type") != "volume":
                continue
            if (
                role != "opensearch-search"
                or mount.get("Destination") != "/usr/share/opensearch/data"
                or mount.get("Name") != storage_proof["name"]
                or mount.get("Source") != storage_proof["metadata"]["mountpoint"]
                or mount.get("Driver") != "local"
                or mount.get("RW") is not True
            ):
                raise RuntimeError("catalog_runtime_volume_binding_changed")
            matches.append(mount)
    if len(matches) != 1:
        raise RuntimeError("catalog_runtime_volume_binding_changed")


def catalog_identity(context):
    plan = context["plan"]
    return {
        "schema_version": 1,
        "project": plan["project"],
        "revision": plan["revision"],
        "bundle_id": context["bundle"].bundle_id,
        "manifest_sha256": plan["manifest_sha256"],
        "compose_sha256": plan["compose_sha256"],
        "security_material_sha256": plan["security_material_sha256"],
        "active_changed": False,
    }


def require_runtime(context):
    root = context["root"]
    receipt = read_record(root, "runtime.json")
    images = load_images(context)
    proof = inspect_runtime(context, images)
    overlay = {
        "services": {name: {"image": item["image_id"]} for name, item in images.items()}
    }
    expected = {
        **catalog_identity(context),
        "status": "running",
        **proof,
        "image_ids": {name: item["image_id"] for name, item in images.items()},
        "image_lock_sha256": digest_record(overlay),
        "images_sha256": digest_record(read_record(root, "images.json")),
    }
    if (
        receipt != expected
        or type(receipt.get("schema_version")) is not int
        or receipt.get("active_changed") is not False
        or read_record(root, "image-lock.json") != overlay
    ):
        raise RuntimeError("catalog_runtime_receipt_changed")
    return receipt, images
