"""Closed committed-source contexts for the four reviewed product images.

This is neither an image builder nor a release/runtime proof. The pure planner
accepts a Git tree and committed bytes, never the working tree. Dockerfile and
deny-all ignore *bytes* are reviewed pins; this module deliberately does not
interpret arbitrary Docker syntax. New COPY inputs or build recipes require an
explicit contract review. Scope/answer/query-embedding registry images and the
separately verified catalog build are outside this module.

``source_sha256`` is SHA256 of ``private_io.canonical(service_source_map)``. The
map binds every recipe, ignore and COPY input to its Git mode, size and SHA256.
Image labels/IDs only bind caller-supplied inspection to that planned identity;
they cannot by themselves establish that Docker built those bytes or used the
pinned bases. The host must retain the trusted context/build receipts and seal
actual immutable IDs before using an image proof in a client lease.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import time

from search.operations.shared import private_io
from search.operations.shared.bounded_process import captured_pipes
from search.operations.shared.source_identity import require_clean_revision


REVISION = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
LABEL_PREFIX = "io.depositar.ai.source."
MAX_TRACKED_FILES = 4096
MAX_SOURCE_FILES = 160
# One ls-tree plus four HEAD/status clean pairs at the staging boundaries.
MAX_GIT_COMMANDS = MAX_SOURCE_FILES + 10
MAX_FILE_BYTES = 2 * 1024**2
MAX_TOTAL_BYTES = 16 * 1024**2
MAX_GIT_OUTPUT_BYTES = 2 * 1024**2
MAX_ENVIRONMENT_ENTRIES = 128
MAX_ENVIRONMENT_ENTRY_BYTES = 4096
MAX_ENVIRONMENT_BYTES = 64 * 1024
ENVIRONMENT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
COMMAND_TIMEOUT_SECONDS = 15
TOTAL_BUDGET_SECONDS = 180
PYTHON_BASE = (
    "python:3.13.7-slim-bookworm@sha256:"
    "adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d"
)
NODE_BASE = (
    "node:24.14.0-bookworm-slim@sha256:"
    "d8e448a56fc63242f70026718378bd4b00f8c82e78d20eefb199224a4d8e33d8"
)
RERANKER_BASE = (
    "quay.io/jupyter/pytorch-notebook@sha256:"
    "d6084f229d9287b49a82558767ec55b68f9ccfd247bc42590d3f0c580f73182c"
)
NGINX_BASE = (
    "nginx:1.30.4-alpine@sha256:"
    "dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c"
)


def _paths(prefix, names):
    return frozenset(prefix + name for name in names.split())


COMMON = _paths(
    "search/",
    "__init__.py serialization.py secrets.py transport/__init__.py transport/http.py "
    "model_policy.py scope.py",
)
HTTP = _paths(
    "search/http/",
    "__init__.py application.py body.py responses.py settings.py views.py",
)
CATALOG_QUERY = _paths(
    "search/catalog/",
    "__init__.py errors.py policy.py query.py",
)
CATALOG_RETRIEVAL = _paths("search/catalog/", "bundle.py codec.py embedding.py")
WEB_CONTRACT = (
    _paths("search/", "__init__.py serialization.py secrets.py")
    | CATALOG_QUERY
    | _paths("search/api/", "__init__.py")
    | _paths("search/api/conversation/", "__init__.py calendar.py contract.py time.py")
    | _paths("search/api/retrieval/", "__init__.py schemas.py")
    | _paths("search/api/shared/", "__init__.py admission.py schemas.py")
    | _paths("search/planning/", "__init__.py clock.py contract.py predicates.py")
)
API = (
    _paths(
        "search/api/",
        "__init__.py worker.py requirements.lock",
    )
    | _paths(
        "search/api/conversation/",
        "__init__.py app.py calendar.py contract.py engine.py time.py worker.py",
    )
    | _paths(
        "search/api/retrieval/",
        "__init__.py engine.py index.py pipeline.py query.py results.py schemas.py worker.py",
    )
    | _paths(
        "search/api/shared/",
        "__init__.py admission.py backend.py config.py middleware.py model_transport.py peers.py readiness.py schemas.py worker_io.py",
    )
)
PLANNING = _paths(
    "search/planning/",
    "__init__.py clock.py contract.py index_filters.py model.py predicates.py",
)
RERANKER_PY = _paths("search/reranker/", "__init__.py contract.py model.py server.py")
RERANKER = RERANKER_PY | {"search/reranker/requirements.lock"}
WEB = _paths(
    "search/web/",
    "__init__.py app.py identity.py sessions.py upstream.py requirements.lock",
)
ASSETS = (
    _paths(
        "web/", "package.json package-lock.json index.html tsconfig.json vite.config.ts"
    )
    | _paths(
        "web/src/",
        "api.ts env.d.ts main.tsx search-access.tsx search-progress.tsx search-results.tsx styles.css",
    )
    | _paths("web/src/search/", "session.ts")
    | _paths("web/src/assets/", "search-symbol.svg search-logo.svg")
)
API_PATTERNS = (
    "search/http/*.py",
    "search/transport/*.py",
    "search/api/*.py",
    "search/api/conversation/*.py",
    "search/api/retrieval/*.py",
    "search/api/shared/*.py",
    "search/planning/*.py",
    "search/reranker/*.py",
)

# Only these reviewed Dockerfile/ignore versions are understood. Changing a
# pinned recipe without reviewing COPY closure, modes and base pins fails shut.
RECIPES = {
    "conversation-api": {
        "dockerfile": "deploy/docker/api/Dockerfile",
        "dockerfile_sha256": "eb29b30dd2c33010a53a913d022e4f612e4a132a2c95af6cd40e114dff4a64ea",
        "dockerignore_sha256": "5f4d8c2e23458948efff1404b345a595c8ddb72bf6e9721808ac7a1b27ccf3eb",
        "bases": (PYTHON_BASE,),
        "inputs": COMMON
        | HTTP
        | CATALOG_QUERY
        | CATALOG_RETRIEVAL
        | API
        | PLANNING
        | RERANKER_PY
        | {
            "search/api/requirements-haystack.lock",
            "search/operations/__init__.py",
            "search/operations/shared/__init__.py",
            "search/operations/shared/runtime_probe.py",
        },
        "patterns": API_PATTERNS,
    },
    "web": {
        "dockerfile": "deploy/docker/web/Dockerfile",
        "dockerfile_sha256": "84961054ed874eeab4f20da17cbd044288b9f561b5a5380a7336e6ce0f21de21",
        "dockerignore_sha256": "623613abd6969d8aa591bc6f4734a98ac73ce016f0d179a36046b0435a417f89",
        "bases": (PYTHON_BASE,),
        "inputs": WEB_CONTRACT | HTTP | WEB | {"search/api/requirements.lock"},
        "patterns": (
            "search/http/*.py",
            "search/web/*.py",
        ),
    },
    "reranker": {
        "dockerfile": "deploy/docker/reranker/Dockerfile",
        "dockerfile_sha256": "ab1a605bb5985b94cd6596e3fe6d9e418b5b47198f63c3a25f8375436e96aced",
        "dockerignore_sha256": "aa4f80684c05c95a03a45f46714c6ee5c59bbfe620c3e132bc31cd5478738b95",
        "bases": (RERANKER_BASE,),
        "inputs": RERANKER
        | {
            "search/__init__.py",
            "search/serialization.py",
            "search/secrets.py",
        },
        "patterns": ("search/reranker/*.py",),
    },
    "web-nginx": {
        "dockerfile": "deploy/docker/web-nginx/Dockerfile",
        "dockerfile_sha256": "dd5d6dafbcf50965a1a6482d210e0f5f27c8b40fbd61e0012abbc81be5379d17",
        "dockerignore_sha256": "bfb266c8815591f71888618bcb240881e623ec775f16584e138bfbeaba6c2f88",
        "bases": (NODE_BASE, NGINX_BASE),
        "inputs": ASSETS
        | _paths("deploy/config/web-nginx/", "nginx.conf http.conf website.conf"),
        "patterns": (
            "web/src/*.ts",
            "web/src/*.tsx",
            "web/src/*.css",
            "web/src/search/*.ts",
        ),
    },
}


class SourceBundleError(ValueError):
    """Fixed error codes; never include source bytes, paths or Git stderr."""


def _digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def _revision(value):
    if type(value) is not str or REVISION.fullmatch(value) is None:
        raise SourceBundleError("source_bundle_revision_invalid")


def _path(value):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 255
        or value in {".", ".."}
        or PurePosixPath(value).is_absolute()
        or str(PurePosixPath(value)) != value
        or ".." in PurePosixPath(value).parts
        or any(character in value for character in "\x00\r\n\t\\")
    ):
        raise SourceBundleError("source_bundle_path_invalid")


def _matches(path, pattern):
    # Docker's reviewed rules here are single-directory globs. fnmatch alone
    # would incorrectly admit descendants because its '*' also matches '/'.
    return path.count("/") == pattern.count("/") and fnmatch.fnmatchcase(path, pattern)


def _closure(tracked_tree):
    if (
        type(tracked_tree) is not dict
        or not 1 <= len(tracked_tree) <= MAX_TRACKED_FILES
    ):
        raise SourceBundleError("source_bundle_tree_limit")
    for path, entry in tracked_tree.items():
        _path(path)
        if (
            type(entry) is not dict
            or set(entry) != {"mode", "type", "object"}
            or type(entry["mode"]) is not str
            or type(entry["type"]) is not str
            or entry["mode"] not in {"100644", "100755", "120000", "160000"}
            or entry["type"] not in {"blob", "commit"}
            or type(entry["object"]) is not str
            or REVISION.fullmatch(entry["object"]) is None
        ):
            raise SourceBundleError("source_bundle_tree_invalid")
    selected = set()
    for recipe in RECIPES.values():
        selected.update(recipe["inputs"])
        selected.update({recipe["dockerfile"], recipe["dockerfile"] + ".dockerignore"})
        for path in tracked_tree:
            if (
                any(_matches(path, pattern) for pattern in recipe["patterns"])
                and path not in recipe["inputs"]
            ):
                raise SourceBundleError("source_bundle_unreviewed_copy_input")
    if not 1 <= len(selected) <= MAX_SOURCE_FILES or not selected <= set(tracked_tree):
        raise SourceBundleError("source_bundle_copy_closure_incomplete")
    for path in selected:
        if (
            tracked_tree[path]["mode"] not in {"100644", "100755"}
            or tracked_tree[path]["type"] != "blob"
        ):
            raise SourceBundleError("source_bundle_copy_mode_invalid")
    return sorted(selected)


def _record(value):
    if (
        type(value) is not dict
        or set(value) != {"mode", "size", "sha256"}
        or type(value["mode"]) is not str
        or value["mode"] not in {"100644", "100755"}
        or type(value["size"]) is not int
        or not 1 <= value["size"] <= MAX_FILE_BYTES
        or type(value["sha256"]) is not str
        or DIGEST.fullmatch(value["sha256"]) is None
    ):
        raise SourceBundleError("source_bundle_record_invalid")


def _plan(revision, records):
    _revision(revision)
    expected = set().union(
        *(
            recipe["inputs"]
            | {recipe["dockerfile"], recipe["dockerfile"] + ".dockerignore"}
            for recipe in RECIPES.values()
        )
    )
    if (
        type(records) is not dict
        or set(records) != expected
        or len(records) > MAX_SOURCE_FILES
    ):
        raise SourceBundleError("source_bundle_source_map_invalid")
    total = 0
    for record in records.values():
        _record(record)
        total += record["size"]
    if total > MAX_TOTAL_BYTES:
        raise SourceBundleError("source_bundle_total_byte_limit")
    services = {}
    for name, recipe in RECIPES.items():
        dockerfile = recipe["dockerfile"]
        dockerignore = dockerfile + ".dockerignore"
        if any(
            records[path]["sha256"] != recipe[key] or records[path]["mode"] != "100644"
            for path, key in (
                (dockerfile, "dockerfile_sha256"),
                (dockerignore, "dockerignore_sha256"),
            )
        ):
            raise SourceBundleError("source_bundle_recipe_unreviewed")
        source_map = {
            path: dict(records[path])
            for path in sorted(recipe["inputs"] | {dockerfile, dockerignore})
        }
        digest = _digest(source_map)
        services[name] = {
            "dockerfile": dockerfile,
            "dockerignore": dockerignore,
            "copy_inputs": sorted(recipe["inputs"]),
            "base_images": list(recipe["bases"]),
            "source_map": source_map,
            "source_sha256": digest,
            "build_arguments": {"SOURCE_REVISION": revision},
            "required_labels": {
                "org.opencontainers.image.revision": revision,
                LABEL_PREFIX + "revision": revision,
                LABEL_PREFIX + "service": name,
                LABEL_PREFIX + "sha256": digest,
            },
        }
    return {
        "schema_version": 1,
        "status": "source_plan_only",
        "revision": revision,
        "files": {path: dict(records[path]) for path in sorted(records)},
        "source_map_sha256": _digest(records),
        "total_bytes": total,
        "services": services,
        "runtime_verified": False,
        "images_built": False,
    }


def plan_source_bundle(revision, tracked_tree, committed_sources):
    """Plan reviewed COPY closures from explicit committed Git blob bytes.

    ``tracked_tree`` maps paths to exactly ``mode,type,object`` (Git ls-tree
    strings). ``committed_sources`` contains *only* the union of selected files,
    mapping each path to bytes. Blob object IDs are also checked, so caller bytes
    cannot silently differ from the supplied Git tree. Inputs are not authority;
    the host must authenticate the revision/tree before staging or building.
    """
    _revision(revision)
    selected = _closure(tracked_tree)
    if type(committed_sources) is not dict or set(committed_sources) != set(selected):
        raise SourceBundleError("source_bundle_committed_closure_invalid")
    records, total = {}, 0
    for path in selected:
        raw = committed_sources[path]
        if type(raw) is not bytes or not 1 <= len(raw) <= MAX_FILE_BYTES:
            raise SourceBundleError("source_bundle_file_byte_limit")
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise SourceBundleError("source_bundle_total_byte_limit")
        blob = b"blob " + str(len(raw)).encode("ascii") + b"\x00" + raw
        if hashlib.sha1(blob).hexdigest() != tracked_tree[path]["object"]:
            raise SourceBundleError("source_bundle_git_blob_changed")
        records[path] = {
            "mode": tracked_tree[path]["mode"],
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    return _plan(revision, records)


def _verified_plan(plan):
    if type(plan) is not dict or set(plan) != {
        "schema_version",
        "status",
        "revision",
        "files",
        "source_map_sha256",
        "total_bytes",
        "services",
        "runtime_verified",
        "images_built",
    }:
        raise SourceBundleError("source_bundle_plan_invalid")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise SourceBundleError("source_bundle_plan_invalid")
    expected = _plan(plan["revision"], plan["files"])
    # Canonical comparison distinguishes True from 1 and excludes loose numeric
    # equality in caller-supplied records or manifest fields.
    try:
        encoded = private_io.canonical(plan)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise SourceBundleError("source_bundle_plan_invalid") from None
    if encoded != private_io.canonical(expected):
        raise SourceBundleError("source_bundle_plan_changed")
    return expected


def parse_image_environment(entries):
    """Parse bounded Docker Config.Env entries; never echo values on error.

    This shared parser is also suitable for the actual container's Env list.
    The returned values must stay in memory, not reports or source manifests:
    an unexpected image can contain credentials. Parsing is not an attestation;
    the host independently inspects and authenticates the same immutable image.
    """
    if type(entries) is not list or len(entries) > MAX_ENVIRONMENT_ENTRIES:
        raise SourceBundleError("source_bundle_image_environment_invalid")
    result, total = {}, 0
    for entry in entries:
        if (
            type(entry) is not str
            or not 1 <= len(entry) <= MAX_ENVIRONMENT_ENTRY_BYTES
            or any(character in entry for character in "\x00\r\n")
            or "=" not in entry
        ):
            raise SourceBundleError("source_bundle_image_environment_invalid")
        try:
            size = len(entry.encode("utf-8"))
        except UnicodeError:
            raise SourceBundleError("source_bundle_image_environment_invalid") from None
        total += size
        if size > MAX_ENVIRONMENT_ENTRY_BYTES or total > MAX_ENVIRONMENT_BYTES:
            raise SourceBundleError("source_bundle_image_environment_limit")
        key, value = entry.split("=", 1)
        if ENVIRONMENT_KEY.fullmatch(key) is None or key in result:
            raise SourceBundleError("source_bundle_image_environment_invalid")
        result[key] = value
    return result


def _required_environment(service, revision):
    if service == "web-nginx":
        # The reviewed nginx Dockerfile uses ARG/LABEL, not ENV SOURCE_REVISION.
        return {}
    expected = {
        "SOURCE_REVISION": revision,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    if service == "conversation-api":
        expected.update(
            HAYSTACK_TELEMETRY_ENABLED="false",
            HAYSTACK_CONTENT_TRACING_ENABLED="false",
        )
    if service == "reranker":
        expected.update(
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            HF_HUB_DISABLE_TELEMETRY="1",
            USE_TF="0",
            USE_FLAX="0",
            TOKENIZERS_PARALLELISM="false",
            HF_HOME="/tmp/hf",
        )
    return expected


def validate_built_image(plan, service, image_id, inspection):
    """Check supplied image inspection; perform no Docker or build operation.

    The six-field summary includes only a digest of the independently inspected
    image environment, never its values. The private client lease requires a new
    same-immutable-image Env inspection and this digest before exact container
    override matching. A match is not a build attestation: a privileged caller
    can forge labels. The host authenticates its sealed build operation and
    re-inspects at the eventual mutation boundary.
    """
    expected = _verified_plan(plan)
    return _validate_built_image(
        expected, service, expected["revision"], image_id, inspection
    )


def validate_compatible_built_image(
    plan, service, origin_revision, image_id, inspection
):
    """Check current service bytes against an image's ORIGINAL source revision.

    The caller must independently authenticate the origin's sealed source/build
    receipts and prove its service source map equals this committed plan. This
    helper never relabels an image or treats labels as build attestation.
    """
    _revision(origin_revision)
    return _validate_built_image(
        _verified_plan(plan), service, origin_revision, image_id, inspection
    )


def _validate_built_image(expected, service, revision, image_id, inspection):
    if type(service) is not str or service not in RECIPES:
        raise SourceBundleError("source_bundle_service_invalid")
    if type(image_id) is not str or IMAGE_ID.fullmatch(image_id) is None:
        raise SourceBundleError("source_bundle_image_id_invalid")
    if type(inspection) is not dict or inspection.get("Id") != image_id:
        raise SourceBundleError("source_bundle_image_identity_changed")
    config = inspection.get("Config")
    labels = config.get("Labels") if type(config) is dict else None
    required_labels = {
        **expected["services"][service]["required_labels"],
        "org.opencontainers.image.revision": revision,
        LABEL_PREFIX + "revision": revision,
    }
    if (
        type(labels) is not dict
        or len(labels) > 128
        or any(
            type(key) is not str
            or type(value) is not str
            or len(key) + len(value) > 2048
            for key, value in labels.items()
        )
        or any(labels.get(key) != value for key, value in required_labels.items())
    ):
        raise SourceBundleError("source_bundle_image_labels_changed")
    environment = parse_image_environment(config.get("Env"))
    if any(
        environment.get(key) != value
        for key, value in _required_environment(service, revision).items()
    ):
        raise SourceBundleError("source_bundle_image_environment_flags_changed")
    return {
        "schema_version": 1,
        "revision": revision,
        "service": service,
        "source_sha256": expected["services"][service]["source_sha256"],
        "image_id": image_id,
        "image_environment_sha256": _digest(environment),
    }


def _runner(argv, *, timeout):
    """Bound pipes during capture, not after subprocess.run allocated output."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        },
    )
    with captured_pipes(
        process,
        timeout=timeout,
        max_bytes=MAX_GIT_OUTPUT_BYTES,
        timeout_error=lambda: SourceBundleError("source_bundle_git_timeout"),
        output_error=lambda: SourceBundleError("source_bundle_git_output_limit"),
    ) as (remaining, stdout, stderr):
        return subprocess.CompletedProcess(
            argv,
            process.wait(timeout=remaining),
            b"".join(stdout),
            b"".join(stderr),
        )


