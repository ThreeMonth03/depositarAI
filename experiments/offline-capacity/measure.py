"""Measure the original sequential resource processor with private bounded traces."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import resource
import subprocess
import time
from unittest.mock import patch

from search.operations.documents import batches, controller, extractor, source
from search.operations.shared import private_io
from search.serialization import strict_json


MAX_EVENT_BYTES = 16 * 1024
MAX_TRACE_BYTES = 8 * 1024**2
MAX_REPORT_BYTES = 256 * 1024
RESOURCES = 160
PASSES = 4
ROOT = Path(__file__).resolve().parents[2]


class Trace:
    """Record original call timings without changing resource results or policy."""

    def __init__(self, descriptor, name, outcomes):
        self.stream = os.fdopen(
            os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=descriptor,
            ),
            "wb",
        )
        self.outcomes = outcomes
        self.active = None
        self.events = []
        self.bytes = 0
        self.write_seconds = 0.0
        self.original_acquire = source.acquire
        self.original_extraction = controller._extraction
        self.original_process = controller.process
        self.original_write = private_io.write_json

    def emit(self, event):
        event["observed_unix_ns"] = time.time_ns()
        raw = private_io.canonical(event)
        if len(raw) > MAX_EVENT_BYTES or self.bytes + len(raw) > MAX_TRACE_BYTES:
            raise ValueError("capacity_trace_limit_exceeded")
        started = time.monotonic()
        self.stream.write(raw)
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.write_seconds += time.monotonic() - started
        self.bytes += len(raw)
        self.events.append(event)

    def acquire(self, cache_root, metadata, **kwargs):
        started = time.monotonic()
        try:
            value = self.original_acquire(cache_root, metadata, **kwargs)
        except BaseException as error:
            self.emit(
                {
                    "stage": "acquisition",
                    "resource_id": metadata["id"],
                    "seconds": time.monotonic() - started,
                    "error_type": type(error).__name__,
                    "code": str(error)
                    if isinstance(error, source.SourceError)
                    else None,
                }
            )
            raise
        self.emit(
            {
                "stage": "acquisition",
                "resource_id": metadata["id"],
                "seconds": time.monotonic() - started,
                "bytes": value["bytes"],
                "cache_hit": value["cache_hit"],
                "content_sha256": value["sha256"],
            }
        )
        return value

    def extraction(self, *args, **kwargs):
        started = time.monotonic()
        try:
            value = self.original_extraction(*args, **kwargs)
        except BaseException as error:
            self.emit(
                {
                    "stage": "extraction",
                    "resource_id": self.active,
                    "seconds": time.monotonic() - started,
                    "error_type": type(error).__name__,
                }
            )
            raise
        report, hit = value
        self.emit(
            {
                "stage": "extraction",
                "resource_id": self.active,
                "seconds": time.monotonic() - started,
                "cache_hit": hit,
                "status": report["status"],
                "text_chars": len(report["text"]),
                "columns": len(report["columns"]),
                "text_truncated": report["text_truncated"],
                "may_require_ocr": report["may_require_ocr"],
            }
        )
        return value

    def process(self, dataset_id, metadata, *args, **kwargs):
        if self.active is not None:
            raise ValueError("capacity_parallel_resource_rejected")
        self.active = metadata["id"]
        self.emit(
            {
                "stage": "process_start",
                "resource_id": self.active,
                "helper_name": kwargs["container_name"],
            }
        )
        first_event = len(self.events)
        trace_before = self.write_seconds
        started = time.monotonic()
        try:
            value = self.original_process(dataset_id, metadata, *args, **kwargs)
        except BaseException as error:
            self.emit(
                {
                    "stage": "process",
                    "dataset_id": dataset_id,
                    "resource_id": self.active,
                    "format": metadata["format"],
                    "declared_bytes": metadata.get("size"),
                    "seconds": time.monotonic() - started,
                    "error_type": type(error).__name__,
                }
            )
            raise
        else:
            elapsed = time.monotonic() - started
            nested_trace = self.write_seconds - trace_before
            stages = sum(
                row["seconds"]
                for row in self.events[first_event:]
                if row["stage"] in {"acquisition", "extraction"}
            )
            self.emit(
                {
                    "stage": "process",
                    "dataset_id": dataset_id,
                    "resource_id": self.active,
                    "format": metadata["format"],
                    "declared_bytes": metadata.get("size"),
                    "seconds": time.monotonic() - started,
                    "result": value,
                    "nested_trace_write_seconds": nested_trace,
                    "seconds_excluding_trace_io": elapsed - nested_trace,
                    "profile_finalize_seconds": max(
                        0.0, elapsed - nested_trace - stages
                    ),
                }
            )
            return value
        finally:
            self.active = None

    def write(self, descriptor, name, value, **kwargs):
        info = os.fstat(descriptor)
        is_outcome = (info.st_dev, info.st_ino) == self.outcomes
        started = time.monotonic()
        result = self.original_write(descriptor, name, value, **kwargs)
        if is_outcome:
            self.emit(
                {
                    "stage": "outcome_publication",
                    "resource_id": value["resource_id"],
                    "seconds": time.monotonic() - started,
                    "status": value["status"],
                }
            )
        return result

    def attach(self, stack):
        stack.enter_context(patch.object(source, "acquire", self.acquire))
        stack.enter_context(patch.object(controller, "_extraction", self.extraction))
        stack.enter_context(patch.object(controller, "process", self.process))
        stack.enter_context(patch.object(private_io, "write_json", self.write))
        stack.callback(self.stream.close)


def _read_plan(state_root, expected):
    descriptor = private_io.open_directory(state_root)
    try:
        raw = private_io.read_file(
            descriptor, "plan.json", mode=0o600, max_bytes=batches.MAX_PLAN_BYTES
        )
    finally:
        os.close(descriptor)
    value = strict_json(raw)
    if (
        private_io.canonical(value) != raw
        or hashlib.sha256(raw).hexdigest() != expected
    ):
        raise ValueError("capacity_plan_digest_mismatch")
    if len(value["resources"]) != RESOURCES or Counter(
        row["format"] for row in value["resources"]
    ) != {"CSV": 32, "PDF": 128}:
        raise ValueError("capacity_selection_changed")
    return value


def _initial(options, plan):
    before = batches.status(options.state_root, options.expected_plan_sha256)
    expected_pending = RESOURCES if options.phase == "A" else RESOURCES // 2
    if options.phase != "replay" and before["pending_count"] != expected_pending:
        raise ValueError("capacity_phase_progress_mismatch")
    if options.phase == "replay" and before["pending_count"]:
        raise ValueError("capacity_replay_not_terminal")
    if options.phase == "A":
        for name in ("acquisition", "extractions", "profiles", "outcomes"):
            if any((options.state_root / name).iterdir()):
                raise ValueError("capacity_local_cold_state_not_empty")
    return {
        "schema_version": 1,
        "status": "running",
        "started_unix_ns": time.time_ns(),
        "phase": options.phase,
        "source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "catalog_manifest_sha256": plan["catalog_manifest_sha256"],
        "plan_sha256": options.expected_plan_sha256,
        "before": before,
        "gpu_used": False,
        "models_called": False,
        "parser_limits": {
            "cpus": 2,
            "memory_bytes": 2 * 1024**3,
            "seconds": 25,
            "kill_after_seconds": 5,
        },
        "measured_parser_peak_memory_bytes": None,
        "timing_scope": "local-cold/mixed sequential original calls; source server caches uncontrolled; full batch wall includes durable trace IO",
        "extraction_timing_scope": "parser plus extraction cache lookup, validation and publication",
        "profile_finalize_timing_scope": "process residual excluding acquisition, extraction and nested trace writes; includes hashing, profile publication and Python overhead",
        "batches": [],
    }


def measure(options):
    plan = _read_plan(options.state_root, options.expected_plan_sha256)
    report = _initial(options, plan)
    output_root = controller._external_root(options.output_dir)
    output = private_io.open_directory(output_root, create=True)
    name = "phase-" + options.phase.lower()
    published = False
    try:
        private_io.write_json(
            output, name + ".json", report, max_bytes=MAX_REPORT_BYTES
        )
        published = True
        started = time.monotonic()
        if options.phase == "replay":
            forbidden = []

            def reject(*_args, **_kwargs):
                forbidden.append("unexpected_io")
                raise AssertionError("capacity_terminal_replay_triggered_io")

            with ExitStack() as stack:
                for module, function in (
                    (source, "acquire"),
                    (extractor, "extract"),
                    (extractor, "build_image"),
                    (extractor, "helper_exists"),
                    (controller, "_extraction"),
                ):
                    stack.enter_context(patch.object(module, function, reject))
                final = batches.run(
                    options.bundle,
                    options.state_root,
                    options.expected_plan_sha256,
                    source_root=ROOT,
                )
            report["forbidden_io_calls"] = len(forbidden)
            report["after"] = final
        else:
            info = (options.state_root / "outcomes").stat()
            trace = Trace(output, name + ".events.jsonl", (info.st_dev, info.st_ino))
            with ExitStack() as stack:
                trace.attach(stack)
                for number in range(PASSES):
                    previous = (
                        report["before"]
                        if not report["batches"]
                        else report["batches"][-1]["result"]
                    )
                    batch_started = time.monotonic()
                    result = batches.run(
                        options.bundle,
                        options.state_root,
                        options.expected_plan_sha256,
                        max_resources=20,
                        source_root=ROOT,
                    )
                    if result["attempted_count"] != 20:
                        raise ValueError("capacity_pass_attempt_count_changed")
                    report["batches"].append(
                        {
                            "number": number + 1,
                            "seconds": time.monotonic() - batch_started,
                            "previously_terminal": RESOURCES
                            - previous["pending_count"],
                            "result": result,
                        }
                    )
                    private_io.write_json(
                        output,
                        name + ".json",
                        report,
                        replace=True,
                        max_bytes=MAX_REPORT_BYTES,
                    )
                    print(
                        private_io.canonical(
                            {"phase": options.phase, **report["batches"][-1]}
                        ).decode(),
                        end="",
                        flush=True,
                    )
            report["after"] = report["batches"][-1]["result"]
            expected_pending = 80 if options.phase == "A" else 0
            if report["after"]["pending_count"] != expected_pending:
                raise ValueError("capacity_phase_progress_mismatch")
            report["event_count"] = len(trace.events)
            report["trace_bytes"] = trace.bytes
            report["trace_write_seconds"] = trace.write_seconds
        report.update(
            status="completed",
            wall_seconds=time.monotonic() - started,
            completed_unix_ns=time.time_ns(),
            host_controller_maxrss_kib=resource.getrusage(
                resource.RUSAGE_SELF
            ).ru_maxrss,
        )
    except BaseException as error:
        report.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        try:
            private_io.verify_directory(output, output_root)
            if published:
                private_io.write_json(
                    output,
                    name + ".json",
                    report,
                    replace=True,
                    max_bytes=MAX_REPORT_BYTES,
                )
        finally:
            os.close(output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--expected-plan-sha256", required=True)
    parser.add_argument("--phase", choices=("A", "B", "replay"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    options = parser.parse_args()
    report = measure(options)
    print(private_io.canonical(report).decode(), end="")


if __name__ == "__main__":
    main()
