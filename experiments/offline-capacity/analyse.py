"""Summarize the frozen 160-resource run for its explicit 4,698-resource target.

Run from the repository root with ``python -m experiments.offline-capacity.analyse``.
This reads private completed measurements only; it never acquires resources,
extracts documents, invokes models, or estimates catalog/vector indexing time.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import math
import os
from pathlib import Path
import re

from search.catalog.build import normalize
from search.operations.documents import controller, enrichment, extractor
from search.operations.shared import private_io
from search.serialization import strict_json


MAX_JSON_BYTES = 8 * 1024**2
MAX_TRACE_BYTES = 8 * 1024**2
MAX_OUTPUT_BYTES = 2 * 1024**2
TARGET_COUNT = 4698
SAMPLE_COUNT = 160


def _require(condition):
    if not condition:
        raise ValueError("capacity_analysis_input_invalid")


def _read(path, limit=MAX_JSON_BYTES, expected=None):
    path = Path(path)
    parent = controller._external_root(path.parent)
    descriptor = private_io.open_directory(parent)
    try:
        raw = private_io.read_file(descriptor, path.name, mode=0o600, max_bytes=limit)
        private_io.verify_directory(descriptor, parent)
    finally:
        os.close(descriptor)
    digest = hashlib.sha256(raw).hexdigest()
    _require(expected is None or digest == expected)
    return raw, digest


def _json(path, expected=None, limit=MAX_JSON_BYTES):
    raw, digest = _read(path, limit, expected)
    value = strict_json(raw)
    _require(private_io.canonical(value) == raw)
    return value, digest


def _seconds(value):
    _require(type(value) in (int, float) and math.isfinite(value) and value >= 0)
    return value


def _distribution(values):
    values = sorted(values)
    if not values:
        return {
            "count": 0,
            "sum": 0,
            "mean": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "max": None,
        }

    def percentile(fraction):
        position = (len(values) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        return values[lower] + (values[upper] - values[lower]) * (position - lower)

    return {
        "count": len(values),
        "sum": sum(values),
        "mean": sum(values) / len(values),
        "p50": percentile(0.5),
        "p90": percentile(0.9),
        "p95": percentile(0.95),
        "max": values[-1],
    }


def _selection(state, expected_selection, expected_population, expected_plan):
    selection, selection_sha = _json(state / "selection.json", expected_selection)
    population, population_sha = _json(state / "population.json", expected_population)
    plan, plan_sha = _json(state / "profiles/plan.json", expected_plan)
    rows = selection["resources"]
    identifiers = [row["resource_id"] for row in rows]
    _require(
        selection["schema_version"]
        == population["schema_version"]
        == plan["schema_version"]
        == 1
        and len(rows) == SAMPLE_COUNT
        and len(set(identifiers)) == SAMPLE_COUNT
        and rows
        == sorted(rows, key=lambda row: (row["dataset_id"], row["resource_id"]))
        and Counter(row["format"] for row in rows) == {"CSV": 32, "PDF": 128}
        and population["target"]["resources"] == TARGET_COUNT
        and population["sample"]["resources"] == SAMPLE_COUNT
        and population["sources"] == selection["sources"]
        and [
            (row["dataset_id"], row["resource_id"], row["format"], row["url_sha256"])
            for row in plan["resources"]
        ]
        == [
            (
                row["dataset_id"],
                row["resource_id"],
                row["format"],
                row["source_url_sha256"],
            )
            for row in rows
        ]
    )
    _read(
        state / "pilot-source.jsonl",
        16 * 1024**2,
        selection["sources"]["snapshot_sha256"],
    )
    strata = {(row["format"], row["size_stratum"]): row for row in population["strata"]}
    observed = Counter((row["format"], row["size_stratum"]) for row in rows)
    _require(
        len(strata) == len(population["strata"]) == 10
        and sum(row["population_count"] for row in strata.values()) == TARGET_COUNT
    )
    for key, row in strata.items():
        count, selected = row["population_count"], row["sample_count"]
        _require(
            type(count) is int
            and type(selected) is int
            and count >= selected > 0
            and observed[key] == selected
        )
        _require(abs(row["expansion_weight"] - count / selected) < 1e-10)
    return (
        rows,
        strata,
        plan,
        {
            "selection.json": selection_sha,
            "population.json": population_sha,
            "plan.json": plan_sha,
        },
    )


def _measurements(results, identifiers, plan, expected_plan, expected_runner):
    phases, groups, hashes = [], {}, {}
    for index, phase in enumerate(("a", "b")):
        name = "phase-" + phase
        report, digest = _json(results / (name + ".json"), limit=256 * 1024)
        _require(
            report["status"] == "completed"
            and report["phase"] == phase.upper()
            and report["plan_sha256"] == expected_plan
            and report["runner_sha256"] == expected_runner
            and report["catalog_manifest_sha256"] == plan["catalog_manifest_sha256"]
            and report["gpu_used"] is False
            and report["models_called"] is False
            and report["before"]["pending_count"] == 160 - index * 80
            and report["after"]["pending_count"] == 80 - index * 80
            and len(report["batches"]) == 4
        )
        for number, batch in enumerate(report["batches"]):
            _require(
                batch["result"]["attempted_count"] == 20
                and batch["previously_terminal"] == index * 80 + number * 20
            )
            _seconds(batch["seconds"])
        raw, trace_sha = _read(results / (name + ".events.jsonl"), MAX_TRACE_BYTES)
        events = [strict_json(line) for line in raw.splitlines(keepends=True)]
        _require(
            len(events) == report["event_count"] and len(raw) == report["trace_bytes"]
        )
        phase_ids = identifiers[index * 80 : (index + 1) * 80]
        _require(
            [
                event["resource_id"]
                for event in events
                if event["stage"] == "process_start"
            ]
            == phase_ids
        )
        phase_groups = defaultdict(lambda: defaultdict(list))
        previous_timestamp = report["started_unix_ns"]
        for event, line in zip(events, raw.splitlines(keepends=True)):
            timestamp = event["observed_unix_ns"]
            _require(
                private_io.canonical(event) == line
                and type(timestamp) is int
                and previous_timestamp <= timestamp <= report["completed_unix_ns"]
            )
            previous_timestamp = timestamp
            _require(
                event["resource_id"] in phase_ids
                and event["stage"]
                in {
                    "process_start",
                    "acquisition",
                    "extraction",
                    "process",
                    "outcome_publication",
                }
            )
            if "seconds" in event:
                _seconds(event["seconds"])
            phase_groups[event["resource_id"]][event["stage"]].append(event)
        _require(set(phase_groups) == set(phase_ids))
        for identifier, stages in phase_groups.items():
            _require(
                len(stages["process_start"])
                == len(stages["acquisition"])
                == len(stages["process"])
                == 1
                and len(stages["extraction"]) <= 1
                and len(stages["outcome_publication"]) == 2
            )
            groups[identifier] = (index, stages)
        phases.append(report)
        hashes[name + ".json"], hashes[name + ".events.jsonl"] = digest, trace_sha
    _require(
        set(groups) == set(identifiers)
        and phases[0]["source_revision"] == phases[1]["source_revision"]
    )
    return phases, groups, hashes


def _resource_rows(state, selection, groups, manifest, plan_sha):
    rows = []
    for selected in selection:
        identifier = selected["resource_id"]
        phase, stages = groups[identifier]
        process, acquired = stages["process"][0], stages["acquisition"][0]
        outcome, outcome_sha = _json(
            state / "profiles/outcomes" / (identifier + ".json"), limit=4096
        )
        _require(
            outcome["status"]
            in {"ok", "empty_text", "unsupported_format", "parse_error", "failed"}
        )
        _require(
            outcome["plan_sha256"] == plan_sha
            and outcome["dataset_id"] == selected["dataset_id"]
            and outcome["resource_id"] == identifier
            and outcome["attempt"] == 1
            and outcome["helper_name"] == stages["process_start"][0]["helper_name"]
        )
        _require(
            process["dataset_id"] == selected["dataset_id"]
            and process["format"].strip().upper() == selected["format"]
            and process["declared_bytes"] == selected["declared_size"]
        )
        _require(
            [event["status"] for event in stages["outcome_publication"]]
            == ["running", outcome["status"]]
        )
        acquisition_seconds = acquired["seconds"]
        extraction = stages["extraction"][0] if stages["extraction"] else None
        extraction_seconds = extraction["seconds"] if extraction else 0
        stage_seconds = acquisition_seconds + extraction_seconds
        useful = False
        profile_finalize = 0
        residual_upper = 0
        if outcome["status"] == "failed":
            _require(
                "error_type" in process
                and "result" not in process
                and "error_type" in (extraction or acquired)
                and type(outcome["code"]) is str
                and re.fullmatch(r"document_[a-z_]{1,80}", outcome["code"]) is not None
            )
            # Failed process events lack the nested trace-write timer. Original
            # stage calls retain failed work; show the unallocated residual bound.
            processing = stage_seconds
            residual_upper = max(0, process["seconds"] - stage_seconds)
        else:
            _require(
                type(outcome["profile_sha256"]) is str
                and controller.SHA256.fullmatch(outcome["profile_sha256"]) is not None
            )
            _require(
                process["result"] == {key: outcome[key] for key in process["result"]}
            )
            _require(
                extraction is not None and extraction["status"] == outcome["status"]
            )
            _require(
                acquired["cache_hit"] == outcome["acquisition_cache_hit"]
                and extraction["cache_hit"] == outcome["extraction_cache_hit"]
            )
            processing = _seconds(process["seconds_excluding_trace_io"])
            profile_finalize = _seconds(process["profile_finalize_seconds"])
            _require(abs(processing - stage_seconds - profile_finalize) < 0.01)
            profile, _ = _json(
                state / "profiles/profiles" / (outcome["profile_sha256"] + ".json"),
                outcome["profile_sha256"],
                controller.MAX_RECORD_BYTES,
            )
            enrichment._profile_fields(profile, manifest)
            _require(
                profile["source"]["dataset_id"] == selected["dataset_id"]
                and profile["source"]["resource_id"] == identifier
                and profile["source"]["url_sha256"] == selected["source_url_sha256"]
                and profile["source"]["content_sha256"] == acquired["content_sha256"]
                and profile["source"]["bytes"] == acquired["bytes"]
            )
            result = extractor.validate_report(
                profile["result"],
                expected_format=selected["format"],
                header_policy=profile["policy"]["header_policy"],
            )
            _require(
                result["status"] == outcome["status"]
                and result["input_sha256"] == acquired["content_sha256"]
            )
            if result["status"] == "ok" and (
                selected["format"] != "CSV"
                or profile["policy"]["header_policy"] == "first_record"
            ):
                prepared = normalize.resource_profile_content(
                    selected["format"],
                    name="",
                    columns=result["columns"],
                    text=result["text"],
                    counters={},
                )
                useful = bool(prepared["index_text"])
        _require(process["seconds"] + 0.01 >= stage_seconds)
        rows.append(
            {
                "dataset_id": selected["dataset_id"],
                "resource_id": identifier,
                "format": selected["format"],
                "size_stratum": selected["size_stratum"],
                "phase": "A" if phase == 0 else "B",
                "status": outcome["status"],
                "failure_code": outcome.get("code"),
                "useful_index_context": useful,
                "acquired_bytes": acquired.get("bytes", 0),
                "acquisition_succeeded": "bytes" in acquired,
                "acquisition_cache_hit": acquired.get("cache_hit", False),
                "extraction_attempted": extraction is not None,
                "extraction_cache_hit": extraction.get("cache_hit", False)
                if extraction
                else False,
                "acquisition_seconds": acquisition_seconds,
                "extraction_seconds": extraction_seconds,
                "process_seconds_excluding_trace_io": processing,
                "failed_process_residual_upper_seconds": residual_upper,
                "profile_finalize_seconds": profile_finalize,
                "outcome_publication_seconds": sum(
                    event["seconds"] for event in stages["outcome_publication"]
                ),
                "outcome_sha256": outcome_sha,
            }
        )
    return rows


def _summary(rows):
    acquisition = sum(row["acquisition_seconds"] for row in rows)
    extraction = sum(row["extraction_seconds"] for row in rows)
    acquired_bytes = sum(row["acquired_bytes"] for row in rows)
    attempts = sum(row["extraction_attempted"] for row in rows)
    return {
        "selected": len(rows),
        "statuses": dict(Counter(row["status"] for row in rows)),
        "failure_codes": dict(
            Counter(row["failure_code"] for row in rows if row["failure_code"])
        ),
        "useful_index_contexts": sum(row["useful_index_context"] for row in rows),
        "useful_index_context_fraction": sum(
            row["useful_index_context"] for row in rows
        )
        / len(rows)
        if rows
        else None,
        "acquisition_successes": sum(row["acquisition_succeeded"] for row in rows),
        "acquisition_cache_hits": sum(row["acquisition_cache_hit"] for row in rows),
        "extraction_attempts_including_failures": attempts,
        "extraction_cache_hits": sum(row["extraction_cache_hit"] for row in rows),
        "acquired_bytes": acquired_bytes,
        "acquired_bytes_per_all_attempt_acquisition_second": acquired_bytes
        / acquisition
        if acquisition
        else None,
        "extraction_attempts_per_second": attempts / extraction if extraction else None,
        "timings": {
            key: _distribution(
                [
                    row[key]
                    for row in rows
                    if key != "extraction_seconds" or row["extraction_attempted"]
                ]
            )
            for key in (
                "acquisition_seconds",
                "extraction_seconds",
                "profile_finalize_seconds",
                "process_seconds_excluding_trace_io",
                "failed_process_residual_upper_seconds",
            )
        },
    }


def _estimate(rows, strata):
    estimates, totals = [], Counter()
    statuses = Counter()
    for (declared_format, size_bin), stratum in strata.items():
        selected = [
            row
            for row in rows
            if (row["format"], row["size_stratum"]) == (declared_format, size_bin)
        ]
        _require(len(selected) == stratum["sample_count"])
        weight = stratum["population_count"] / len(selected)
        summary = _summary(selected)
        for status, count in summary["statuses"].items():
            statuses[status] += count * weight
        totals["useful_index_contexts"] += summary["useful_index_contexts"] * weight
        for key in (
            "acquisition_seconds",
            "extraction_seconds",
            "profile_finalize_seconds",
            "process_seconds_excluding_trace_io",
            "failed_process_residual_upper_seconds",
            "acquired_bytes",
        ):
            totals[key] += sum(row[key] for row in selected) * weight
        totals["observed_tail_stress_seconds"] += (
            max(
                row["process_seconds_excluding_trace_io"]
                + row["failed_process_residual_upper_seconds"]
                for row in selected
            )
            * stratum["population_count"]
        )
        estimates.append(
            {
                "format": declared_format,
                "size_stratum": size_bin,
                "population_count": stratum["population_count"],
                "sample_count": len(selected),
                "expansion_weight": weight,
                "sample": summary,
            }
        )
    point = totals["process_seconds_excluding_trace_io"]
    return {
        "target_resources": TARGET_COUNT,
        "sample_resources": len(rows),
        "weighted_terminal_status_counts": dict(statuses),
        "weighted_useful_index_context_count": totals["useful_index_contexts"],
        "weighted_useful_index_context_fraction": totals["useful_index_contexts"]
        / TARGET_COUNT,
        "point_estimate_seconds": point,
        "failed_process_unallocated_residual_upper_seconds": totals[
            "failed_process_residual_upper_seconds"
        ],
        "point_estimate_policy": "weighted successful process excluding trace IO; failed resources retain original acquisition/extraction time, with their unallocated process residual separately bounded",
        "weighted_original_stage_seconds": {
            key: totals[key]
            for key in (
                "acquisition_seconds",
                "extraction_seconds",
                "profile_finalize_seconds",
            )
        },
        "weighted_observed_acquired_bytes": totals["acquired_bytes"],
        "network_sensitivity_seconds": {
            "acquisition_half_time": point - totals["acquisition_seconds"] / 2,
            "acquisition_double_time": point + totals["acquisition_seconds"],
        },
        "observed_stratum_max_stress_seconds": totals["observed_tail_stress_seconds"],
        "strata": estimates,
    }


def analyse(options):
    state = controller._external_root(options.state_dir)
    results = controller._external_root(options.results_dir)
    selected, strata, plan, hashes = _selection(
        state,
        options.expected_selection_sha256,
        options.expected_population_sha256,
        options.expected_plan_sha256,
    )
    phases, groups, measurement_hashes = _measurements(
        results,
        [row["resource_id"] for row in selected],
        plan,
        options.expected_plan_sha256,
        options.expected_runner_sha256,
    )
    rows = _resource_rows(
        state,
        selected,
        groups,
        plan["catalog_manifest_sha256"],
        options.expected_plan_sha256,
    )
    estimate = _estimate(rows, strata)
    overhead = []
    for phase, measured in zip(("A", "B"), phases):
        members = [row for row in rows if row["phase"] == phase]
        batch_wall = sum(batch["seconds"] for batch in measured["batches"])
        trace = _seconds(measured["trace_write_seconds"])
        processing = sum(row["process_seconds_excluding_trace_io"] for row in members)
        publication = sum(row["outcome_publication_seconds"] for row in members)
        residual = batch_wall - trace - processing - publication
        _require(residual >= -0.01)
        overhead.append(
            {
                "phase": phase,
                "wall_seconds": _seconds(measured["wall_seconds"]),
                "batch_wall_seconds": batch_wall,
                "trace_write_seconds": trace,
                "outcome_publication_seconds": publication,
                "batch_residual_upper_seconds": max(0, residual),
                "failed_process_residual_upper_seconds": sum(
                    row["failed_process_residual_upper_seconds"] for row in members
                ),
                "outside_batch_wall_seconds": max(
                    0, measured["wall_seconds"] - batch_wall
                ),
                "batches": [
                    {
                        "previously_terminal": batch["previously_terminal"],
                        "seconds": batch["seconds"],
                    }
                    for batch in measured["batches"]
                ],
            }
        )
    hashes.update(measurement_hashes)
    replay = None
    if (results / "phase-replay.json").exists():
        replay, replay_sha = _json(results / "phase-replay.json", limit=256 * 1024)
        _require(
            replay["status"] == "completed"
            and replay["phase"] == "replay"
            and replay["plan_sha256"] == options.expected_plan_sha256
            and replay["runner_sha256"] == options.expected_runner_sha256
            and replay["catalog_manifest_sha256"] == plan["catalog_manifest_sha256"]
            and replay["after"]["attempted_count"]
            == replay["after"]["pending_count"]
            == replay["forbidden_io_calls"]
            == 0
        )
        hashes["phase-replay.json"] = replay_sha
    report = {
        "schema_version": 1,
        "status": "completed",
        "inputs_sha256": hashes,
        "analysis_source_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "runner_sha256": options.expected_runner_sha256,
        "catalog_manifest_sha256": plan["catalog_manifest_sha256"],
        "sample": _summary(rows),
        "formats": {
            declared: _summary([row for row in rows if row["format"] == declared])
            for declared in ("CSV", "PDF")
        },
        "target_estimate": estimate,
        "observed_overhead": overhead,
        "terminal_replay": {
            "forbidden_io_calls": 0,
            "wall_seconds": replay["wall_seconds"],
        }
        if replay
        else None,
        "assumptions": [
            "Weights apply only to the frozen capped4698-resource benchmark target, not all8782 eligible or9402 metadata resources.",
            "This is local-cold initial state with natural within-run cache reuse; origin/CDN and OS caches are uncontrolled.",
            "Acquisition and extraction attempt times include failures/timeouts. Failed partial transfer bytes are unavailable; byte rates count successful acquired bytes only.",
            "Process time already includes acquisition, extraction and profile finalization; do not add these stage totals to process ETA.",
            "Failed process events lack a trace subtraction timer: known original stages are retained; remaining raw process wall bounds unallocated failed finalization plus trace IO.",
            "Batch residual includes catalog/profile verification, image lookup, bookkeeping, trace serialization and bounded failed-process residual; it is not separately measured verification time.",
            "Per-pass verification revisits completed profiles. Observed0..140 completed profiles do not calibrate verification overhead for235 passes over4698 resources.",
            "Half/double acquisition time and observed-stratum maxima are sensitivity scenarios, not confidence intervals; sparse unknown-size strata and uncommon failures remain uncertain.",
            "No estimate covers catalog publication, BM25 indexing, embedding/vector indexing, query quality, or website deployment.",
        ],
        "resources": rows,
    }
    output = Path(options.output)
    parent = controller._external_root(output.parent)
    descriptor = private_io.open_directory(parent)
    try:
        raw = private_io.canonical(report)
        try:
            previous = private_io.read_file(
                descriptor, output.name, mode=0o600, max_bytes=MAX_OUTPUT_BYTES
            )
        except FileNotFoundError:
            private_io.write_file(
                descriptor, output.name, raw, max_bytes=MAX_OUTPUT_BYTES
            )
        else:
            _require(previous == raw)
        private_io.verify_directory(descriptor, parent)
    finally:
        os.close(descriptor)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    for field in ("selection", "population", "plan", "runner"):
        parser.add_argument("--expected-" + field + "-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args(argv)
    try:
        report = analyse(options)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        print('{"status":"failed","code":"capacity_analysis_failed"}')
        return 1
    print(
        private_io.canonical(
            {
                "status": "completed",
                "sample_resources": report["sample"]["selected"],
                "target_resources": TARGET_COUNT,
                "point_estimate_seconds": report["target_estimate"][
                    "point_estimate_seconds"
                ],
            }
        ).decode(),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
