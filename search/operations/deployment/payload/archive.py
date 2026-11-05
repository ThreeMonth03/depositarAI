"""Verify bounded Docker-save archives without extracting their paths."""

from __future__ import annotations

import hashlib
import os
import tarfile

from search.operations.deployment import source_bundle
from search.operations.deployment.images import build as image_build
from search.operations.shared import private_io

from . import contract


def _read_member(archive, member):
    digest, retained = hashlib.sha256(), []
    source = archive.extractfile(member)
    with source:
        while part := source.read(1024**2):
            digest.update(part)
            if member.size <= contract.MAX_RECORD_BYTES:
                retained.append(part)
    return digest.hexdigest(), retained


def _read_members(fd):
    digests, small, sizes, total = {}, {}, {}, 0
    os.lseek(fd, 0, os.SEEK_SET)
    with (
        os.fdopen(os.dup(fd), "rb") as stream,
        tarfile.open(fileobj=stream, mode="r|") as archive,
    ):
        for member in archive:
            if len(digests) >= contract.MAX_MEMBERS:
                contract.fail("archive_member_limit")
            name = contract.relative(member.name.rstrip("/"))
            if name in digests or not (member.isfile() or member.isdir()):
                contract.fail("archive_member_invalid")
            if member.isdir():
                digests[name] = None
                continue
            total += member.size
            sizes[name] = member.size
            if member.size < 0 or total > contract.MAX_ARCHIVE_BYTES:
                contract.fail("archive_size_limit")
            digests[name], retained = _read_member(archive, member)
            if retained:
                small[name] = b"".join(retained)
                if (
                    sum(map(len, small.values()))
                    > 32 * contract.MAX_RECORD_BYTES
                ):
                    contract.fail("archive_metadata_limit")
    return digests, sizes, small


def _classic_layers(config, layers, digests):
    rootfs = config.get("rootfs", {})
    diff_ids = rootfs.get("diff_ids") if type(rootfs) is dict else None
    if (
        type(layers) is not list
        or type(diff_ids) is not list
        or len(layers) != len(diff_ids)
    ):
        contract.fail("archive_layers_invalid")
    for layer, digest in zip(layers, diff_ids):
        contract.hash_value(digest, source_bundle.IMAGE_ID)
        if digests.get(contract.relative(layer)) != digest[7:]:
            contract.fail("archive_layer_digest_mismatch")


def _check_classic(digests, small, images):
    manifest = image_build._json(small.get("manifest.json"))
    if type(manifest) is not list or len(manifest) != len(images):
        contract.fail("archive_inventory_incomplete")
    expected = {entry["image_id"] for entry in images.values()}
    found = set()
    for row in manifest:
        contract.keys(
            row, {"Config", "RepoTags", "Layers"}, "archive_manifest_invalid"
        )
        config_name = contract.relative(row["Config"])
        image_id = "sha256:" + (digests.get(config_name) or "")
        if image_id not in expected or image_id in found:
            contract.fail("archive_image_mismatch")
        config = image_build._json(small.get(config_name))
        contract.image_environment(
            config.get("config") if type(config) is dict else None
        )
        _classic_layers(config, row["Layers"], digests)
        found.add(image_id)
    if found != expected:
        contract.fail("archive_inventory_incomplete")
    return "docker-save-classic"


def _check_blob_names(digests):
    for name, digest in digests.items():
        if (
            name.startswith("blobs/sha256/")
            and digest is not None
            and name != "blobs/sha256/" + digest
        ):
            contract.fail("archive_blob_digest_mismatch")


def _blob(descriptor, digests, sizes, small, *, json_value=False):
    if type(descriptor) is not dict or not {
        "digest",
        "size",
        "mediaType",
    } <= set(descriptor):
        contract.fail("archive_descriptor_invalid")
    identifier = contract.hash_value(
        descriptor["digest"], source_bundle.IMAGE_ID
    )
    name = "blobs/sha256/" + identifier[7:]
    if (
        type(descriptor["size"]) is not int
        or descriptor["size"] <= 0
        or digests.get(name) != identifier[7:]
        or sizes.get(name) != descriptor["size"]
    ):
        contract.fail("archive_descriptor_missing")
    return image_build._json(small.get(name)) if json_value else name


def _oci_roots(small, images):
    index = image_build._json(small.get("index.json"))
    if (
        type(index) is not dict
        or index.get("schemaVersion") != 2
        or type(index.get("manifests")) is not list
    ):
        contract.fail("archive_index_invalid")
    expected = {entry["image_id"]: entry for entry in images.values()}
    descriptors = index["manifests"]
    if len(descriptors) != len(expected) or any(
        type(entry) is not dict for entry in descriptors
    ):
        contract.fail("archive_inventory_incomplete")
    identifiers = [entry.get("digest") for entry in descriptors]
    if (
        any(type(identifier) is not str for identifier in identifiers)
        or set(identifiers) != set(expected)
        or len(set(identifiers)) != len(identifiers)
    ):
        contract.fail("archive_image_mismatch")
    return descriptors, expected


