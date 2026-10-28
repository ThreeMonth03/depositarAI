"""Own a foreground SSH demo connection and verify it without model requests.

Each hop executes its next SSH client on that host, retaining server-only keys.
Private operator profiles and SSH configurations are trusted execution inputs.
Only the first hop's files are local; subsequent paths belong to previous hosts.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

from search.operations.shared.bounded_http import AbortableConnection
from search.operations.shared.bounded_process import captured_pipes
from search.serialization import strict_json


ORIGIN = "http://127.0.0.1:18081"
PROFILE_BYTES = 16384
SSH_OUTPUT_BYTES = 65536
CHECK_SECONDS = 120
START_SECONDS = 30
HTTP_SECONDS = 8
DESTINATION = re.compile(
    r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]{0,252}\Z"
)
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
READY_PREFIX = "DEPOSITAR_DEMO_READY_"


class DemoConnectionError(ValueError):
    """Fixed stage diagnostics; never include SSH output or response bodies."""

    def __init__(self, stage, code):
        self.stage, self.code = stage, code
        super().__init__(code)


class DemoInterrupted(BaseException):
    """Let termination signals unwind the owned process contexts."""

    def __init__(self, number):
        self.number = number


@contextmanager
def termination_signals():
    previous = {
        number: signal.getsignal(number)
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
    }

    def interrupted(number, _frame):
        # A second terminal signal must not interrupt cleanup of the first.
        for handled in previous:
            signal.signal(handled, signal.SIG_IGN)
        raise DemoInterrupted(number)

    try:
        for number in previous:
            signal.signal(number, interrupted)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def require(value, stage, code):
    if not value:
        raise DemoConnectionError(stage, code)


def absolute_path(value):
    return (
        type(value) is str
        and 1 <= len(value) <= 2048
        and PurePosixPath(value).is_absolute()
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def validate_profile(value):
    require(type(value) is dict, "profile", "profile_invalid")
    require(
        set(value)
        == {
            "schema_version",
            "browser_origin",
            "remote_port",
            "hops",
            "remote_checker",
            "remote_profile",
            "expected",
        }
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["browser_origin"] == ORIGIN
        and type(value["remote_port"]) is int
        and value["remote_port"] == 18081,
        "profile",
        "profile_invalid",
    )
    hops = value["hops"]
    require(type(hops) is list and 1 <= len(hops) <= 3, "profile", "hops_invalid")
    for index, hop in enumerate(hops):
        require(
            type(hop) is dict
            and set(hop) == {"destination", "ssh_config", "local_forward_port"}
            and type(hop["destination"]) is str
            and DESTINATION.fullmatch(hop["destination"]) is not None
            and (hop["ssh_config"] is None or absolute_path(hop["ssh_config"]))
            and type(hop["local_forward_port"]) is int
            and 1024 <= hop["local_forward_port"] <= 65535
            and (index != 0 or hop["local_forward_port"] == 18081),
            "profile",
            "hop_invalid",
        )
    require(
        absolute_path(value["remote_checker"])
        and absolute_path(value["remote_profile"]),
        "profile",
        "remote_paths_invalid",
    )
    expected = value["expected"]
    require(
        type(expected) is dict
        and set(expected)
        == {
            "frontend_revision",
            "api_revision",
            "conversation_contract_version",
            "catalog_manifest_sha256",
        },
        "profile",
        "expected_identity_invalid",
    )
    for name, pattern in (
        ("frontend_revision", HEX40),
        ("api_revision", HEX40),
        ("catalog_manifest_sha256", HEX64),
    ):
        require(
            type(expected[name]) is str
            and pattern.fullmatch(expected[name]) is not None,
            "profile",
            "expected_identity_invalid",
        )
    require(
        type(expected["conversation_contract_version"]) is int
        and expected["conversation_contract_version"] == 3,
        "profile",
        "expected_identity_invalid",
    )
    return value


def load_profile(path):
    try:
        path = Path(path).expanduser()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) in (0o400, 0o600)
                and 0 < info.st_size <= PROFILE_BYTES,
                "profile",
                "profile_permissions_invalid",
            )
            data = stream.read(PROFILE_BYTES + 1)
        require(len(data) <= PROFILE_BYTES, "profile", "profile_invalid")
        return validate_profile(strict_json(data))
    except DemoConnectionError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise DemoConnectionError("profile", "profile_unreadable") from None


def ssh_prefix(hop, *, terminal=False, readiness_nonce=None):
    args = ["ssh"]
    if hop["ssh_config"] is not None:
        args += ["-F", hop["ssh_config"]]
    args += [
        "-tt" if terminal else "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "PermitLocalCommand=yes" if readiness_nonce else "PermitLocalCommand=no",
        "-o",
        "RemoteCommand=none",
        "-o",
        "ClearAllForwardings=no",
        "-o",
        "ForkAfterAuthentication=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "ForwardX11Trusted=no",
    ]
    if readiness_nonce:
        args += ["-o", f"LocalCommand=printf '{READY_PREFIX}{readiness_nonce}\\n'"]
    return args


def ssh_command(profile, *, tunnel, nonce=None):
    """Quote each remote argv once; never interpolate shell fragments."""
    if tunnel:
        require(
            type(nonce) is str and HEX64.fullmatch(nonce) is not None,
            "tunnel",
            "readiness_nonce_invalid",
        )
    command = (
        None
        if tunnel
        else [
            "python3",
            "-I",
            "-S",
            "-B",
            profile["remote_checker"],
            "--config",
            profile["remote_profile"],
        ]
    )
    hops = profile["hops"]
    for index in reversed(range(len(hops))):
        hop = hops[index]
        args = ssh_prefix(
            hop,
            terminal=tunnel and index != len(hops) - 1,
            readiness_nonce=nonce if tunnel and index == len(hops) - 1 else None,
        )
        if tunnel:
            target_port = (
                profile["remote_port"]
                if index == len(hops) - 1
                else hops[index + 1]["local_forward_port"]
            )
            args += [
                "-L",
                f"127.0.0.1:{hop['local_forward_port']}:127.0.0.1:{target_port}",
            ]
            if command is None:
                args += ["-N"]
                # LocalCommand runs synchronously after local forwarding setup.
                # A marker from this innermost client proves every outer hop
                # reached its command after binding its own forwarding port.
                # This fixed command contains no OpenSSH percent expansions.
        args += [hop["destination"]]
        if command is not None:
            args += [shlex.join(command)]
        command = args
    return command


def remote_check(profile):
    try:
        process = subprocess.Popen(
            ssh_command(profile, tunnel=False),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        with captured_pipes(
            process,
            timeout=CHECK_SECONDS,
            max_bytes=SSH_OUTPUT_BYTES,
            timeout_error=lambda: DemoConnectionError(
                "remote_check", "remote_check_timeout"
            ),
            output_error=lambda: DemoConnectionError(
                "remote_check", "remote_check_output_limit"
            ),
        ) as (remaining, out, _err):
            require(
                process.wait(timeout=remaining) == 0,
                "remote_check",
                "remote_check_failed",
            )
            value = strict_json(b"".join(out))
        return validate_remote_check(value, profile["expected"])
    except DemoConnectionError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError, RecursionError):
        raise DemoConnectionError("remote_check", "remote_check_failed") from None


def validate_remote_check(value, expected):
    fields = {
        "schema_version",
        "status",
        "frontend_revision",
        "api_revision",
        "catalog_manifest_sha256",
        "conversation_contract_version",
        "runtime",
        "homepage_verified",
        "assets_verified",
        "backend_identity_verified",
        "upstream_tls_verified",
        "authenticated_version_status",
        "unauthenticated_version_status",
        "model_inference_verified",
        "public_readiness_verified",
        "deployment_performed",
    }
    require(
        type(value) is dict and set(value) == fields,
        "remote_check",
        "remote_check_invalid",
    )
    require(
        type(value["schema_version"]) is int
        and value["schema_version"] == 2
        and value["status"] == "frontend_checked",
        "remote_check",
        "remote_check_invalid",
    )
    for key, wanted in expected.items():
        require(
            type(value[key]) is type(wanted) and value[key] == wanted,
            "remote_check",
            "deployment_identity_mismatch",
        )
    for key in (
        "homepage_verified",
        "backend_identity_verified",
        "upstream_tls_verified",
    ):
        require(value[key] is True, "remote_check", "remote_check_invalid")
    for key in (
        "model_inference_verified",
        "public_readiness_verified",
        "deployment_performed",
    ):
        require(value[key] is False, "remote_check", "remote_check_invalid")
    for key, wanted in (
        ("authenticated_version_status", 200),
        ("unauthenticated_version_status", 401),
    ):
        require(
            type(value[key]) is int and value[key] == wanted,
            "remote_check",
            "remote_check_invalid",
        )
    require(
        type(value["assets_verified"]) is int and 1 <= value["assets_verified"] <= 16,
        "remote_check",
        "remote_check_invalid",
    )
    runtime = value["runtime"]
    require(
        type(runtime) is dict and set(runtime) == {"web", "web-nginx"},
        "remote_check",
        "remote_check_invalid",
    )
    for row in runtime.values():
        require(
            type(row) is dict
            and set(row) == {"container_id", "image"}
            and type(row["container_id"]) is str
            and HEX64.fullmatch(row["container_id"]) is not None
            and type(row["image"]) is str
            and IMAGE.fullmatch(row["image"]) is not None,
            "remote_check",
            "remote_check_invalid",
        )
    return value


def available_port(port):
    try:
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
    except OSError:
        raise DemoConnectionError("tunnel", "local_port_in_use") from None


class Tunnel:
    """Drain bounded SSH output and own only this fresh local process group."""

    def __init__(self, process, nonce):
        self.process = process
        self.failure = None
        self.marker = (READY_PREFIX + nonce).encode("ascii")
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.reader = threading.Thread(target=self._drain, daemon=True)
        try:
            self.reader.start()
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
            raise

    def _drain(self):
        try:
            with selectors.DefaultSelector() as selector:
                for name, stream in (
                    ("stdout", self.process.stdout),
                    ("stderr", self.process.stderr),
                ):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                size = 0
                pending = bytearray()
                while selector.get_map() and not self.stop.is_set():
                    for key, _ in selector.select(0.1):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                        size += len(data)
                        if size > SSH_OUTPUT_BYTES:
                            self.failure = "tunnel_output_limit"
                            return
                        if key.data == "stdout":
                            pending.extend(data)
                            while b"\n" in pending:
                                line, _, rest = pending.partition(b"\n")
                                pending = bytearray(rest)
                                # Each intermediate PTY may add another CR.
                                line = bytes(line).rstrip(b"\r")
                                if line.startswith(READY_PREFIX.encode("ascii")):
                                    if line != self.marker or self.ready.is_set():
                                        self.failure = "forward_readiness_invalid"
                                        return
                                    self.ready.set()
        except (OSError, ValueError):
            self.failure = "tunnel_output_failed"

    def verify(self):
        require(self.failure is None, "tunnel", self.failure or "tunnel_failed")
        require(self.process.poll() is None, "tunnel", "tunnel_closed")

    def wait_ready(self, deadline):
        while not self.ready.wait(0.05):
            self.verify()
            require(time.monotonic() < deadline, "tunnel", "forward_readiness_timeout")
        self.verify()

    def close(self):
        # Even an exited leader may leave descendants in this owned group.
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=5)
        self.stop.set()
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()


@contextmanager
def open_tunnel(profile):
    available_port(profile["hops"][0]["local_forward_port"])
    try:
        nonce = secrets.token_hex(32)
        process = subprocess.Popen(
            ssh_command(profile, tunnel=True, nonce=nonce),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        raise DemoConnectionError("tunnel", "ssh_unavailable") from None
    tunnel = Tunnel(process, nonce)
    try:
        yield tunnel
    finally:
        tunnel.close()


class Loopback(AbortableConnection, http.client.HTTPConnection):
    def __init__(self, timeout):
        http.client.HTTPConnection.__init__(self, "127.0.0.1", 18081, timeout=timeout)
        self._init_abortable()

    def connect(self):
        self._publish_channel(
            socket.create_connection(("127.0.0.1", 18081), timeout=self.timeout)
        )


def get(path, *, deadline, maximum):
    remaining = deadline - time.monotonic()
    require(remaining > 0, "http", "http_timeout")
    connection = Loopback(min(3, remaining))
    watchdog = threading.Timer(remaining, connection.abort)
    watchdog.daemon = True
    watchdog.start()
    response = None
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Origin": ORIGIN,
                "Accept": "text/html" if path == "/" else "application/json",
            },
        )
        response = connection.getresponse()
        require(
            response.status == 200,
            "http",
            "origin_denied" if response.status == 403 else "http_status_invalid",
        )
        body = response.read(maximum + 1)
        require(
            len(body) <= maximum and time.monotonic() < deadline,
            "http",
            "http_body_invalid",
        )
        return body
    finally:
        watchdog.cancel()
        if response is not None:
            response.close()
        connection.finish()


def local_check(*, deadline=None):
    deadline = (
        min(deadline, time.monotonic() + HTTP_SECONDS)
        if deadline is not None
        else time.monotonic() + HTTP_SECONDS
    )
    require(
        strict_json(get("/healthz", deadline=deadline, maximum=4096))
        == {"status": "ok"},
        "http",
        "health_invalid",
    )
    page = get("/", deadline=deadline, maximum=65536)
    require(
        b'<div id="root"></div>' in page and b"<title>depositar AI</title>" in page,
        "http",
        "homepage_invalid",
    )
    access = strict_json(get("/api/access", deadline=deadline, maximum=4096))
    require(
        access
        == {
            "mode": "private",
            "principal": {"kind": "private", "id": None},
            "can_search": True,
            "login_url": None,
            "logout_url": None,
            "limits": {"turns": None, "window_seconds": None},
        }
        and access["can_search"] is True,
        "http",
        "private_access_invalid",
    )


def verify_local(tunnel):
    deadline = time.monotonic() + START_SECONDS
    tunnel.wait_ready(deadline)
    while True:
        tunnel.verify()
        try:
            local_check(deadline=deadline)
            tunnel.verify()
            return
        except (OSError, http.client.HTTPException):
            require(time.monotonic() < deadline, "http", "tunnel_not_reachable")
            time.sleep(0.1)
        except (ValueError, TypeError, RecursionError) as error:
            if isinstance(error, DemoConnectionError):
                raise
            raise DemoConnectionError("http", "http_response_invalid") from None


def run(profile, *, connect=False, emit=print):
    require(os.name == "posix", "profile", "platform_unsupported")
    remote_check(profile)
    with open_tunnel(profile) as tunnel:
        verify_local(tunnel)
        report = {
            "schema_version": 1,
            "status": "demo_ready",
            "browser_origin": ORIGIN,
            "ssh_hops": len(profile["hops"]),
            "deployment_identity_verified": True,
            "local_http_verified": True,
            "model_requests_submitted": 0,
            **profile["expected"],
        }
        if connect:
            emit(json.dumps(report, sort_keys=True))
            while True:
                tunnel.verify()
                time.sleep(0.2)
        return {**report, "status": "demo_checked", "own_tunnel_closed": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("check", "connect"))
    parser.add_argument(
        "--config", required=True, help="operator-owned 0400/0600 private JSON profile"
    )
    options = parser.parse_args(argv)
    try:
        with termination_signals():
            value = run(
                load_profile(options.config),
                connect=options.operation == "connect",
                emit=lambda row: print(row, flush=True),
            )
        print(json.dumps(value, sort_keys=True))
        return 0
    except DemoInterrupted as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "closed",
                    "code": "interrupted",
                    "signal": signal.Signals(error.number).name,
                    "model_requests_submitted": 0,
                }
            )
        )
        return 128 + error.number
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "closed",
                    "code": "interrupted",
                    "model_requests_submitted": 0,
                }
            )
        )
        return 130
    except DemoConnectionError as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "failed",
                    "stage": error.stage,
                    "code": error.code,
                    "model_requests_submitted": 0,
                }
            ),
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "failed",
                    "stage": "connection",
                    "code": "demo_connection_failed",
                    "model_requests_submitted": 0,
                }
            ),
            file=sys.stderr,
        )
        return 1
