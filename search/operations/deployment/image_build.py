"""Build four reviewed private images; never start services or publish images.

The caller supplies an already staged, trusted source bundle and an independent
expected revision. Durable intent precedes each build. A partial/uncertain role
is not retried or cleaned up; completed roles are reusable only after fresh
source, IID and actual immutable-image inspection. Labels are not independent
attestation: these receipts bind this controlled build to its sealed context.
Image environment values and Docker output remain in memory, never receipts.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time

from search.operations.deployment import source_bundle
from search.operations.shared import private_io
from search.operations.shared.bounded_process import captured_pipes


DOCKER_HOST = "unix:///var/run/docker.sock"
MAX_OUTPUT_BYTES = 4 * 1024**2
MAX_TOTAL_OUTPUT_BYTES = 32 * 1024**2
MAX_COMMANDS = 16
BUILD_TIMEOUT_SECONDS = 900
INSPECT_TIMEOUT_SECONDS = 30
TOTAL_BUDGET_SECONDS = 4000
MAX_RECORD_BYTES = 1024**2
SERVICES = ("conversation-api", "web", "reranker", "web-nginx")
TAGS = {
    "conversation-api": "depositar-search-conversation:",
    "web": "depositar-search-web:",
    "reranker": "depositar-search-reranker:",
    "web-nginx": "depositar-search-web-nginx:",
}


class ImageBuildError(ValueError):
    """Fixed error codes; no source bytes, command output or Env values."""


def _digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ImageBuildError("image_build_json_invalid")
            result[key] = value
        return result

    def constant(_):
        raise ImageBuildError("image_build_json_invalid")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ImageBuildError("image_build_json_invalid") from None


def _record(descriptor, name):
    raw = private_io.read_file(descriptor, name, mode=0o600, max_bytes=MAX_RECORD_BYTES)
    value = _json(raw)
    if private_io.canonical(value) != raw:
        raise ImageBuildError("image_build_record_noncanonical")
    return value


def _run(argv, *, timeout, environment):
    """Capture bounded pipes live; isolate and always terminate the child group."""
    process = subprocess.Popen(
        argv,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        umask=0o077,
    )
    with captured_pipes(
        process,
        timeout=timeout,
        max_bytes=MAX_OUTPUT_BYTES,
        timeout_error=lambda: ImageBuildError("image_build_command_timeout"),
        output_error=lambda: ImageBuildError("image_build_output_limit"),
    ) as (remaining, stdout, stderr):
        try:
            status = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise ImageBuildError("image_build_command_timeout") from None
        return subprocess.CompletedProcess(
            argv, status, b"".join(stdout), b"".join(stderr)
        )


class _Docker:
    def __init__(self, client_root, runner):
        self.prefix = ["docker", "--host", DOCKER_HOST, "--config", str(client_root)]
        self.environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        self.runner = _run if runner is None else runner
        self.deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
        self.calls, self.total_bytes = 0, 0

    def call(self, arguments, *, timeout):
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        if self.calls > MAX_COMMANDS or remaining <= 0:
            raise ImageBuildError("image_build_command_budget")
        try:
            result = self.runner(
                self.prefix + arguments,
                timeout=min(timeout, remaining),
                environment=dict(self.environment),
            )
            stdout, stderr = result.stdout, result.stderr
            if type(stdout) is str:
                stdout = stdout.encode("utf-8")
            if type(stderr) is str:
                stderr = stderr.encode("utf-8")
            if type(stdout) is not bytes or type(stderr) is not bytes:
                raise ImageBuildError("image_build_command_invalid")
            size = len(stdout) + len(stderr)
            self.total_bytes += size
            if size > MAX_OUTPUT_BYTES or self.total_bytes > MAX_TOTAL_OUTPUT_BYTES:
                raise ImageBuildError("image_build_output_limit")
            if time.monotonic() > self.deadline:
                raise ImageBuildError("image_build_command_timeout")
            if type(result.returncode) is not int or result.returncode != 0:
                raise ImageBuildError("image_build_command_failed")
            return stdout
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise ImageBuildError("image_build_command_failed") from None

    def inspect(self, reference):
        value = _json(
            self.call(
                ["image", "inspect", "--format", "{{json .}}", reference],
                timeout=INSPECT_TIMEOUT_SECONDS,
            )
        )
        if type(value) is not dict:
            raise ImageBuildError("image_build_inspection_invalid")
        return value


def _verify_source(root_fd, context_fd, root, plan, *, lock_fd=None):
    private_io.verify_directory(root_fd, root)
    private_io.verify_directory(context_fd, root / "context")
    if lock_fd is not None:
        _verify_lock(lock_fd, root_fd)
    _equal(_record(root_fd, "manifest.json"), plan, "image_build_manifest_changed")
    source_bundle._verify_context(context_fd, plan["files"])


def _iid(descriptor):
    before = os.stat("image.iid", dir_fd=descriptor, follow_symlinks=False)
    mode = stat.S_IMODE(before.st_mode)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or mode not in {0o600, 0o644}
        or before.st_size not in {71, 72}
    ):
        raise ImageBuildError("image_build_iid_unsafe")
    raw = private_io.read_file(descriptor, "image.iid", mode=mode, max_bytes=72)
    after = os.stat("image.iid", dir_fd=descriptor, follow_symlinks=False)
    if private_io.identity(before) != private_io.identity(after):
        raise ImageBuildError("image_build_iid_changed")
    try:
        value = raw.removesuffix(b"\n").decode("ascii")
    except UnicodeError:
        raise ImageBuildError("image_build_iid_invalid") from None
    if source_bundle.IMAGE_ID.fullmatch(value) is None:
        raise ImageBuildError("image_build_iid_invalid")
    return value


def _identity(plan, service):
    return {
        "schema_version": 1,
        "revision": plan["revision"],
        "service": service,
        "manifest_sha256": _digest(plan),
        "source_sha256": plan["services"][service]["source_sha256"],
    }


def _equal(actual, expected, error):
    try:
        same = private_io.canonical(actual) == private_io.canonical(expected)
    except (ValueError, TypeError, RecursionError, OverflowError):
        same = False
    if not same:
        raise ImageBuildError(error)


def _inventory(images_fd, plan):
    if not set(os.listdir(images_fd)) <= set(SERVICES):
        raise ImageBuildError("image_build_unknown_state")
    completed = {}
    for service in os.listdir(images_fd):
        descriptor = private_io.open_child_directory(images_fd, service)
        try:
            if set(os.listdir(descriptor)) != {
                "started.json",
                "image.iid",
                "complete.json",
            }:
                raise ImageBuildError("image_build_partial_state")
            identity = _identity(plan, service)
            _equal(
                _record(descriptor, "started.json"),
                identity,
                "image_build_started_changed",
            )
            complete = _record(descriptor, "complete.json")
            if type(complete) is not dict or set(complete) != {
                "identity",
                "tag",
                "image",
            }:
                raise ImageBuildError("image_build_receipt_invalid")
            _equal(complete["identity"], identity, "image_build_receipt_changed")
            if complete["tag"] != TAGS[service] + plan["revision"]:
                raise ImageBuildError("image_build_tag_changed")
            image = complete["image"]
            if type(image) is not dict or image.get("image_id") != _iid(descriptor):
                raise ImageBuildError("image_build_receipt_changed")
            completed[service] = complete
        finally:
            os.close(descriptor)
    return completed


def _build_arguments(root, plan, service, slot):
    recipe = plan["services"][service]
    arguments = [
        "build",
        "--pull",
        "--platform",
        "linux/amd64",
        "--network",
        "default",
        "--progress",
        "plain",
        "--file",
        str(root / "context" / recipe["dockerfile"]),
        "--tag",
        TAGS[service] + plan["revision"],
        "--iidfile",
        str(slot / "image.iid"),
    ]
    for name, value in sorted(recipe["build_arguments"].items()):
        arguments.extend(["--build-arg", name + "=" + value])
    for name, value in sorted(recipe["required_labels"].items()):
        arguments.extend(["--label", name + "=" + value])
    return [*arguments, str(root / "context")]


def _inspect_image(docker, plan, service, image_id):
    # Verify both the public source tag and the actual immutable ID. The final
    # runtime must use the immutable ID, never infer identity from the tag.
    tag = TAGS[service] + plan["revision"]
    first = source_bundle.validate_built_image(
        plan, service, image_id, docker.inspect(tag)
    )
    second = source_bundle.validate_built_image(
        plan, service, image_id, docker.inspect(image_id)
    )
    _equal(first, second, "image_build_inspection_changed")
    return second


def _verify_lock(descriptor, root_fd):
    actual = os.fstat(descriptor)
    named = os.stat("image-build.lock", dir_fd=root_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(actual.st_mode)
        or actual.st_uid != os.geteuid()
        or actual.st_nlink != 1
        or stat.S_IMODE(actual.st_mode) != 0o600
        or private_io.identity(actual) != private_io.identity(named)
    ):
        raise ImageBuildError("image_build_lock_unsafe")


def _lock(root_fd):
    descriptor = os.open(
        "image-build.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
        dir_fd=root_fd,
    )
    try:
        _verify_lock(descriptor, root_fd)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ImageBuildError("image_build_lock_busy") from None
        _verify_lock(descriptor, root_fd)
        os.fsync(root_fd)
        result, descriptor = descriptor, None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


def build_product_images(
    staged_root, expected_revision, runner=None, *, expected_manifest_sha256=None
):
    """Build/reverify the four fixed recipes from a trusted staged source root.

    No registry push, services, GPU allocation, port or Docker daemon setting is
    changed. Build RUN steps use Docker's normal temporary builder; this is not
    a sandbox for untrusted source/daemon/root. Context identity is rechecked at
    every mutation boundary. Failed/uncertain build intents require operator
    investigation or a separately staged new root, not blind retry/cleanup.

    Injected runner(argv, *, timeout, environment) must honor those bounds and
    return CompletedProcess. The default runner uses a clean per-call child Env,
    private empty client config, local Unix socket and bounded live pipe capture.

    Product adapters must supply the digest of an independently authenticated
    committed source plan. The optional default preserves the trusted-staged-
    root low-level API; a staged manifest alone is never Git/source authority.
    """
    source_bundle._revision(expected_revision)
    if expected_manifest_sha256 is not None and (
        type(expected_manifest_sha256) is not str
        or source_bundle.DIGEST.fullmatch(expected_manifest_sha256) is None
    ):
        raise ImageBuildError("image_build_expected_manifest_invalid")
    root = Path(staged_root)
    root_fd = context_fd = images_fd = lock_fd = None
    try:
        root_fd = private_io.open_directory(root)
        if not set(os.listdir(root_fd)) <= {
            "manifest.json",
            "context",
            "image-build.lock",
            "images",
        }:
            raise ImageBuildError("image_build_unknown_root_state")
        plan = source_bundle._verified_plan(_record(root_fd, "manifest.json"))
        if plan["revision"] != expected_revision:
            raise ImageBuildError("image_build_revision_changed")
        if (
            expected_manifest_sha256 is not None
            and _digest(plan) != expected_manifest_sha256
        ):
            raise ImageBuildError("image_build_expected_manifest_changed")
        context_fd = private_io.open_child_directory(root_fd, "context")
        _verify_source(root_fd, context_fd, root, plan)
        lock_fd = _lock(root_fd)
        images_fd = private_io.open_child_directory(
            root_fd, "images", create="images" not in os.listdir(root_fd)
        )
        completed = _inventory(images_fd, plan)
        result = {}
        with tempfile.TemporaryDirectory(prefix="depositar-image-client-") as temporary:
            client = Path(temporary)
            client_fd = private_io.open_directory(client)
            try:
                if os.listdir(client_fd):
                    raise ImageBuildError("image_build_client_config_not_empty")
                docker = _Docker(client, runner)
                if (
                    _json(
                        docker.call(
                            ["info", "--format", "{{json .OSType}}"],
                            timeout=INSPECT_TIMEOUT_SECONDS,
                        )
                    )
                    != "linux"
                ):
                    raise ImageBuildError("image_build_daemon_unsupported")
                for service in SERVICES:
                    _verify_source(root_fd, context_fd, root, plan, lock_fd=lock_fd)
                    private_io.verify_directory(images_fd, root / "images")
                    fresh = service not in completed
                    slot = root / "images" / service
                    descriptor = private_io.open_child_directory(
                        images_fd, service, create=fresh
                    )
                    try:
                        identity = _identity(plan, service)
                        if fresh:
                            private_io.write_json(
                                descriptor,
                                "started.json",
                                identity,
                                max_bytes=MAX_RECORD_BYTES,
                            )
                            private_io.verify_directory(descriptor, slot)
                            _verify_source(
                                root_fd, context_fd, root, plan, lock_fd=lock_fd
                            )
                            docker.call(
                                _build_arguments(root, plan, service, slot),
                                timeout=BUILD_TIMEOUT_SECONDS,
                            )
                        private_io.verify_directory(descriptor, slot)
                        image_id = _iid(descriptor)
                        image = _inspect_image(docker, plan, service, image_id)
                        private_io.verify_directory(descriptor, slot)
                        _verify_source(root_fd, context_fd, root, plan, lock_fd=lock_fd)
                        record = {
                            "identity": identity,
                            "tag": TAGS[service] + plan["revision"],
                            "image": image,
                        }
                        if fresh:
                            private_io.write_json(
                                descriptor,
                                "complete.json",
                                record,
                                max_bytes=MAX_RECORD_BYTES,
                            )
                        else:
                            _equal(
                                completed[service],
                                record,
                                "image_build_completed_image_changed",
                            )
                        result[service] = image
                    finally:
                        os.close(descriptor)
                _verify_source(root_fd, context_fd, root, plan, lock_fd=lock_fd)
                private_io.verify_directory(images_fd, root / "images")
                final = _inventory(images_fd, plan)
                if set(final) != set(SERVICES):
                    raise ImageBuildError("image_build_inventory_incomplete")
                for service in SERVICES:
                    _equal(
                        final[service],
                        {
                            "identity": _identity(plan, service),
                            "tag": TAGS[service] + plan["revision"],
                            "image": result[service],
                        },
                        "image_build_final_receipt_changed",
                    )
                return {
                    "schema_version": 1,
                    "revision": expected_revision,
                    "images": result,
                    "services_started": False,
                    "registry_published": False,
                }
            finally:
                os.close(client_fd)
    except (OSError, ValueError) as error:
        if isinstance(error, ImageBuildError):
            raise
        raise ImageBuildError("image_build_state_unsafe") from None
    finally:
        for descriptor in (images_fd, lock_fd, context_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)
