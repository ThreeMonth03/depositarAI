"""Synthetic two-turn trace and independently authored expectations."""

import copy


IDENTIFIER = "1" * 32
OTHER = "2" * 32
DATASET = "00000000-0000-4000-8000-000000000001"
STAMP = "2026-11-12T00:00:00+00:00"
REVISION = "a" * 40
MANIFEST = "b" * 64


def state(year=None):
    return {
        "topic": "climate" if year else "",
        "time": {"start": f"{year}-01-01", "end": f"{year}-12-31"}
        if year
        else None,
        "formats": ["CSV"] if year else [],
        "unapplied_time": None,
    }


def event(turn=1, *, identifier=IDENTIFIER, manifest=MANIFEST):
    year = 2024 if turn == 1 else 2023
    after = state(year)
    conditions = {
        "time": dict(
            after["time"],
            field="data_coverage",
            relation="overlaps",
            endpoints="inclusive",
        ),
        "place": None,
        "formats": ["CSV"],
    }
    return {
        "trace_version": 1,
        "test_session_id": identifier,
        "epoch": 0,
        "turn": turn,
        "request_id": f"{turn:032x}",
        "upstream_request_id": f"upstream-{turn}",
        "timestamp": STAMP,
        "consent": {
            "notice_version": "search-trace-v1",
            "consented_at": STAMP,
        },
        "context": {"reference_time": STAMP, "timezone": "Asia/Taipei"},
        "input": {
            "query": "climate 2024 CSV" if turn == 1 else "use 2023",
            "top_k": 5,
        },
        "state_before": state() if turn == 1 else state(2024),
        "pending_before": None,
        "execution": {
            "kind": "legacy_conditions",
            "parsed_conditions": copy.deepcopy(conditions),
            "applied_conditions": copy.deepcopy(conditions),
        },
        "state_after": after,
        "pending_after": None,
        "output": {
            "version": turn,
            "status": "ok",
            "reason": "none",
            "message": "one dataset",
            "state": copy.deepcopy(after),
            "notices": [],
            "search": {
                "request_id": f"upstream-{turn}",
                "revision": REVISION,
                "catalog_manifest_sha256": manifest,
                "status": "ok",
                "cards": [
                    {
                        "rank": 1,
                        "dataset_id": DATASET,
                        "title": "Climate observations",
                        "resource_formats": ["CSV"],
                        "temporal_coverage": {
                            "start": "2020",
                            "end": "2025",
                            "status": "complete",
                        },
                        "condition_status": "match",
                    }
                ],
                "ranking": "opensearch-haystack-rrf",
                "rerank_status": "disabled",
                "candidate_count": 1,
                "condition_counts": {
                    "match": 1,
                    "unknown": 0,
                    "outside": 0,
                    "not_checked": 0,
                },
                "warnings": [],
                "filter_scope": "index_prefilter",
                "parsed_conditions": copy.deepcopy(conditions),
                "applied_conditions": copy.deepcopy(conditions),
            },
        },
        "identities": {
            "bff_revision": "c" * 40,
            "backend_revision": REVISION,
            "catalog_manifest_sha256": manifest,
            "capture_config_sha256": "d" * 64,
            "model_identity": None,
        },
        "timing_ms": {"bff_total": 2.0, "backend_total": 1.0},
        "capture": {"complete": True, "gaps": []},
        "unavailable_trace": ["model_identity", "runtime_config_identity"],
    }


def plan():
    return {
        "schema_version": 1,
        "cases": [
            {
                "id": "climate",
                "turns": [
                    {
                        "query": "climate 2024 CSV",
                        "expected": {
                            "time": {
                                "start": "2024-01-01",
                                "end": "2024-12-31",
                            },
                            "formats": ["CSV"],
                            "retain_topic": False,
                        },
                    },
                    {
                        "query": "use 2023",
                        "expected": {
                            "time": {
                                "start": "2023-01-01",
                                "end": "2023-12-31",
                            },
                            "formats": ["CSV"],
                            "retain_topic": True,
                        },
                    },
                ],
            }
        ],
    }


def sealed_bundle(test, base):
    import json
    from pathlib import Path

    from search.catalog.build import builder

    source = base / "source.jsonl"
    source.write_text(
        json.dumps(
            {
                "id": DATASET,
                "name": "climate-observations",
                "private": False,
                "state": "active",
                "type": "dataset",
                "title": "Climate observations",
                "start_time": "2020",
                "end_time": "2025",
                "created_time": "2026",
                "metadata_created": "2026-01-01T00:00:00",
                "metadata_modified": "2026-01-02T00:00:00",
                "resources": [{"format": "CSV"}],
            }
        )
        + "\n"
    )
    source.chmod(0o600)
    output = base / "bundles"
    output.mkdir(mode=0o700)
    built = builder.build_bundle(source, output)
    bundle = Path(built["bundle_path"])
    for artifact in bundle.iterdir():
        test.addCleanup(artifact.chmod, 0o600)
    test.addCleanup(bundle.chmod, 0o700)
    return bundle, built["manifest_sha256"]
