#!/usr/bin/env python3
"""Launch the private product CLI from a source checkout."""

from pathlib import Path
import sys


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from search.operations.cli import main

    raise SystemExit(main())
