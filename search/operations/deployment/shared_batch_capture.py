"""Private case inputs and transactional JSON capture, without running queries."""

from contextlib import contextmanager
import copy
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from scripts.check_search_quality import validate_cases
from search.operations.shared import private_io
from search.serialization import strict_json


MAX_CASE_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 8 * 1024**2
OUTPUT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,94}\.json\Z")


class SharedBatchCaptureError(ValueError):
    """Closed diagnostics, never raw queries, responses, paths or child errors."""


def _require(condition, code):
    if not condition:
        raise SharedBatchCaptureError("shared_batch_capture_" + code)


def _path(value):
    path = Path(value)
    _require(
        path.is_absolute()
        and path != Path("/")
        and ".." not in path.parts
        and path.resolve() == path,
        "path_invalid",
    )
    return path


def _directory_identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode)


class _Directories:
    """Pin every ancestor, including names, rather than only the last directory."""

    def __init__(self, path):
        self.path = _path(path)
        self.fds, self.links = [], []
        try:
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.fds.append(fd)
            for component in self.path.parts[1:]:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
                self.fds.append(child)
                self.links.append(
                    (fd, component, child, _directory_identity(os.fstat(child)))
                )
                fd = child
            self.fd = fd
            self.verify()
        except BaseException:
            self.close()
            raise

    def verify(self):
        for parent, name, fd, expected in self.links:
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            _require(
                stat.S_ISDIR(named.st_mode)
                and _directory_identity(named) == expected
                and _directory_identity(os.fstat(fd)) == expected,
                "directory_changed",
            )
        private_io.verify_directory(self.fd, self.path)

    def close(self):
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()


def _exists(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _outside_git(fds):
    for fd in fds:
        _require(
            not _exists(fd, ".git")
            and not all(_exists(fd, name) for name in ("HEAD", "objects", "refs")),
            "inside_git",
        )


def _cases(value):
    validate_cases(value)
    _require(
        len(value["cases"]) <= 3
        and sum(len(case["turns"]) for case in value["cases"]) <= 8,
        "cases_invalid",
    )
    # The shared plan digest binds raw query text, expectations and turn order.
    return copy.deepcopy(value)


def load_cases(path):
    """Read a private bounded plan, preserving raw queries and expectations."""
    directories = None
    try:
        path = _path(path)
        directories = _Directories(path.parent)
        _outside_git(directories.fds)
        raw = private_io.read_file(
            directories.fd, path.name, mode=0o600, max_bytes=MAX_CASE_BYTES
        )
        value = _cases(strict_json(raw))
        directories.verify()
        _outside_git(directories.fds)
        return value
    except SharedBatchCaptureError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError):
        raise SharedBatchCaptureError("shared_batch_capture_cases_invalid") from None
    finally:
        if directories is not None:
            directories.close()


