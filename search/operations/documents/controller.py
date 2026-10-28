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


def _selected_resources(bundle_path, manifest_sha256, resource_ids):
    if (
        not isinstance(resource_ids, (list, tuple))
        or not 1 <= len(resource_ids) <= MAX_RESOURCES
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
    CatalogBundle.load(bundle_path, expected_manifest_sha256=manifest_sha256)
    descriptor = private_io.open_directory(bundle_path, mode=0o500)
    try:
        manifest_bytes = private_io.read_file(
            descriptor, "bundle-manifest.json", mode=0o400, max_bytes=64 * 1024
        )
        if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
            raise ProfileError("document_catalog_changed")
        manifest = strict_json(manifest_bytes)
        artifact = manifest["artifacts"]["corpus.v2.jsonl.gz"]
        raw = private_io.read_file(
            descriptor,
            "corpus.v2.jsonl.gz",
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


@contextmanager
def _state(path):
    descriptor = private_io.open_directory(path, create=True)
    lock = None
    try:
        lock = os.open(
            ".profiles.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
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
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
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


def _extraction(acquired, image, expected_format, encoding, header_policy, descriptor):
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


def run(
    bundle_path,
    manifest_sha256,
    state_root,
    resource_ids,
    *,
    refresh=False,
    encoding="utf-8",
    header_policy="first_record",
    source_root=None,
):
    """Write private profiles and return IDs/statuses without source text or URLs."""
    from search.operations.documents import extractor, source

    if (
        type(refresh) is not bool
        or type(encoding) is not str
        or type(header_policy) is not str
        or encoding not in ENCODINGS
        or header_policy not in HEADER_POLICIES
    ):
        raise ProfileError("document_extraction_policy_invalid")
    selected = _selected_resources(bundle_path, manifest_sha256, resource_ids)
    state_root = _external_root(state_root)
    if (
        state_root == Path(bundle_path)
        or state_root in Path(bundle_path).parents
        or Path(bundle_path) in state_root.parents
    ):
        raise ProfileError("document_state_overlaps_catalog")
    source_root = (
        Path(source_root)
        if source_root is not None
        else Path(__file__).resolve().parents[3]
    )
    rows = []
    with _state(state_root) as descriptor:
        acquisition_fd = _child(descriptor, "acquisition")
        os.close(acquisition_fd)
        extractions = _child(descriptor, "extractions")
        profiles = None
        try:
            profiles = _child(descriptor, "profiles")
            image = extractor.build_image(source_root)
            private_io.verify_directory(descriptor, state_root)
            for dataset_id, resource in selected:
                row = {"dataset_id": dataset_id, "resource_id": resource["id"]}
                try:
                    acquired = source.acquire(
                        state_root / "acquisition", resource, refresh=refresh
                    )
                    row["acquisition_cache_hit"] = acquired["cache_hit"]
                    result, hit = _extraction(
                        acquired,
                        image,
                        resource["format"].strip().upper(),
                        encoding,
                        header_policy,
                        extractions,
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
                        "policy": {
                            "encoding": encoding,
                            "header_policy": header_policy,
                        },
                        "result": result,
                    }
                    profile_sha256 = digest(profile)
                    _publish(profiles, profile_sha256 + ".json", profile)
                    row.update(
                        status=result["status"],
                        extraction_cache_hit=hit,
                        profile_sha256=profile_sha256,
                        profile_file="profiles/" + profile_sha256 + ".json",
                    )
                except (source.SourceError, extractor.ExtractorError) as error:
                    code = str(error)
                    if code == "document_extractor_cleanup_failed":
                        raise
                    row.update(
                        status="failed",
                        code=code
                        if re.fullmatch(r"document_[a-z_]{1,80}", code)
                        else "document_resource_failed",
                    )
                rows.append(row)
        finally:
            try:
                for child, name in (
                    (profiles, "profiles"),
                    (extractions, "extractions"),
                ):
                    if child is not None:
                        try:
                            private_io.verify_directory(child, state_root / name)
                        except (OSError, ValueError) as error:
                            raise ProfileError("document_state_changed") from error
            finally:
                if profiles is not None:
                    os.close(profiles)
                os.close(extractions)
    failed = sum(row["status"] in {"failed", "parse_error"} for row in rows)
    return {
        "schema_version": 1,
        "status": "partial" if failed else "completed",
        "catalog_manifest_sha256": manifest_sha256,
        "selected_count": len(rows),
        "failed_count": failed,
        "acquisition_cache_hits": sum(
            row.get("acquisition_cache_hit", False) for row in rows
        ),
        "extraction_cache_hits": sum(
            row.get("extraction_cache_hit", False) for row in rows
        ),
        "resources": rows,
    }
