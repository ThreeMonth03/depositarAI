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
import urllib.error
import uuid

from search.catalog.build import (
    builder,
    normalize,
    policy,
    snapshot as snapshots,
    storage as build_storage,
)
from search.catalog.bundle import MAX_MANIFEST_BYTES, CatalogBundle
from search.operations.metadata import source, storage
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
FAILURE_CODES = frozenset(
    {
        "timeout",
        "source_incomplete",
        "source_count_changed",
        "source_content_changed",
        "source_invalid",
        "clock_invalid",
        "clock_reversed",
        "clock_future",
        "evidence_invalid",
        "io_failure",
        "sync_invalid",
        "unexpected_failure",
    }
)


class ObservationError(storage.SyncError):
    """Refuse invalid observation evidence before publishing latest."""

    def __init__(self, message, *, code="evidence_invalid"):
        super().__init__(message)
        self.code = code


def _verify_cache_directories(root, root_fd, folders, run_id, run_fd):
    try:
        private_io.verify_directory(root_fd, root)
        for name, descriptor in folders.items():
            private_io.verify_directory(descriptor, root / name)
        private_io.verify_directory(run_fd, root / "runs" / run_id)
    except ValueError as error:
        raise storage.SyncError(
            "metadata cache directory changed during sync"
        ) from error


def _admit_cache_layout(root_fd, folders):
    """Admit cache contents; retain each opened FD in the caller-owned map."""
    try:
        marker = storage.read_record(root_fd, "cache.json")
    except FileNotFoundError:
        if set(os.listdir(root_fd)) != {".sync.lock"}:
            raise storage.SyncError(
                "unmarked cache is not empty; inspect it before reuse"
            )
        for name in ("snapshots", "bundles", "runs"):
            folders[name] = storage.private_directory(
                root_fd, name, create=True
            )
        storage.write_record(root_fd, "cache.json", MARKER)
    else:
        if (
            type(marker) is not dict
            or marker != MARKER
            or type(marker.get("schema_version")) is not int
        ):
            raise storage.SyncError("invalid public metadata cache marker")
        for name in ("snapshots", "bundles", "runs"):
            folders[name] = storage.private_directory(root_fd, name)
    if len(os.listdir(folders["runs"])) >= MAX_RUNS:
        raise storage.SyncError(
            "metadata cache run limit reached; archive evidence first"
        )


@contextmanager
def _locked_cache(root):
    root = storage.real_path(root)
    if root in (Path("/"), Path.home(), ROOT):
        raise storage.SyncError("use a dedicated private cache directory")
    root_fd = private_io.open_directory(root)
    lock_fd = None
    folders = {}
    try:
        lock_fd = os.open(
            ".sync.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
        info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise storage.SyncError("unsafe metadata sync lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise storage.SyncError(
                "another metadata sync is running"
            ) from None
        if not storage.same_file(
            info, os.stat(".sync.lock", dir_fd=root_fd, follow_symlinks=False)
        ):
            raise storage.SyncError(
                "metadata sync lock changed while being opened"
            )
        _admit_cache_layout(root_fd, folders)
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
        raise storage.SyncError(
            "baseline must be (bundle_path, manifest_sha256)"
        )
    path, manifest = value
    return CatalogBundle.load(
        storage.real_path(path), expected_manifest_sha256=manifest
    )


def compare(before, after):
    old = (
        {row["dataset_id"]: row for row in before.documents} if before else {}
    )
    new = {row["dataset_id"]: row for row in after.documents}
    added, missing = (
        sorted(new.keys() - old.keys()),
        sorted(old.keys() - new.keys()),
    )
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
                        not in {
                            "depositar_bundle_id",
                            "depositar_document_sha256",
                        }
                        and a.get(key) != b.get(key)
                    ),
                    "before_sha256": a["depositar_document_sha256"],
                    "after_sha256": b["depositar_document_sha256"],
                }
            )
    reasons = []
    if missing:
        reasons.append(
            "missing_datasets_require_visibility_and_completeness_review"
        )
    if old and len(missing) * 10 >= len(old):
        reasons.append("at_least_ten_percent_of_baseline_missing")
    if old and len(new) > len(old) * 2:
        reasons.append("dataset_count_more_than_doubled")
    return {
        "status": "initial_snapshot"
        if before is None
        else (
            "review_required"
            if added or missing or changed
            else "no_search_change"
        ),
        "baseline_count": len(old),
        "candidate_count": len(new),
        "added": added,
        "missing": missing,
        "changed": changed,
        "unchanged_count": len(old.keys() & new.keys()) - len(changed),
        "review_reasons": reasons,
        "same_bundle": before is not None
        and before.bundle_id == after.bundle_id,
        "promotion_allowed": False,
    }


