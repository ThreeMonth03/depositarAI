"""Profile explicitly selected public resources using bounded Fess extraction."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat

from search.catalog.bundle import CatalogBundle
from search.operations.metadata.source import strict_json
from search.operations.shared import private_io


MAX_RESOURCES = 20
MAX_RECORD_BYTES = 512 * 1024
UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ENCODINGS = frozenset({"utf-8", "cp950", "utf-16"})
HEADER_POLICIES = frozenset({"first_record", "absent"})
ERROR_CODES = frozenset(
    {
        "document_state_path_invalid",
        "document_selection_invalid",
        "document_catalog_changed",
        "document_resource_missing_or_ambiguous",
        "document_resource_format_unsupported",
        "document_state_lock_invalid",
        "document_state_busy",
        "document_profile_record_conflict",
        "document_extraction_cache_invalid",
        "document_extraction_content_mismatch",
        "document_extraction_policy_invalid",
        "document_state_overlaps_catalog",
        "document_profile_cache_invalid",
        "document_state_changed",
    }
)


class ProfileError(ValueError):
    """A profile boundary failed; the message is a fixed diagnostic code."""


def digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def _external_root(path):
    path = Path(path)
    if (
        not path.is_absolute()
        or path.resolve() != path
        or ".git" in path.parts
        or any((parent / ".git").exists() for parent in (path, *path.parents))
    ):
        raise ProfileError("document_state_path_invalid")
    return path


def _selected_resources(
    bundle_path,
    manifest_sha256,
    resource_ids,
    *,
    max_resources=MAX_RESOURCES,
    metadata_only=False,
):
    if (
        not isinstance(resource_ids, (list, tuple))
        or not 1 <= len(resource_ids) <= max_resources
        or any(
            type(value) is not str or UUID.fullmatch(value) is None
            for value in resource_ids
        )
        or len(set(resource_ids)) != len(resource_ids)
        or type(manifest_sha256) is not str
        or SHA256.fullmatch(manifest_sha256) is None
    ):
        raise ProfileError("document_selection_invalid")
    bundle_path = Path(bundle_path)
    bundle = CatalogBundle.load(bundle_path, expected_manifest_sha256=manifest_sha256)
    if metadata_only and any(row["resource_context"] for row in bundle.documents):
        raise ProfileError("document_selection_invalid")
    expanded = _catalog_artifact(bundle_path, manifest_sha256, "corpus.v2.jsonl.gz")
    requested = set(resource_ids)
    matches = {identifier: [] for identifier in resource_ids}
    for line in expanded.splitlines():
        dataset = json.loads(line)
        for resource in dataset["resources"]:
            if resource["id"] in requested:
                matches[resource["id"]].append((dataset["dataset_id"], resource))
    if any(len(found) != 1 for found in matches.values()):
        raise ProfileError("document_resource_missing_or_ambiguous")
    selected = [matches[identifier][0] for identifier in resource_ids]
    if any(
        resource["format"].strip().upper() not in {"CSV", "PDF"}
        for _, resource in selected
    ):
        raise ProfileError("document_resource_format_unsupported")
    return selected


def _catalog_artifact(bundle_path, manifest_sha256, name):
    """Read an artifact of an already validated bundle with its manifest binding."""
    descriptor = private_io.open_directory(bundle_path, mode=0o500)
    try:
        manifest_bytes = private_io.read_file(
            descriptor, "bundle-manifest.json", mode=0o400, max_bytes=64 * 1024
        )
        if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
            raise ProfileError("document_catalog_changed")
        manifest = strict_json(manifest_bytes)
        artifact = manifest["artifacts"][name]
        raw = private_io.read_file(
            descriptor,
            name,
            mode=0o400,
            max_bytes=64 * 1024**2,
        )
        if (
            hashlib.sha256(raw).hexdigest() != artifact["sha256"]
            or len(raw) != artifact["bytes"]
        ):
            raise ProfileError("document_catalog_changed")
    finally:
        os.close(descriptor)
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        expanded = stream.read(artifact["expanded_bytes"] + 1)
    if len(expanded) != artifact["expanded_bytes"]:
        raise ProfileError("document_catalog_changed")
    return expanded


@contextmanager
def _state(path, *, create=True, read_only=False):
    descriptor = private_io.open_directory(path, create=create)
    lock = None
    try:
        lock = os.open(
            ".profiles.lock",
            (os.O_RDONLY if read_only else os.O_RDWR | (os.O_CREAT if create else 0))
            | os.O_NOFOLLOW
            | os.O_NONBLOCK,
            0o600,
            dir_fd=descriptor,
        )
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ProfileError("document_state_lock_invalid")
        fcntl.flock(
            lock, (fcntl.LOCK_SH if read_only else fcntl.LOCK_EX) | fcntl.LOCK_NB
        )
        private_io.verify_directory(descriptor, path)
        try:
            yield descriptor
        finally:
            try:
                private_io.verify_directory(descriptor, path)
            except (OSError, ValueError) as error:
                raise ProfileError("document_state_changed") from error
    except BlockingIOError as error:
        raise ProfileError("document_state_busy") from error
    finally:
        if lock is not None:
            os.close(lock)
        os.close(descriptor)


def _read_record(descriptor, name):
    try:
        return strict_json(
            private_io.read_file(
                descriptor, name, mode=0o600, max_bytes=MAX_RECORD_BYTES
            )
        )
    except FileNotFoundError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise ProfileError("document_profile_cache_invalid") from error


def _publish(descriptor, name, value):
    try:
        private_io.write_json(descriptor, name, value, max_bytes=MAX_RECORD_BYTES)
    except FileExistsError:
        if private_io.canonical(_read_record(descriptor, name)) != private_io.canonical(
            value
        ):
            raise ProfileError("document_profile_record_conflict") from None


def _child(descriptor, name):
    try:
        return private_io.open_child_directory(descriptor, name, create=True)
    except FileExistsError:
        return private_io.open_child_directory(descriptor, name)


def _extraction(
    acquired,
    image,
    expected_format,
    encoding,
    header_policy,
    descriptor,
    container_name,
):
    from search.operations.documents import extractor

    identity = {
        "schema_version": 1,
        "content_sha256": acquired["sha256"],
        "image_id": image["image_id"],
        "extractor_source_sha256": image["source_sha256"],
        "expected_format": expected_format,
        "encoding": encoding,
        "header_policy": header_policy,
    }
    name = digest(identity) + ".json"
    try:
        record = _read_record(descriptor, name)
    except FileNotFoundError:
        result = extractor.extract(
            acquired["local_path"],
            expected_format,
            encoding,
            header_policy,
            image=image,
            container_name=container_name,
        )
        cache_hit = False
    else:
        if (
            type(record) is not dict
            or set(record) != {"identity", "result"}
            or private_io.canonical(record["identity"])
            != private_io.canonical(identity)
        ):
            raise ProfileError("document_extraction_cache_invalid")
        result = record["result"]
        cache_hit = True
    extractor.validate_report(
        result, expected_format=expected_format, header_policy=header_policy
    )
    if result["input_sha256"] != acquired["sha256"]:
        raise ProfileError("document_extraction_content_mismatch")
    if not cache_hit:
        _publish(descriptor, name, {"identity": identity, "result": result})
    return result, cache_hit


def process(
    dataset_id,
    resource,
    manifest_sha256,
    state_root,
    image,
    encoding,
    header_policy,
    extractions,
    profiles,
    *,
    container_name,
):
    """Publish one immutable profile through the shared acquisition/parser path."""
    from search.operations.documents import source

    acquired = source.acquire(state_root / "acquisition", resource)
    result, hit = _extraction(
        acquired,
        image,
        resource["format"].strip().upper(),
        encoding,
        header_policy,
        extractions,
        container_name,
    )
    profile = {
        "schema_version": 1,
        "source": {
            "catalog_manifest_sha256": manifest_sha256,
            "dataset_id": dataset_id,
            "resource_id": resource["id"],
            "url_sha256": acquired["source_url_sha256"],
            "content_sha256": acquired["sha256"],
            "bytes": acquired["bytes"],
            "acquired_at": acquired["acquired_at"],
            "complete": True,
        },
        "extractor": {
            "image_id": image["image_id"],
            "source_sha256": image["source_sha256"],
        },
        "policy": {"encoding": encoding, "header_policy": header_policy},
        "result": result,
    }
    profile_sha256 = digest(profile)
    _publish(profiles, profile_sha256 + ".json", profile)
    return {
        "dataset_id": dataset_id,
        "resource_id": resource["id"],
        "status": result["status"],
        "profile_sha256": profile_sha256,
        "acquisition_cache_hit": acquired["cache_hit"],
        "extraction_cache_hit": hit,
    }
