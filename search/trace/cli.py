"""Maintain private traces without printing query or result payloads."""

import argparse
import json
from pathlib import Path

from search.catalog.errors import BundleError
from search.operations.deployment.candidate.batch.capture import (
    capture_run,
    load_cases,
)
from search.operations.shared import private_io
from search.quality.evaluator import evaluate
from search.serialization import strict_json
from .limits import TraceLimits
from .quality import capture
from .store import TraceStore


def _limits(path):
    if path is None:
        return TraceLimits()
    fd = private_io.open_directory(path.parent)
    try:
        value = strict_json(
            private_io.read_file(fd, path.name, mode=0o600, max_bytes=65536)
        )
        if type(value) is not dict:
            raise ValueError("trace_limits_invalid")
        return TraceLimits(**value)
    finally:
        import os

        os.close(fd)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "export", "delete", "prune", "quality"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--limits", type=Path)
        if name not in {"inspect", "prune"}:
            command.add_argument("--session", required=True)
        elif name == "inspect":
            command.add_argument("--session")
        if name in {"export", "quality"}:
            command.add_argument("--output", type=Path, required=True)
        if name == "quality":
            command.add_argument("--plan", type=Path, required=True)
            command.add_argument("--case", required=True)
            command.add_argument("--bundle", type=Path, required=True)
            command.add_argument("--manifest", required=True)
    return parser


def _export(args, store):
    value = store.export(args.session)
    events = value.pop("events")
    value["event_files"] = [
        f"turn-{event['turn']:03d}.json" for event in events
    ]
    with capture_run(args.output, excluded=[args.root]) as output:
        output.write_json("manifest.json", value, max_bytes=65536)
        for event, name in zip(events, value["event_files"]):
            output.write_json(name, event)
    return {"status": "exported", "complete": value["complete"]}


def _quality(args, store):
    plan = load_cases(args.plan)
    value = capture(
        store.export(args.session),
        plan,
        args.case,
        args.bundle,
        args.manifest,
    )
    report = evaluate(value, plan)
    with capture_run(
        args.output, excluded=[args.root, args.plan, args.bundle]
    ) as output:
        output.write_json("capture.json", value)
        output.write_json("report.json", report)
    return {"status": "quality_exported", "quality": report["status"]}


def _execute(args):
    store = TraceStore(args.root, _limits(args.limits))
    if args.command == "inspect":
        return {"status": "inspected", "sessions": store.inspect(args.session)}
    if args.command == "delete":
        return {"status": "deleted", "removed": store.delete(args.session)}
    if args.command == "prune":
        return {"status": "pruned", "removed": store.prune()}
    if args.command == "export":
        return _export(args, store)
    return _quality(args, store)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        result = _execute(args)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        BundleError,
    ):
        print(
            json.dumps(
                {"status": "unavailable", "error": "trace_operation_failed"}
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
