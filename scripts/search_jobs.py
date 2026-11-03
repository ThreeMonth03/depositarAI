#!/usr/bin/env python3
"""Submit or inspect fixed jobs through the account's private Pueue queue."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from search.operations.jobs import queue
from search.operations.jobs.pueue import JobError


def _dispatch(registry, arguments):
    if arguments.operation == "submit":
        return queue.submit(registry, queue.read(arguments.request))
    elif arguments.operation == "status" and arguments.job is None:
        return queue.statuses(registry)
    elif arguments.operation == "status":
        return queue.status(registry, arguments.job)
    elif arguments.operation == "result":
        return queue.result(registry, arguments.job)
    elif arguments.operation == "cancel":
        return queue.cancel(registry, arguments.job)
    elif arguments.operation == "archive":
        from search.operations.jobs.archive import archive

        return archive(registry, arguments.job)
    return queue.execute(registry, arguments.job)


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
        value = _dispatch(registry, arguments)
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
