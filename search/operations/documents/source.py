"""Acquire bounded public CSV/PDF files through curl and verified private cache."""

import hashlib
import http.client
import io
import math
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

from search.operations.documents import cache
from search.operations.shared.bounded_process import captured_pipes


MAX_FILE_BYTES = cache.MAX_FILE_BYTES
MAX_HEADER_BYTES = 65536
DEADLINE_SECONDS = 25
CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_CAPTURE_BYTES = MAX_FILE_BYTES + MAX_HEADER_BYTES + 4096


class SourceError(ValueError):
    """A fixed acquisition diagnostic suitable for an operator receipt."""

    @property
    def code(self):
        return str(self)


def source_identity(resource):
    """Bind reuse to this resource's declared source, not a whole catalog."""
    if type(resource) is not dict:
        raise SourceError("document_source_metadata_invalid")
    url = resource.get("url")
    if type(url) is not str or not 1 <= len(url) <= 2048:
        raise SourceError("document_source_url_unsupported")
    try:
        parts = urlsplit(url)
        allowed = (
            parts.scheme == "https"
            and parts.hostname == "data.depositar.io"
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
            and parts.path.startswith("/")
            and not any(
                ord(character) <= 32 or ord(character) == 127 for character in url
            )
        )
    except ValueError:
        allowed = False
    try:
        url_bytes = url.encode("utf-8")
    except UnicodeError:
        allowed = False
    if not allowed:
        raise SourceError("document_source_url_unsupported")
    identifier, modified, declared_format = (
        resource.get("id"),
        resource.get("last_modified"),
        resource.get("format"),
    )
    if (
        type(identifier) is not str
        or not 1 <= len(identifier) <= 128
        or any(ord(character) < 32 for character in identifier)
        or type(declared_format) is not str
        or len(declared_format) > 128
        or declared_format.strip().upper() not in {"CSV", "PDF"}
        or modified is not None
        and (type(modified) is not str or len(modified) > 128)
    ):
        raise SourceError("document_source_metadata_invalid")
    try:
        identifier.encode("utf-8")
        if modified is not None:
            modified.encode("utf-8")
    except UnicodeError:
        raise SourceError("document_source_metadata_invalid") from None
    if modified is not None and any(ord(character) < 32 for character in modified):
        raise SourceError("document_source_metadata_invalid")
    size = resource.get("size")
    if size is not None and (
        type(size) not in (int, float)
        or type(size) is float
        and not math.isfinite(size)
        or size < 0
    ):
        raise SourceError("document_source_metadata_invalid")
    return {
        "resource_id": identifier,
        "source_url_sha256": hashlib.sha256(url_bytes).hexdigest(),
        "format": declared_format.strip().upper(),
        "declared_size": size,
        "declared_last_modified": modified,
    }


def _run(command):
    try:
        process = subprocess.Popen(
            command,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            umask=0o077,
        )
    except OSError:
        raise SourceError("document_source_tool_unavailable") from None
    with captured_pipes(
        process,
        timeout=DEADLINE_SECONDS,
        max_bytes=MAX_CAPTURE_BYTES,
        timeout_error=lambda: SourceError("document_source_timeout"),
        output_error=lambda: SourceError("document_source_too_large"),
    ) as (remaining, stdout, _stderr):
        try:
            status = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise SourceError("document_source_timeout") from None
        return status, b"".join(stdout)


def _header_value(headers, name, limit):
    values = headers.get_all(name, [])
    if not values:
        return None
    if len(values) != 1:
        raise SourceError("document_source_headers_invalid")
    value = values[0].strip()
    if len(value) > limit or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise SourceError("document_source_headers_invalid")
    return value


