"""Freeze a metadata-only stratified resource sample before acquiring any files.

Run as ``python -m experiments.offline-capacity.sample --help`` from the repo.
All inventories, snapshots and selections are private files outside Git. This
experiment defines its own capped benchmark population, not product policy.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import io
import json
import os
from pathlib import Path

from search.catalog.build import normalize, policy
from search.operations.documents import controller, source
from search.operations.metadata.source import strict_json
from search.operations.shared import private_io


SEED = "depositarai-offline-capacity-v1"
BINS = ("le_256_kib", "le_1_mib", "le_4_mib", "le_16_mib", "unknown")
SAMPLE_COUNTS = {"CSV": 32, "PDF": 128}
MAX_OUTPUT_BYTES = 16 * 1024**2


def _read(path, expected_sha256):
    path = Path(path)
    parent = controller._external_root(path.parent)
    mode = parent.stat().st_mode & 0o777
    if mode not in (0o500, 0o700) or not controller.SHA256.fullmatch(expected_sha256):
        raise ValueError("capacity_input_invalid")
    descriptor = private_io.open_directory(parent, mode=mode)
    try:
        file_mode = path.lstat().st_mode & 0o777
        if file_mode not in (0o400, 0o600):
            raise ValueError("capacity_input_invalid")
        raw = private_io.read_file(
            descriptor, path.name, mode=file_mode, max_bytes=policy.MAX_COMPRESSED_BYTES
        )
        private_io.verify_directory(descriptor, parent, mode=mode)
    finally:
        os.close(descriptor)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("capacity_input_digest_mismatch")
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        expanded = stream.read(policy.MAX_EXPANDED_BYTES + 1)
    if len(expanded) > policy.MAX_EXPANDED_BYTES:
        raise ValueError("capacity_input_too_large")
    records = []
    for line in expanded.splitlines(keepends=True):
        if not line.endswith(b"\n") or len(line) > policy.MAX_LINE_BYTES:
            raise ValueError("capacity_input_line_invalid")
        record = strict_json(line)
        if type(record) is not dict:
            raise ValueError("capacity_input_record_invalid")
        records.append((record, line))
    if not 1 <= len(records) <= policy.MAX_ROWS:
        raise ValueError("capacity_input_row_count_invalid")
    return records


def _rank(stage, resource):
    identity = "\0".join((SEED, stage, resource["dataset_id"], resource["resource_id"]))
    return hashlib.sha256(identity.encode("ascii")).digest()


def _size_stratum(size):
    if size is None or size == 0:
        return "unknown"
    for limit, label in zip((256 * 1024, 1024**2, 4 * 1024**2, 16 * 1024**2), BINS):
        if size <= limit:
            return label
    raise ValueError("capacity_size_outside_policy")


def _inventory(corpus):
    eligible, rejected, formats = [], Counter(), Counter()
    seen = set()
    for dataset in corpus:
        for resource in dataset["resources"]:
            declared = resource["format"].strip().upper()
            if declared not in SAMPLE_COUNTS:
                continue
            formats[declared] += 1
            try:
                identity = source.source_identity(resource)
            except source.SourceError as error:
                rejected[str(error)] += 1
                continue
            size = identity["declared_size"]
            if size is not None and size > source.MAX_FILE_BYTES:
                rejected["declared_over_16_mib"] += 1
                continue
            if not controller.UUID.fullmatch(resource["id"]):
                rejected["resource_id_invalid"] += 1
                continue
            if resource["id"] in seen:
                raise ValueError("capacity_resource_ambiguous")
            seen.add(resource["id"])
            eligible.append(
                {
                    "dataset_id": dataset["dataset_id"],
                    "resource_id": resource["id"],
                    "format": identity["format"],
                    "declared_size": size,
                    "size_stratum": _size_stratum(size),
                    "organization_id": dataset["organization"]["id"],
                    "source_url_sha256": identity["source_url_sha256"],
                }
            )
    eligible.sort(key=lambda row: (row["dataset_id"], row["resource_id"]))
    return eligible, formats, rejected


def _target(eligible):
    datasets = defaultdict(list)
    for row in eligible:
        datasets[row["dataset_id"]].append(row)
    target = [
        row
        for rows in datasets.values()
        for row in sorted(rows, key=lambda item: _rank("target", item))[:3]
    ]
    return sorted(target, key=lambda row: (row["dataset_id"], row["resource_id"]))


def _quotas(populations, count):
    """Reserve two per nonempty size bin, then apportion the remainder exactly."""
    quotas = {key: min(2, size) for key, size in populations.items()}
    remaining = count - sum(quotas.values())
    available = {key: size - quotas[key] for key, size in populations.items()}
    total = sum(available.values())
    if remaining < 0 or total < remaining:
        raise ValueError("capacity_stratum_population_insufficient")
    if not remaining:
        return quotas
    for key, size in available.items():
        quotas[key] += remaining * size // total
    leftovers = count - sum(quotas.values())
    order = sorted(
        available,
        key=lambda key: (-(remaining * available[key] % total), BINS.index(key)),
    )
    for key in order[:leftovers]:
        quotas[key] += 1
    return quotas


def _sample(target):
    strata = defaultdict(list)
    for row in target:
        strata[(row["format"], row["size_stratum"])].append(row)
    selected, weights = [], []
    for declared_format, count in SAMPLE_COUNTS.items():
        sizes = {label: len(strata[(declared_format, label)]) for label in BINS}
        quotas = _quotas(sizes, count)
        for label in BINS:
            population, sample_count = sizes[label], quotas[label]
            selected.extend(
                sorted(
                    strata[(declared_format, label)],
                    key=lambda row: _rank("sample", row),
                )[:sample_count]
            )
            weights.append(
                {
                    "format": declared_format,
                    "size_stratum": label,
                    "population_count": population,
                    "sample_count": sample_count,
                    "population_fraction": population / len(target),
                    "sample_fraction": sample_count / sum(SAMPLE_COUNTS.values()),
                    "expansion_weight": population / sample_count
                    if sample_count
                    else None,
                    "inclusion_probability": sample_count / population
                    if population
                    else None,
                }
            )
    return sorted(
        selected, key=lambda row: (row["dataset_id"], row["resource_id"])
    ), weights


def _summary(rows):
    unknown = sum(row["size_stratum"] == "unknown" for row in rows)
    return {
        "resources": len(rows),
        "datasets": len({row["dataset_id"] for row in rows}),
        "known_organizations": len(
            {row["organization_id"] for row in rows if row["organization_id"]}
        ),
        "organization_unknown_resources": sum(
            not row["organization_id"] for row in rows
        ),
        "formats": dict(Counter(row["format"] for row in rows)),
        "declared_bytes": sum(row["declared_size"] or 0 for row in rows),
        "unknown_size_resources": unknown,
        "declared_plus_unknown_cap_bytes": sum(
            row["declared_size"] or source.MAX_FILE_BYTES for row in rows
        ),
        "hard_acquisition_cap_bytes": len(rows) * source.MAX_FILE_BYTES,
    }


def freeze(source_path, source_sha256, corpus_path, corpus_sha256, output):
    """Write one deterministic selection without reading past extraction outcomes."""
    raw_records = _read(source_path, source_sha256)
    corpus = [row for row, _ in _read(corpus_path, corpus_sha256)]
    by_id = {row["dataset_id"]: row for row in corpus}
    if len(by_id) != len(corpus) or len(corpus) != len(raw_records):
        raise ValueError("capacity_snapshot_corpus_mismatch")
    for record, _ in raw_records:
        normalized = normalize.normalize_package(record, {})[2]
        if by_id.get(normalized["dataset_id"]) != normalized:
            raise ValueError("capacity_snapshot_corpus_mismatch")
    eligible, formats, rejected = _inventory(corpus)
    target = _target(eligible)
    selected, weights = _sample(target)
    dataset_ids = {row["dataset_id"] for row in selected}
    snapshot = b"".join(
        line for record, line in raw_records if record["id"] in dataset_ids
    )
    provenance = {
        "source_sha256": source_sha256,
        "corpus_sha256": corpus_sha256,
        "snapshot_sha256": hashlib.sha256(snapshot).hexdigest(),
        "original_dataset_count": len(corpus),
        "snapshot_dataset_count": len(dataset_ids),
        "snapshot_preserves_all_dataset_resources": True,
    }
    sampling = {
        "seed": SEED,
        "target_policy": "up_to_3_per_dataset_by_fixed_hash_benchmark_only",
        "sample_policy": "fixed_hash_within_format_size_strata",
        "quota_policy": "two_per_nonempty_size_bin_then_proportional_largest_remainder",
        "csv_deliberately_oversampled": True,
        "size_zero_or_missing": "unknown",
        "selection_depends_on_outcomes": False,
        "prior_extraction_controls_forced_into_sample": False,
        "local_cache": "fresh_separate_cache_required",
        "origin_or_cdn_cache": "not_controlled",
        "weight_scope": "fixed_capped_target_only_not_all_eligible_resources",
    }
    population = {
        "schema_version": 1,
        "sources": provenance,
        "sampling_policy": sampling,
        "csv_pdf_metadata_counts": dict(formats),
        "rejections": dict(rejected),
        "eligible": _summary(eligible),
        "target": _summary(target),
        "sample": _summary(selected),
        "strata": weights,
        "eligible_strata": [
            {
                "format": declared_format,
                "size_stratum": label,
                "resources": sum(
                    row["format"] == declared_format and row["size_stratum"] == label
                    for row in eligible
                ),
            }
            for declared_format in SAMPLE_COUNTS
            for label in BINS
        ],
    }
    selection = {
        "schema_version": 1,
        "experiment": "offline-capacity-v1",
        "sources": provenance,
        "sampling_policy": sampling,
        "resources": selected,
    }
    artifacts = {
        "eligible-inventory.json": private_io.canonical(eligible),
        "target-inventory.json": private_io.canonical(target),
        "target-resource-ids.json": private_io.canonical(
            [row["resource_id"] for row in target]
        ),
        "resource-ids.json": private_io.canonical(
            [row["resource_id"] for row in selected]
        ),
        "selection.json": private_io.canonical(selection),
        "population.json": private_io.canonical(population),
        "pilot-source.jsonl": snapshot,
    }
    output = controller._external_root(output)
    descriptor = private_io.open_directory(output)
    try:
        # Existing bytes must match: rerunning cannot silently redraw a sample.
        for name, raw in artifacts.items():
            try:
                previous = private_io.read_file(
                    descriptor, name, mode=0o600, max_bytes=MAX_OUTPUT_BYTES
                )
            except FileNotFoundError:
                continue
            if previous != raw:
                raise ValueError("capacity_frozen_selection_conflict")
        for name, raw in artifacts.items():
            try:
                private_io.write_file(descriptor, name, raw, max_bytes=MAX_OUTPUT_BYTES)
            except FileExistsError:
                pass
        private_io.verify_directory(descriptor, output)
    finally:
        os.close(descriptor)
    return {
        "status": "frozen",
        "population": population,
        "artifacts": {
            name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            for name, raw in artifacts.items()
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--corpus-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args(argv)
    try:
        report = freeze(
            options.source,
            options.source_sha256,
            options.corpus,
            options.corpus_sha256,
            options.output,
        )
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        print(json.dumps({"status": "failed", "code": "capacity_selection_failed"}))
        return 1
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
