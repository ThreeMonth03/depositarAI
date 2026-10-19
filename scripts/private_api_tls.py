#!/usr/bin/env python3
"""Prepare and operate the independent private TLS API endpoint."""

from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from search.operations.deployment.private_api_tls import main

    raise SystemExit(main())
