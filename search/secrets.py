"""Read private hexadecimal keys without trusting mutable file metadata."""

import os
from pathlib import Path
import re
import stat


def read_secret(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) not in (0o400, 0o600)
            or metadata.st_size not in (64, 65)
        ):
            raise ValueError("invalid secret file")
        raw = os.read(fd, 66)
        current = os.fstat(fd)
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        # Reading can legitimately update atime, particularly on Ceph.
        if any(getattr(current, key) != getattr(metadata, key) for key in fields):
            raise ValueError("secret changed during read")
    finally:
        os.close(fd)
    value = raw.decode("ascii").removesuffix("\n")
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid secret format")
    return value
