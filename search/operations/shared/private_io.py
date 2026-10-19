"""Shared Linux private-state IO for cache, lifecycle receipts and catalog plans.

File modes are caller policy. All paths are opened without following links;
regular-file reads are bounded/nonblocking, and publication fsyncs the directory.
This module does not interpret receipts, delete data or operate Docker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import uuid


def identity(info):
    return tuple(
        getattr(info, key)
        for key in (
            "st_dev",
            "st_ino",
            "st_uid",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


def canonical(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _name(name):
    if (
        type(name) is not str
        or name in {"", ".", ".."}
        or "/" in name
        or "\x00" in name
    ):
        raise ValueError("private_io_name_invalid")


def read_file(parent_fd, name, *, mode, max_bytes):
    """Read one operator-owned, single-link regular file and detect replacement."""
    _name(name)
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("private_io_byte_limit_invalid")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != mode
        ):
            raise ValueError("private_io_file_unsafe")
        if not 1 <= before.st_size <= max_bytes:
            raise ValueError("private_io_file_size_invalid")
        chunks, size = [], 0
        while size <= max_bytes:
            part = os.read(fd, min(65536, max_bytes + 1 - size))
            if not part:
                break
            chunks.append(part)
            size += len(part)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            size != before.st_size
            or identity(before) != identity(after)
            or identity(after) != identity(named)
        ):
            raise ValueError("private_io_file_changed")
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_json(parent_fd, name, value, *, replace=False, max_bytes):
    """Publish a private JSON record durably; refuse overwrite unless explicit."""
    _name(name)
    write_file(parent_fd, name, canonical(value), replace=replace, max_bytes=max_bytes)


def write_file(parent_fd, name, encoded, *, replace=False, max_bytes):
    """Publish bounded raw bytes with the same private, durable no-clobber policy."""
    _name(name)
    if (
        type(encoded) is not bytes
        or type(max_bytes) is not int
        or not 1 <= len(encoded) <= max_bytes
    ):
        raise ValueError("private_io_record_size_invalid")
    if replace:
        try:
            read_file(parent_fd, name, mode=0o600, max_bytes=max_bytes)
        except FileNotFoundError:
            pass
    temporary = ".record-" + uuid.uuid4().hex
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        else:
            os.link(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def open_directory(path, *, mode=0o700, create=False):
    """Walk using stable directory descriptors, creating at most one leaf."""
    path = Path(path)
    if (
        not path.is_absolute()
        or path == Path("/")
        or ".." in path.parts
        or path.resolve() != path
    ):
        raise ValueError("private_io_directory_path_unsafe")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, component in enumerate(path.parts[1:]):
            if create and index == len(path.parts) - 2:
                try:
                    os.mkdir(component, mode=mode, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        actual, named = os.fstat(fd), path.lstat()
        if (
            not stat.S_ISDIR(actual.st_mode)
            or actual.st_uid != os.geteuid()
            or stat.S_IMODE(actual.st_mode) != mode
        ):
            raise ValueError("private_io_directory_unsafe")
        if any(
            getattr(actual, field) != getattr(named, field)
            for field in ("st_dev", "st_ino", "st_uid", "st_mode")
        ):
            raise ValueError("private_io_directory_changed")
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def open_child_directory(parent_fd, name, *, mode=0o700, create=False):
    """Open a private child using an already trusted, retained parent FD."""
    _name(name)
    if create:
        os.mkdir(name, mode=mode, dir_fd=parent_fd)
        os.fsync(parent_fd)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        actual = os.fstat(fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            actual.st_uid != os.geteuid()
            or stat.S_IMODE(actual.st_mode) != mode
            or any(
                getattr(actual, field) != getattr(named, field)
                for field in ("st_dev", "st_ino", "st_uid", "st_mode")
            )
        ):
            raise ValueError("private_io_directory_unsafe")
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def verify_directory(fd, path, *, mode=0o700):
    """Reject writes through an FD whose configured directory was replaced."""
    actual, named = os.fstat(fd), Path(path).lstat()
    if actual.st_uid != os.geteuid() or stat.S_IMODE(actual.st_mode) != mode:
        raise ValueError("private_io_directory_unsafe")
    if any(
        getattr(actual, field) != getattr(named, field)
        for field in ("st_dev", "st_ino", "st_uid", "st_mode")
    ):
        raise ValueError("private_io_directory_changed")
