"""Build and atomically publish an immutable public metadata bundle."""

import os
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from . import codec
from . import identity
from . import normalize
from . import policy
from . import snapshot
from . import storage
from search.catalog import policy as catalog_policy


def _make_manifest(
    artifacts: Mapping[str, Mapping[str, Any]],
    dataset_ids: Sequence[str],
    resource_count: int,
    counters: Mapping[str, Any],
) -> Tuple[Dict[str, Any], bytes, str]:
    ids_payload = b"".join(
        identifier.encode("ascii") + b"\n" for identifier in dataset_ids
    )
    core: Dict[str, Any] = {
        "schema_version": 1,
        "bundle_format": "depositar-public-metadata-v2",
        "artifacts": dict(artifacts),
        "dataset_count": len(dataset_ids),
        "resource_count": resource_count,
        "dataset_ids_sha256": codec.sha256_bytes(ids_payload),
        "builder": {
            "name": catalog_policy.BUNDLE_BUILDER_NAME,
            "source_sha256": identity.source_digest(),
            "python_minimum": "3.9",
        },
        "policy": policy.POLICY,
        "normalization_counters": dict(sorted(counters.items())),
    }
    bundle_digest = codec.sha256_bytes(codec.canonical_json(core))
    manifest = dict(core)
    manifest["bundle_id"] = bundle_digest
    manifest_bytes = codec.canonical_json(manifest) + b"\n"
    return manifest, manifest_bytes, bundle_digest


def build_bundle(input_path: os.PathLike, output_root: os.PathLike) -> Dict[str, Any]:
    """Validate *input_path* and atomically publish a deterministic bundle."""

    source = storage._absolute_without_resolving(input_path)
    root = storage._absolute_without_resolving(output_root)
    root_descriptor = storage._open_output_root(root)
    lock_descriptor: Optional[int] = None
    staging_name: Optional[str] = None
    staging_descriptor: Optional[int] = None
    try:
        try:
            common_path = os.path.commonpath((source, root))
        except ValueError as exc:
            raise policy.BundleError("source and output root are incompatible") from exc
        if common_path == root:
            raise policy.BundleError("source must not be inside the output root")

        lock_descriptor = storage._open_lock(root_descriptor)
        raw_records = snapshot.load_source(source)
        counters: Dict[str, Any] = {}
        normalized: List[
            Tuple[str, str, Dict[str, Any], Dict[str, Any], Dict[str, Any]]
        ] = []
        seen_ids = set()
        seen_urls = set()
        for raw_record in raw_records:
            dataset_id, dataset_url, corpus = normalize.normalize_package(
                raw_record, counters
            )
            if dataset_id in seen_ids:
                raise policy.BundleError("duplicate dataset UUID: %s" % dataset_id)
            if dataset_url in seen_urls:
                raise policy.BundleError("duplicate normalized dataset URL")
            seen_ids.add(dataset_id)
            seen_urls.add(dataset_url)
            fess = normalize.fess_document(corpus, counters)
            normalized.append((dataset_id, dataset_url, raw_record, corpus, fess))
        normalized.sort(key=lambda item: item[0])

        dataset_ids = [item[0] for item in normalized]
        source_rows = [item[2] for item in normalized]
        corpus_rows = [item[3] for item in normalized]
        fess_rows = [item[4] for item in normalized]
        resource_count = sum(item["resource_count"] for item in corpus_rows)

        staging_name, staging_descriptor = storage._make_staging_directory(
            root_descriptor
        )
        artifact_rows: Sequence[Tuple[str, Sequence[Mapping[str, Any]], int]] = (
            (policy.ARTIFACT_NAMES[0], source_rows, 1),
            (policy.ARTIFACT_NAMES[1], corpus_rows, 2),
            (policy.ARTIFACT_NAMES[2], fess_rows, 1),
        )
        artifacts: Dict[str, Dict[str, Any]] = {}
        for name, rows, schema_version in artifact_rows:
            expanded_bytes = storage.write_gzip_jsonl(staging_descriptor, name, rows)
            artifacts[name] = storage._artifact_description(
                staging_descriptor,
                name,
                len(rows),
                schema_version,
                expanded_bytes,
            )
        manifest, manifest_bytes, bundle_digest = _make_manifest(
            artifacts, dataset_ids, resource_count, counters
        )
        storage._write_plain(staging_descriptor, policy.MANIFEST_NAME, manifest_bytes)
        storage._fsync_directory(staging_descriptor)
        os.fchmod(staging_descriptor, 0o500)
        storage._fsync_directory(staging_descriptor)
        # Re-hash every sealed artifact immediately before publication.  This
        # catches accidental same-UID mutation after the manifest descriptors
        # were constructed instead of publishing a self-inconsistent bundle.
        storage._verify_existing(staging_descriptor, manifest_bytes, artifacts)

        destination_name = "sha256-%s" % bundle_digest
        destination = os.path.join(root, destination_name)
        try:
            storage._publish_directory(
                root_descriptor,
                staging_name,
                staging_descriptor,
                destination_name,
            )
            staging_name = None
            storage._fsync_directory(root_descriptor)
            created = True
        except FileExistsError:
            destination_descriptor = storage._open_child_directory(
                root_descriptor,
                destination_name,
                "bundle destination is not a safe directory",
            )
            try:
                storage._verify_existing(
                    destination_descriptor, manifest_bytes, artifacts
                )
            finally:
                os.close(destination_descriptor)
            os.fchmod(staging_descriptor, 0o700)
            storage._cleanup_directory_entry(
                root_descriptor, staging_name, staging_descriptor
            )
            staging_name = None
            storage._fsync_directory(root_descriptor)
            created = False

        return {
            "bundle_id": manifest["bundle_id"],
            "manifest_sha256": codec.sha256_bytes(manifest_bytes),
            "bundle_path": destination,
            "created": created,
            "dataset_count": len(dataset_ids),
            "resource_count": resource_count,
            "manifest": manifest,
        }
    finally:
        if staging_name is not None and staging_descriptor is not None:
            try:
                os.fchmod(staging_descriptor, 0o700)
                storage._cleanup_directory_entry(
                    root_descriptor, staging_name, staging_descriptor
                )
            except OSError:
                pass
        if staging_descriptor is not None:
            os.close(staging_descriptor)
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        os.close(root_descriptor)
