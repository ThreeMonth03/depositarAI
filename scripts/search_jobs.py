#!/usr/bin/env python3
"""Submit or inspect fixed jobs through the account's private Pueue queue."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from search.operations.jobs import queue
from search.operations.jobs.pueue import JobError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("submit").add_argument("--request", required=True)
    commands.add_parser("status").add_argument("--job")
    for name in ("result", "cancel", "execute", "archive"):
        commands.add_parser(name).add_argument("--job", required=True)
    arguments = parser.parse_args(argv)
    try:
        registry = queue.load_registry()
        if arguments.operation == "submit":
            value = queue.submit(registry, queue.read(arguments.request))
        elif arguments.operation == "status" and arguments.job is None:
            value = queue.statuses(registry)
        elif arguments.operation == "status":
            value = queue.status(registry, arguments.job)
        elif arguments.operation == "result":
            value = queue.result(registry, arguments.job)
        elif arguments.operation == "cancel":
            value = queue.cancel(registry, arguments.job)
        elif arguments.operation == "archive":
            from search.operations.jobs.archive import archive

            value = archive(registry, arguments.job)
        else:
            value = queue.execute(registry, arguments.job)
    except JobError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    except KeyboardInterrupt:
        print('{"error":"job_interrupted"}')
        return 130
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        print('{"error":"job_failed"}')
        return 1
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
