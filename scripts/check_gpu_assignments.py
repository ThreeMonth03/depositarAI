#!/usr/bin/env python3
"""Validate mutually exclusive physical-GPU assignments for platform roles.

The validator is deliberately independent of Docker and Compose so the same
policy can be checked in CI and before a deployment.  Values are comma-
separated full GPU UUIDs; numeric indexes, ``all``, and MIG UUIDs are rejected.
When ``--available-from-stdin`` is used, stdin must contain the strict output of
``nvidia-smi --query-gpu=uuid,index --format=csv,noheader,nounits``.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence


GPU_UUID_PATTERN = re.compile(
    r"^GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

ROLE_CARDINALITY: Mapping[str, tuple[int, int | None]] = {
    "embedding": (0, 1),
    "scope": (1, 1),
    "answer": (2, 2),
    "jupyter": (0, None),
}


def _parse_role(value: str, role: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError(f"{role} GPU assignment must be a comma-separated string")

    entries = tuple(item.strip() for item in value.split(",")) if value else ()
    if any(not item for item in entries):
        raise ValueError(f"{role} GPU assignment contains an empty entry")

    invalid = [item for item in entries if GPU_UUID_PATTERN.fullmatch(item) is None]
    if invalid:
        raise ValueError(
            f"{role} accepts only full physical GPU UUIDs from nvidia-smi: "
            + ", ".join(invalid)
        )
    # NVIDIA UUID hex is case-insensitive. Canonicalize before duplicate and
    # ownership checks so spelling the same physical UUID with different hex
    # case cannot bypass cross-role isolation.
    entries = tuple("GPU-" + item[4:].lower() for item in entries)
    if len(entries) != len(set(entries)):
        raise ValueError(f"{role} GPU assignment contains a duplicate UUID")

    minimum, maximum = ROLE_CARDINALITY[role]
    if len(entries) < minimum or (maximum is not None and len(entries) > maximum):
        if minimum == maximum:
            expectation = f"exactly {minimum}"
        elif maximum is None:
            expectation = f"at least {minimum}"
        else:
            expectation = f"between {minimum} and {maximum}"
        raise ValueError(f"{role} must select {expectation} physical GPU(s)")
    return entries


def parse_available_gpu_inventory(value: str) -> set[str]:
    """Parse strict ``UUID,index`` records emitted by ``nvidia-smi``."""

    available: set[str] = set()
    seen_indexes: set[int] = set()
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        fields = tuple(field.strip() for field in line.split(","))
        if len(fields) != 2 or GPU_UUID_PATTERN.fullmatch(fields[0]) is None:
            raise ValueError(
                f"invalid nvidia-smi inventory record on line {line_number}"
            )
        if not fields[1].isdigit():
            raise ValueError(f"invalid nvidia-smi GPU index on line {line_number}")
        index = int(fields[1])
        gpu_uuid = "GPU-" + fields[0][4:].lower()
        if gpu_uuid in available:
            raise ValueError(f"nvidia-smi reported duplicate GPU UUID {gpu_uuid}")
        if index in seen_indexes:
            raise ValueError(f"nvidia-smi reported duplicate GPU index {index}")
        available.add(gpu_uuid)
        seen_indexes.add(index)
    if not available:
        raise ValueError("nvidia-smi reported no physical GPUs")
    return available


def validate_gpu_assignments(
    *,
    embedding: str,
    scope: str,
    answer: str,
    jupyter: str,
    available_inventory: str | None = None,
) -> dict[str, tuple[str, ...]]:
    """Return validated role assignments, or raise ``ValueError`` fail-closed."""

    raw_assignments = {
        "embedding": embedding,
        "scope": scope,
        "answer": answer,
        "jupyter": jupyter,
    }
    assignments = {
        role: _parse_role(raw_assignments[role], role) for role in ROLE_CARDINALITY
    }

    owners: dict[str, str] = {}
    for role, gpu_uuids in assignments.items():
        for gpu_uuid in gpu_uuids:
            previous_role = owners.get(gpu_uuid)
            if previous_role is not None:
                raise ValueError(
                    f"physical GPU {gpu_uuid} is assigned to both "
                    f"{previous_role} and {role}"
                )
            owners[gpu_uuid] = role

    if available_inventory is not None:
        available = parse_available_gpu_inventory(available_inventory)
        missing = [gpu_uuid for gpu_uuid in owners if gpu_uuid not in available]
        if missing:
            raise ValueError(
                "assigned physical GPU is not present in nvidia-smi inventory: "
                + ", ".join(missing)
            )
    return assignments


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedding-gpus", default="")
    parser.add_argument("--scope-gpus", required=True)
    parser.add_argument("--answer-gpus", required=True)
    parser.add_argument("--jupyter-gpus", default="")
    parser.add_argument(
        "--available-from-stdin",
        action="store_true",
        help="validate assignments against strict nvidia-smi UUID,index records",
    )
    args = parser.parse_args(argv)
    available = sys.stdin.read() if args.available_from_stdin else None
    try:
        validate_gpu_assignments(
            embedding=args.embedding_gpus,
            scope=args.scope_gpus,
            answer=args.answer_gpus,
            jupyter=args.jupyter_gpus,
            available_inventory=available,
        )
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print("GPU assignments are valid and pairwise disjoint.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
