#!/usr/bin/env python3
"""Deploy or recover an attested standalone CPU frontend release."""

from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from search.operations.deployment.frontend_delivery import main

    raise SystemExit(main())
