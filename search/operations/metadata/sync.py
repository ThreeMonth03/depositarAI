"""Sync only public metadata into a verified private cache, never a live index.

``baseline`` is None or (sealed_bundle_path, manifest_sha256). TTL is opt-in;
the default always performs two fresh full scans. Offline snapshots never use
TTL. All successful results are staged, never authorized for promotion.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from search.catalog.build import (
    builder,
    normalize,
    policy,
    snapshot as snapshots,
    storage as build_storage,
)
from search.catalog.bundle import MAX_MANIFEST_BYTES, CatalogBundle
from search.operations.metadata import source
from search.operations.metadata.storage import (
    MAX_RECORD_BYTES as MAX_RECORD_BYTES,
    SyncError as SyncError,
    real_path as _real_path,
    same_file as _same_file,
    file_bytes as _file_bytes,
    read_record as _json_record,
    write_record as _write_record,
    private_directory as _private_directory,
)
from search.operations.shared import private_io


MAX_RUNS = 128
MAX_TTL_SECONDS = 7 * 86400
ROOT = Path(__file__).resolve().parents[3]
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MARKER = {
    "schema_version": 1,
    "kind": "depositar-public-metadata-cache",
    "endpoint": source.ENDPOINT,
}


def _verify_cache_directories(root, root_fd, folders, run_id, run_fd):
    try:
        private_io.verify_directory(root_fd, root)
        for name, descriptor in folders.items():
            private_io.verify_directory(descriptor, root / name)
        private_io.verify_directory(run_fd, root / "runs" / run_id)
    except ValueError as error:
        raise SyncError("metadata cache directory changed during sync") from error


@contextmanager
def _locked_cache(root):
    root = _real_path(root)
    if root in (Path("/"), Path.home(), ROOT):
        raise SyncError("use a dedicated private cache directory")
    root_fd = private_io.open_directory(root)
    lock_fd = None
    folders = {}
    try:
        lock_fd = os.open(
            ".sync.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root_fd
        )
        info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise SyncError("unsafe metadata sync lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("another metadata sync is running") from None
        if not _same_file(
            info, os.stat(".sync.lock", dir_fd=root_fd, follow_symlinks=False)
        ):
            raise SyncError("metadata sync lock changed while being opened")
        try:
            marker = _json_record(root_fd, "cache.json")
        except FileNotFoundError:
            if set(os.listdir(root_fd)) != {".sync.lock"}:
                raise SyncError("unmarked cache is not empty; inspect it before reuse")
            for name in ("snapshots", "bundles", "runs"):
                folders[name] = _private_directory(root_fd, name, create=True)
            _write_record(root_fd, "cache.json", MARKER)
        else:
            if (
                type(marker) is not dict
                or marker != MARKER
                or type(marker.get("schema_version")) is not int
            ):
                raise SyncError("invalid public metadata cache marker")
            for name in ("snapshots", "bundles", "runs"):
                folders[name] = _private_directory(root_fd, name)
        if len(os.listdir(folders["runs"])) >= MAX_RUNS:
            raise SyncError("metadata cache run limit reached; archive evidence first")
        yield root, root_fd, folders
    finally:
        for fd in folders.values():
            os.close(fd)
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(root_fd)


def _baseline(value):
    if value is None:
        return None
    if not isinstance(value, tuple) or len(value) != 2:
        raise SyncError("baseline must be (bundle_path, manifest_sha256)")
    path, manifest = value
    return CatalogBundle.load(_real_path(path), expected_manifest_sha256=manifest)


def compare(before, after):
    old = {row["dataset_id"]: row for row in before.documents} if before else {}
    new = {row["dataset_id"]: row for row in after.documents}
    added, missing = sorted(new.keys() - old.keys()), sorted(old.keys() - new.keys())
    changed = []
    for identifier in sorted(old.keys() & new.keys()):
        a, b = old[identifier], new[identifier]
        if a["depositar_document_sha256"] != b["depositar_document_sha256"]:
            changed.append(
                {
                    "dataset_id": identifier,
                    "fields": sorted(
                        key
                        for key in a.keys() | b.keys()
                        if key
                        not in {"depositar_bundle_id", "depositar_document_sha256"}
                        and a.get(key) != b.get(key)
                    ),
                    "before_sha256": a["depositar_document_sha256"],
                    "after_sha256": b["depositar_document_sha256"],
                }
            )
    reasons = []
    if missing:
        reasons.append("missing_datasets_require_visibility_and_completeness_review")
    if old and len(missing) * 10 >= len(old):
        reasons.append("at_least_ten_percent_of_baseline_missing")
    if old and len(new) > len(old) * 2:
        reasons.append("dataset_count_more_than_doubled")
    return {
        "status": "initial_snapshot"
        if before is None
        else ("review_required" if added or missing or changed else "no_search_change"),
        "baseline_count": len(old),
        "candidate_count": len(new),
        "added": added,
        "missing": missing,
        "changed": changed,
        "unchanged_count": len(old.keys() & new.keys()) - len(changed),
        "review_reasons": reasons,
        "same_bundle": before is not None and before.bundle_id == after.bundle_id,
        "promotion_allowed": False,
    }


def _verified_latest(root, root_fd, folders):
    value = _json_record(root_fd, "latest.json")
    fields = {
        "schema_version",
        "endpoint",
        "run_id",
        "snapshot_sha256",
        "bundle_id",
        "manifest_sha256",
        "fetched_at",
        "source_consistency",
        "document_count",
    }
    if (
        type(value) is not dict
        or set(value) != fields
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["endpoint"] != source.ENDPOINT
        or not isinstance(value["run_id"], str)
        or re.fullmatch(r"[0-9a-f]{32}", value["run_id"]) is None
        or any(
            not isinstance(value[key], str) or SHA256.fullmatch(value[key]) is None
            for key in ("snapshot_sha256", "bundle_id", "manifest_sha256")
        )
        or type(value["fetched_at"]) not in (int, float)
        or not math.isfinite(value["fetched_at"])
        or value["source_consistency"]
        not in {"two_matching_full_scans", "offline_not_checked"}
        or type(value["document_count"]) is not int
        or not 1 <= value["document_count"] <= policy.MAX_ROWS
    ):
        raise SyncError("invalid latest metadata cache pointer")
    run_fd = _private_directory(folders["runs"], value["run_id"])
    try:
        if "failure.json" in os.listdir(run_fd):
            raise SyncError("latest metadata run is quarantined")
        receipt = _json_record(run_fd, "result.json")
        for key in (
            "snapshot_sha256",
            "bundle_id",
            "manifest_sha256",
            "fetched_at",
            "source_consistency",
            "document_count",
        ):
            if receipt.get(key) != value[key]:
                raise SyncError("latest pointer does not match its immutable receipt")
    finally:
        os.close(run_fd)
    snapshot = root / "snapshots" / ("sha256-" + value["snapshot_sha256"] + ".jsonl.gz")
    raw = _file_bytes(
        folders["snapshots"],
        snapshot.name,
        mode=0o400,
        limit=policy.MAX_COMPRESSED_BYTES,
    )
    if hashlib.sha256(raw).hexdigest() != value["snapshot_sha256"]:
        raise SyncError("cached metadata snapshot digest mismatch")
    bundle_path = root / "bundles" / ("sha256-" + value["bundle_id"])
    catalog = CatalogBundle.load(
        bundle_path, expected_manifest_sha256=value["manifest_sha256"]
    )
    bundle_fd = private_io.open_directory(bundle_path, mode=0o500)
    try:
        manifest_raw = _file_bytes(
            bundle_fd, "bundle-manifest.json", mode=0o400, limit=MAX_MANIFEST_BYTES
        )
    finally:
        os.close(bundle_fd)
    if hashlib.sha256(manifest_raw).hexdigest() != value["manifest_sha256"]:
        raise SyncError("cached bundle manifest changed after verification")
    manifest = source.strict_json(manifest_raw)
    if (
        manifest["artifacts"]["source.jsonl.gz"]["sha256"] != value["snapshot_sha256"]
        or len(catalog.documents) != value["document_count"]
    ):
        raise SyncError("cached snapshot and sealed bundle identities disagree")
    return value, snapshot, bundle_path, catalog


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise SyncError("metadata sync deadline exceeded")


def sync(
    cache_root,
    *,
    baseline=None,
    snapshot=None,
    ttl_seconds=0,
    force=False,
    fetch=None,
    profile_files=(),
    profile_set=None,
):
    """Return a staged immutable snapshot/bundle/diff; never change active state."""
    if (
        type(ttl_seconds) not in (int, float)
        or not math.isfinite(ttl_seconds)
        or not 0 <= ttl_seconds <= MAX_TTL_SECONDS
        or type(force) is not bool
        or not isinstance(profile_files, (list, tuple))
        or len(profile_files) > 20
        or profile_set is not None
        and not isinstance(profile_set, (str, Path))
        or profile_files
        and profile_set is not None
    ):
        raise SyncError("TTL, force or profile selection is outside its bounded policy")
    if snapshot is not None and fetch is not None:
        raise SyncError("offline snapshot and network fetch are mutually exclusive")
    before = _baseline(
        baseline
    )  # Refuse bad active identity before any network request.
    started = time.monotonic()
    deadline = started + source.DEADLINE_SECONDS
    with _locked_cache(cache_root) as (root, root_fd, folders):
        run_id = uuid.uuid4().hex
        run_fd = _private_directory(folders["runs"], run_id, create=True)
        try:
            _write_record(
                run_fd, "started.json", {"schema_version": 1, "run_id": run_id}
            )
            cache_hit, built, latest = False, None, None
            if snapshot is None and ttl_seconds > 0 and not force:
                try:
                    latest, snapshot_path, bundle_path, catalog = _verified_latest(
                        root, root_fd, folders
                    )
                except FileNotFoundError:
                    if "latest.json" in os.listdir(root_fd):
                        raise SyncError("latest metadata cache is incomplete") from None
                if latest is not None:
                    age = time.time() - latest["fetched_at"]
                    if age < 0:
                        raise SyncError("cached metadata timestamp is in the future")
                    cache_hit = age <= ttl_seconds
            if not cache_hit:
                records = (
                    source.stable_download(deadline, fetch)
                    if snapshot is None
                    else snapshots.load_source(str(_real_path(snapshot)))
                )
                _check_deadline(deadline)
                _verify_cache_directories(root, root_fd, folders, run_id, run_fd)
                ordered = sorted(
                    (
                        (normalize.normalize_package(item, {})[0], item)
                        for item in records
                    ),
                    key=lambda pair: pair[0],
                )
                if len({identifier for identifier, _ in ordered}) != len(ordered):
                    raise SyncError("metadata snapshot contains duplicate dataset IDs")
                rows = [item for _, item in ordered]
                if any(
                    len(source.canonical(item) + b"\n") > policy.MAX_LINE_BYTES
                    for item in rows
                ):
                    raise SyncError("metadata snapshot exceeds its line limit")
                build_storage.write_gzip_jsonl(run_fd, "snapshot.jsonl.gz", rows)
                incoming = root / "runs" / run_id / "snapshot.jsonl.gz"
                built = builder.build_bundle(incoming, root / "bundles")
                bundle_path = Path(built["bundle_path"])
                catalog = CatalogBundle.load(
                    bundle_path, expected_manifest_sha256=built["manifest_sha256"]
                )
                raw = _file_bytes(
                    run_fd,
                    "snapshot.jsonl.gz",
                    mode=0o400,
                    limit=policy.MAX_COMPRESSED_BYTES,
                )
                snapshot_sha = hashlib.sha256(raw).hexdigest()
                snapshot_path = (
                    root / "snapshots" / ("sha256-" + snapshot_sha + ".jsonl.gz")
                )
                try:
                    os.link(
                        "snapshot.jsonl.gz",
                        snapshot_path.name,
                        src_dir_fd=run_fd,
                        dst_dir_fd=folders["snapshots"],
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    existing = _file_bytes(
                        folders["snapshots"],
                        snapshot_path.name,
                        mode=0o400,
                        limit=policy.MAX_COMPRESSED_BYTES,
                    )
                    if hashlib.sha256(existing).hexdigest() != snapshot_sha:
                        raise SyncError(
                            "existing immutable metadata snapshot is corrupted"
                        )
                os.unlink("snapshot.jsonl.gz", dir_fd=run_fd)
                os.fsync(run_fd)
                os.fsync(folders["snapshots"])
                latest = {
                    "schema_version": 1,
                    "endpoint": source.ENDPOINT,
                    "run_id": run_id,
                    "snapshot_sha256": snapshot_sha,
                    "bundle_id": catalog.bundle_id,
                    "manifest_sha256": catalog.manifest_sha256,
                    "fetched_at": time.time(),
                    "source_consistency": "two_matching_full_scans"
                    if snapshot is None
                    else "offline_not_checked",
                    "document_count": len(catalog.documents),
                }
            # The metadata cache retains only downloaded snapshot identity. Rebuild
            # the cheap baseline on cache hits so selected profile files never leak
            # between invocations through a previous enriched bundle.
            if cache_hit:
                built = builder.build_bundle(snapshot_path, root / "bundles")
                bundle_path = Path(built["bundle_path"])
                catalog = CatalogBundle.load(
                    bundle_path, expected_manifest_sha256=built["manifest_sha256"]
                )
            profile_report = {
                "selected_count": 0,
                "included_count": 0,
                "skipped_count": 0,
                "resources": [],
            }
            if profile_files or profile_set is not None:
                from search.operations.documents.enrichment import load_profiles

                baseline_source = built["manifest"]["artifacts"]["source.jsonl.gz"]
                context, profile_report = load_profiles(
                    profile_files,
                    bundle_path,
                    catalog.manifest_sha256,
                    snapshot_path,
                    profile_set=profile_set,
                    deadline=deadline,
                )
                built = builder.build_bundle(
                    snapshot_path, root / "bundles", resource_context=context
                )
                if built["manifest"]["artifacts"]["source.jsonl.gz"] != baseline_source:
                    from search.operations.documents.enrichment import EnrichmentError

                    raise EnrichmentError("document_enrichment_source_mismatch")
                bundle_path = Path(built["bundle_path"])
                catalog = CatalogBundle.load(
                    bundle_path, expected_manifest_sha256=built["manifest_sha256"]
                )
            latest = {
                **latest,
                "run_id": run_id,
                "bundle_id": catalog.bundle_id,
                "manifest_sha256": catalog.manifest_sha256,
            }
            _check_deadline(deadline)
            _verify_cache_directories(root, root_fd, folders, run_id, run_fd)
            report = {
                "schema_version": 1,
                "status": "ready",
                "run_id": run_id,
                "snapshot_path": str(snapshot_path),
                "snapshot_sha256": latest["snapshot_sha256"],
                "bundle_path": str(bundle_path),
                "bundle_id": catalog.bundle_id,
                "manifest_sha256": catalog.manifest_sha256,
                "document_count": len(catalog.documents),
                "cache_hit": cache_hit,
                "bundle_created": built["created"] if built else False,
                "unchanged": before is not None
                and before.bundle_id == catalog.bundle_id
                and before.manifest_sha256 == catalog.manifest_sha256,
                "source_endpoint": source.ENDPOINT,
                "source_consistency": latest["source_consistency"],
                "fetched_at": latest["fetched_at"],
                "diff": compare(before, catalog),
                "elapsed_seconds": time.monotonic() - started,
                "receipt_path": str(root / "runs" / run_id / "result.json"),
                "promotion_allowed": False,
                "live_index_modified": False,
                "models_called": False,
                "resource_profiles": profile_report,
            }
            _write_record(run_fd, "result.json", report)
            _write_record(root_fd, "latest.json", latest, replace=True)
            return report
        except Exception as error:
            _write_record(
                run_fd,
                "failure.json",
                {
                    "schema_version": 1,
                    "status": "quarantined",
                    "error_type": type(error).__name__,
                    "promotion_allowed": False,
                    "live_index_modified": False,
                },
            )
            raise
        finally:
            os.close(run_fd)
