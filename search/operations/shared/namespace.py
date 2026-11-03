"""Require directory paths protected from other unprivileged Unix accounts.

Root, the executing account and storage administrators remain trusted. Linux
POSIX permission/ACL-mask and sticky semantics must be enforced by the
filesystem; these checks do not authenticate a daemon or inspect arbitrary ACL
policies.
"""

import os
from pathlib import Path
import stat


class NamespaceError(ValueError):
    """A fixed policy diagnostic without paths or private contents."""


def _check_directory(info):
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)
    ):
        raise NamespaceError("namespace_unsafe")


def check_directory(path, *, private=False):
    """Check each directory without following links or changing permissions.

    Policy refusals raise :class:`NamespaceError`; filesystem access failures
    propagate as :class:`OSError` for the caller's domain error handling.
    """
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise NamespaceError("namespace_path_invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent = os.open("/", flags)
    try:
        info = os.fstat(parent)
        _check_directory(info)
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=parent)
            try:
                info = os.fstat(child)
                _check_directory(info)
                named = os.stat(
                    component, dir_fd=parent, follow_symlinks=False
                )
                if any(
                    getattr(info, field) != getattr(named, field)
                    for field in ("st_dev", "st_ino", "st_uid", "st_mode")
                ):
                    raise NamespaceError("namespace_changed")
            except BaseException:
                os.close(child)
                raise
            os.close(parent)
            parent = child
        if private and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise NamespaceError("namespace_private_directory_unsafe")
    finally:
        os.close(parent)
