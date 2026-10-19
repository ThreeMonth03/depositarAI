"""Bounded source packaging and exact Git restore, never deployment.

Only selected committed source and its reachable Git history are packaged.
Secrets, model stores, catalog runtime and untracked files are not copied.
An expected commit must come from the trusted source workflow, not merely
from the artifact being verified. Digests detect corruption, not authorship.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[2]
REVISION = re.compile(r"[0-9a-f]{40}\Z")
SCANNER_VERSION = "8.30.1"
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_FILES = 20000
MAX_COMMAND_BYTES = 4 * 1024 * 1024
REVIEWED_MANIFEST = ".github/security/gitleaks-reviewed.json"
MAX_REVIEW_BYTES = 64 * 1024
MAX_REVIEW_ENTRIES = 256
REVIEW_REASONS = frozenset(
    {
        "public_artifact_digest",
        "public_source_revision",
        "nonsecret_argument_identifier",
    }
)


class ArtifactError(ValueError):
    """Fixed diagnostic code; do not leak subprocess output or private paths."""


def _run(argv, *, cwd=None, timeout=120, environment=None):
    try:
        result = subprocess.run(
            [str(value) for value in argv],
            cwd=cwd,
            env=environment,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ArtifactError("source_command_failed") from error
    if (
        result.returncode != 0
        or len(result.stdout) > MAX_COMMAND_BYTES
        or len(result.stderr) > MAX_COMMAND_BYTES
    ):
        raise ArtifactError("source_command_failed")
    return result.stdout


def _git(repo, *args):
    return _run(["git", "-C", repo, *args])


def _expected_revision(value):
    if type(value) is not str or REVISION.fullmatch(value) is None:
        raise ArtifactError("source_revision_invalid")
    return value


def _absolute_path(value):
    path = Path(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or any(character in str(path) for character in "\x00\r\n")
        or path != path.resolve()
        or any(parent.is_symlink() for parent in (path, *path.parents))
    ):
        raise ArtifactError("source_path_invalid")
    return path


def _directory(path):
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise ArtifactError("source_directory_unsafe")


def _new_directory(value, *, excluded=()):
    path = _absolute_path(value)
    roots = (Path("/"), Path.home(), ROOT, *excluded)
    if any(
        path == root or root in path.parents or path in root.parents
        for root in roots[2:]
    ):
        raise ArtifactError("source_destination_not_dedicated")
    if path in roots[:2]:
        raise ArtifactError("source_destination_not_dedicated")
    _directory(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as error:
        raise ArtifactError("source_destination_exists") from error
    return path


def _read_file(path, *, maximum, digest_only=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or not 0 < before.st_size <= maximum
        ):
            raise ArtifactError("source_artifact_unsafe")
        blocks = []
        digest = hashlib.sha256()
        count = 0
        while True:
            block = os.read(fd, min(1024 * 1024, maximum + 1 - count))
            if not block:
                break
            count += len(block)
            if count > maximum:
                raise ArtifactError("source_artifact_too_large")
            digest.update(block)
            if not digest_only:
                blocks.append(block)
        after = os.fstat(fd)
        attributes = (
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
        if count != before.st_size or any(
            getattr(before, name) != getattr(after, name) for name in attributes
        ):
            raise ArtifactError("source_artifact_changed")
        return digest.hexdigest() if digest_only else b"".join(blocks)
    finally:
        os.close(fd)


def _source_revision(repo):
    _git(repo, "diff", "--quiet", "HEAD", "--")
    if _git(
        repo,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        ".github/workflows",
        "scripts/ci",
    ):
        raise ArtifactError("source_package_helpers_dirty")
    # Execute the checked-out guard in an isolated interpreter. Import caching
    # from a previous checkout must not substitute another release's guard.
    output = _run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            "import sys; from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); "
            "from search.operations.shared.source_identity import require_clean_revision; "
            "print(require_clean_revision(Path(sys.argv[1])))",
            repo,
        ],
        timeout=45,
    ).strip()
    return _expected_revision(output.decode("ascii"))


def _tree(repo, revision):
    raw = _git(repo, "ls-tree", "-rz", "--full-tree", revision)
    result = {}
    for record in raw.split(b"\x00"):
        if not record:
            continue
        description, name = record.split(b"\t", 1)
        mode, kind, digest = description.split()
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ArtifactError("source_tree_entry_unsupported")
        relative = name.decode("utf-8")
        _relative_name(relative)
        result[relative] = (digest.decode("ascii"), mode == b"100755")
        if len(result) > MAX_FILES:
            raise ArtifactError("source_tree_too_large")
    if not result or "scripts/depositarai.py" not in result:
        raise ArtifactError("source_tree_incomplete")
    return result


def _relative_name(name):
    path = PurePosixPath(name)
    if (
        not name
        or len(name) > 1024
        or path.is_absolute()
        or any(part in ("", ".", "..", ".git") for part in name.split("/"))
        or "\\" in name
        or any(character in name for character in "\x00\r\n")
    ):
        raise ArtifactError("source_archive_path_invalid")


def _verify_archive(path, tree):
    inventory = {}
    seen = set()
    total = 0
    with tarfile.open(path, mode="r:gz") as archive:
        for member in archive:
            name = member.name.rstrip("/") if member.isdir() else member.name
            _relative_name(name)
            if name in seen or not (
                name == "depositar-ai" or name.startswith("depositar-ai/")
            ):
                raise ArtifactError("source_archive_entry_invalid")
            seen.add(name)
            if len(seen) > MAX_FILES * 2:
                raise ArtifactError("source_archive_too_large")
            if member.isdir():
                continue
            if not member.isfile() or not 0 <= member.size <= MAX_FILE_BYTES:
                raise ArtifactError("source_archive_entry_unsupported")
            total += member.size
            if total > MAX_SOURCE_BYTES:
                raise ArtifactError("source_archive_too_large")
            relative = name[len("depositar-ai/") :]
            if relative not in tree:
                raise ArtifactError("source_archive_inventory_mismatch")
            stream = archive.extractfile(member)
            if stream is None:
                raise ArtifactError("source_archive_entry_invalid")
            digest = hashlib.sha1(b"blob " + str(member.size).encode("ascii") + b"\x00")
            count = 0
            with stream:
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    count += len(block)
                    digest.update(block)
            if count != member.size:
                raise ArtifactError("source_archive_entry_invalid")
            inventory[relative] = (digest.hexdigest(), bool(member.mode & 0o111))
    if inventory != tree:
        raise ArtifactError("source_archive_inventory_mismatch")


def _names(revision):
    prefix = "depositar-ai-" + revision
    return (prefix + ".tar.gz", prefix + ".bundle", "SOURCE_REVISION")


def _verify_bundle(path, revision):
    if _run(["git", "bundle", "list-heads", path]) != (revision + " HEAD\n").encode(
        "ascii"
    ):
        raise ArtifactError("source_bundle_revision_mismatch")


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactError("source_review_manifest_invalid")
        result[key] = value
    return result


def _reviewed_entries(repo, revision, tree):
    """Load exact reviewed history positions from selected committed bytes.

    There is no environment override, automatic baseline or rule/path regex.
    A missing manifest means zero exceptions. New commits cannot match an old
    fingerprint even when a secret is added at the same path and line later.
    """
    if REVIEWED_MANIFEST not in tree:
        return [], None
    raw = _git(repo, "show", revision + ":" + REVIEWED_MANIFEST)
    if not 0 < len(raw) <= MAX_REVIEW_BYTES:
        raise ArtifactError("source_review_manifest_invalid")
    value = json.loads(
        raw,
        object_pairs_hook=_strict_object,
        parse_constant=lambda _: (_ for _ in ()).throw(
            ArtifactError("source_review_manifest_invalid")
        ),
    )
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "entries"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["entries"]) is not list
        or len(value["entries"]) > MAX_REVIEW_ENTRIES
    ):
        raise ArtifactError("source_review_manifest_invalid")
    result = []
    seen = set()
    source_lines = {}
    for item in value["entries"]:
        if (
            type(item) is not dict
            or set(item) != {"fingerprint", "reasonCode"}
            or type(item["fingerprint"]) is not str
            or type(item["reasonCode"]) is not str
            or item["reasonCode"] not in REVIEW_REASONS
        ):
            raise ArtifactError("source_review_entry_invalid")
        fingerprint = item["fingerprint"]
        match = re.fullmatch(
            r"([0-9a-f]{40}):([^:]+):generic-api-key:([1-9][0-9]*)", fingerprint
        )
        if match is None or fingerprint in seen:
            raise ArtifactError("source_review_fingerprint_invalid")
        commit, path, line = match[1], match[2], int(match[3])
        _relative_name(path)
        if line > 10000000:
            raise ArtifactError("source_review_fingerprint_invalid")
        try:
            if (commit, path) not in source_lines:
                _git(repo, "merge-base", "--is-ancestor", commit, revision)
                source_lines[commit, path] = len(
                    _git(repo, "show", commit + ":" + path).splitlines()
                )
            if line > source_lines[commit, path]:
                raise ArtifactError("source_review_line_unavailable")
        except ArtifactError as error:
            raise ArtifactError("source_review_position_unavailable") from error
        seen.add(fingerprint)
        result.append(fingerprint)
    return result, hashlib.sha256(raw).hexdigest()


def _scan(repo, revision, scanner, tree):
    scanner = _absolute_path(scanner)
    info = scanner.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid not in (0, os.geteuid())
        or info.st_mode & 0o022
        or not info.st_mode & 0o111
    ):
        raise ArtifactError("source_scanner_unsafe")
    if _run([scanner, "version"], timeout=20).strip() != SCANNER_VERSION.encode(
        "ascii"
    ):
        raise ArtifactError("source_scanner_version_invalid")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GITLEAKS_")
    }
    reviewed, review_digest = _reviewed_entries(repo, revision, tree)
    with tempfile.TemporaryDirectory(prefix="depositar-source-scan-") as raw:
        root = Path(raw)
        configuration = root / "default.toml"
        configuration.write_text("[extend]\nuseDefault = true\n", encoding="ascii")
        ignore = root / "reviewed.gitleaksignore"
        ignore.write_text("".join(value + "\n" for value in reviewed), encoding="utf-8")
        try:
            _run(
                [
                    scanner,
                    "git",
                    "--config",
                    configuration,
                    "--gitleaks-ignore-path",
                    ignore,
                    "--ignore-gitleaks-allow",
                    "--redact=100",
                    "--no-banner",
                    "--log-level",
                    "error",
                    "--timeout",
                    "300",
                    "--log-opts=" + revision,
                    repo,
                ],
                timeout=330,
                environment=environment,
            )
        except ArtifactError as error:
            raise ArtifactError("source_secret_scan_failed") from error
    return {
        "path": REVIEWED_MANIFEST,
        "source_revision": revision if review_digest is not None else None,
        "sha256": review_digest,
        "fingerprint_count": len(reviewed),
    }


def package(destination, revision, scanner, *, repo_root=ROOT):
    revision = _expected_revision(revision)
    repo = _absolute_path(repo_root)
    if _source_revision(repo) != revision:
        raise ArtifactError("source_revision_mismatch")
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ArtifactError("source_history_incomplete")
    tree = _tree(repo, revision)
    reviewed_policy = _scan(repo, revision, scanner, tree)
    target = _new_directory(destination, excluded=(repo,))
    archive_name, bundle_name, revision_name = _names(revision)
    _git(
        repo,
        "archive",
        "--format=tar.gz",
        "--prefix=depositar-ai/",
        "--output=" + str(target / archive_name),
        revision,
    )
    _verify_archive(target / archive_name, tree)
    _git(repo, "bundle", "create", target / bundle_name, "HEAD")
    _verify_bundle(target / bundle_name, revision)
    if _source_revision(repo) != revision:
        raise ArtifactError("source_revision_changed")
    (target / revision_name).write_bytes((revision + "\n").encode("ascii"))
    records = []
    for name in _names(revision):
        digest = _read_file(target / name, maximum=MAX_ARTIFACT_BYTES, digest_only=True)
        records.append(digest + "  " + name + "\n")
    (target / "SHA256SUMS").write_text("".join(records), encoding="ascii")
    return {
        "schema_version": 1,
        "status": "packaged",
        "source_revision": revision,
        "tracked_file_count": len(tree),
        "secret_scan": "selected_reachable_history",
        "scanner_version": SCANNER_VERSION,
        "reviewed_policy": reviewed_policy,
        "runtime_included": False,
        "deployment_performed": False,
    }


def restore(package_directory, revision, destination):
    revision = _expected_revision(revision)
    source = _absolute_path(package_directory)
    _directory(source)
    expected_names = _names(revision)
    if set(os.listdir(source)) != {*expected_names, "SHA256SUMS"}:
        raise ArtifactError("source_artifact_inventory_invalid")
    manifest = _read_file(source / "SHA256SUMS", maximum=4096)
    records = manifest.decode("ascii").splitlines(keepends=True)
    if len(records) != len(expected_names):
        raise ArtifactError("source_digest_manifest_invalid")
    for name, record in zip(expected_names, records):
        match = re.fullmatch(r"([0-9a-f]{64})  " + re.escape(name) + r"\n", record)
        if match is None:
            raise ArtifactError("source_digest_manifest_invalid")
        digest = _read_file(source / name, maximum=MAX_ARTIFACT_BYTES, digest_only=True)
        if digest != match[1]:
            raise ArtifactError("source_artifact_digest_mismatch")
    if _read_file(source / "SOURCE_REVISION", maximum=41) != (revision + "\n").encode(
        "ascii"
    ):
        raise ArtifactError("source_revision_mismatch")
    archive_name, bundle_name, _ = expected_names
    _verify_bundle(source / bundle_name, revision)
    target = _new_directory(destination, excluded=(source,))
    _run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "init.templateDir=",
            "clone",
            "--no-checkout",
            source / bundle_name,
            target,
        ]
    )
    tree = _tree(target, revision)
    _verify_archive(source / archive_name, tree)
    _git(target, "bundle", "verify", source / bundle_name)
    _git(target, "-c", "core.hooksPath=/dev/null", "checkout", "--detach", revision)
    if _source_revision(target) != revision or _git(
        target, "status", "--porcelain", "--untracked-files=all"
    ):
        raise ArtifactError("source_restore_not_clean")
    output = _run(
        [sys.executable, "-I", "-S", "-B", target / "scripts/depositarai.py", "--help"],
        cwd=target,
        timeout=30,
    )
    if b"usage:" not in output or b"--config" not in output:
        raise ArtifactError("source_cli_help_invalid")
    if _source_revision(target) != revision or _git(
        target, "status", "--porcelain", "--untracked-files=all"
    ):
        raise ArtifactError("source_restore_not_clean")
    return {
        "schema_version": 1,
        "status": "restored",
        "source_revision": revision,
        "tracked_file_count": len(tree),
        "digests_verified": True,
        "archive_matches_git_tree": True,
        "clean_git_checkout": True,
        "cli_help_verified": True,
        "runtime_restored": False,
        "deployment_performed": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "package", help="scan and archive exact committed source"
    )
    create.add_argument(
        "destination", help="new absolute directory outside the checkout"
    )
    create.add_argument("revision", help="trusted expected 40-character commit SHA")
    create.add_argument("scanner", help="absolute verified Gitleaks 8.30.1 binary")
    load = commands.add_parser("restore", help="digest-check and restore source only")
    load.add_argument("package_directory", help="trusted source artifact directory")
    load.add_argument("revision", help="expected SHA from the trusted workflow")
    load.add_argument("destination", help="new absolute directory outside the checkout")
    options = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if options.command == "package":
            report = package(options.destination, options.revision, options.scanner)
        else:
            report = restore(
                options.package_directory, options.revision, options.destination
            )
    except (OSError, ValueError, UnicodeError, tarfile.TarError, ImportError) as error:
        code = (
            str(error) if isinstance(error, ArtifactError) else "source_artifact_failed"
        )
        print(json.dumps({"status": "failed", "code": code}), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
