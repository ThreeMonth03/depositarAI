"""Skip unrelated specialist builds; the full core CPU gate always runs."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess


JOBS = frozenset({"private-website"})
SHA = re.compile(r"[0-9a-f]{40}\Z")


def required_jobs(paths: list[str]) -> frozenset[str]:
    if not paths:
        return JOBS
    required = set()
    for path in paths:
        if (
            not path
            or path.startswith("/")
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(character) < 32 for character in path)
        ):
            return JOBS
        if path == "README.md" or path.startswith("docs/"):
            continue
        if path.startswith("web/") or path == "scripts/test-search-web.sh":
            required.add("private-website")
        else:
            # Shared Python, requirements, workflows, deployment and unknown
            # paths conservatively keep every specialist gate.
            return JOBS
    return frozenset(required)


def changed_jobs(environment: dict[str, str], *, cwd: Path) -> frozenset[str]:
    if environment.get("GITHUB_EVENT_NAME") != "pull_request":
        return JOBS
    base = environment.get("CI_BASE_SHA", "")
    head = environment.get("CI_HEAD_SHA", "")
    if SHA.fullmatch(base) is None or SHA.fullmatch(head) is None:
        return JOBS
    try:
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            check=True,
            timeout=10,
        )
        if current.stdout.strip().decode("ascii") != head:
            return JOBS
        diff = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", base, head, "--"],
            cwd=cwd,
            capture_output=True,
            check=True,
            timeout=10,
        )
        # NUL-separated names include both old and new paths for renames.
        paths = diff.stdout.decode("utf-8").split("\0")
        if paths[-1] != "" or len(diff.stdout) > 1024 * 1024:
            return JOBS
        return required_jobs(paths[:-1])
    except (OSError, subprocess.SubprocessError, UnicodeError):
        # Missing shallow base or an unreadable diff must never skip checks.
        return JOBS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=sorted(JOBS))
    options = parser.parse_args()
    jobs = changed_jobs(dict(os.environ), cwd=Path(__file__).resolve().parents[2])
    print("run=" + ("true" if options.job in jobs else "false"))


if __name__ == "__main__":
    main()
