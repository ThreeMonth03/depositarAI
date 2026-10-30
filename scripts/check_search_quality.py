#!/usr/bin/env python3
"""Check captured search replies against expected state and source metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from search.quality import contract, evaluator
from search.serialization import strict_json


MAX_INPUT_BYTES = 8 * 1024 * 1024


def read_input(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input too large")
    return strict_json(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args(argv)
    if args.input is None and args.plan is None:
        parser.error("provide --input or --plan")
    try:
        plan = (
            contract.validate_cases(read_input(args.plan))
            if args.plan is not None
            else None
        )
        if args.input is None:
            report = {
                "schema_version": 1,
                "status": "plan_valid",
                "plan_sha256": contract.plan_digest(plan),
                "case_count": len(plan["cases"]),
                "turn_count": sum(len(case["turns"]) for case in plan["cases"]),
            }
        else:
            report = evaluator.evaluate(read_input(args.input), plan)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        print(
            json.dumps({"status": "invalid_input", "error": "quality_capture_invalid"})
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] in {"passed", "plan_valid"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
