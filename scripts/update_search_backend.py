#!/usr/bin/env python3
"""Coordinate a source-pinned private backend binding operation."""

from pathlib import Path
import sys


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from search.operations.deployment.transition.controller import main as run

    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
