"""Build a private, immutable Depositár public-metadata search bundle.

The builder deliberately has no network or third-party dependencies.  It accepts
the JSON Lines produced by ``ckanapi search datasets`` (plain or gzip), validates
the complete snapshot, and atomically publishes a content-addressed directory."""

import argparse
import sys
from typing import (
    Optional,
    Sequence,
)

from . import builder
from . import codec
from . import policy


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m search.catalog.build", description=__doc__
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser(
        "build", help="build and atomically publish a catalog bundle"
    )
    build.add_argument(
        "--input", required=True, help="ckanapi JSONL or JSONL.gz snapshot"
    )
    build.add_argument(
        "--output-root", required=True, help="existing private directory (mode 0700)"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "build":
            result = builder.build_bundle(arguments.input, arguments.output_root)
        else:
            raise policy.BundleError("unsupported command")
    except (policy.BundleError, OSError) as exc:
        print("catalog bundle build failed: %s" % exc, file=sys.stderr)
        return 1
    summary = {
        key: result[key]
        for key in (
            "bundle_id",
            "manifest_sha256",
            "bundle_path",
            "created",
            "dataset_count",
            "resource_count",
        )
    }
    print(codec.canonical_json(summary).decode("utf-8"))
    return 0
