#!/usr/bin/env python3
"""Verify or safely converge the persistent ConfigSync dictionary tree."""

from __future__ import annotations

import os
import stat
import sys


MAX_ENTRIES = 4096
MAX_DEPTH = 16
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600


class PolicyError(Exception):
    pass


def _open_directory(parent_fd: int | None, name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        if parent_fd is None:
            return os.open(name, flags)
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        raise PolicyError("cannot safely open a required directory") from error


def _validate_metadata(
    metadata: os.stat_result,
    *,
    expected_uid: int,
    expected_device: int,
    kind: str,
    require_single_link: bool = False,
) -> None:
    if kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
        raise PolicyError("a required directory is a symlink or special file")
    if kind == "file" and not stat.S_ISREG(metadata.st_mode):
        raise PolicyError("the dictionary tree contains a symlink or special file")
    if metadata.st_uid != expected_uid:
        raise PolicyError("the dictionary tree contains an entry with the wrong owner")
    if metadata.st_dev != expected_device:
        raise PolicyError("the dictionary tree crosses a filesystem boundary")
    if require_single_link and metadata.st_nlink != 1:
        raise PolicyError("the dictionary tree contains a multiply-linked file")


def _apply_or_check_mode(fd: int, expected_mode: int, converge: bool) -> None:
    metadata = os.fstat(fd)
    actual_mode = stat.S_IMODE(metadata.st_mode)
    if converge:
        if actual_mode != expected_mode:
            os.fchmod(fd, expected_mode)
        if stat.S_IMODE(os.fstat(fd).st_mode) != expected_mode:
            raise PolicyError("could not converge dictionary entry permissions")
    elif actual_mode != expected_mode:
        raise PolicyError(
            "the dictionary tree contains an entry with unsafe permissions"
        )


def enforce(data_root: str, converge: bool) -> None:
    expected_uid = os.getuid()
    root_fd = _open_directory(None, data_root)
    try:
        root_metadata = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != expected_uid
        ):
            raise PolicyError("the catalog data root has unsafe metadata")
        expected_device = root_metadata.st_dev

        opensearch_fd = _open_directory(root_fd, "opensearch")
        try:
            _validate_metadata(
                os.fstat(opensearch_fd),
                expected_uid=expected_uid,
                expected_device=expected_device,
                kind="directory",
            )
            dictionary_fd = _open_directory(opensearch_fd, "dictionary")
            try:
                dictionary_metadata = os.fstat(dictionary_fd)
                _validate_metadata(
                    dictionary_metadata,
                    expected_uid=expected_uid,
                    expected_device=expected_device,
                    kind="directory",
                )
                _apply_or_check_mode(dictionary_fd, DIRECTORY_MODE, converge)

                entry_count = 0

                def visit(directory_fd: int, depth: int) -> None:
                    nonlocal entry_count
                    if depth > MAX_DEPTH:
                        raise PolicyError("the dictionary tree exceeds the depth limit")
                    try:
                        entries = os.scandir(directory_fd)
                    except OSError as error:
                        raise PolicyError(
                            "cannot enumerate the dictionary tree"
                        ) from error
                    with entries:
                        for entry in entries:
                            entry_count += 1
                            if entry_count > MAX_ENTRIES:
                                raise PolicyError(
                                    "the dictionary tree exceeds the entry limit"
                                )
                            if not hasattr(os, "O_PATH"):
                                raise PolicyError(
                                    "the host cannot safely inspect dictionary entries"
                                )
                            try:
                                inspect_fd = os.open(
                                    entry.name,
                                    os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW,
                                    dir_fd=directory_fd,
                                )
                            except OSError as error:
                                raise PolicyError(
                                    "the dictionary tree contains an inaccessible or unsafe entry"
                                ) from error
                            try:
                                metadata = os.fstat(inspect_fd)
                                if stat.S_ISDIR(metadata.st_mode):
                                    _validate_metadata(
                                        metadata,
                                        expected_uid=expected_uid,
                                        expected_device=expected_device,
                                        kind="directory",
                                    )
                                    open_flags = os.O_RDONLY | os.O_DIRECTORY
                                else:
                                    _validate_metadata(
                                        metadata,
                                        expected_uid=expected_uid,
                                        expected_device=expected_device,
                                        kind="file",
                                        require_single_link=True,
                                    )
                                    open_flags = os.O_RDONLY | os.O_NONBLOCK
                                try:
                                    child_fd = os.open(
                                        entry.name,
                                        open_flags | os.O_CLOEXEC | os.O_NOFOLLOW,
                                        dir_fd=directory_fd,
                                    )
                                except OSError as error:
                                    raise PolicyError(
                                        "the dictionary tree contains an inaccessible entry"
                                    ) from error
                                try:
                                    opened = os.fstat(child_fd)
                                    if (opened.st_dev, opened.st_ino) != (
                                        metadata.st_dev,
                                        metadata.st_ino,
                                    ):
                                        raise PolicyError(
                                            "the dictionary tree changed while it was opened"
                                        )
                                    if stat.S_ISDIR(metadata.st_mode):
                                        _apply_or_check_mode(
                                            child_fd, DIRECTORY_MODE, converge
                                        )
                                        visit(child_fd, depth + 1)
                                    else:
                                        _apply_or_check_mode(
                                            child_fd, FILE_MODE, converge
                                        )
                                finally:
                                    os.close(child_fd)
                                current = os.stat(
                                    entry.name,
                                    dir_fd=directory_fd,
                                    follow_symlinks=False,
                                )
                                if (current.st_dev, current.st_ino) != (
                                    metadata.st_dev,
                                    metadata.st_ino,
                                ):
                                    raise PolicyError(
                                        "the dictionary tree changed while it was inspected"
                                    )
                            finally:
                                os.close(inspect_fd)

                visit(dictionary_fd, 1)
            finally:
                os.close(dictionary_fd)
        finally:
            os.close(opensearch_fd)
    finally:
        os.close(root_fd)


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in {"--check", "--converge"}:
        print(
            "usage: search_dictionary_permissions.py (--check|--converge) DATA_ROOT",
            file=sys.stderr,
        )
        return 2
    try:
        enforce(sys.argv[2], sys.argv[1] == "--converge")
    except (OSError, PolicyError) as error:
        print(f"Error: ConfigSync dictionary policy: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