def _observation(kind, snapshot_sha256, run_id, interval=(None, None)):
    return {
        "schema_version": 1,
        "kind": kind,
        "clock": "operator_utc",
        "started_at": interval[0],
        "completed_at": interval[1],
        "source_endpoint": source.ENDPOINT,
        "snapshot_sha256": snapshot_sha256,
        "evidence_run_id": run_id,
    }


def _timestamp(value):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ObservationError(
            "invalid metadata operator timestamp", code="clock_invalid"
        )
    return value


def _validate_observation(value, latest, checked_at):
    fields = set(_observation("unknown_legacy", "", ""))
    if (
        type(value) is not dict
        or set(value) != fields
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["kind"]) is not str
        or value["kind"]
        not in {"complete_online", "unknown_offline", "unknown_legacy"}
        or value["clock"] != "operator_utc"
        or value["source_endpoint"] != source.ENDPOINT
        or value["snapshot_sha256"] != latest["snapshot_sha256"]
        or type(value["evidence_run_id"]) is not str
        or re.fullmatch(r"[0-9a-f]{32}", value["evidence_run_id"]) is None
    ):
        raise ObservationError("invalid metadata source observation")
    fetched = _timestamp(latest["fetched_at"])
    if fetched > _timestamp(checked_at):
        raise ObservationError(
            "cached metadata timestamp is in the future", code="clock_future"
        )
    if value["kind"] == "complete_online":
        started, completed = (
            _timestamp(value["started_at"]),
            _timestamp(value["completed_at"]),
        )
        if started > completed:
            raise ObservationError(
                "metadata observation clock reversed", code="clock_reversed"
            )
        if completed > fetched:
            raise ObservationError(
                "metadata observation is in the future", code="clock_future"
            )
        consistent = latest["source_consistency"] == "two_matching_full_scans"
    else:
        consistent = (
            value["started_at"] is None and value["completed_at"] is None
        )
        if value["kind"] == "unknown_offline":
            consistent = (
                consistent
                and latest["source_consistency"] == "offline_not_checked"
            )
    if not consistent:
        raise ObservationError("metadata observation consistency mismatch")


def _receipt_times(receipt, latest, checked_at):
    decision = _timestamp(receipt.get("decision_at"))
    if decision < latest["fetched_at"] or decision > checked_at:
        raise ObservationError(
            "metadata decision timestamp is in the future",
            code="clock_future",
        )
    used = receipt.get("cache_used_at")
    if (
        "cache_used_at" not in receipt
        or type(receipt.get("cache_hit")) is not bool
    ):
        raise ObservationError("invalid metadata cache-use evidence")
    if receipt["cache_hit"]:
        if not latest["fetched_at"] <= _timestamp(used) <= decision:
            raise ObservationError(
                "metadata cache-use clock reversed", code="clock_reversed"
            )
    elif used is not None:
        raise ObservationError("unexpected metadata cache-use timestamp")


