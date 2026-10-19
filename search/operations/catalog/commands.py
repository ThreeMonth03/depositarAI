"""Run catalog commands with the existing text and private-output policy.

Callers retain command selection and authority checks. This runner inherits the
operator environment after removing catalog source and Compose overrides; its
stdout-only bound is separate from the binary runtime Docker client contract.
"""

from __future__ import annotations

import os
import subprocess


MAX_OUTPUT_BYTES = 4 * 1024**2


class BackendError(ValueError):
    """A preparation invariant failed; the preserved slot must not be started."""


def default_runner(repo_root):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SEARCH_", "COMPOSE_", "SOURCE_REVISION"))
    }

    def execute(argv, *, timeout):
        return subprocess.run(
            argv,
            cwd=repo_root,
            env=environment,
            timeout=timeout,
            check=False,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            umask=0o077,
        )

    return execute


def command_output(runner, argv, *, timeout=30):
    result = runner(argv, timeout=timeout)
    if (
        result.returncode != 0
        or type(result.stdout) is not str
        or len(result.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES
    ):
        raise BackendError(
            "catalog preparation command failed; inspect private evidence"
        )
    return result.stdout
