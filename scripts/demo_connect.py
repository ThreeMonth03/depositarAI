#!/usr/bin/env python3
"""Connect to a reviewed private demo through existing OpenSSH hops."""

from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from search.operations.deployment.demo_connection import main

    raise SystemExit(main())
