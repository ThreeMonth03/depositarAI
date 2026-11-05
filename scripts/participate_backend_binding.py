#!/usr/bin/env python3
"""Serve the fixed backend binding participant over foreground stdin."""

import argparse
from pathlib import Path
import sys


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("backend_transition_arguments_invalid")


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from search.operations.deployment.frontend import binding
    from search.operations.deployment.transition.transport import PeerFrames

    parser = _Parser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--expected-entry", required=True)
    try:
        args = parser.parse_args(argv)
        with binding.open_participant(
            args.config,
            expected_source=args.expected_source,
            expected_entry=args.expected_entry,
        ) as participant:
            frames = PeerFrames(
                sys.stdin.buffer,
                sys.stdout.buffer,
                deadline=participant.deadline,
            )
            participant.serve(frames)
            return 1 if participant.uncertain else 0
    except KeyboardInterrupt:
        sys.stderr.write("backend_transition_interrupted\n")
        return 130
    except (ValueError, OSError, TypeError, KeyError, RecursionError):
        sys.stderr.write("backend_transition_recovery_required\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
