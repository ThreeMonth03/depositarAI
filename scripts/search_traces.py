#!/usr/bin/env python3
"""Inspect, export, delete and expire consented private search traces."""

from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from search.trace.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