def _source_receipt(receipt, latest, observation, run_id, version, checked_at):
    if (
        type(receipt) is not dict
        or type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != version
        or receipt.get("run_id") != run_id
        or receipt.get("status") != "ready"
        or receipt.get("source_endpoint") != source.ENDPOINT
        or any(
            receipt.get(key) != latest[key]
            for key in ("snapshot_sha256", "source_consistency", "fetched_at")
        )
    ):
        raise ObservationError("metadata source receipt identity mismatch")
    _timestamp(receipt["fetched_at"])
    if version == 1:
        if observation["kind"] != "unknown_legacy" or any(
            key in receipt
            for key in ("source_observation", "decision_at", "cache_used_at")
        ):
            raise ObservationError("invalid legacy metadata source receipt")
    else:
        original = receipt.get("source_observation")
        _validate_observation(original, latest, checked_at)
        if source.canonical(original) != source.canonical(observation):
            raise ObservationError("metadata source observation changed")
        _receipt_times(receipt, latest, checked_at)
        if receipt["cache_hit"] != (run_id != observation["evidence_run_id"]):
            raise ObservationError("metadata cache-use lineage mismatch")


def _read_receipt(folders, run_id):
    run_fd = storage.private_directory(folders["runs"], run_id)
    try:
        if "failure.json" in os.listdir(run_fd):
            raise storage.SyncError("latest metadata run is quarantined")
        return storage.read_record(run_fd, "result.json")
    finally:
        os.close(run_fd)


def _verified_latest(root, root_fd, folders, checked_at):
    value = storage.read_record(root_fd, "latest.json")
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
    if type(value) is dict and value.get("schema_version") == 2:
        fields.add("source_observation")
    if (
        type(value) is not dict
        or set(value) != fields
        or type(value["schema_version"]) is not int
        or value["schema_version"] not in (1, 2)
        or value["endpoint"] != source.ENDPOINT
        or not isinstance(value["run_id"], str)
        or re.fullmatch(r"[0-9a-f]{32}", value["run_id"]) is None
        or any(
            not isinstance(value[key], str)
            or SHA256.fullmatch(value[key]) is None
            for key in ("snapshot_sha256", "bundle_id", "manifest_sha256")
        )
        or type(value["fetched_at"]) not in (int, float)
        or not math.isfinite(value["fetched_at"])
        or type(value["source_consistency"]) is not str
        or value["source_consistency"]
        not in {"two_matching_full_scans", "offline_not_checked"}
        or type(value["document_count"]) is not int
        or not 1 <= value["document_count"] <= policy.MAX_ROWS
    ):
        raise storage.SyncError("invalid latest metadata cache pointer")
    receipt = _read_receipt(folders, value["run_id"])
    for key in (
        "snapshot_sha256",
        "bundle_id",
        "manifest_sha256",
        "fetched_at",
        "source_consistency",
        "document_count",
    ):
        if type(receipt) is not dict or receipt.get(key) != value[key]:
            raise storage.SyncError(
                "latest pointer does not match its immutable receipt"
            )
    observation = (
        _observation(
            "unknown_legacy", value["snapshot_sha256"], value["run_id"]
        )
        if value["schema_version"] == 1
        else value["source_observation"]
    )
    _validate_observation(observation, value, checked_at)
    _source_receipt(
        receipt,
        value,
        observation,
        value["run_id"],
        value["schema_version"],
        checked_at,
    )
    origin_id = observation["evidence_run_id"]
    origin = (
        receipt
        if origin_id == value["run_id"]
        else _read_receipt(folders, origin_id)
    )
    _source_receipt(
        origin,
        value,
        observation,
        origin_id,
        1 if observation["kind"] == "unknown_legacy" else 2,
        checked_at,
    )
    snapshot = (
        root
        / "snapshots"
        / ("sha256-" + value["snapshot_sha256"] + ".jsonl.gz")
    )
    raw = storage.file_bytes(
        folders["snapshots"],
        snapshot.name,
        mode=0o400,
        limit=policy.MAX_COMPRESSED_BYTES,
    )
    if hashlib.sha256(raw).hexdigest() != value["snapshot_sha256"]:
        raise storage.SyncError("cached metadata snapshot digest mismatch")
    bundle_path = root / "bundles" / ("sha256-" + value["bundle_id"])
    catalog = CatalogBundle.load(
        bundle_path, expected_manifest_sha256=value["manifest_sha256"]
    )
    bundle_fd = private_io.open_directory(bundle_path, mode=0o500)
    try:
        manifest_raw = storage.file_bytes(
            bundle_fd,
            "bundle-manifest.json",
            mode=0o400,
            limit=MAX_MANIFEST_BYTES,
        )
    finally:
        os.close(bundle_fd)
    if hashlib.sha256(manifest_raw).hexdigest() != value["manifest_sha256"]:
        raise storage.SyncError(
            "cached bundle manifest changed after verification"
        )
    manifest = source.strict_json(manifest_raw)
    if (
        manifest["artifacts"]["source.jsonl.gz"]["sha256"]
        != value["snapshot_sha256"]
        or len(catalog.documents) != value["document_count"]
    ):
        raise storage.SyncError(
            "cached snapshot and sealed bundle identities disagree"
        )
    return (
        {
            **value,
            "schema_version": 2,
            "source_observation": observation,
        },
        snapshot,
        bundle_path,
        catalog,
    )


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise ObservationError(
            "metadata sync deadline exceeded", code="timeout"
        )


