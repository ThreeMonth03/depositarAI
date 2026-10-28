"""Import explicit private profiles into the existing sealed catalog builder."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re

from search.catalog import resources
from search.catalog.build import normalize, snapshot
from search.operations.documents import controller, extractor, source
from search.operations.metadata.source import strict_json
from search.operations.shared import private_io


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ERROR_CODES = frozenset(
    {
        "document_enrichment_profile_invalid",
        "document_enrichment_provenance_invalid",
        "document_enrichment_selection_invalid",
        "document_enrichment_source_mismatch",
        "document_enrichment_result_invalid",
        "document_enrichment_content_mismatch",
    }
)


class EnrichmentError(ValueError):
    """An explicit profile does not match this catalog or extraction contract."""


def _read_profile(path):
    path = Path(path)
    try:
        parent = controller._external_root(path.parent)
        if re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is None:
            raise ValueError("profile filename must be its SHA256")
        descriptor = private_io.open_directory(parent)
        try:
            raw = private_io.read_file(
                descriptor, path.name, mode=0o600, max_bytes=controller.MAX_RECORD_BYTES
            )
            private_io.verify_directory(descriptor, parent)
        finally:
            os.close(descriptor)
        profile = strict_json(raw)
        digest = hashlib.sha256(raw).hexdigest()
        if path.stem != digest or private_io.canonical(profile) != raw:
            raise ValueError("profile canonical digest mismatch")
        return digest, profile
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise EnrichmentError("document_enrichment_profile_invalid") from error


def _profile_fields(profile, manifest_sha256):
    if (
        type(profile) is not dict
        or set(profile) != {"schema_version", "source", "extractor", "policy", "result"}
        or type(profile["schema_version"]) is not int
        or profile["schema_version"] != 1
    ):
        raise EnrichmentError("document_enrichment_profile_invalid")
    origin, tool, policy = profile["source"], profile["extractor"], profile["policy"]
    if (
        type(origin) is not dict
        or set(origin)
        != {
            "catalog_manifest_sha256",
            "dataset_id",
            "resource_id",
            "url_sha256",
            "content_sha256",
            "bytes",
            "acquired_at",
            "complete",
        }
        or origin["catalog_manifest_sha256"] != manifest_sha256
        or type(origin["complete"]) is not bool
        or origin["complete"] is not True
        or any(
            type(origin[key]) is not str or _SHA256.fullmatch(origin[key]) is None
            for key in ("catalog_manifest_sha256", "url_sha256", "content_sha256")
        )
        or any(
            type(origin[key]) is not str
            or controller.UUID.fullmatch(origin[key]) is None
            for key in ("dataset_id", "resource_id")
        )
        or type(origin["bytes"]) is not int
        or not 1 <= origin["bytes"] <= source.MAX_FILE_BYTES
        or type(origin["acquired_at"]) is not int
        or origin["acquired_at"] < 0
        or type(tool) is not dict
        or set(tool) != {"image_id", "source_sha256"}
        or type(tool["source_sha256"]) is not str
        or _SHA256.fullmatch(tool["source_sha256"]) is None
        or type(tool["image_id"]) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", tool["image_id"]) is None
        or type(policy) is not dict
        or set(policy) != {"encoding", "header_policy"}
        or type(policy["encoding"]) is not str
        or policy["encoding"] not in controller.ENCODINGS
        or type(policy["header_policy"]) is not str
        or policy["header_policy"] not in controller.HEADER_POLICIES
    ):
        raise EnrichmentError("document_enrichment_provenance_invalid")


def load_profiles(paths, bundle_path, manifest_sha256, snapshot_path):
    """Return grounded enrichment plus an ID-only report; never fetch or parse files.

    Profiles must refer to the metadata-only bundle built from this exact snapshot.
    An explicit selection is bounded like profile-resources; omitted profiles never
    enter the index through a cache scan or an implicit directory glob.
    """
    if (
        not isinstance(paths, (list, tuple))
        or not 1 <= len(paths) <= controller.MAX_RESOURCES
    ):
        raise EnrichmentError("document_enrichment_selection_invalid")
    profiles = [_read_profile(path) for path in paths]
    for _, profile in profiles:
        _profile_fields(profile, manifest_sha256)
    identifiers = [profile["source"]["resource_id"] for _, profile in profiles]
    if len(set(identifiers)) != len(identifiers):
        raise EnrichmentError("document_enrichment_selection_invalid")
    try:
        selected = controller._selected_resources(
            bundle_path, manifest_sha256, identifiers
        )
    except controller.ProfileError as error:
        raise EnrichmentError("document_enrichment_selection_invalid") from error
    normalized = [
        (record, normalize.normalize_package(record, {})[2])
        for record in snapshot.load_source(str(snapshot_path))
    ]
    normalized.sort(key=lambda pair: pair[1]["dataset_id"])
    candidate = hashlib.sha256()
    for record, _ in normalized:
        candidate.update(private_io.canonical(record))
    sealed_source = controller._catalog_artifact(
        Path(bundle_path), manifest_sha256, "source.jsonl.gz"
    )
    if candidate.digest() != hashlib.sha256(sealed_source).digest():
        raise EnrichmentError("document_enrichment_source_mismatch")
    corpus = {item["dataset_id"]: item for _, item in normalized}
    contexts, report = {}, []
    for (digest, profile), (dataset_id, resource) in zip(profiles, selected):
        origin, policy = profile["source"], profile["policy"]
        identity = source.source_identity(resource)
        if (
            origin["dataset_id"] != dataset_id
            or origin["url_sha256"] != identity["source_url_sha256"]
        ):
            raise EnrichmentError("document_enrichment_source_mismatch")
        try:
            result = extractor.validate_report(
                profile["result"],
                expected_format=identity["format"],
                header_policy=policy["header_policy"],
            )
        except (ValueError, TypeError) as error:
            raise EnrichmentError("document_enrichment_result_invalid") from error
        if result["input_sha256"] != origin["content_sha256"]:
            raise EnrichmentError("document_enrichment_content_mismatch")
        row = {
            "dataset_id": dataset_id,
            "resource_id": resource["id"],
            "profile_sha256": digest,
        }
        reason = None
        if result["status"] != "ok":
            reason = result["status"]
        elif identity["format"] == "CSV" and policy["header_policy"] != "first_record":
            reason = "csv_header_absent"
        elif identity["format"] == "CSV" and not result["columns"]:
            reason = "csv_columns_empty"
        elif identity["format"] == "PDF" and not result["text"].strip():
            reason = "pdf_text_empty"
        if reason:
            report.append({**row, "status": "skipped", "reason": reason})
            continue
        prepared = normalize.resource_profile_content(
            identity["format"],
            name=resource["name"],
            columns=result["columns"],
            text=result["text"],
            counters={},
        )
        if not prepared["index_text"]:
            report.append({**row, "status": "skipped", "reason": "no_public_text"})
            continue
        raw = {
            "context": {
                "resource_id": resource["id"],
                "name": prepared["name"],
                "format": identity["format"],
                "url": resources.resource_url(
                    corpus[dataset_id]["url"], resource["id"]
                ),
                "columns": prepared["columns"],
                "excerpt": prepared["excerpt"],
            },
            "index_text": prepared["index_text"],
            "profile_sha256": digest,
            "source_manifest_sha256": manifest_sha256,
            "content_sha256": origin["content_sha256"],
            "extractor_image_id": profile["extractor"]["image_id"],
            "extractor_source_sha256": profile["extractor"]["source_sha256"],
            "acquired_at": origin["acquired_at"],
            "truncated": result["text_truncated"] or prepared["truncated"],
        }
        entries = contexts.setdefault(dataset_id, [])
        entries.append(raw)
        entries.sort(key=lambda item: item["context"]["resource_id"])
        try:
            resources.validate_enrichment_resources(entries)
        except ValueError as error:
            raise EnrichmentError("document_enrichment_selection_invalid") from error
        report.append({**row, "status": "included"})
    return contexts, {
        "selected_count": len(report),
        "included_count": sum(row["status"] == "included" for row in report),
        "skipped_count": sum(row["status"] == "skipped" for row in report),
        "resources": report,
    }