class _Git:
    def __init__(self, repo, runner):
        self.repo = repo
        self.runner = _runner if runner is None else runner
        self.deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
        self.calls = 0

    def raw(self, argv, *, max_bytes=MAX_GIT_OUTPUT_BYTES):
        # refs/replace must not relabel another tree/blob with the selected SHA.
        # Normalize here so injected runners and shared clean-source probes use
        # the same literal Git-object semantics as the default runner.
        if type(argv) is not list or not argv or argv[0] != "git":
            raise SourceBundleError("source_bundle_git_command_invalid")
        argv = ["git", "--no-replace-objects", *argv[1:]]
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        if self.calls > MAX_GIT_COMMANDS or remaining <= 0:
            raise SourceBundleError("source_bundle_git_budget_exceeded")
        try:
            result = self.runner(argv, timeout=min(COMMAND_TIMEOUT_SECONDS, remaining))
            output = result.stdout
            if type(output) is str:
                output = output.encode("utf-8")
            if result.returncode != 0 or type(output) is not bytes:
                raise SourceBundleError("source_bundle_git_failed")
            if len(output) > max_bytes or time.monotonic() > self.deadline:
                raise SourceBundleError("source_bundle_git_output_limit")
            return output
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise SourceBundleError("source_bundle_git_failed") from None

    def text_runner(self, argv, *, timeout):
        del timeout
        try:
            output = self.raw(argv, max_bytes=64 * 1024).decode("utf-8")
        except UnicodeError:
            raise SourceBundleError("source_bundle_git_failed") from None
        return subprocess.CompletedProcess(argv, 0, output, "")

    def clean(self, revision):
        try:
            actual = require_clean_revision(self.repo, runner=self.text_runner)
        except ValueError:
            raise SourceBundleError("source_bundle_clean_revision_required") from None
        if actual != revision:
            raise SourceBundleError("source_bundle_revision_changed")

    def tree(self, revision):
        raw = self.raw(["git", "-C", str(self.repo), "ls-tree", "-r", "-z", revision])
        if not raw.endswith(b"\x00"):
            raise SourceBundleError("source_bundle_git_tree_invalid")
        entries = raw[:-1].split(b"\x00")
        if not 1 <= len(entries) <= MAX_TRACKED_FILES:
            raise SourceBundleError("source_bundle_tree_limit")
        result = {}
        for row in entries:
            try:
                metadata, path = row.split(b"\t", 1)
                mode, kind, object_id = metadata.decode("ascii").split(" ")
                path = path.decode("utf-8")
            except (UnicodeError, ValueError):
                raise SourceBundleError("source_bundle_git_tree_invalid") from None
            if path in result:
                raise SourceBundleError("source_bundle_git_tree_invalid")
            result[path] = {"mode": mode, "type": kind, "object": object_id}
        return result


