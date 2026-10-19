"""Private metadata record IO shared by sync and catalog preparation.

The source adapter owns the bounded JSON policy. Private IO owns descriptor,
identity and durable-publication checks; this layer preserves cache diagnostics.
It does not download metadata, lock caches or authorize catalog promotion.
"""

from pathlib import Path

from search.operations.metadata import source
from search.operations.shared import private_io


MAX_RECORD_BYTES = 4 * 1024**2


class SyncError(ValueError):
    """A cache boundary failed; callers must not promote any intermediate data."""


def real_path(path):
    """Require an absolute real path without symlink components."""
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise SyncError("use an absolute real path without symlink components")
    return path


def same_file(left, right):
    """Compare retained file identities without including access time."""
    return private_io.identity(left) == private_io.identity(right)


def file_bytes(parent_fd, name, *, mode, limit):
    """Read a bounded private file using the cache's diagnostic contract."""
    try:
        return private_io.read_file(parent_fd, name, mode=mode, max_bytes=limit)
    except ValueError as error:
        raise SyncError(
            "cache file ownership, mode, links or size are unsafe"
        ) from error


def read_record(parent_fd, name):
    """Read a mode-0600 record with the bounded metadata-source JSON policy."""
    return source.strict_json(
        file_bytes(parent_fd, name, mode=0o600, limit=MAX_RECORD_BYTES)
    )


def write_record(parent_fd, name, value, *, replace=False):
    """Publish a private cache record, replacing it only when explicitly allowed."""
    try:
        private_io.write_json(
            parent_fd, name, value, replace=replace, max_bytes=MAX_RECORD_BYTES
        )
    except ValueError as error:
        raise SyncError("cache record publication invariant failed") from error


def private_directory(parent_fd, name, *, create=False):
    """Open an operator-owned mode-0700 child through a retained parent FD."""
    try:
        return private_io.open_child_directory(parent_fd, name, create=create)
    except ValueError as error:
        raise SyncError(
            "cache directory must remain operator-owned mode 0700"
        ) from error