def _oci_manifest(descriptor, digests, sizes, small):
    indexes = {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
    manifests = {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
    for _ in range(8):
        value = _blob(descriptor, digests, sizes, small, json_value=True)
        if type(value) is not dict or value.get("schemaVersion") != 2:
            contract.fail("archive_descriptor_invalid")
        if descriptor["mediaType"] in manifests:
            return value
        if (
            descriptor["mediaType"] not in indexes
            or type(value.get("manifests")) is not list
        ):
            contract.fail("archive_media_type_unsupported")
        eligible = [
            child
            for child in value["manifests"]
            if type(child) is dict
            and type(child.get("platform")) is dict
            and child["platform"].get("os") == "linux"
            and child["platform"].get("architecture") == "amd64"
        ]
        if len(eligible) != 1:
            contract.fail("archive_platform_incomplete")
        descriptor = eligible[0]
    contract.fail("archive_descriptor_depth")


def _oci_layers(value, config, digests, sizes, small):
    layer_types = {
        "application/vnd.oci.image.layer.v1.tar",
        "application/vnd.oci.image.layer.v1.tar+gzip",
        "application/vnd.oci.image.layer.v1.tar+zstd",
        "application/vnd.docker.image.rootfs.diff.tar",
        "application/vnd.docker.image.rootfs.diff.tar.gzip",
    }
    layers = value.get("layers")
    rootfs = config.get("rootfs")
    if (
        type(layers) is not list
        or type(rootfs) is not dict
        or type(rootfs.get("diff_ids")) is not list
        or len(layers) != len(rootfs["diff_ids"])
    ):
        contract.fail("archive_layers_invalid")
    for layer, diff_id in zip(layers, rootfs["diff_ids"]):
        contract.hash_value(diff_id, source_bundle.IMAGE_ID)
        _blob(layer, digests, sizes, small)
        if layer["mediaType"] not in layer_types:
            contract.fail("archive_layer_type_unsupported")
        if (
            layer["mediaType"]
            in {
                "application/vnd.oci.image.layer.v1.tar",
                "application/vnd.docker.image.rootfs.diff.tar",
            }
            and layer["digest"] != diff_id
        ):
            contract.fail("archive_layer_digest_mismatch")
    return layers


def _oci_image(value, root, expected, digests, sizes, small):
    config_descriptor = value.get("config")
    config = _blob(config_descriptor, digests, sizes, small, json_value=True)
    if (
        type(config) is not dict
        or config.get("os") != "linux"
        or config.get("architecture") != "amd64"
    ):
        contract.fail("archive_platform_incomplete")
    environment, _ = contract.image_environment(config.get("config"))
    if (
        hashlib.sha256(private_io.canonical(environment)).hexdigest()
        != expected[root["digest"]]["image_environment_sha256"]
    ):
        contract.fail("archive_environment_mismatch")
    layers = _oci_layers(value, config, digests, sizes, small)
    return (
        config_descriptor["digest"][7:],
        tuple(layer["digest"][7:] for layer in layers),
    )


def _check_compatibility(digests, small, selected):
    # The classic manifest must load the same config/layer graphs, without
    # adding an image outside the selected OCI roots.
    compatibility = image_build._json(small.get("manifest.json"))
    if type(compatibility) is not list or len(compatibility) != len(selected):
        contract.fail("archive_compatibility_mismatch")
    actual = set()
    for row in compatibility:
        if (
            type(row) is not dict
            or not {"Config", "Layers", "RepoTags"} <= set(row)
            or type(row["Layers"]) is not list
        ):
            contract.fail("archive_compatibility_mismatch")
        actual.add(
            (
                digests.get(contract.relative(row["Config"])),
                tuple(
                    digests.get(contract.relative(name))
                    for name in row["Layers"]
                ),
            )
        )
    if actual != selected or len(actual) != len(compatibility):
        contract.fail("archive_compatibility_mismatch")


def _check_oci(digests, sizes, small, images):
    """Bind containerd roots to their supported linux/amd64 graph.

    Other-platform references may lack locally cached blobs. Compressed
    layers bind their stored bytes; their diff-ids are not rehashed here.
    """
    if image_build._json(small.get("oci-layout")) != {
        "imageLayoutVersion": "1.0.0"
    }:
        contract.fail("archive_layout_invalid")
    _check_blob_names(digests)
    descriptors, expected = _oci_roots(small, images)
    selected = set()
    for root in descriptors:
        value = _oci_manifest(root, digests, sizes, small)
        selected.add(_oci_image(value, root, expected, digests, sizes, small))
    _check_compatibility(digests, small, selected)
    return "docker-save-oci-linux-amd64"


def check_archive(fd, images):
    """Verify the bounded archive graph through a duplicate of the given FD."""
    digests, sizes, small = _read_members(fd)
    if "index.json" in small or "oci-layout" in small:
        return _check_oci(digests, sizes, small, images)
    return _check_classic(digests, small, images)
