#!/usr/bin/env python3
"""Capture, export and publish tested frontend images on a trusted hosted runner."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

REPOSITORY = "ThreeMonth03/depositarAI"
WORKFLOW = ".github/workflows/frontend-release.yml"
ROLES = ("web", "web-nginx")
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
MAX_TOTAL_BYTES = 1024**3
MANIFEST = "frontend-release.json"
BUNDLE = "frontend-release.sigstore.json"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _command(arguments, *, timeout=120):
    return subprocess.run(
        arguments, cwd=ROOT, capture_output=True, text=True, timeout=timeout
    )


def _run(arguments, *, timeout=120):
    result = _command(arguments, timeout=timeout)
    _require(result.returncode == 0, "frontend release command failed")
    _require(len(result.stdout) <= 1024**2, "command output too large")
    return result.stdout


def _context():
    revision = os.environ.get("GITHUB_SHA", "")
    _require(
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
        and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
        and os.environ.get("EXPECTED_RELEASE_REVISION") == revision
        and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and os.environ.get("GITHUB_WORKFLOW_REF")
        == REPOSITORY + "/" + WORKFLOW + "@refs/heads/main"
        and SHA.fullmatch(revision),
        "only the original repository's dispatched main revision may publish",
    )
    _require(
        _run(["git", "rev-parse", "HEAD"]).strip() == revision,
        "checkout does not match the workflow revision",
    )
    _run(["git", "diff", "--exit-code", "HEAD", "--"])
    return revision


def _unique(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, "duplicate JSON field")
        value[key] = item
    return value


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique)


def _write_new(path, value):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def _inspect(role, revision, *, release_tag=False):
    from search.operations.deployment.frontend_policy import validate_image_labels

    tag = revision if release_tag else "ci"
    reference = f"depositar-search-{role}:{tag}"
    rows = _json(
        _run(["docker", "image", "inspect", "--platform", "linux/amd64", reference])
    )
    _require(type(rows) is list and len(rows) == 1, "unexpected image inventory")
    row = rows[0]
    _require(type(row) is dict, "invalid image inspection")
    descriptor = row.get("Descriptor", {})
    config = row.get("Config")
    _require(type(config) is dict, "invalid image configuration")
    labels = config.get("Labels")
    _require(
        type(descriptor) is dict and type(labels) is dict, "missing image identity"
    )
    identifier = row.get("Id")
    _require(
        type(identifier) is str
        and DIGEST.fullmatch(identifier)
        and descriptor.get("digest") == identifier
        and descriptor.get("mediaType")
        in {
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        }
        and descriptor.get("platform") == {"architecture": "amd64", "os": "linux"}
        and row.get("Os") == "linux"
        and row.get("Architecture") == "amd64",
        "image platform, revision or contract does not match",
    )
    validate_image_labels(labels, revision, role)
    # This is a platform manifest digest on containerd Docker, not a config ID.
    # The manifest cryptographically binds its config and layer graph.
    return {"platform_manifest": identifier}


def capture(output):
    revision = _context()
    value = {
        "schema_version": 1,
        "revision": revision,
        "images": {role: _inspect(role, revision) for role in ROLES},
    }
    _write_new(Path(output), value)


def _tested_images(path, revision):
    path = Path(path)
    _require(
        path.is_file() and not path.is_symlink() and path.stat().st_size <= 16384,
        "tested image receipt is missing or invalid",
    )
    value = _json(path.read_bytes())
    _require(
        type(value) is dict
        and set(value) == {"schema_version", "revision", "images"}
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["revision"] == revision
        and type(value["images"]) is dict
        and set(value["images"]) == set(ROLES),
        "tested image receipt does not match the workflow",
    )
    for row in value["images"].values():
        _require(
            type(row) is dict
            and set(row) == {"platform_manifest"}
            and type(row["platform_manifest"]) is str
            and DIGEST.fullmatch(row["platform_manifest"]),
            "invalid tested image identity",
        )
    return value["images"]


def export(tested_images, output):
    from search.operations.deployment.frontend_artifact import (
        describe_archive,
        validate_manifest,
    )
    from search.transport.version import CONVERSATION_CONTRACT_VERSION

    revision = _context()
    tested = _tested_images(tested_images, revision)
    # Verify both identities before touching either destination tag.
    _require(
        {role: _inspect(role, revision) for role in ROLES} == tested,
        "tested image tag changed",
    )
    output = Path(output)
    output.mkdir(mode=0o700)
    images = {}
    for role in ROLES:
        reference = f"depositar-search-{role}:{revision}"
        _run(["docker", "image", "tag", f"depositar-search-{role}:ci", reference])
        _require(
            _inspect(role, revision, release_tag=True) == tested[role], "tag drift"
        )
        archive = output / f"{role}.oci.tar"
        _run(
            [
                "docker",
                "image",
                "save",
                "--platform",
                "linux/amd64",
                "--output",
                str(archive),
                reference,
            ],
            timeout=300,
        )
        entry = describe_archive(archive, revision, role)
        _require(
            entry["platform_manifest"] == tested[role]["platform_manifest"],
            "saved image is not the tested image",
        )
        _require(
            _inspect(role, revision) == tested[role]
            and _inspect(role, revision, release_tag=True) == tested[role],
            "image identity changed while exporting",
        )
        images[role] = entry
    _require(
        sum(row["archive_bytes"] for row in images.values()) <= MAX_TOTAL_BYTES,
        "frontend archives exceed the release size budget",
    )
    manifest = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "revision": revision,
        "source_ref": "refs/heads/main",
        "workflow": WORKFLOW,
        "platform": "linux/amd64",
        "conversation_contract_version": CONVERSATION_CONTRACT_VERSION,
        "images": images,
    }
    validate_manifest(manifest, repository=REPOSITORY, revision=revision)
    _write_new(output / MANIFEST, manifest)


def verify(directory, *, signed=False):
    from search.operations.deployment.frontend_artifact import (
        load_manifest,
        validate_archive,
    )

    revision = _context()
    directory = Path(directory)
    names = {MANIFEST, *(f"{role}.oci.tar" for role in ROLES)}
    if signed:
        names.add(BUNDLE)
    _require(
        directory.is_dir()
        and not directory.is_symlink()
        and {p.name for p in directory.iterdir()} == names,
        "release directory must contain exactly the approved assets",
    )
    value = load_manifest(
        directory / MANIFEST, repository=REPOSITORY, revision=revision
    )
    for role in ROLES:
        validate_archive(
            directory / f"{role}.oci.tar", value["images"][role], revision, role
        )
    if signed:
        bundle = directory / BUNDLE
        _require(
            bundle.is_file()
            and not bundle.is_symlink()
            and 0 < bundle.stat().st_size <= 4 * 1024**2,
            "missing signature bundle",
        )
        _require(type(_json(bundle.read_bytes())) is dict, "invalid signature bundle")
    return value


def _api(route, *, missing=False, fields=(), method="POST"):
    arguments = ["gh", "api", f"repos/{REPOSITORY}/{route}"]
    if fields:
        arguments += ["--method", method]
        for key, value in fields:
            arguments += (
                ["-F", f"{key}={str(value).lower()}"]
                if type(value) is bool
                else ["-f", f"{key}={value}"]
            )
    result = _command(arguments)
    value = _json(result.stdout)
    _require(type(value) is dict, "invalid GitHub release API response")
    if missing and result.returncode and value.get("status") in (404, "404"):
        return None
    _require(result.returncode == 0, "GitHub release API failed")
    return value


def publish(directory):
    value = verify(directory, signed=True)
    directory = Path(directory)
    revision = value["revision"]
    tag = "frontend-" + revision
    _require(
        _api("releases/tags/" + tag, missing=True) is None, "release already exists"
    )
    _require(
        _api("git/ref/tags/" + tag, missing=True) is None, "release tag already exists"
    )
    # Create the exact source tag first: release --target alone cannot override
    # an existing tag pointing elsewhere. Never move or overwrite any tag.
    reference = _api(
        "git/refs", fields=(("ref", "refs/tags/" + tag), ("sha", revision))
    )
    _require(
        reference.get("object", {}).get("type") == "commit"
        and reference["object"].get("sha") == revision,
        "release tag does not identify the tested revision",
    )
    release = _api(
        "releases",
        fields=(
            ("tag_name", tag),
            ("target_commitish", revision),
            ("name", "Frontend " + revision),
            (
                "body",
                "Tested frontend image archives and their signed manifest. Source: "
                + revision,
            ),
            ("draft", True),
        ),
    )
    identifier = release.get("id")
    _require(
        type(identifier) is int and identifier > 0, "invalid draft release identity"
    )
    paths = [
        directory / name
        for name in (MANIFEST, BUNDLE, "web.oci.tar", "web-nginx.oci.tar")
    ]
    _run(
        ["gh", "release", "upload", tag, "--repo", REPOSITORY, *map(str, paths)],
        timeout=600,
    )
    release = _api(f"releases/{identifier}")
    assets = release.get("assets", [])
    _require(
        release.get("draft") is True
        and release.get("tag_name") == tag
        and len(assets) == len(paths)
        and {x.get("name") for x in assets} == {p.name for p in paths},
        "draft release asset inventory is incomplete",
    )
    for path in paths:
        asset = next(x for x in assets if x["name"] == path.name)
        _require(
            asset.get("size") == path.stat().st_size, "uploaded asset size mismatch"
        )
        if asset.get("digest") is not None:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024**2), b""):
                    digest.update(block)
            _require(
                asset["digest"] == "sha256:" + digest.hexdigest(),
                "uploaded asset digest mismatch",
            )
    reference = _api("git/ref/tags/" + tag)
    _require(reference.get("object", {}).get("sha") == revision, "release tag changed")
    published = _api(
        f"releases/{identifier}",
        fields=(("draft", False), ("make_latest", "false")),
        method="PATCH",
    )
    _require(published.get("draft") is False, "release was not published")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture_parser = commands.add_parser("capture")
    capture_parser.add_argument("--output", required=True)
    export_parser = commands.add_parser("export")
    export_parser.add_argument("--tested-images", required=True)
    export_parser.add_argument("--output", required=True)
    for name in ("verify", "publish"):
        commands.add_parser(name).add_argument("--directory", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            capture(args.output)
        elif args.command == "export":
            export(args.tested_images, args.output)
        elif args.command == "verify":
            verify(args.directory)
        else:
            publish(args.directory)
    except (OSError, ValueError, subprocess.SubprocessError):
        print('{"error":"frontend_release_failed"}', file=sys.stderr)
        return 1
    print(json.dumps({"status": "frontend_" + args.command + "_completed"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
