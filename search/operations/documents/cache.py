"""Keep verified resource bodies and acquisition receipts in private
storage.
"""

from contextlib import ExitStack, contextmanager
from functools import partial
import fcntl
import hashlib
import os
import re
import stat

from search.operations.shared import private_io
from search.serialization import strict_json


MAX_FILE_BYTES = 16 * 1024**2
MAX_RECORD_BYTES = 8192
DIGEST = re.compile(r"[0-9a-f]{64}")
RECORD_KEYS = {
    "schema_version",
    "cache_key",
    "source",
    "sha256",
    "bytes",
    "acquired_at",
    "content_type",
    "etag",
    "last_modified",
}


class CacheError(ValueError):
    """A resource cache invariant failed without exposing private paths."""


def key_for(identity):
    return hashlib.sha256(private_io.canonical(identity)).hexdigest()


def _verify_lock(descriptor, held, expected):
    try:
        current = os.fstat(held)
        named = os.stat(".lock", dir_fd=descriptor, follow_symlinks=False)
    except OSError:
        raise CacheError("document_cache_lock_invalid") from None
    if private_io.identity(expected) != private_io.identity(
        current
    ) or private_io.identity(current) != private_io.identity(named):
        raise CacheError("document_cache_lock_invalid")


@contextmanager
def _cache_lock(descriptor, read_only):
    flags = os.O_RDONLY if read_only else os.O_RDWR | os.O_CREAT
    lock = os.open(
        ".lock",
        flags | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
        dir_fd=descriptor,
    )
    try:
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise CacheError("document_cache_lock_invalid")
        try:
            mode = fcntl.LOCK_SH if read_only else fcntl.LOCK_EX
            fcntl.flock(lock, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CacheError("document_cache_busy") from None
        verify = partial(_verify_lock, descriptor, lock, info)
        verify()
        try:
            yield verify
        finally:
            verify()
    finally:
        os.close(lock)


@contextmanager
def locked(root, *, read_only=False, on_lock=None):
    """Lock acquisition or read frozen cache without writes.

    ``on_lock`` receives a verifier bound to the actual held lock FD. It is
    valid only inside this context and must be checked before publication.
    """
    if type(read_only) is not bool or (
        on_lock is not None and not callable(on_lock)
    ):
        raise CacheError("document_cache_lock_invalid")
    with ExitStack() as stack:
        try:
            descriptor = private_io.open_directory(root, create=not read_only)
            stack.callback(os.close, descriptor)
            verify = stack.enter_context(_cache_lock(descriptor, read_only))
            private_io.verify_directory(descriptor, root)
            if on_lock is not None:
                on_lock(verify)
        except CacheError:
            raise
        except (OSError, ValueError):
            raise CacheError("document_cache_io_failed") from None
        try:
            yield descriptor
        finally:
            try:
                private_io.verify_directory(descriptor, root)
            except (OSError, ValueError):
                raise CacheError("document_cache_io_failed") from None


def body_name(digest):
    if type(digest) is not str or DIGEST.fullmatch(digest) is None:
        raise CacheError("document_cache_record_invalid")
    return "sha256-" + digest + ".bin"


def _record(descriptor, key, identity):
    try:
        raw = private_io.read_file(
            descriptor,
            "resource-" + key + ".json",
            mode=0o600,
            max_bytes=MAX_RECORD_BYTES,
        )
    except FileNotFoundError:
        return None
    try:
        record = strict_json(raw)
        if (
            type(record) is not dict
            or set(record) != RECORD_KEYS
            or private_io.canonical(record) != raw
            or type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or record["cache_key"] != key
            or private_io.canonical(record["source"])
            != private_io.canonical(identity)
            or type(record["bytes"]) is not int
            or not 1 <= record["bytes"] <= MAX_FILE_BYTES
            or type(record["acquired_at"]) is not int
            or record["acquired_at"] < 0
        ):
            raise ValueError
        body_name(record["sha256"])
        for name, limit in (
            ("content_type", 256),
            ("etag", 512),
            ("last_modified", 128),
        ):
            value = record[name]
            if value is None and name != "content_type":
                continue
            if (
                type(value) is not str
                or len(value) > limit
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in value
                )
            ):
                raise ValueError
        return record
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise CacheError("document_cache_record_invalid") from None


def load(descriptor, key, identity, *, max_body_bytes=MAX_FILE_BYTES):
    """Return a receipt only after verifying the full cached body."""
    if (
        type(max_body_bytes) is not int
        or not 1 <= max_body_bytes <= MAX_FILE_BYTES
    ):
        raise CacheError("document_cache_body_limit")
    try:
        record = _record(descriptor, key, identity)
        if record is None:
            return None
        if record["bytes"] > max_body_bytes:
            raise CacheError("document_cache_body_limit")
        raw = private_io.read_file(
            descriptor,
            body_name(record["sha256"]),
            mode=0o600,
            max_bytes=max_body_bytes,
        )
        if (
            len(raw) != record["bytes"]
            or hashlib.sha256(raw).hexdigest() != record["sha256"]
        ):
            raise CacheError("document_cache_body_invalid")
        return record
    except CacheError:
        raise
    except (OSError, ValueError):
        raise CacheError("document_cache_body_invalid") from None


def store(descriptor, key, identity, body, response, acquired_at):
    """Publish a content-addressed body before replacing its source receipt."""
    if type(body) is not bytes or not 1 <= len(body) <= MAX_FILE_BYTES:
        raise CacheError("document_cache_body_invalid")
    digest = hashlib.sha256(body).hexdigest()
    name = body_name(digest)
    try:
        try:
            existing = private_io.read_file(
                descriptor, name, mode=0o600, max_bytes=MAX_FILE_BYTES
            )
        except FileNotFoundError:
            private_io.write_file(
                descriptor, name, body, max_bytes=MAX_FILE_BYTES
            )
        else:
            if existing != body:
                raise CacheError("document_cache_body_invalid")
        record = {
            "schema_version": 1,
            "cache_key": key,
            "source": identity,
            "sha256": digest,
            "bytes": len(body),
            "acquired_at": acquired_at,
            "content_type": response["content_type"],
            "etag": response["etag"],
            "last_modified": response["last_modified"],
        }
        private_io.write_json(
            descriptor,
            "resource-" + key + ".json",
            record,
            replace=True,
            max_bytes=MAX_RECORD_BYTES,
        )
        return record
    except CacheError:
        raise
    except (OSError, ValueError, KeyError):
        raise CacheError("document_cache_io_failed") from None
