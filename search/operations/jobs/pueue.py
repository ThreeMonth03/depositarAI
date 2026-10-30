"""Bounded transport to the reviewed private Pueue instance."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from search.operations.shared import private_io
from search.operations.shared.bounded_process import captured_pipes
from search.serialization import strict_json


BINARY_SHA256 = "c1b10d7e4e62211075ddd0e1dc3e8cbfc5a43d662cb3be7402a28504e23fcb51"
ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
MAX_OUTPUT = 2 * 1024**2


class JobError(ValueError):
    """A fixed diagnostic without child output or private paths."""


def require(condition, code):
    if not condition:
        raise JobError("job_" + code)


def command(argv, *, timeout=30, cwd=None):
    """Run fixed argv with bounded pipes and no inherited credentials/hooks."""
    try:
        with subprocess.Popen(
            [str(part) for part in argv],
            cwd=cwd,
            env=ENVIRONMENT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            umask=0o077,
        ) as process:
            with captured_pipes(
                process,
                timeout=timeout,
                max_bytes=MAX_OUTPUT,
                timeout_error=lambda: JobError("job_command_timeout"),
                output_error=lambda: JobError("job_command_output_limit"),
            ) as (remaining, stdout, stderr):
                status = process.wait(timeout=remaining)
                return subprocess.CompletedProcess(
                    argv, status, b"".join(stdout), b"".join(stderr)
                )
    except (OSError, subprocess.SubprocessError):
        raise JobError("job_command_failed") from None


def configuration(registry):
    """Return the sole supported daemon configuration, without shell hooks."""
    root, runtime = Path(registry["root"]), Path(registry["runtime_root"])

    def quoted(value):
        return json.dumps(str(value))

    return (
        "shared:\n"
        f"  pueue_directory: {quoted(root / 'pueue')}\n"
        f"  alias_file: {quoted(root / 'aliases.yml')}\n"
        f"  runtime_directory: {quoted(runtime)}\n"
        f"  unix_socket_path: {quoted(runtime / 'depositarai-heavy.socket')}\n"
        "  use_unix_socket: true\n"
        "client:\n"
        "  restart_in_place: false\n"
        "  read_local_logs: true\n"
        "daemon:\n"
        "  pause_group_on_failure: true\n"
        "  pause_all_on_failure: true\n"
        '  shell_command: ["/bin/sh", "-c", "{{ pueue_command_string }}"]\n'
        "  callback: null\n"
        "  env_vars: {}\n"
    ).encode()


class Pueue:
    def __init__(self, registry):
        self.registry = registry

    def call(self, *arguments):
        registry = self.registry
        root = Path(registry["root"])
        fd = private_io.open_directory(root)
        try:
            encoded = private_io.read_file(fd, "pueue.yml", mode=0o600, max_bytes=16384)
            require(encoded == configuration(registry), "daemon_config_changed")
            require(
                private_io.read_file(fd, "aliases.yml", mode=0o600, max_bytes=64)
                == b"{}\n",
                "aliases_changed",
            )
        finally:
            os.close(fd)
        binary = Path(registry["pueue"])
        parent = private_io.open_directory(binary.parent)
        try:
            encoded = private_io.read_file(
                parent, binary.name, mode=0o700, max_bytes=4 * 1024**2
            )
            require(
                hashlib.sha256(encoded).hexdigest() == BINARY_SHA256, "binary_changed"
            )
        finally:
            os.close(parent)
        require(stat.S_ISREG(binary.lstat().st_mode), "binary_invalid")
        result = command([binary, "--config", root / "pueue.yml", *arguments])
        require(result.returncode == 0, "queue_command_failed")
        return result.stdout

    def state(self):
        value = strict_json(self.call("status", "--json"))
        require(
            type(value) is dict
            and set(value) == {"tasks", "groups"}
            and type(value["tasks"]) is dict
            and type(value["groups"]) is dict
            and set(value["groups"]) == {"default", "heavy"},
            "queue_state_invalid",
        )
        group = value["groups"]["heavy"]
        require(
            type(group) is dict
            and set(group) == {"status", "parallel_tasks"}
            and group["status"] in {"Running", "Paused"}
            and type(group["parallel_tasks"]) is int
            and group["parallel_tasks"] == 1,
            "queue_parallelism_invalid",
        )
        require(
            len(value["tasks"]) <= 128
            and all(
                type(task) is dict
                and task.get("group") == "heavy"
                and type(task.get("status")) is dict
                and len(task["status"]) == 1
                for task in value["tasks"].values()
            ),
            "queue_peer_invalid",
        )
        return value

    def seal_environment(self, task_id):
        """Check and trim the persisted environment before releasing a task."""
        task = self.state()["tasks"].get(str(task_id))
        require(
            type(task) is dict and "Stashed" in task["status"],
            "task_not_stashed",
        )
        environment = task.get("envs")
        require(
            type(environment) is dict
            and len(environment) <= 128
            and all(environment.get(key) == value for key, value in ENVIRONMENT.items())
            and all(
                type(key) is str and 1 <= len(key) <= 128 and "\x00" not in key
                for key in environment
            ),
            "task_environment_changed",
        )
        # Pueue captures the client process environment; remove even runtime-
        # supplied extras rather than assuming Popen's input is its final state.
        for key in sorted(environment.keys() - ENVIRONMENT.keys()):
            self.call("env", "unset", str(task_id), "--", key)
        require(
            self.state()["tasks"][str(task_id)].get("envs") == ENVIRONMENT,
            "task_environment_changed",
        )