def _write_source(parent_fd, name, raw, record):
    private_io.write_file(parent_fd, name, raw, max_bytes=MAX_FILE_BYTES)
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise SourceBundleError("source_bundle_staged_file_unsafe")
        # Docker COPY preserves host modes. Nonsecret committed source needs to
        # remain readable by the image's nonroot API process after COPY.
        os.fchmod(descriptor, 0o755 if record["mode"] == "100755" else 0o644)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if private_io.identity(after) != private_io.identity(named) or (
            before.st_dev,
            before.st_ino,
        ) != (after.st_dev, after.st_ino):
            raise SourceBundleError("source_bundle_staged_file_changed")
    finally:
        os.close(descriptor)
    os.fsync(parent_fd)


def _create_source_directory(parent_fd, name):
    """Set nonsecret COPY-directory modes explicitly, independent of umask."""
    descriptor = private_io.open_child_directory(parent_fd, name, create=True)
    try:
        os.fchmod(descriptor, 0o755)
        os.fsync(descriptor)
        actual = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            actual.st_uid != os.geteuid()
            or stat.S_IMODE(actual.st_mode) != 0o755
            or private_io.identity(actual) != private_io.identity(named)
        ):
            raise SourceBundleError("source_bundle_staged_directory_changed")
        os.fsync(parent_fd)
        result, descriptor = descriptor, None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verify_context(descriptor, records, *, prefix=""):
    expected_names = {
        path[len(prefix) :].split("/", 1)[0]
        for path in records
        if path.startswith(prefix)
    }
    if set(os.listdir(descriptor)) != expected_names:
        raise SourceBundleError("source_bundle_staged_closure_changed")
    for name in sorted(expected_names):
        path = prefix + name
        if path in records:
            record = records[path]
            raw = private_io.read_file(
                descriptor,
                name,
                mode=0o755 if record["mode"] == "100755" else 0o644,
                max_bytes=MAX_FILE_BYTES,
            )
            if (
                len(raw) != record["size"]
                or hashlib.sha256(raw).hexdigest() != record["sha256"]
            ):
                raise SourceBundleError("source_bundle_staged_source_changed")
        else:
            child = private_io.open_child_directory(descriptor, name, mode=0o755)
            try:
                _verify_context(child, records, prefix=path + "/")
            finally:
                os.close(child)


