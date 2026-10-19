"""Seed pinned model bytes through retained directory and file descriptors.

An allocated sticky, primary-group ancestor is an availability dependency, not
a model-content authority. Only an operator-owned 0700 source root may use that
exception; all files remain bound to reviewed sizes, ownership and SHA-256.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import sys


CHUNK_BYTES = 1024 * 1024
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
WRITE_FLAGS = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class ModelSourceError(ValueError):
    """A fixed, non-secret source or staging invariant failed."""


def _directory_identity(info):
    # Shared ancestors can legitimately acquire sibling entries while copying.
    # Their inode, ownership and permissions must stay fixed, not their mtime.
    return tuple(
        getattr(info, field)
        for field in ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode")
    )


def _file_identity(info):
    return (
        *_directory_identity(info),
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _ancestor_policy(info, uid, gid):
    """Return whether this ancestor needs the private allocated-root policy."""
    mode = stat.S_IMODE(info.st_mode)
    if not mode & 0o022:
        return False
    if info.st_uid == 0 and mode & stat.S_ISVTX:
        return False  # Preserve the reviewed root-owned /tmp-style exception.
    if mode & stat.S_ISVTX and mode & 0o020 and not mode & 0o002 and info.st_gid == gid:
        return True
    raise ModelSourceError("model_source_ancestor_unsafe")


def _source_directory_policy(info, uid):
    if info.st_uid not in {0, uid} or stat.S_IMODE(info.st_mode) & 0o022:
        raise ModelSourceError("model_source_directory_unsafe")


def _private_directory_policy(info, uid):
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ModelSourceError("model_destination_directory_unsafe")


class _PinnedRoot:
    def __init__(self, root, fd, uid, records, descriptors):
        self.root, self.fd, self.uid = root, fd, uid
        self.records, self.descriptors = records, descriptors
        self.directory_fds = {"": fd}

    def verify(self):
        for parent, name, descriptor, expected in self.records:
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(named.st_mode)
                or _directory_identity(named) != expected
                or _directory_identity(opened) != expected
            ):
                raise ModelSourceError("model_source_directory_changed")

    def relative_parent(self, relative, *, private=False, create=False):
        """Retain every parent FD; private creation never repairs existing modes."""
        if (
            type(private) is not bool
            or type(create) is not bool
            or create
            and not private
        ):
            raise ModelSourceError("model_destination_creation_policy_invalid")
        if type(relative) is not str:
            raise ModelSourceError("model_source_relative_path_invalid")
        parts = Path(relative).parts
        if (
            not parts
            or Path(relative).is_absolute()
            or any(part in {".", ".."} for part in parts)
            or "\x00" in relative
        ):
            raise ModelSourceError("model_source_relative_path_invalid")
        if private:
            _private_directory_policy(os.fstat(self.fd), self.uid)
        parent = self.fd
        prefix = []
        for name in parts[:-1]:
            self.verify()
            prefix.append(name)
            key = "/".join(prefix)
            if key in self.directory_fds:
                parent = self.directory_fds[key]
                if private:
                    _private_directory_policy(os.fstat(parent), self.uid)
                continue
            try:
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise
                self.verify()
                os.mkdir(name, mode=0o700, dir_fd=parent)
                os.fsync(parent)
                named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode):
                raise ModelSourceError("model_source_directory_unsafe")
            child = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
            self.descriptors.append(child)
            opened = os.fstat(child)
            if not stat.S_ISDIR(named.st_mode) or _directory_identity(
                named
            ) != _directory_identity(opened):
                raise ModelSourceError("model_source_directory_changed")
            _source_directory_policy(opened, self.uid)
            if private:
                _private_directory_policy(opened, self.uid)
            self.records.append((parent, name, child, _directory_identity(opened)))
            self.directory_fds[key] = child
            parent = child
        self.verify()
        return parent, parts[-1]


@contextmanager
def open_source_root(path):
    """Keep every no-follow ancestry FD pinned until the operation finishes."""
    if not isinstance(path, (str, Path)):
        raise ModelSourceError("model_source_root_path_invalid")
    root = Path(path)
    if (
        not root.is_absolute()
        or root == Path("/")
        or ".." in root.parts
        or "\x00" in str(root)
    ):
        raise ModelSourceError("model_source_root_path_invalid")
    uid, gid = os.geteuid(), os.getegid()
    descriptor = os.open("/", DIRECTORY_FLAGS)
    descriptors, records = [descriptor], []
    allocated = False
    try:
        for index, name in enumerate(root.parts[1:]):
            named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(name, DIRECTORY_FLAGS, dir_fd=descriptor)
            descriptors.append(child)
            opened = os.fstat(child)
            if not stat.S_ISDIR(named.st_mode) or _directory_identity(
                named
            ) != _directory_identity(opened):
                raise ModelSourceError("model_source_directory_changed")
            final = index == len(root.parts) - 2
            if final:
                _source_directory_policy(opened, uid)
                if allocated and (
                    opened.st_uid != uid or stat.S_IMODE(opened.st_mode) != 0o700
                ):
                    raise ModelSourceError("model_source_allocated_root_not_private")
            else:
                allocated = _ancestor_policy(opened, uid, gid) or allocated
            records.append((descriptor, name, child, _directory_identity(opened)))
            descriptor = child
        pinned = _PinnedRoot(root, descriptor, uid, records, descriptors)
        pinned.verify()
        yield pinned
        pinned.verify()
    finally:
        for opened in reversed(descriptors):
            os.close(opened)


@contextmanager
def open_private_root(path):
    """Retain an existing operator-owned 0700 store without changing it."""
    with open_source_root(path) as root:
        _private_directory_policy(os.fstat(root.fd), root.uid)
        yield root


@contextmanager
def create_private_root(parent, name):
    """Create only one new leaf through retained private-parent ancestry FDs.

    No existing root is adopted or overwritten. Partial roots remain on failure
    for explicit inspection, and every nested private parent is created with
    its own 0700 mkdir rather than pathlib's ambient-umask recursive defaults.
    """
    if (
        type(name) is not str
        or name in {"", ".", ".."}
        or any(character in name for character in "/\x00\r\n")
    ):
        raise ModelSourceError("model_destination_root_name_invalid")
    with open_source_root(parent) as source:
        _private_directory_policy(os.fstat(source.fd), source.uid)
        source.verify()
        os.mkdir(name, mode=0o700, dir_fd=source.fd)
        os.fsync(source.fd)
        named = os.stat(name, dir_fd=source.fd, follow_symlinks=False)
        descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=source.fd)
        source.descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        _private_directory_policy(opened, source.uid)
        if _directory_identity(opened) != _directory_identity(named):
            raise ModelSourceError("model_source_directory_changed")
        source.records.append(
            (source.fd, name, descriptor, _directory_identity(opened))
        )
        root = _PinnedRoot(
            source.root / name,
            descriptor,
            source.uid,
            source.records,
            source.descriptors,
        )
        root.verify()
        yield root
        root.verify()


def _source_file_policy(info, uid, expected_size):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {0, uid}
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
        or info.st_size != expected_size
    ):
        raise ModelSourceError("model_source_file_unsafe")


def _destination_policy(info, uid):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ModelSourceError("model_staging_file_unsafe")


def copy_verified_file(
    source_root, relative, destination, expected_sha256, expected_size
):
    """Copy only stable pinned bytes into an existing private staging file."""
    if (
        type(expected_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or type(expected_size) is not int
        or not 0 < expected_size <= 32 * 1024**3
    ):
        raise ModelSourceError("model_source_authority_invalid")
    if not isinstance(destination, (str, Path)):
        raise ModelSourceError("model_staging_path_invalid")
    destination = Path(destination)
    if (
        ".." in destination.parts
        or not destination.is_absolute()
        or "\x00" in str(destination)
    ):
        raise ModelSourceError("model_staging_path_invalid")
    with (
        open_source_root(source_root) as source,
        open_source_root(destination.parent) as target,
    ):
        target_info = os.fstat(target.fd)
        if (
            target_info.st_uid != source.uid
            or stat.S_IMODE(target_info.st_mode) != 0o700
        ):
            raise ModelSourceError("model_staging_directory_unsafe")
        parent, name = source.relative_parent(relative)
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        _source_file_policy(before, source.uid, expected_size)
        source_fd = os.open(name, READ_FLAGS, dir_fd=parent)
        destination_fd = None
        try:
            opened = os.fstat(source_fd)
            _source_file_policy(opened, source.uid, expected_size)
            if _file_identity(opened) != _file_identity(before):
                raise ModelSourceError("model_source_file_changed")
            staged = os.stat(destination.name, dir_fd=target.fd, follow_symlinks=False)
            _destination_policy(staged, source.uid)
            if (staged.st_dev, staged.st_ino) == (opened.st_dev, opened.st_ino):
                raise ModelSourceError("model_staging_overlaps_source")
            destination_fd = os.open(destination.name, WRITE_FLAGS, dir_fd=target.fd)
            if _file_identity(os.fstat(destination_fd)) != _file_identity(staged):
                raise ModelSourceError("model_staging_file_changed")
            os.ftruncate(destination_fd, 0)
            digest, size = hashlib.sha256(), 0
            while size <= expected_size:
                block = os.read(source_fd, min(CHUNK_BYTES, expected_size + 1 - size))
                if not block:
                    break
                size += len(block)
                if size > expected_size:
                    raise ModelSourceError("model_source_file_changed")
                digest.update(block)
                view = memoryview(block)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise ModelSourceError("model_staging_short_write")
                    view = view[written:]
            os.fsync(destination_fd)
            after = os.fstat(source_fd)
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _file_identity(opened) != _file_identity(after) or _file_identity(
                after
            ) != _file_identity(named):
                raise ModelSourceError("model_source_file_changed")
            if size != expected_size or digest.hexdigest() != expected_sha256:
                raise ModelSourceError("model_source_sha256_mismatch")
            completed = os.fstat(destination_fd)
            target_named = os.stat(
                destination.name, dir_fd=target.fd, follow_symlinks=False
            )
            _destination_policy(completed, source.uid)
            if (
                _directory_identity(completed) != _directory_identity(staged)
                or completed.st_size != expected_size
                or _file_identity(completed) != _file_identity(target_named)
            ):
                raise ModelSourceError("model_staging_file_changed")
        finally:
            if destination_fd is not None:
                os.close(destination_fd)
            os.close(source_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    check = subcommands.add_parser("check")
    check.add_argument("--source-root", type=Path, required=True)
    copy = subcommands.add_parser("copy")
    copy.add_argument("--source-root", type=Path, required=True)
    copy.add_argument("--relative", required=True)
    copy.add_argument("--destination", type=Path, required=True)
    copy.add_argument("--sha256", required=True)
    copy.add_argument("--size", type=int, required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "check":
            with open_source_root(arguments.source_root):
                pass
        else:
            copy_verified_file(
                arguments.source_root,
                arguments.relative,
                arguments.destination,
                arguments.sha256,
                arguments.size,
            )
    except (OSError, ModelSourceError, UnicodeError) as error:
        code = (
            str(error)
            if isinstance(error, ModelSourceError)
            else "model_source_io_failed"
        )
        print("Error: " + code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
