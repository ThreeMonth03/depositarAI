"""Preserve private job source snapshots and independently restorable Git artifacts."""

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile

from search.operations.shared import private_io
from search.serialization import strict_json

from .pueue import JobError, command, require


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


MAX_BUNDLE_BYTES = 64 * 1024**2
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_BUNDLE_FILES = {"source.bundle", "source.json"}


def _artifact_path(value):
    path = Path(value)
    require(
        path.is_absolute() and ".." not in path.parts and path.resolve() == path,
        "source_artifact_path_invalid",
    )
    return path


def _git(repository, *arguments):
    result = command(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "init.templateDir=",
            "-C",
            repository,
            *arguments,
        ],
        timeout=60,
    )
    require(result.returncode == 0, "source_artifact_git_failed")
    return result.stdout


def _clean_source(source, revision):
    require(
        _git(source, "rev-parse", "HEAD").strip() == revision.encode("ascii")
        and not _git(
            source, "status", "--porcelain", "--untracked-files=all", "--ignored"
        ),
        "source_artifact_source_changed",
    )


def _bundle_record(directory, fd, revision):
    require(set(os.listdir(fd)) == _BUNDLE_FILES, "source_artifact_incomplete")
    raw = private_io.read_file(
        fd, "source.bundle", mode=0o600, max_bytes=MAX_BUNDLE_BYTES
    )
    record = strict_json(
        private_io.read_file(fd, "source.json", mode=0o600, max_bytes=4096)
    )
    require(
        type(record) is dict
        and set(record) == {"revision", "bundle_name", "bytes", "sha256"}
        and record["revision"] == revision
        and record["bundle_name"] == "source.bundle"
        and type(record["bytes"]) is int
        and record["bytes"] == len(raw)
        and record["sha256"] == hashlib.sha256(raw).hexdigest(),
        "source_artifact_changed",
    )
    private_io.verify_directory(fd, directory)
    return record, raw


def _restore_proof(repository, directory, revision, raw):
    # Header/prerequisite checks do not read the pack. A fresh bare clone and
    # fsck prove the saved objects are usable without checking out source code.
    with tempfile.TemporaryDirectory(
        prefix=".source-verify-", dir=directory.parent
    ) as name:
        temporary = Path(name)
        temporary.chmod(0o700)
        parent = private_io.open_directory(temporary)
        try:
            private_io.write_file(
                parent, "source.bundle", raw, max_bytes=MAX_BUNDLE_BYTES
            )
        finally:
            os.close(parent)
        bundle = temporary / "source.bundle"
        require(
            _git(repository, "bundle", "list-heads", bundle).splitlines()
            == [revision.encode("ascii") + b" HEAD"],
            "source_artifact_revision_changed",
        )
        _git(repository, "bundle", "verify", bundle)
        restored = temporary / "repository.git"
        _git(repository, "clone", "--bare", "--no-hardlinks", "--", bundle, restored)
        require(
            _git(restored, "rev-parse", revision + "^{commit}").strip()
            == revision.encode("ascii")
            and _REVISION.fullmatch(
                _git(restored, "rev-parse", revision + "^{tree}")
                .decode("ascii")
                .strip()
            ),
            "source_artifact_revision_changed",
        )
        _git(restored, "fsck", "--full", "--strict", "--no-reflogs")


def verify_bundle(repository, artifact_directory, revision):
    """Verify private saved bytes and their independent Git restore proof."""
    fd = None
    try:
        require(
            type(revision) is str and _REVISION.fullmatch(revision),
            "source_artifact_revision_invalid",
        )
        directory, repository = (
            _artifact_path(artifact_directory),
            _artifact_path(repository),
        )
        fd = private_io.open_directory(directory)
        before, raw = _bundle_record(directory, fd, revision)
        _restore_proof(repository, directory, revision, raw)
        require(
            _bundle_record(directory, fd, revision)[0] == before,
            "source_artifact_changed",
        )
        return before
    except JobError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise JobError("job_source_artifact_invalid") from None
    finally:
        if fd is not None:
            os.close(fd)


def preserve_bundle(source, artifact_directory, revision):
    """Preserve one exact clean source revision, reusing only verified artifacts.

    The caller holds the queue's submission and execution locks. Existing or
    partially written artifacts are never replaced; uncertain evidence requires
    reconciliation before a frozen worktree can be removed.
    """
    source_fd = parent = fd = None
    try:
        require(
            type(revision) is str and _REVISION.fullmatch(revision),
            "source_artifact_revision_invalid",
        )
        source, directory = _artifact_path(source), _artifact_path(artifact_directory)
        require(
            source != directory
            and source not in directory.parents
            and directory not in source.parents,
            "source_artifact_overlaps_source",
        )
        source_fd = private_io.open_directory(source)
        parent = private_io.open_directory(directory.parent)
        _clean_source(source, revision)
        try:
            fd = private_io.open_child_directory(parent, directory.name, create=True)
        except FileExistsError:
            record = verify_bundle(source, directory, revision)
            _clean_source(source, revision)
            private_io.verify_directory(source_fd, source)
            return record
        bundle = directory / "source.bundle"
        _git(source, "bundle", "create", bundle, "HEAD")
        bundle_fd = os.open(
            "source.bundle", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd
        )
        try:
            info = os.fstat(bundle_fd)
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid()
                and info.st_nlink == 1
                and 1 <= info.st_size <= MAX_BUNDLE_BYTES,
                "source_artifact_invalid",
            )
            os.fchmod(bundle_fd, 0o600)
            os.fsync(bundle_fd)
        finally:
            os.close(bundle_fd)
        raw = private_io.read_file(
            fd, "source.bundle", mode=0o600, max_bytes=MAX_BUNDLE_BYTES
        )
        record = {
            "revision": revision,
            "bundle_name": "source.bundle",
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        _restore_proof(source, directory, revision, raw)
        _clean_source(source, revision)
        private_io.verify_directory(source_fd, source)
        private_io.verify_directory(fd, directory)
        private_io.verify_directory(parent, directory.parent)
        private_io.write_json(fd, "source.json", record, max_bytes=4096)
        require(
            _bundle_record(directory, fd, revision)[0] == record,
            "source_artifact_changed",
        )
        return record
    except JobError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise JobError("job_source_artifact_invalid") from None
    finally:
        for retained in (fd, parent, source_fd):
            if retained is not None:
                os.close(retained)
