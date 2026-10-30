"""Restore committed file modes inside a newly created private job worktree."""

import os
from pathlib import Path, PurePosixPath
import re
import stat

from search.operations.shared import private_io

from .pueue import require


def restore_modes(directory, tree):
    """Apply Git regular-file modes through retained nofollow descriptors.

    NFS default ACLs may create 0664 files even when Git reports a clean 100644
    checkout. Only the freshly owned job snapshot is normalized; caller files,
    links and untracked entries are never changed.
    """
    require(type(tree) is bytes and tree.endswith(b"\0"), "source_tree_invalid")
    rows = tree[:-1].split(b"\0")
    require(1 <= len(rows) <= 4096, "source_tree_limit")
    entries = {}
    for row in rows:
        metadata, filename = row.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        name = filename.decode("utf-8")
        relative = PurePosixPath(name)
        require(
            mode in {b"100644", b"100755"}
            and kind == b"blob"
            and re.fullmatch(rb"[0-9a-f]{40}", object_id)
            and not relative.is_absolute()
            and relative.parts
            and ".." not in relative.parts
            and relative.as_posix() == name
            and name not in entries,
            "source_tree_invalid",
        )
        entries[name] = (relative.parts, 0o755 if mode == b"100755" else 0o644)
    directory = Path(directory)
    parent = private_io.open_directory(directory.parent)
    root = None
    try:
        root = os.open(
            directory.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        require(os.fstat(root).st_uid == os.geteuid(), "source_owner_changed")
        os.fchmod(root, 0o700)
        for parts, mode in entries.values():
            current = os.dup(root)
            fd = None
            try:
                for part in parts[:-1]:
                    child = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=current,
                    )
                    os.close(current)
                    current = child
                    require(
                        os.fstat(current).st_uid == os.geteuid(), "source_owner_changed"
                    )
                fd = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=current,
                )
                before = os.fstat(fd)
                require(
                    stat.S_ISREG(before.st_mode)
                    and before.st_uid == os.geteuid()
                    and before.st_nlink == 1,
                    "source_file_unsafe",
                )
                if stat.S_IMODE(before.st_mode) != mode:
                    os.fchmod(fd, mode)
                    os.fsync(fd)
                after = os.fstat(fd)
                named = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
                require(
                    private_io.identity(after) == private_io.identity(named)
                    and stat.S_IMODE(after.st_mode) == mode
                    and (
                        before.st_dev,
                        before.st_ino,
                        before.st_size,
                        before.st_mtime_ns,
                    )
                    == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
                    "source_file_changed",
                )
            finally:
                if fd is not None:
                    os.close(fd)
                os.close(current)
        private_io.verify_directory(root, directory)
        os.fsync(root)
    finally:
        if root is not None:
            os.close(root)
        os.close(parent)
