#!/usr/bin/env python3
"""Bounded sealed-catalog import, reconcile and vector-pass operator CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping, Optional, Sequence

from search.catalog.admin import AdminAccess
from search.catalog.bundle import CatalogBundle
from search.catalog.errors import CatalogError, failure_record
from search.catalog.importer import (
    CatalogImporter,
    MAX_TOKEN_OPERATION_DEADLINE_SECONDS,
)
from search.catalog.vectors import (
    VectorBuilder,
    DEFAULT_VECTOR_PASS_DEADLINE_SECONDS,
    MAX_VECTOR_DEADLINE_SECONDS,
)


FESS_URL = "http://127.0.0.1:8080"
ADMIN_USERNAME = "admin"
ADMIN_SECRET_PATH = Path("/run/secrets/fess_initial_admin_password")
SECRET_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_SECRET_BYTES = 128


class CLIError(ValueError):
    """A local invocation or secret-file invariant failed."""


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_uid,
        left.st_gid,
        left.st_nlink,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_uid,
        right.st_gid,
        right.st_nlink,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def read_admin_secret(path: Path = ADMIN_SECRET_PATH) -> str:
    try:
        before = path.lstat()
    except OSError:
        raise CLIError("administrator secret is unavailable") from None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise CLIError("administrator secret cannot be opened safely") from None
    try:
        opened = os.fstat(descriptor)
        if (
            not _same_file(before, opened)
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or not 2 <= opened.st_size <= MAX_SECRET_BYTES
        ):
            raise CLIError("administrator secret metadata is unsafe")
        raw = os.read(descriptor, MAX_SECRET_BYTES + 1)
        if len(raw) != opened.st_size or os.read(descriptor, 1):
            raise CLIError("administrator secret changed while being read")
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError:
            raise CLIError("administrator secret changed while being read") from None
        if not _same_file(opened, current):
            raise CLIError("administrator secret changed while being read")
    finally:
        os.close(descriptor)
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise CLIError("administrator secret has an invalid record shape")
    try:
        value = raw[:-1].decode("ascii", "strict")
    except UnicodeError:
        raise CLIError("administrator secret has an invalid encoding") from None
    if SECRET_RE.fullmatch(value) is None:
        raise CLIError("administrator secret has an invalid format")
    return value


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def token_deadline_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("deadline must be a number") from None
    if not 0 < parsed <= MAX_TOKEN_OPERATION_DEADLINE_SECONDS:
        raise argparse.ArgumentTypeError(
            "deadline must be in (0, %d] seconds"
            % int(MAX_TOKEN_OPERATION_DEADLINE_SECONDS)
        )
    return parsed


def vector_deadline_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("deadline must be a number") from None
    if not 0 < parsed <= MAX_VECTOR_DEADLINE_SECONDS:
        raise argparse.ArgumentTypeError(
            "deadline must be in (0, %d] seconds" % int(MAX_VECTOR_DEADLINE_SECONDS)
        )
    return parsed


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    commands = parser.add_subparsers(dest="command", required=True)

    import_command = commands.add_parser("import", help="import one sealed bundle")
    import_command.add_argument("--batch-size", type=int, default=100)
    import_command.add_argument(
        "--deadline-seconds", type=token_deadline_seconds, default=600.0
    )

    reconcile_command = commands.add_parser(
        "reconcile", help="read-only verification of one quarantined bundle"
    )
    reconcile_command.add_argument(
        "--deadline-seconds", type=token_deadline_seconds, default=180.0
    )

    pass_command = commands.add_parser(
        "vector-pass", help="run one production pass with private durable evidence"
    )
    pass_command.add_argument("--journal-dir", type=Path, required=True)
    pass_command.add_argument("--run-id", required=True)
    pass_command.add_argument("--recipe-sha256", required=True)
    pass_command.add_argument("--expected-before-state-sha256")
    pass_command.add_argument(
        "--deadline-seconds",
        type=vector_deadline_seconds,
        default=DEFAULT_VECTOR_PASS_DEADLINE_SECONDS,
    )
    pass_command.add_argument("--poll-seconds", type=float, default=1.0)

    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = parse_args(argv)
    password: Optional[str] = None
    try:
        bundle = CatalogBundle.load(
            arguments.bundle,
            expected_manifest_sha256=arguments.manifest_sha256,
        )
        password = read_admin_secret()
        access = AdminAccess(FESS_URL)
        if arguments.command in {"import", "reconcile"}:
            catalog = CatalogImporter(bundle, access)
            if arguments.command == "import":
                result = catalog.import_bundle(
                    ADMIN_USERNAME,
                    password,
                    batch_size=arguments.batch_size,
                    deadline_seconds=arguments.deadline_seconds,
                ).as_dict()
            elif arguments.command == "reconcile":
                result = catalog.reconcile_bundle(
                    ADMIN_USERNAME,
                    password,
                    deadline_seconds=arguments.deadline_seconds,
                ).as_dict()
        elif arguments.command == "vector-pass":
            catalog = VectorBuilder(bundle, access)
            from search.vector_journal import DurableVectorJournal

            identity = {
                "schema_version": 1,
                "run_id": arguments.run_id,
                "bundle_id": bundle.bundle_id,
                "manifest_sha256": bundle.manifest_sha256,
                "recipe_sha256": arguments.recipe_sha256,
            }
            with DurableVectorJournal(arguments.journal_dir, identity) as journal:
                result = catalog.build_vector_pass(
                    ADMIN_USERNAME,
                    password,
                    journal=journal,
                    run_id=arguments.run_id,
                    recipe_sha256=arguments.recipe_sha256,
                    expected_before_state_sha256=arguments.expected_before_state_sha256,
                    deadline_seconds=arguments.deadline_seconds,
                    poll_seconds=arguments.poll_seconds,
                ).as_dict()
        else:  # argparse makes this unreachable; keep the boundary explicit.
            raise CLIError("unsupported catalog-admin command")
        print(canonical_json(result))
        return 0
    except CatalogError as exc:
        polluted = bool(getattr(exc, "possibly_polluted", False))
        print(
            canonical_json(failure_record(exc, possibly_polluted=polluted)),
            file=sys.stderr,
        )
        return 4 if polluted else 3
    except (CLIError, OSError, ValueError) as exc:
        print(
            canonical_json(failure_record(exc, possibly_polluted=False)),
            file=sys.stderr,
        )
        return 2
    finally:
        password = None


if __name__ == "__main__":
    raise SystemExit(main())