class _Capture:
    def __init__(self, destination, excluded):
        self.destination = _path(destination)
        _require(
            type(excluded) in (list, tuple) and len(excluded) <= 128,
            "excluded_invalid",
        )
        for value in excluded:
            other = Path(value)
            _require(
                other.is_absolute() and ".." not in other.parts, "excluded_invalid"
            )
            other = other.resolve()
            _require(
                self.destination != other
                and self.destination not in other.parents
                and other not in self.destination.parents,
                "excluded_overlap",
            )
        self.parents = _Directories(self.destination.parent)
        self._fd = None
        self._cleanup_fd = None
        self.pending, self.published = {}, {}
        try:
            _outside_git(self.parents.fds)
            self.parents.verify()
            os.mkdir(self.destination.name, mode=0o700, dir_fd=self.parents.fd)
            os.fsync(self.parents.fd)
            self._fd = private_io.open_child_directory(
                self.parents.fd, self.destination.name
            )
            self._cleanup_fd = os.dup(self._fd)
            self.verify()
        except BaseException:
            self.close()
            raise

    def verify(self):
        try:
            _require(self._fd is not None, "closed")
            self.parents.verify()
            private_io.verify_directory(self._fd, self.destination)
            _outside_git([*self.parents.fds, self._fd])
            records = {
                entry["temporary"]: entry for entry in self.pending.values()
            } | self.published
            _require(set(os.listdir(self._fd)) == set(records), "output_changed")
            for name, entry in records.items():
                info = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
                _require(
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) == 0o600
                    and info.st_nlink == 1
                    and info.st_size == entry["bytes"]
                    and private_io.identity(info) == entry["identity"],
                    "output_changed",
                )
        except SharedBatchCaptureError:
            raise
        except (OSError, ValueError, TypeError):
            raise SharedBatchCaptureError(
                "shared_batch_capture_directory_changed"
            ) from None

    def write_json(self, name, value, *, max_bytes=MAX_OUTPUT_BYTES):
        """Stage no-clobber JSON; final names become visible only on context exit."""
        try:
            _require(type(name) is str and OUTPUT_NAME.fullmatch(name), "name_invalid")
            _require(
                type(max_bytes) is int and 1 <= max_bytes <= MAX_OUTPUT_BYTES,
                "size_invalid",
            )
            _require(
                name not in self.pending and name not in self.published, "output_exists"
            )
            self.verify()
            raw = private_io.canonical(value)
            _require(1 <= len(raw) <= max_bytes, "size_invalid")
            temporary = ".capture-" + uuid.uuid4().hex + ".json"
            private_io.write_file(self._fd, temporary, raw, max_bytes=max_bytes)
            info = os.stat(temporary, dir_fd=self._fd, follow_symlinks=False)
            entry = {
                "temporary": temporary,
                "identity": private_io.identity(info),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "maximum": max_bytes,
            }
            self.pending[name] = entry
            self.verify()
            return {key: entry[key] for key in ("sha256", "bytes")} | {"name": name}
        except SharedBatchCaptureError:
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            raise SharedBatchCaptureError("shared_batch_capture_write_failed") from None

    def publish(self):
        self.verify()
        for name, entry in list(self.pending.items()):
            temporary = entry["temporary"]
            raw = private_io.read_file(
                self._fd, temporary, mode=0o600, max_bytes=entry["maximum"]
            )
            _require(
                len(raw) == entry["bytes"]
                and hashlib.sha256(raw).hexdigest() == entry["sha256"],
                "output_changed",
            )
            os.link(
                temporary,
                name,
                src_dir_fd=self._fd,
                dst_dir_fd=self._fd,
                follow_symlinks=False,
            )
            # Track the link before fsync/verification so failure can revoke it.
            self.published[name] = entry
            os.unlink(temporary, dir_fd=self._fd)
            del self.pending[name]
            final = private_io.read_file(
                self._fd, name, mode=0o600, max_bytes=entry["maximum"]
            )
            _require(
                len(final) == entry["bytes"]
                and hashlib.sha256(final).hexdigest() == entry["sha256"],
                "output_changed",
            )
            final_identity = private_io.identity(
                os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            )
            # Linking changes ctime; every other original file property is pinned.
            _require(final_identity[:-1] == entry["identity"][:-1], "output_changed")
            entry["identity"] = final_identity
            os.fsync(self._fd)
            self.verify()
        self.verify()

    def discard(self):
        if self._cleanup_fd is None:
            return
        fd = self._cleanup_fd
        try:
            # This FD belongs only to the newly created capture directory.
            if os.fstat(fd).st_uid == os.geteuid():
                os.fchmod(fd, 0o700)
            records = {
                entry["temporary"]: entry for entry in self.pending.values()
            } | self.published
            for name, entry in records.items():
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == entry["identity"][:2]:
                        os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass
            os.fsync(fd)
        except OSError:
            pass

    def close(self):
        if self._cleanup_fd is not None:
            try:
                os.close(self._cleanup_fd)
            except OSError:
                pass
            self._cleanup_fd = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self.parents.close()


@contextmanager
def capture_run(destination, *, excluded=()):
    """Publish staged JSON only after body, pinned paths and final checks pass.

    The caller supplies all private input/operator/native/model roots to exclude.
    Runtime/query failures may be recorded as data inside the context; unexpected
    body or filesystem failures publish no completed capture. Only this run's
    inode-identified files are revoked, preserving foreign files on collisions.
    """
    capture = None
    try:
        capture = _Capture(destination, excluded)
        yield capture
        capture.publish()
    except SharedBatchCaptureError:
        if capture is not None:
            capture.discard()
        raise
    except Exception:
        if capture is not None:
            capture.discard()
        raise SharedBatchCaptureError("shared_batch_capture_failed") from None
    except BaseException:
        if capture is not None:
            capture.discard()
        raise
    finally:
        if capture is not None:
            capture.close()
