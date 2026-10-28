"""Run bounded, offline Fess extraction without starting its application."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid

from search.operations.shared import private_io
from search.operations.shared.bounded_process import captured_pipes
from search.serialization import strict_json


EXTRACTOR_SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 16 * 1024**2
MAX_REPORT_BYTES = 512 * 1024
MAX_TEXT_CHARS = 65536
MAX_COLUMNS = 256
MAX_COLUMN_CHARS = 512
MAX_COLUMN_TOTAL_CHARS = 8192
MAX_ROWS_OBSERVED = 10000
FESS_IMAGE = (
    "ghcr.io/codelibs/fess:15.8.0@sha256:"
    "442a58831e87aae6ae22ef60e0536ececbda553bd5f5e8e5dc98cc022578dc75"
)
SUPPORTED_ENCODINGS = frozenset({"utf-8", "utf-8-sig", "utf-16", "cp950"})
HEADER_POLICIES = frozenset({"first_record", "absent"})
_SOURCE_FILES = ("Dockerfile", "Dockerfile.dockerignore", "Main.java", "log4j2.xml")
_SOURCE_DIRECTORY = Path("deploy/docker/extractor")
_SOURCE_LABEL = "org.depositarai.extractor.source-sha256"
_BASE_LABEL = "org.depositarai.extractor.base"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MIME = re.compile(r"[a-z0-9][a-z0-9.+_-]*/[a-z0-9][a-z0-9.+_-]*\Z")
_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "detected_mime",
        "input_sha256",
        "columns",
        "text",
        "text_truncated",
        "rows_observed",
        "rows_complete",
        "may_require_ocr",
    }
)


class ExtractorError(ValueError):
    """A fixed diagnostic without resource bytes or Docker output."""


def _string(value, maximum):
    if type(value) is not str or len(value) > maximum or "\x00" in value:
        raise ExtractorError("document_extractor_report_invalid")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise ExtractorError("document_extractor_report_invalid") from None


def validate_report(value, *, expected_format=None, header_policy=None):
    """Validate the closed output contract, including empty-content semantics."""
    error = "document_extractor_report_invalid"
    if (
        expected_format is not None
        and (type(expected_format) is not str or expected_format not in {"CSV", "PDF"})
    ) or (
        header_policy is not None
        and (type(header_policy) is not str or header_policy not in HEADER_POLICIES)
    ):
        raise ExtractorError(error)
    if type(value) is not dict or set(value) != _REPORT_KEYS:
        raise ExtractorError(error)
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != EXTRACTOR_SCHEMA_VERSION
        or type(value["status"]) is not str
        or value["status"]
        not in {"ok", "empty_text", "unsupported_format", "parse_error"}
    ):
        raise ExtractorError(error)
    mime = value["detected_mime"]
    _string(mime, 128)
    if mime and _MIME.fullmatch(mime) is None:
        raise ExtractorError(error)
    if type(value["input_sha256"]) is not str or not _HEX.fullmatch(
        value["input_sha256"]
    ):
        raise ExtractorError(error)
    columns = value["columns"]
    if type(columns) is not list or len(columns) > MAX_COLUMNS:
        raise ExtractorError(error)
    for column in columns:
        _string(column, MAX_COLUMN_CHARS)
    if sum(map(len, columns)) > MAX_COLUMN_TOTAL_CHARS:
        raise ExtractorError(error)
    _string(value["text"], MAX_TEXT_CHARS)
    if (
        type(value["text_truncated"]) is not bool
        or type(value["may_require_ocr"]) is not bool
        or value["rows_complete"] is not False
    ):
        raise ExtractorError(error)
    rows = value["rows_observed"]
    if rows is not None and (
        type(rows) is not int or not 0 <= rows <= MAX_ROWS_OBSERVED
    ):
        raise ExtractorError(error)
    status = value["status"]
    if status in {"unsupported_format", "parse_error"}:
        if (
            columns
            or value["text"]
            or value["text_truncated"]
            or rows is not None
            or value["may_require_ocr"]
        ):
            raise ExtractorError(error)
    else:
        if mime not in {"text/csv", "text/plain", "application/pdf"}:
            raise ExtractorError(error)
        usable = bool(value["text"].strip() or any(c.strip() for c in columns))
        if (status == "ok") != usable:
            raise ExtractorError(error)
        if status == "empty_text" and columns:
            raise ExtractorError(error)
        if mime == "application/pdf" and (columns or rows is not None):
            raise ExtractorError(error)
    if value["may_require_ocr"] != (
        status == "empty_text" and mime == "application/pdf"
    ):
        raise ExtractorError(error)
    if header_policy == "absent" and columns:
        raise ExtractorError(error)
    if expected_format is not None and status in {"ok", "empty_text"}:
        allowed = (
            {"text/csv", "text/plain"}
            if expected_format == "CSV"
            else {"application/pdf"}
        )
        if mime not in allowed:
            raise ExtractorError(error)
    return {**value, "columns": list(columns)}


def _run(argv, *, timeout, max_bytes, environment):
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=environment,
        umask=0o077,
    )
    with captured_pipes(
        process,
        timeout=timeout,
        max_bytes=max_bytes,
        timeout_error=lambda: ExtractorError("document_extractor_command_timeout"),
        output_error=lambda: ExtractorError("document_extractor_output_limit"),
    ) as (remaining, stdout, stderr):
        try:
            status = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise ExtractorError("document_extractor_command_timeout") from None
        return subprocess.CompletedProcess(
            argv, status, b"".join(stdout), b"".join(stderr)
        )


def _call(argv, *, timeout, runner, max_bytes=MAX_REPORT_BYTES):
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    try:
        result = (
            _run(argv, timeout=timeout, max_bytes=max_bytes, environment=environment)
            if runner is None
            else runner(argv, timeout=timeout, environment=environment)
        )
        if (
            type(result.returncode) is not int
            or type(result.stdout) is not bytes
            or type(result.stderr) is not bytes
        ):
            raise ExtractorError("document_extractor_command_failed")
        if len(result.stdout) + len(result.stderr) > max_bytes:
            raise ExtractorError("document_extractor_output_limit")
        return result
    except ExtractorError:
        raise
    except subprocess.TimeoutExpired:
        raise ExtractorError("document_extractor_command_timeout") from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ExtractorError("document_extractor_command_failed") from None


@contextmanager
def _client():
    with tempfile.TemporaryDirectory(prefix="depositarai-extractor-client-") as name:
        config = Path(name)
        (config / "config.json").write_text('{"auths":{}}\n')
        yield ["docker", "--host", "unix:///var/run/docker.sock", "--config", name]


def _inspect(prefix, reference, runner):
    result = _call(
        prefix + ["image", "inspect", "--format", "{{json .}}", reference],
        timeout=15,
        runner=runner,
    )
    if result.returncode:
        return None
    try:
        value = strict_json(result.stdout)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ExtractorError("document_extractor_image_invalid") from None
    if type(value) is not dict:
        raise ExtractorError("document_extractor_image_invalid")
    return value


def _image_identity(value, source_sha256):
    if type(value) is not dict or type(value.get("Config")) is not dict:
        raise ExtractorError("document_extractor_image_invalid")
    labels = value["Config"].get("Labels")
    image_id = value.get("Id")
    if (
        type(image_id) is not str
        or not _IMAGE_ID.fullmatch(image_id)
        or type(labels) is not dict
        or labels.get(_SOURCE_LABEL) != source_sha256
        or labels.get(_BASE_LABEL) != FESS_IMAGE
    ):
        raise ExtractorError("document_extractor_image_invalid")
    return {
        "image_id": image_id,
        "source_sha256": source_sha256,
        "base_image": FESS_IMAGE,
    }


def _source_files(source_root):
    directory = Path(source_root).absolute() / _SOURCE_DIRECTORY
    if directory.resolve() != directory:
        raise ExtractorError("document_extractor_source_invalid")
    descriptor = None
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        return {
            name: private_io.read_file(
                descriptor, name, mode=0o644, max_bytes=128 * 1024
            )
            for name in _SOURCE_FILES
        }
    except (OSError, ValueError):
        raise ExtractorError("document_extractor_source_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def build_image(source_root, *, runner=None):
    """Build or reuse the immutable helper identified by its exact source bytes."""
    files = _source_files(source_root)
    source_sha256 = hashlib.sha256(
        private_io.canonical(
            {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
        )
    ).hexdigest()
    tag = "depositarai-resource-extractor:" + source_sha256
    with _client() as prefix:
        existing = _inspect(prefix, tag, runner)
        if existing is not None:
            return _image_identity(existing, source_sha256)
        with tempfile.TemporaryDirectory(prefix="depositarai-extractor-build-") as name:
            context = Path(name)
            directory = context / _SOURCE_DIRECTORY
            directory.mkdir(parents=True)
            for filename, raw in files.items():
                (directory / filename).write_bytes(raw)
            result = _call(
                prefix
                + [
                    "build",
                    "--network",
                    "none",
                    "--progress",
                    "plain",
                    "--label",
                    _SOURCE_LABEL + "=" + source_sha256,
                    "--label",
                    _BASE_LABEL + "=" + FESS_IMAGE,
                    "--tag",
                    tag,
                    "--file",
                    str(directory / "Dockerfile"),
                    str(context),
                ],
                timeout=900,
                runner=runner,
                max_bytes=4 * 1024**2,
            )
            if result.returncode:
                raise ExtractorError("document_extractor_build_failed")
        return _image_identity(_inspect(prefix, tag, runner), source_sha256)


def _input_bytes(path):
    path = Path(path).absolute()
    descriptor = None
    try:
        if path.resolve() != path:
            raise ExtractorError("document_extractor_input_invalid")
        mode = stat.S_IMODE(path.lstat().st_mode)
        if mode not in {0o400, 0o600}:
            raise ExtractorError("document_extractor_input_invalid")
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        return private_io.read_file(
            descriptor, path.name, mode=mode, max_bytes=MAX_INPUT_BYTES
        )
    except (OSError, ValueError):
        raise ExtractorError("document_extractor_input_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _cleanup(prefix, name, runner):
    result = _call(prefix + ["rm", "--force", name], timeout=10, runner=runner)
    if result.returncode and not any(
        marker in result.stderr for marker in (b"No such container", b"No such object")
    ):
        raise ExtractorError("document_extractor_cleanup_failed")


def extract(
    path,
    expected_format,
    encoding="utf-8",
    header_policy="first_record",
    *,
    image,
    runner=None,
):
    """Snapshot one private file, extract it offline, and verify its bound output."""
    if (
        type(expected_format) is not str
        or expected_format not in {"CSV", "PDF"}
        or type(encoding) is not str
        or encoding not in SUPPORTED_ENCODINGS
        or type(header_policy) is not str
        or header_policy not in HEADER_POLICIES
    ):
        raise ExtractorError("document_extractor_settings_invalid")
    if (
        type(image) is not dict
        or type(image.get("source_sha256")) is not str
        or not _HEX.fullmatch(image["source_sha256"])
        or type(image.get("image_id")) is not str
        or not _IMAGE_ID.fullmatch(image["image_id"])
    ):
        raise ExtractorError("document_extractor_image_invalid")
    raw = _input_bytes(path)
    digest = hashlib.sha256(raw).hexdigest()
    with _client() as prefix:
        identity = _image_identity(
            _inspect(prefix, image["image_id"], runner), image["source_sha256"]
        )
        if identity != image:
            raise ExtractorError("document_extractor_image_invalid")
        with tempfile.TemporaryDirectory(prefix="depositarai-extractor-input-") as name:
            snapshot = Path(name) / ("resource." + expected_format.lower())
            snapshot.write_bytes(raw)
            snapshot.chmod(0o600)
            container = "depositarai-extract-" + uuid.uuid4().hex
            command = prefix + [
                "run",
                "--pull",
                "never",
                "--rm",
                "--name",
                container,
                "--runtime",
                "runc",
                "--network",
                "none",
                "--read-only",
                "--hostname",
                "resource-extract",
                "--add-host",
                "resource-extract:127.0.0.1",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--cpus",
                "2",
                "--memory",
                "2g",
                "--pids-limit",
                "96",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
                "--env",
                "CUDA_VISIBLE_DEVICES=-1",
                "--env",
                "NVIDIA_VISIBLE_DEVICES=void",
                "--mount",
                f"type=bind,src={snapshot},dst=/input/{snapshot.name},readonly",
                image["image_id"],
                expected_format,
                "/input/" + snapshot.name,
                encoding,
                header_policy,
            ]
            try:
                result = _call(command, timeout=30, runner=runner)
                if result.returncode:
                    raise ExtractorError("document_extractor_command_failed")
                try:
                    report = validate_report(
                        strict_json(result.stdout),
                        expected_format=expected_format,
                        header_policy=header_policy,
                    )
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    raise ExtractorError("document_extractor_report_invalid") from None
                if report["input_sha256"] != digest:
                    raise ExtractorError("document_extractor_input_changed")
                return report
            finally:
                _cleanup(prefix, container, runner)