def _response(raw):
    consumed = 0
    for _ in range(5):
        boundary = raw.find(b"\r\n\r\n")
        if boundary < 0 or consumed + boundary + 4 > MAX_HEADER_BYTES:
            raise SourceError("document_source_headers_invalid")
        status_line, _, header_bytes = raw[:boundary].partition(b"\r\n")
        status_match = re.fullmatch(
            rb"HTTP/(?:1\.[01]|2|3) ([0-9]{3})(?: [^\r\n]*)?", status_line
        )
        if status_match is None:
            raise SourceError("document_source_headers_invalid")
        code = int(status_match[1])
        consumed += boundary + 4
        raw = raw[boundary + 4 :]
        if 100 <= code < 200:
            continue
        if code != 200:
            raise SourceError("document_source_http_failed")
        try:
            headers = http.client.parse_headers(io.BytesIO(header_bytes + b"\r\n\r\n"))
        except (http.client.HTTPException, ValueError):
            raise SourceError("document_source_headers_invalid") from None
        length = _header_value(headers, "Content-Length", 20)
        encoding = _header_value(headers, "Content-Encoding", 32)
        transfer = _header_value(headers, "Transfer-Encoding", 32)
        if (
            encoding not in (None, "identity")
            or transfer not in (None, "chunked")
            or transfer
            and length
        ):
            raise SourceError("document_source_headers_invalid")
        if length is not None:
            if not length.isdecimal():
                raise SourceError("document_source_headers_invalid")
            if int(length) > MAX_FILE_BYTES:
                raise SourceError("document_source_too_large")
            if int(length) != len(raw):
                raise SourceError("document_source_incomplete")
        if len(raw) > MAX_FILE_BYTES:
            raise SourceError("document_source_too_large")
        if not raw:
            raise SourceError("document_source_empty")
        return raw, {
            "content_type": _header_value(headers, "Content-Type", 256) or "",
            "etag": _header_value(headers, "ETag", 512),
            "last_modified": _header_value(headers, "Last-Modified", 128),
        }
    raise SourceError("document_source_headers_invalid")


def fetch(resource):
    """Fetch one complete allowed resource, returning bytes and safe headers."""
    identity = source_identity(resource)
    if (
        identity["declared_size"] is not None
        and identity["declared_size"] > MAX_FILE_BYTES
    ):
        raise SourceError("document_source_too_large")
    status, raw = _run(
        [
            "curl",
            "-q",
            "--silent",
            "--show-error",
            "--fail",
            "--include",
            "--proxy",
            "",
            "--noproxy",
            "*",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--max-redirs",
            "0",
            "--connect-timeout",
            "5",
            "--max-time",
            str(DEADLINE_SECONDS),
            "--max-filesize",
            str(MAX_FILE_BYTES),
            "--header",
            "Accept-Encoding: identity",
            "--user-agent",
            "depositar-resource-profile/1",
            resource["url"],
        ]
    )
    if status == 63:
        raise SourceError("document_source_too_large")
    if status == 28:
        raise SourceError("document_source_timeout")
    if status == 22:
        raise SourceError("document_source_http_failed")
    if status:
        raise SourceError("document_source_fetch_failed")
    return _response(raw)


def acquire(cache_root, resource, *, refresh=False, now=None):
    """Reuse a verified fresh body, or acquire and publish one complete file.

    ``acquired_at`` is the original UTC epoch second on a cache hit. TTL reuse
    does not claim that the source was checked again. Refresh replaces only the
    source receipt; earlier content-addressed bodies remain available.
    """
    if type(refresh) is not bool:
        raise SourceError("document_source_options_invalid")
    now = int(time.time()) if now is None else now
    if type(now) is not int or now < 0:
        raise SourceError("document_source_clock_invalid")
    root = Path(cache_root)
    identity = source_identity(resource)
    key = cache.key_for(identity)
    try:
        with cache.locked(root) as descriptor:
            record = cache.load(descriptor, key, identity)
            if record is not None and record["acquired_at"] > now:
                raise SourceError("document_cache_clock_invalid")
            hit = (
                record is not None
                and not refresh
                and now - record["acquired_at"] < CACHE_TTL_SECONDS
            )
            if not hit:
                body, response = fetch(resource)
                record = cache.store(descriptor, key, identity, body, response, now)
            return {
                "local_path": root / cache.body_name(record["sha256"]),
                "sha256": record["sha256"],
                "bytes": record["bytes"],
                "acquired_at": record["acquired_at"],
                "cache_hit": hit,
                "source_url_sha256": identity["source_url_sha256"],
                "cache_key": key,
                "content_type": record["content_type"],
                "etag": record["etag"],
                "last_modified": record["last_modified"],
            }
    except cache.CacheError as error:
        raise SourceError(str(error)) from None
