#!/usr/bin/env python3
"""Check an existing standalone frontend without changing its deployment."""

from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from search.operations.deployment.frontend.check import main

    raise SystemExit(main())