def _repository_path(repo_root):
    repo = Path(repo_root)
    if (
        not repo.is_absolute()
        or repo == Path("/")
        or repo.resolve() != repo
        or ".." in repo.parts
    ):
        raise SourceBundleError("source_bundle_host_path_invalid")
    return repo


def _committed_sources(git, revision):
    """One shared bounded Git/COPY closure reader for planning and staging."""
    git.clean(revision)
    tree = git.tree(revision)
    sources, total = {}, 0
    for path in _closure(tree):
        raw = git.raw(
            ["git", "-C", str(git.repo), "show", revision + ":" + path],
            max_bytes=MAX_FILE_BYTES,
        )
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise SourceBundleError("source_bundle_total_byte_limit")
        sources[path] = raw
    plan = plan_source_bundle(revision, tree, sources)
    git.clean(revision)
    return plan, sources


def committed_source_plan(repo_root, verified_clean_sha, runner=None):
    """Authenticate one clean literal Git revision's reviewed source closure.

    This read-only independent plan can bind a previously staged manifest before
    any Docker command. It never reads working-tree bytes or creates artifacts.
    Git replacement refs cannot relabel other tree/blob bytes with this SHA.
    """
    _revision(verified_clean_sha)
    git = _Git(_repository_path(repo_root), runner)
    plan, _ = _committed_sources(git, verified_clean_sha)
    return plan


