#!/usr/bin/env python3
"""Aggregate private batch captures against one complete quality plan, offline."""

import argparse
import json
import os
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from search.operations.shared import private_io
from search.quality import batches
from search.serialization import strict_json


MAX_PLAN_BYTES = 64 * 1024


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise ValueError("quality_batches_arguments_invalid")


def _read(path, maximum):
    path = Path(path)
    directory = private_io.open_directory(path.parent)
    try:
        raw = private_io.read_file(directory, path.name, mode=0o600, max_bytes=maximum)
        private_io.verify_directory(directory, path.parent)
        return raw
    finally:
        os.close(directory)


def _files(path):
    directory = private_io.open_directory(path)
    try:
        result = {
            name: private_io.read_file(
                directory,
                name,
                mode=0o600,
                max_bytes=batches.MAX_RECEIPT_BYTES
                if name == "run.json"
                else batches.MAX_FILE_BYTES,
            )
            for name in sorted(batches.NAMES)
        }
        private_io.verify_directory(directory, path)
        return result
    finally:
        os.close(directory)


def _write(path, report):
    path = Path(path)
    directory = private_io.open_directory(path.parent)
    try:
        private_io.verify_directory(directory, path.parent)
        private_io.write_json(
            directory, path.name, report, max_bytes=batches.MAX_TOTAL_BYTES
        )
        private_io.verify_directory(directory, path.parent)
    finally:
        os.close(directory)


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument(
        "--batch",
        action="append",
        nargs=2,
        type=Path,
        metavar=("SUBPLAN", "CAPTURE_DIRECTORY"),
        default=[],
    )
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        if len(args.batch) > 32:
            raise ValueError("batch count invalid")
        full_plan = strict_json(_read(args.plan, MAX_PLAN_BYTES))
        baseline = (
            strict_json(_read(args.baseline, batches.MAX_RECEIPT_BYTES))
            if args.baseline
            else None
        )
        records, total = [], 0
        for plan, directory in args.batch:
            files = _files(directory)
            total += sum(len(raw) for raw in files.values())
            if total > batches.MAX_TOTAL_BYTES:
                raise ValueError("input too large")
            records.append(
                {"plan": strict_json(_read(plan, MAX_PLAN_BYTES)), "files": files}
            )
        report = batches.aggregate(full_plan, records, baseline=baseline)
        _write(args.output, report)
        summary = {
            "status": report["status"],
            "plan_sha256": report["plan_sha256"],
            "summary": report["summary"],
            "batch_count": len(records),
            "topical_relevance": report["topical_relevance"],
        }
        code = 0 if report["status"] == "passed" else 1
    except (
        OSError,
        ValueError,
        TypeError,
        OverflowError,
        RuntimeError,
        RecursionError,
    ):
        summary, code = (
            {"status": "invalid_input", "error": "quality_batches_input_invalid"},
            2,
        )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
