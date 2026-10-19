"""Private file descriptors, bounded writes and atomic bundle publication."""

import ctypes
import errno
import fcntl
import gzip
import hashlib
import os
import stat
import uuid
from pathlib import Path
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from . import codec
from . import policy


def _absolute_without_resolving(path: os.PathLike) -> str:
    return os.path.abspath(os.fsdecode(os.fspath(path)))


def _inode_identity(value: os.stat_result) -> Tuple[int, int]:
    return value.st_dev, value.st_ino


def _file_identity(value: os.stat_result) -> Tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_directory_chain(path: str, description: str) -> int:
    """Open every absolute path component relative to its pinned parent fd."""

    if not os.path.isabs(path):
        raise policy.BundleError("%s must be absolute" % description)
    try:
        descriptor = os.open(os.path.sep, _directory_flags())
    except OSError as exc:
        raise policy.BundleError("unable to open filesystem root safely") from exc
    try:
        for component in Path(path).parts[1:]:
            try:
                child = os.open(
                    component,
                    _directory_flags(),
                    dir_fd=descriptor,
                )
            except OSError as exc:
                raise policy.BundleError(
                    "%s contains an unavailable or symlink directory component"
                    % description
                ) from exc
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_child_directory(parent_fd: int, name: str, error_message: str) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise policy.BundleError(error_message) from exc
    try:
        opened = os.fstat(descriptor)
        try:
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise policy.BundleError(error_message) from None
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or _inode_identity(opened) != _inode_identity(named)
        ):
            raise policy.BundleError(error_message)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _safe_source_fd(path: str) -> Tuple[int, os.stat_result]:
    parent_path, name = os.path.split(path)
    if not name:
        raise policy.BundleError("source must name a regular file")
    parent_fd = _open_directory_chain(parent_path, "source path")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise policy.BundleError(
                "unable to open source leaf safely; symlinks are not allowed"
            ) from exc
    finally:
        os.close(parent_fd)
    try:
        opened = os.fstat(descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise policy.BundleError("unable to inspect source safely") from exc
    try:
        if not stat.S_ISREG(opened.st_mode):
            raise policy.BundleError("source must be a regular file")
        if opened.st_uid != os.geteuid():
            raise policy.BundleError("source must be owned by the effective user")
        if opened.st_nlink != 1:
            raise policy.BundleError("source must not be hard-linked")
        if stat.S_IMODE(opened.st_mode) & 0o022:
            raise policy.BundleError("source must not be group/world writable")
        if opened.st_size > policy.MAX_COMPRESSED_BYTES:
            raise policy.BundleError("source exceeds the compressed-size limit")
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def _open_output_root(path: str) -> int:
    if path == os.path.sep:
        raise policy.BundleError("filesystem root cannot be an output root")
    descriptor = _open_directory_chain(path, "output root path")
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise policy.BundleError("output root must be a directory")
        if info.st_uid != os.geteuid():
            raise policy.BundleError("output root must be owned by the effective user")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise policy.BundleError("output root permissions must be exactly 0700")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_lock(root_fd: int) -> int:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(policy.LOCK_NAME, flags, 0o600, dir_fd=root_fd)
    except OSError as exc:
        raise policy.BundleError("unable to create the output lock safely") from exc
    try:
        info = os.fstat(descriptor)
        by_name = os.stat(policy.LOCK_NAME, dir_fd=root_fd, follow_symlinks=False)
        if _inode_identity(info) != _inode_identity(by_name):
            raise policy.BundleError("output lock changed while it was opened")
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise policy.BundleError("output lock is unsafe")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise policy.BundleError("output lock permissions must be exactly 0600")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise policy.BundleError("another bundle build is active") from exc
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _make_staging_directory(root_fd: int) -> Tuple[str, int]:
    for _attempt in range(128):
        name = ".catalog-bundle-%s" % uuid.uuid4().hex
        try:
            os.mkdir(name, 0o700, dir_fd=root_fd)
        except FileExistsError:
            continue
        try:
            descriptor = _open_child_directory(
                root_fd, name, "new staging directory changed while it was opened"
            )
            metadata = os.fstat(descriptor)
            if (
                metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                os.close(descriptor)
                raise policy.BundleError("new staging directory is unsafe")
            return name, descriptor
        except Exception:
            try:
                os.rmdir(name, dir_fd=root_fd)
            except OSError:
                pass
            raise
    raise policy.BundleError("unable to reserve a unique staging directory")


def write_gzip_jsonl(
    directory_fd: int, name: str, rows: Sequence[Mapping[str, Any]]
) -> int:
    if not 1 <= len(rows) <= policy.MAX_ROWS:
        raise policy.BundleError("artifact record count is outside its limit")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    expanded_size = 0
    with os.fdopen(descriptor, "wb", closefd=True) as raw:
        with gzip.GzipFile(
            filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=0
        ) as compressed:
            for row in rows:
                encoded = codec.canonical_json(row) + b"\n"
                if len(encoded) > policy.MAX_ARTIFACT_LINE_BYTES:
                    raise policy.BundleError("artifact record exceeds its byte limit")
                if expanded_size + len(encoded) > policy.MAX_ARTIFACT_EXPANDED_BYTES:
                    raise policy.BundleError("artifact exceeds its expanded-size limit")
                expanded_size += len(encoded)
                compressed.write(encoded)
        os.fchmod(raw.fileno(), 0o400)
        raw.flush()
        os.fsync(raw.fileno())
    return expanded_size


def _write_plain(directory_fd: int, name: str, value: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    with os.fdopen(descriptor, "wb", closefd=True) as stream:
        stream.write(value)
        os.fchmod(stream.fileno(), 0o400)
        stream.flush()
        os.fsync(stream.fileno())


def _hash_file(
    directory_fd: int, name: str, expected_mode: int = 0o400
) -> Tuple[str, int]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    digest = hashlib.sha256()
    total = 0
    try:
        info = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _file_identity(info) != _file_identity(named):
            raise policy.BundleError("bundle artifact changed while it was opened")
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise policy.BundleError("bundle artifact is unsafe")
        if stat.S_IMODE(info.st_mode) != expected_mode:
            raise policy.BundleError("bundle artifact has unsafe permissions")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
        after = os.fstat(descriptor)
        by_name = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _file_identity(info) != _file_identity(after) or _file_identity(
            info
        ) != _file_identity(by_name):
            raise policy.BundleError("bundle artifact changed while it was read")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), total


def _artifact_description(
    directory_fd: int,
    name: str,
    records: int,
    schema_version: int,
    expanded_bytes: int,
) -> Dict[str, Any]:
    digest, size = _hash_file(directory_fd, name)
    if size > policy.MAX_ARTIFACT_COMPRESSED_BYTES:
        raise policy.BundleError("artifact exceeds its compressed-size limit")
    return {
        "sha256": digest,
        "bytes": size,
        "records": records,
        "schema_version": schema_version,
        "media_type": "application/x-ndjson+gzip",
        "expanded_bytes": expanded_bytes,
    }


def _fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def _rename_noreplace(root_fd: int, source: str, destination: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "atomic no-replace rename is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        root_fd,
        os.fsencode(source),
        root_fd,
        os.fsencode(destination),
        1,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(error_number, "destination exists", destination)
        raise OSError(error_number, os.strerror(error_number), destination)


def _cleanup_directory_entry(root_fd: int, name: str, directory_fd: int) -> None:
    """Remove only the known contents of the exact held directory inode."""

    for artifact_name in policy.ARTIFACT_NAMES + (policy.MANIFEST_NAME,):
        try:
            os.unlink(artifact_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
    try:
        named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        opened = os.fstat(directory_fd)
    except FileNotFoundError:
        return
    if (
        stat.S_ISDIR(named.st_mode)
        and not stat.S_ISLNK(named.st_mode)
        and _inode_identity(named) == _inode_identity(opened)
    ):
        os.rmdir(name, dir_fd=root_fd)


def _portable_publish_directory(
    root_fd: int,
    source: str,
    source_fd: int,
    destination: str,
) -> None:
    """Fail-closed publication for filesystems without RENAME_NOREPLACE.

    The private output root is serialized by the bundle lock.  An exclusive
    destination directory reserves the content-addressed name; it remains mode
    0700 while the already-fsynced artifacts move into place and changes to the
    consumer-accepted mode 0500 only after the complete directory is fsynced.
    A reader may observe an incomplete reservation, but cannot accept it as a
    sealed bundle.
    """

    created = False
    destination_fd: Optional[int] = None
    try:
        os.mkdir(destination, 0o700, dir_fd=root_fd)
        created = True
        destination_fd = _open_child_directory(
            root_fd,
            destination,
            "portable bundle publication reservation changed while it was opened",
        )
        metadata = os.fstat(destination_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise policy.BundleError(
                "portable bundle publication reservation is unsafe"
            )
        # The staging directory was sealed to 0500 before its final fsync.
        # Directory-entry moves require owner write permission; consumers never
        # reference this random private staging name.
        os.fchmod(source_fd, 0o700)
        for name in policy.ARTIFACT_NAMES + (policy.MANIFEST_NAME,):
            os.rename(
                name,
                name,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
            )
        _fsync_directory(destination_fd)
        os.fchmod(destination_fd, 0o500)
        _fsync_directory(destination_fd)
        if os.listdir(source_fd):
            raise policy.BundleError(
                "portable publication left unexpected staging entries"
            )
        source_named = os.stat(source, dir_fd=root_fd, follow_symlinks=False)
        if _inode_identity(source_named) != _inode_identity(os.fstat(source_fd)):
            raise policy.BundleError(
                "staging directory changed during portable publication"
            )
        os.rmdir(source, dir_fd=root_fd)
    except Exception as exc:
        if isinstance(exc, FileExistsError) and not created:
            raise
        if destination_fd is not None:
            try:
                os.fchmod(destination_fd, 0o700)
                _cleanup_directory_entry(root_fd, destination, destination_fd)
            except OSError:
                # Keep an owner-only, non-consumer-accepted reservation for
                # explicit inspection; never guess that it is safe to publish.
                pass
        if isinstance(exc, FileExistsError):
            raise policy.BundleError(
                "portable publication encountered an unexpected destination entry"
            ) from exc
        raise
    finally:
        if destination_fd is not None:
            os.close(destination_fd)


def _publish_directory(
    root_fd: int,
    source: str,
    source_fd: int,
    destination: str,
) -> None:
    try:
        _rename_noreplace(root_fd, source, destination)
    except OSError as exc:
        unsupported = {
            errno.EINVAL,
            errno.ENOSYS,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        if exc.errno not in unsupported:
            raise
        _portable_publish_directory(root_fd, source, source_fd, destination)


def _verify_existing(
    directory_fd: int,
    manifest_bytes: bytes,
    expected_artifacts: Mapping[str, Mapping[str, Any]],
) -> None:
    info = os.fstat(directory_fd)
    if not stat.S_ISDIR(info.st_mode):
        raise policy.BundleError("bundle destination is not a safe directory")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o500:
        raise policy.BundleError(
            "bundle destination ownership or permissions are unsafe"
        )
    expected_names = set(policy.ARTIFACT_NAMES + (policy.MANIFEST_NAME,))
    if set(os.listdir(directory_fd)) != expected_names:
        raise policy.BundleError("existing bundle has unexpected or missing entries")
    manifest_digest, manifest_size = _hash_file(directory_fd, policy.MANIFEST_NAME)
    if manifest_size != len(manifest_bytes) or manifest_digest != codec.sha256_bytes(
        manifest_bytes
    ):
        raise policy.BundleError(
            "existing bundle manifest does not match the requested build"
        )
    for name in policy.ARTIFACT_NAMES:
        digest, size = _hash_file(directory_fd, name)
        if (
            digest != expected_artifacts[name]["sha256"]
            or size != expected_artifacts[name]["bytes"]
        ):
            raise policy.BundleError(
                "existing bundle artifact does not match its manifest"
            )
