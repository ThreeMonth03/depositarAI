"""Open and remove only validated private trace-owned files."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import stat

from search.operations.shared import private_io
from search.serialization import strict_json


SESSION_ID = re.compile(r"[0-9a-f]{32}")
EVENT_NAME = re.compile(r"turn-([0-9]{3})\.json")
MANIFEST_BYTES = 65536


def session_id(value):
    if type(value) is not str or SESSION_ID.fullmatch(value) is None:
        raise ValueError("trace_session_id_invalid")
    return value


def names(fd, maximum):
    result = []
    with os.scandir(fd) as entries:
        for entry in entries:
            if len(result) >= maximum:
                raise ValueError("trace_directory_limit")
            result.append(entry.name)
    return result


def regular(info, maximum):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or not 1 <= info.st_size <= maximum
    ):
        raise ValueError("trace_file_unsafe")


@contextmanager
def locked_root(path, maximum):
    root = private_io.open_directory(Path(path), create=True)
    lock = None
    try:
        lock = os.open(
            ".trace.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=root,
        )
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("trace_lock_unsafe")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        private_io.verify_directory(root, path)
        for name in names(root, maximum + 1):
            if name != ".trace.lock":
                session_id(name)
        yield root
        private_io.verify_directory(root, path)
    finally:
        if lock is not None:
            os.close(lock)
        os.close(root)


@contextmanager
def child(root, identifier, *, create=False):
    fd = private_io.open_child_directory(
        root, session_id(identifier), create=create
    )
    try:
        yield fd
    finally:
        os.close(fd)


def read_manifest(fd, identifier, maximum):
    raw = private_io.read_file(
        fd, "manifest.json", mode=0o600, max_bytes=MANIFEST_BYTES
    )
    value = strict_json(raw)
    required = {
        "trace_version",
        "test_session_id",
        "created_at",
        "expires_at",
        "turns",
        "gaps",
        "sealed",
        "writer_id",
    }
    if type(value) is not dict or set(value) != required:
        raise ValueError("trace_manifest_invalid")
    if (
        type(value["trace_version"]) is not int
        or value["trace_version"] != 1
        or type(value["writer_id"]) is not str
        or SESSION_ID.fullmatch(value["writer_id"]) is None
        or type(value["sealed"]) is not bool
        or value["test_session_id"] != identifier
        or type(value["created_at"]) not in {int, float}
        or type(value["expires_at"]) not in {int, float}
        or not 0 <= value["created_at"] < value["expires_at"] < 1e12
    ):
        raise ValueError("trace_manifest_invalid")
    _turns(value["turns"], maximum)
    _gaps(value["gaps"])
    return value


def _turns(value, maximum):
    if (
        type(value) is not list
        or len(value) > maximum
        or any(type(turn) is not int or not 1 <= turn <= 100 for turn in value)
        or value != sorted(set(value))
    ):
        raise ValueError("trace_manifest_turns_invalid")


def _gaps(value):
    if type(value) is not list or len(value) > 100:
        raise ValueError("trace_manifest_gaps_invalid")
    for gap in value:
        if (
            type(gap) is not dict
            or set(gap) != {"turn", "status"}
            or type(gap["turn"]) is not int
            or not 1 <= gap["turn"] <= 100
            or gap["status"]
            not in {
                "oversize",
                "queue_full",
                "session_limit",
                "total_limit",
                "unavailable",
                "incomplete",
                "shutdown",
                "projection_invalid",
            }
        ):
            raise ValueError("trace_manifest_gap_invalid")


def inventory(fd, maximum, event_bytes):
    result = {}
    for name in names(fd, maximum + 2):
        match = EVENT_NAME.fullmatch(name)
        if name not in {"manifest.json", ".unsealed.json"} and match is None:
            raise ValueError("trace_directory_unowned")
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        regular(info, MANIFEST_BYTES if match is None else event_bytes)
        result[name] = info
    if "manifest.json" not in result:
        raise ValueError("trace_manifest_missing")
    return result


def remove_session(root, identifier, files, expected_directory):
    """Remove inode-pinned files after all content has been validated."""
    with child(root, identifier) as fd:
        if _directory_identity(os.fstat(fd)) != expected_directory:
            raise ValueError("trace_directory_changed")
        for name, expected in files.items():
            actual = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if private_io.identity(actual) != private_io.identity(expected):
                raise ValueError("trace_file_changed")
        for name, expected in files.items():
            actual = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if private_io.identity(actual) != private_io.identity(expected):
                raise ValueError("trace_file_changed")
            os.unlink(name, dir_fd=fd)
        os.fsync(fd)
        named = os.stat(identifier, dir_fd=root, follow_symlinks=False)
        if _directory_identity(named) != expected_directory:
            raise ValueError("trace_directory_changed")
        os.rmdir(identifier, dir_fd=root)
        os.fsync(root)


def _directory_identity(info):
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode
