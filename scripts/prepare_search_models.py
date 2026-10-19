"""Copy only the two API-pinned Ollama models; never modify a source or service.

A new destination is required. Failed/partial copies remain for inspection and
are never accepted by verify. Model hashes, not a mutable model tag, bind bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from search.model_policy import (  # noqa: E402
    SCOPE_MODEL,
    SCOPE_MODEL_DIGEST,
    PLANNER_MODEL,
    PLANNER_MODEL_DIGEST,
)
from search.serialization import strict_json  # noqa: E402
from search.operations.shared import private_io  # noqa: E402
from scripts import search_model_source  # noqa: E402

PINS = {SCOPE_MODEL: SCOPE_MODEL_DIGEST, PLANNER_MODEL: PLANNER_MODEL_DIGEST}
RECEIPT = "verified-models.json"


def directory(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("use an absolute path without symlink components")
    if not path.is_dir():
        raise ValueError("directory does not exist")
    return path


def checked_file(path, expected, size=None, output=None):
    # Reject symlink ancestors as well as the leaf. Runtime roots are private,
    # operator-owned directories; a hostile root operator is outside this scope.
    directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            # Existing Ollama downloads may be root-owned, read-only to us.
            or before.st_uid not in {0, os.getuid()}
            or before.st_mode & 0o022
            or (size is not None and before.st_size != size)
        ):
            raise ValueError("unsafe source file or size mismatch")
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
            if output is not None:
                output.write(block)
        after = os.fstat(source.fileno())
        named = os.stat(path, follow_symlinks=False)
        if search_model_source._file_identity(
            before
        ) != search_model_source._file_identity(
            after
        ) or search_model_source._file_identity(
            after
        ) != search_model_source._file_identity(named):
            raise ValueError("source changed during reading")
    if digest.hexdigest() != expected:
        raise ValueError("model artifact digest mismatch")
    return before.st_size


def verified_bytes(path, expected, *, max_bytes=128 * 1024):
    """Return the same bounded bytes which passed descriptor and hash checks."""
    size = path.lstat().st_size
    if not 1 <= size <= max_bytes:
        raise ValueError("verified file size is invalid")
    output = io.BytesIO()
    checked_file(path, expected, size, output=output)
    return output.getvalue()


def plan(source):
    source = directory(source)
    files = {}
    for model, expected in PINS.items():
        relative = Path("manifests/registry.ollama.ai/library") / model.replace(
            ":", "/"
        )
        path = source / relative
        raw = verified_bytes(path, expected)
        size = len(raw)
        value = strict_json(raw)
        files[str(relative)] = {"sha256": expected, "bytes": size}
        for descriptor in [value["config"], *value["layers"]]:
            digest, count = descriptor["digest"], descriptor["size"]
            if (
                not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                or type(count) is not int
                or not 0 < count <= 32 * 1024**3
            ):
                raise ValueError("invalid artifact descriptor")
            name = "blobs/" + digest.replace(":", "-")
            record = {"sha256": digest.removeprefix("sha256:"), "bytes": count}
            if name in files and files[name] != record:
                raise ValueError("conflicting blob descriptor")
            files[name] = record
    return files


def _verify_inventory(root, expected):
    root.verify()
    for relative, names in expected.items():
        with os.scandir(root.directory_fds[relative]) as entries:
            observed = set()
            for entry in entries:
                if entry.name not in names:
                    raise ValueError("unexpected model-store files or directories")
                observed.add(entry.name)
        if observed != names:
            raise ValueError("incomplete model-store directory")
    root.verify()


def verify(destination):
    destination = directory(destination)
    with search_model_source.open_private_root(destination) as root:
        receipt_info = os.stat(RECEIPT, dir_fd=root.fd, follow_symlinks=False)
        raw_receipt = private_io.read_file(
            root.fd, RECEIPT, mode=0o400, max_bytes=128 * 1024
        )
        receipt = strict_json(raw_receipt)
        files = plan(destination)
        if receipt != {"schema": 1, "models": PINS, "files": files}:
            raise ValueError("incomplete or mismatched model receipt")
        expected = {}
        for relative in (*files, RECEIPT):
            root.relative_parent(relative, private=True)
            parts = Path(relative).parts
            for index, name in enumerate(parts):
                expected.setdefault("/".join(parts[:index]), set()).add(name)
        _verify_inventory(root, expected)
        for name, item in files.items():
            root.verify()
            checked_file(destination / name, item["sha256"], item["bytes"])
        _verify_inventory(root, expected)
        if private_io.read_file(
            root.fd, RECEIPT, mode=0o400, max_bytes=128 * 1024
        ) != raw_receipt or search_model_source._file_identity(
            os.stat(RECEIPT, dir_fd=root.fd, follow_symlinks=False)
        ) != search_model_source._file_identity(receipt_info):
            raise ValueError("model destination receipt changed")
        root.verify()
        return receipt


def prepare(source, destination):
    source = directory(source)
    destination = Path(destination)
    parent = directory(destination.parent)
    info = parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("destination parent must be operator-owned mode 0700")
    if (
        destination in (ROOT, Path.home(), Path("/"))
        or destination.exists()
        or destination.is_symlink()
    ):
        raise ValueError("use a new dedicated model directory")
    files = plan(source)
    with search_model_source.create_private_root(parent, destination.name) as root:
        # Pre-create/validate every parent before copying multi-GB artifacts.
        parents = {
            name: root.relative_parent(name, private=True, create=True)
            for name in sorted(files)
        }
        # Blobs first, manifests last. Only completed copies have a verification
        # receipt; callers must verify before switching any service to this store.
        for name in sorted(files):
            item = files[name]
            target = destination / name
            parent_fd, leaf = parents[name]
            root.verify()
            fd = os.open(
                leaf,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o400,
                dir_fd=parent_fd,
            )
            with os.fdopen(fd, "wb") as output:
                checked_file(source / name, item["sha256"], item["bytes"], output)
                output.flush()
                os.fsync(output.fileno())
                opened = os.fstat(output.fileno())
                named = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                if search_model_source._file_identity(
                    opened
                ) != search_model_source._file_identity(named):
                    raise ValueError("model destination file changed")
            os.fsync(parent_fd)
            root.verify()
            checked_file(target, item["sha256"], item["bytes"])
            print("verified", name, item["bytes"], flush=True)
        receipt = {"schema": 1, "models": PINS, "files": files}
        raw_receipt = (json.dumps(receipt, indent=2) + "\n").encode("utf-8")
        root.verify()
        fd = os.open(
            RECEIPT,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o400,
            dir_fd=root.fd,
        )
        with os.fdopen(fd, "wb") as output:
            output.write(raw_receipt)
            output.flush()
            os.fsync(output.fileno())
            opened = os.fstat(output.fileno())
            named = os.stat(RECEIPT, dir_fd=root.fd, follow_symlinks=False)
            if search_model_source._file_identity(
                opened
            ) != search_model_source._file_identity(named):
                raise ValueError("model destination receipt changed")
        os.fsync(root.fd)
        root.verify()
        if (
            private_io.read_file(root.fd, RECEIPT, mode=0o400, max_bytes=128 * 1024)
            != raw_receipt
        ):
            raise ValueError("model destination receipt changed")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("copy", "verify"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "copy":
        if args.source is None:
            parser.error("copy requires --source")
        receipt = prepare(args.source, args.destination)
    else:
        receipt = verify(args.destination)
    print(
        json.dumps(
            {
                "models": receipt["models"],
                "files": len(receipt["files"]),
                "bytes": sum(v["bytes"] for v in receipt["files"].values()),
            }
        )
    )


if __name__ == "__main__":
    main()
