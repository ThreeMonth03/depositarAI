"""Bounded local Docker client shared by verified private runtime workflows.

Callers own command and runtime-authority policy. The client retains fixed
operator diagnostics, an isolated CLI environment and per-workflow budgets.
The default command runner and image JSON parsing remain owned by image_build.
"""

from __future__ import annotations

import re
import subprocess
import time

from search.operations.deployment import image_build


MAX_COMMANDS = 64
TOTAL_SECONDS = 1800
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")


class InferenceError(ValueError):
    """Fixed operator errors without command output, Env, secrets or bytes."""


class Docker:
    """Reuse shared bounded pipe capture, with this workflow's closed budget."""

    def __init__(
        self,
        client,
        runner,
        *,
        deadline=None,
        max_commands=MAX_COMMANDS,
        verify_client=None,
    ):
        if type(max_commands) is not int or not 1 <= max_commands <= 128:
            raise InferenceError("inference_command_budget_invalid")
        self.max_commands = max_commands
        if verify_client is not None and not callable(verify_client):
            raise InferenceError("inference_client_guard_invalid")
        self.verify_client = verify_client
        self.prefix = [
            "docker",
            "--host",
            image_build.DOCKER_HOST,
            "--config",
            str(client),
        ]
        self.environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        self.runner = image_build._run if runner is None else runner
        self.deadline = (
            time.monotonic() + TOTAL_SECONDS if deadline is None else deadline
        )
        self.calls, self.total_bytes = 0, 0

    def call(self, arguments, *, timeout=30):
        if self.verify_client is not None:
            self.verify_client()
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        if self.calls > self.max_commands or remaining <= 0:
            raise InferenceError("inference_command_budget")
        try:
            result = self.runner(
                self.prefix + arguments,
                timeout=min(timeout, remaining),
                environment=dict(self.environment),
            )
            if self.verify_client is not None:
                self.verify_client()
            streams = [
                value.encode("utf-8") if type(value) is str else value
                for value in (result.stdout, result.stderr)
            ]
            if (
                any(type(value) is not bytes for value in streams)
                or type(result.returncode) is not int
            ):
                raise InferenceError("inference_command_invalid")
            size = sum(map(len, streams))
            self.total_bytes += size
            if (
                size > image_build.MAX_OUTPUT_BYTES
                or self.total_bytes > image_build.MAX_TOTAL_OUTPUT_BYTES
            ):
                raise InferenceError("inference_output_limit")
            if time.monotonic() > self.deadline:
                raise InferenceError("inference_command_timeout")
            if result.returncode != 0:
                raise InferenceError("inference_command_failed")
            return streams[0]
        except InferenceError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError, UnicodeError):
            raise InferenceError("inference_command_failed") from None

    def inspect(self, reference):
        value = image_build._json(
            self.call(["image", "inspect", "--format", "{{json .}}", reference])
        )
        if type(value) is not dict:
            raise InferenceError("inference_image_inspection_invalid")
        return value


def project_inventory(docker, project):
    """Read bounded container IDs and network names for one Compose project."""
    raw = docker.call(
        [
            "ps",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            "label=com.docker.compose.project=" + project,
        ]
    )
    try:
        ids = raw.decode("ascii").splitlines()
    except UnicodeError:
        raise InferenceError("inference_container_inventory_invalid") from None
    if (
        len(ids) > 3
        or len(set(ids)) != len(ids)
        or any(_CONTAINER_ID.fullmatch(value) is None for value in ids)
    ):
        raise InferenceError("inference_container_inventory_invalid")
    raw = docker.call(
        [
            "network",
            "ls",
            "--no-trunc",
            "--format",
            "{{json .}}",
            "--filter",
            "name=" + project + "_",
        ]
    )
    networks = [image_build._json(line) for line in raw.splitlines()]
    if len(networks) > 2 or any(type(value) is not dict for value in networks):
        raise InferenceError("inference_network_inventory_invalid")
    names = [value.get("Name") for value in networks]
    if any(type(name) is not str for name in names) or len(set(names)) != len(names):
        raise InferenceError("inference_network_inventory_invalid")
    return ids, set(names)