def stage_source_bundle(repo_root, verified_clean_sha, private_root, runner=None):
    """Stage one fresh private context, with clean-SHA checks before and after.

    The existing parent must be operator-owned 0700. A root/partial root is never
    reused, overwritten or removed, including on failure; retained artifacts are
    evidence, not a resumable successful context. Nested source directories are
    0755 inside the private 0700 context root; source files retain Git 0644/0755.
    The manifest is 0600 outside the Docker context. No working-tree source bytes
    or private configuration are read, and no Docker command is run.

    An injected runner(argv, *, timeout) returns CompletedProcess with bytes or
    text stdout and is trusted to obey its own capture/timeout bounds. The default
    runner bounds live capture, process-group cleanup and the whole operation.
    """
    _revision(verified_clean_sha)
    repo, destination = _repository_path(repo_root), Path(private_root)
    if (
        not destination.is_absolute()
        or destination == Path("/")
        or destination.resolve() != destination
        or ".." in destination.parts
    ):
        raise SourceBundleError("source_bundle_host_path_invalid")
    git = _Git(repo, runner)
    plan, sources = _committed_sources(git, verified_clean_sha)
    parent = private_io.open_directory(destination.parent)
    root = context = None
    directories = {}
    try:
        root = private_io.open_child_directory(parent, destination.name, create=True)
        context = private_io.open_child_directory(root, "context", create=True)
        directories[""] = context
        for path, raw in sources.items():
            parts = path.split("/")
            prefix = ""
            for part in parts[:-1]:
                child_prefix = prefix + part + "/"
                if child_prefix not in directories:
                    directories[child_prefix] = _create_source_directory(
                        directories[prefix], part
                    )
                prefix = child_prefix
            _write_source(directories[prefix], parts[-1], raw, plan["files"][path])
        private_io.verify_directory(parent, destination.parent)
        private_io.verify_directory(root, destination)
        private_io.verify_directory(context, destination / "context")
        _verify_context(context, plan["files"])
        git.clean(verified_clean_sha)
        private_io.write_json(root, "manifest.json", plan, max_bytes=1024**2)
        private_io.verify_directory(parent, destination.parent)
        private_io.verify_directory(root, destination)
        private_io.verify_directory(context, destination / "context")
        _verify_context(context, plan["files"])
        git.clean(verified_clean_sha)
        return plan
    except (OSError, ValueError) as error:
        if isinstance(error, SourceBundleError):
            raise
        raise SourceBundleError("source_bundle_staging_failed") from None
    finally:
        for prefix, descriptor in directories.items():
            if prefix:
                os.close(descriptor)
        if context is not None:
            os.close(context)
        if root is not None:
            os.close(root)
        os.close(parent)
