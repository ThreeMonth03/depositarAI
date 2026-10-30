#!/usr/bin/env python3
"""Run one reviewed CPU backend against the existing fixed inference plane."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from search.operations.deployment.candidate.controller import main

if __name__ == "__main__":
    raise SystemExit(main())