def _cached_candidate(root, root_fd, folders, ttl_seconds):
    """Return a verified TTL decision, or no candidate without latest."""
    latest = None
    checked_at = source.operator_time()
    try:
        latest, snapshot_path, bundle_path, catalog = _verified_latest(
            root, root_fd, folders, checked_at
        )
    except FileNotFoundError:
        if "latest.json" in os.listdir(root_fd):
            raise storage.SyncError(
                "latest metadata cache is incomplete"
            ) from None
    if latest is not None:
        age = checked_at - latest["fetched_at"]
        return age <= ttl_seconds, latest, snapshot_path, bundle_path, catalog
    return None


def _stage_snapshot(
    root, root_fd, folders, run_id, run_fd, deadline, snapshot, fetch
):
    """Acquire and seal a snapshot and baseline without publishing latest."""
    if snapshot is None:
        records, interval = source.observed_download(deadline, fetch)
    else:
        records = snapshots.load_source(str(storage.real_path(snapshot)))
        interval = (None, None)
    _check_deadline(deadline)
    _verify_cache_directories(root, root_fd, folders, run_id, run_fd)
    ordered = sorted(
        ((normalize.normalize_package(item, {})[0], item) for item in records),
        key=lambda pair: pair[0],
    )
    if len({identifier for identifier, _ in ordered}) != len(ordered):
        raise storage.SyncError(
            "metadata snapshot contains duplicate dataset IDs"
        )
    rows = [item for _, item in ordered]
    if any(
        len(source.canonical(item) + b"\n") > policy.MAX_LINE_BYTES
        for item in rows
    ):
        raise storage.SyncError("metadata snapshot exceeds its line limit")
    build_storage.write_gzip_jsonl(run_fd, "snapshot.jsonl.gz", rows)
    incoming = root / "runs" / run_id / "snapshot.jsonl.gz"
    built = builder.build_bundle(incoming, root / "bundles")
    bundle_path = Path(built["bundle_path"])
    catalog = CatalogBundle.load(
        bundle_path,
        expected_manifest_sha256=built["manifest_sha256"],
    )
    raw = storage.file_bytes(
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
        existing = storage.file_bytes(
            folders["snapshots"],
            snapshot_path.name,
            mode=0o400,
            limit=policy.MAX_COMPRESSED_BYTES,
        )
        if hashlib.sha256(existing).hexdigest() != snapshot_sha:
            raise storage.SyncError(
                "existing immutable metadata snapshot is corrupted"
            )
    os.unlink("snapshot.jsonl.gz", dir_fd=run_fd)
    os.fsync(run_fd)
    os.fsync(folders["snapshots"])
    latest = {
        "schema_version": 2,
        "endpoint": source.ENDPOINT,
        "run_id": run_id,
        "snapshot_sha256": snapshot_sha,
        "bundle_id": catalog.bundle_id,
        "manifest_sha256": catalog.manifest_sha256,
        "fetched_at": source.operator_time(),
        "source_consistency": "two_matching_full_scans"
        if snapshot is None
        else "offline_not_checked",
        "document_count": len(catalog.documents),
        "source_observation": _observation(
            "complete_online" if snapshot is None else "unknown_offline",
            snapshot_sha,
            run_id,
            interval,
        ),
    }
    _validate_observation(
        latest["source_observation"], latest, latest["fetched_at"]
    )
    return built, bundle_path, catalog, snapshot_path, latest


def _profile_baseline_content(manifest):
    value = dict(manifest)
    value.pop("bundle_id")
    value["builder"] = dict(value["builder"])
    value["builder"].pop("source_sha256")
    return source.canonical(value)


def _profile_baseline_manifest(descriptor, path, expected):
    from search.operations.documents.enrichment import EnrichmentError

    raw = storage.file_bytes(
        descriptor,
        "bundle-manifest.json",
        mode=0o400,
        limit=MAX_MANIFEST_BYTES,
    )
    private_io.verify_directory(descriptor, path, mode=0o500)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise EnrichmentError("document_enrichment_source_mismatch")
    return source.strict_json(raw)


@contextmanager
def _profile_baseline(built, captured_bundle, profile_set):
    if captured_bundle is None:
        yield Path(built["bundle_path"]), built["manifest_sha256"], {}
        return
    from search.operations.documents import enrichment, sets

    record, _ = sets.read(profile_set)
    manifest_sha256 = record["catalog_manifest_sha256"]
    captured_bundle = storage.real_path(captured_bundle)
    descriptor = private_io.open_directory(captured_bundle, mode=0o500)
    try:
        CatalogBundle.load(
            captured_bundle, expected_manifest_sha256=manifest_sha256
        )
        captured = _profile_baseline_manifest(
            descriptor, captured_bundle, manifest_sha256
        )
        metadata_bundle = Path(built["bundle_path"])
        metadata_descriptor = private_io.open_directory(
            metadata_bundle, mode=0o500
        )
        try:
            metadata = _profile_baseline_manifest(
                metadata_descriptor,
                metadata_bundle,
                built["manifest_sha256"],
            )
        finally:
            os.close(metadata_descriptor)
        if _profile_baseline_content(captured) != _profile_baseline_content(
            metadata
        ):
            raise enrichment.EnrichmentError(
                "document_enrichment_source_mismatch"
            )
        yield (
            captured_bundle,
            manifest_sha256,
            {
                "profile_baseline_manifest_sha256": manifest_sha256,
                "metadata_baseline_manifest_sha256": built["manifest_sha256"],
            },
        )
        _profile_baseline_manifest(
            descriptor, captured_bundle, manifest_sha256
        )
    finally:
        os.close(descriptor)


def _failure_details(error):
    if isinstance(error, (source.MetadataSourceError, ObservationError)):
        code = error.code
        if type(code) is not str or code not in FAILURE_CODES:
            code = "source_invalid"
        label = (
            "MetadataSourceError"
            if isinstance(error, source.MetadataSourceError)
            else "ObservationError"
        )
        return code, label
    if isinstance(error, TimeoutError) or (
        isinstance(error, urllib.error.URLError)
        and isinstance(error.reason, TimeoutError)
    ):
        return "timeout", "TimeoutError"
    if isinstance(error, OSError):
        return "io_failure", "OSError"
    if isinstance(error, ValueError):
        label = (
            "SyncError"
            if isinstance(error, storage.SyncError)
            else "ValueError"
        )
        return "sync_invalid", label
    return "unexpected_failure", "Exception"


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
    profile_baseline_bundle=None,
):
    """Return a staged immutable snapshot/bundle/diff; never change active
    state.
    """
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
        or profile_baseline_bundle is not None
        and (
            not isinstance(profile_baseline_bundle, (str, Path))
            or profile_set is None
        )
    ):
        raise storage.SyncError(
            "TTL, force or profile selection is outside its bounded policy"
        )
    if snapshot is not None and fetch is not None:
        raise storage.SyncError(
            "offline snapshot and network fetch are mutually exclusive"
        )
    before = _baseline(
        baseline
    )  # Refuse bad active identity before any network request.
    started = time.monotonic()
    deadline = started + source.DEADLINE_SECONDS
    with _locked_cache(cache_root) as (root, root_fd, folders):
        run_id = uuid.uuid4().hex
        run_fd = storage.private_directory(
            folders["runs"], run_id, create=True
        )
        try:
            storage.write_record(
                run_fd, "started.json", {"schema_version": 2, "run_id": run_id}
            )
            cache_hit, built, latest, cache_used_at = False, None, None, None
            if snapshot is None and ttl_seconds > 0 and not force:
                candidate = _cached_candidate(
                    root, root_fd, folders, ttl_seconds
                )
                if candidate is not None:
                    cache_hit, latest, snapshot_path, bundle_path, catalog = (
                        candidate
                    )
                    cache_used_at = (
                        source.operator_time() if cache_hit else None
                    )
            if not cache_hit:
                built, bundle_path, catalog, snapshot_path, latest = (
                    _stage_snapshot(
                        root,
                        root_fd,
                        folders,
                        run_id,
                        run_fd,
                        deadline,
                        snapshot,
                        fetch,
                    )
                )
            # The metadata cache retains only downloaded snapshot identity.
            # Rebuild
            # the cheap baseline on cache hits so selected profile files never
            # leak
            # between invocations through a previous enriched bundle.
            if cache_hit:
                built = builder.build_bundle(snapshot_path, root / "bundles")
                bundle_path = Path(built["bundle_path"])
                catalog = CatalogBundle.load(
                    bundle_path,
                    expected_manifest_sha256=built["manifest_sha256"],
                )
            profile_report = {
                "selected_count": 0,
                "included_count": 0,
                "skipped_count": 0,
                "resources": [],
            }
            if profile_files or profile_set is not None:
                from search.operations.documents.enrichment import (
                    load_profiles,
                )

                baseline_source = built["manifest"]["artifacts"][
                    "source.jsonl.gz"
                ]
                # Profile admission is per request, including metadata TTL
                # hits; prior receipts never authorize a captured baseline.
                with _profile_baseline(
                    built, profile_baseline_bundle, profile_set
                ) as (profile_bundle, profile_manifest, baseline_report):
                    context, profile_report = load_profiles(
                        profile_files,
                        profile_bundle,
                        profile_manifest,
                        snapshot_path,
                        profile_set=profile_set,
                        deadline=deadline,
                    )
                    profile_report.update(baseline_report)
                    built = builder.build_bundle(
                        snapshot_path,
                        root / "bundles",
                        resource_context=context,
                    )
                    if (
                        built["manifest"]["artifacts"]["source.jsonl.gz"]
                        != baseline_source
                    ):
                        from search.operations.documents.enrichment import (
                            EnrichmentError,
                        )

                        raise EnrichmentError(
                            "document_enrichment_source_mismatch"
                        )
                    bundle_path = Path(built["bundle_path"])
                    catalog = CatalogBundle.load(
                        bundle_path,
                        expected_manifest_sha256=built["manifest_sha256"],
                    )
            latest = {
                **latest,
                "run_id": run_id,
                "bundle_id": catalog.bundle_id,
                "manifest_sha256": catalog.manifest_sha256,
            }
            decision_at = source.operator_time()
            _validate_observation(
                latest["source_observation"], latest, decision_at
            )
            _check_deadline(deadline)
            _verify_cache_directories(root, root_fd, folders, run_id, run_fd)
            report = {
                "schema_version": 2,
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
                "source_observation": latest["source_observation"],
                "decision_at": decision_at,
                "cache_used_at": cache_used_at,
                "diff": compare(before, catalog),
                "elapsed_seconds": time.monotonic() - started,
                "receipt_path": str(root / "runs" / run_id / "result.json"),
                "promotion_allowed": False,
                "live_index_modified": False,
                "models_called": False,
                "resource_profiles": profile_report,
            }
            _receipt_times(report, latest, decision_at)
            storage.write_record(run_fd, "result.json", report)
            storage.write_record(root_fd, "latest.json", latest, replace=True)
            return report
        except Exception as error:
            code, error_type = _failure_details(error)
            storage.write_record(
                run_fd,
                "failure.json",
                {
                    "schema_version": 2,
                    "status": "quarantined",
                    "error_type": error_type,
                    "failure_code": code,
                    "promotion_allowed": False,
                    "live_index_modified": False,
                },
            )
            raise
        finally:
            os.close(run_fd)
