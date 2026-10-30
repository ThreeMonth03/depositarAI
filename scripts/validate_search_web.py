"""Guard the standalone CPU frontend composition."""

import argparse
import json
from pathlib import Path
import sys


def validate(config, *, intranet=False, prepare=False):
    from search.operations.deployment.frontend.policy import (
        validate as validate_compose,
    )

    return validate_compose(config, intranet=intranet, prepare=prepare)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intranet", action="store_true")
    parser.add_argument("--prepare-access", action="store_true")
    args = parser.parse_args()
    description = "Intranet" if args.intranet else "Private"
    try:
        validate(
            json.load(sys.stdin), intranet=args.intranet, prepare=args.prepare_access
        )
    except (ValueError, KeyError, TypeError, OSError):
        print(f"{description} website deployment policy failed.", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"{description} website deployment policy passed.")
