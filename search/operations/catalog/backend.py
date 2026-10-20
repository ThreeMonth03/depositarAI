"""Prepare an isolated CPU catalog slot; never start or switch a live service.

Bootstrap material and the Compose policy are sealed from an exact Git revision.
No research recovery file or host-specific image ID is accepted. Bootstrap may
use an isolated password-hash helper, but the retrieval services are not started.
"""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import re
import stat
import types

from search.catalog.build import storage as build_storage
from search.catalog.bundle import CatalogBundle
from search.operations.catalog.commands import (
    BackendError as BackendError,
    MAX_OUTPUT_BYTES as MAX_OUTPUT_BYTES,
    command_output as _command,
    default_runner as _runner,
)
from search.operations.config import ProductConfig, validate_config
from search.operations.metadata import source as metadata_source, storage
from search.operations.shared import private_io


PROJECT = re.compile(r"depositar-ai-catalog-[0-9a-f]{12}-[0-9a-f]{12}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
HASH_IMAGE_PREFIX = "depositar-opensearch-search-hash:"
APPROVED_OPENSEARCH_BASE = (
    "ghcr.io/codelibs/fess-opensearch:3.8.0@sha256:"
    "c59dd34da6d0c6e0117b8851ded0832f168de9b33e275e62cd3ef3139362d7fc"
)
MAX_SLOTS = 16
STAGED_FILES = (
    "scripts/bootstrap-catalog.sh",
    "scripts/search-lib.sh",
    "scripts/read_env.py",
    "scripts/search_dictionary_permissions.py",
    "scripts/search_model_source.py",
    "deploy/env/catalog.env.example",
    "deploy/config/opensearch/opensearch.yml",
    "deploy/config/opensearch/internal_users.yml.in",
    "deploy/config/opensearch/roles.yml",
    "deploy/config/opensearch/roles_mapping.yml",
    "deploy/compose/compose.catalog.yaml",
    "scripts/validate_catalog_compose.py",
)
BUILD_FILES = (
    "deploy/docker/fess/Dockerfile",
    "deploy/docker/fess/Dockerfile.dockerignore",
    "deploy/docker/fess/entrypoint.sh",
    "deploy/docker/fess/fess_healthcheck.sh",
    "deploy/config/fess/scheduled_job.bulk",
    "deploy/docker/opensearch/Dockerfile",
    "deploy/docker/opensearch/.dockerignore",
    "deploy/docker/opensearch/opensearch_healthcheck.py",
)
# These bytes determine the persisted documents, chunks, vectors, projection
# and access policy. App/UI/gateway commits do not change this recipe. Keep the
# original catalog revision on plans, images and journals when reusing data.
DATA_FILES = (
    "deploy/docker/importer/Dockerfile",
    "deploy/docker/importer/Dockerfile.dockerignore",
    "search/catalog/build/__init__.py",
    "search/catalog/build/__main__.py",
    "search/catalog/build/builder.py",
    "search/catalog/build/cli.py",
    "search/catalog/build/codec.py",
    "search/catalog/build/identity.py",
    "search/catalog/build/normalize.py",
    "search/catalog/build/policy.py",
    "search/catalog/build/snapshot.py",
    "search/catalog/build/storage.py",
    "search/__init__.py",
    "search/serialization.py",
    "search/operations/__init__.py",
    "search/operations/shared/__init__.py",
    "search/catalog/__init__.py",
    "search/catalog/errors.py",
    "search/catalog/policy.py",
    "search/catalog/codec.py",
    "search/catalog/transport.py",
    "search/catalog/session.py",
    "search/catalog/admin.py",
    "search/catalog/bundle.py",
    "search/catalog/inventory.py",
    "search/catalog/importer.py",
    "search/catalog/journal.py",
    "search/catalog/vectors.py",
    "search/catalog_admin.py",
    "search/vector_journal.py",
    "search/operations/shared/private_io.py",
    "search/operations/catalog/administration.py",
    "search/operations/catalog/vectors.py",
    "search/catalog/embedding.py",
    "search/operations/catalog/projection.py",
    "search/operations/catalog/access.py",
    "search/planning/index_filters.py",
    "search/planning/predicates.py",
)
SECRET_NAMES = (
    "opensearch_admin_password",
    "fess_opensearch_password",
    "fess_app_cipher_key",
    "fess_initial_admin_password",
)
SECURITY_FILES = (
    "opensearch.yml",
    "root-ca.pem",
    "node.pem",
    "node-key.pem",
    "internal_users.yml",
    "roles.yml",
    "roles_mapping.yml",
)
MAX_SECURITY_BYTES = 64 * 1024


class IncompatibleCatalog(BackendError):
    """An intact catalog belongs to a different data recipe: a cache miss."""


def _recipe_source(relative, raw):
    # Vector orchestration may improve without changing any generated data.
    # _recipe contains its complete fixed profile; its external dependencies
    # (Compose and embedding identity) are separately sealed in this closure.
    if relative != "search/operations/catalog/vectors.py":
        return raw
    functions = [
        node
        for node in ast.parse(raw).body
        if isinstance(node, ast.FunctionDef) and node.name == "_recipe"
    ]
    if len(functions) != 1:
        raise BackendError("catalog vector recipe source is invalid")
    return ast.dump(functions[0], include_attributes=False).encode("utf-8")


def validate_compatibility(plan, *, repo_root, revision, runner=None):
    """Bind an older catalog to current committed data-producing bytes.

    This grants source compatibility only. Loading the sealed plan and proving
    the actual images, runtime, inventory and ACL remain separate requirements.
    Missing Git history is an error, never permission to trust old labels.
    """
    origin = plan.get("revision") if type(plan) is dict else None
    if (
        type(origin) is not str
        or REVISION.fullmatch(origin) is None
        or type(revision) is not str
        or REVISION.fullmatch(revision) is None
    ):
        raise BackendError("catalog compatibility revision is invalid")
    if origin == revision:
        return
    runner = _runner(repo_root) if runner is None else runner
    # A readable tree may predate a newly required recipe input. That is an
    # incompatible recipe, unlike missing Git history or an unreadable blob.
    original_paths = set(
        _command(
            runner,
            ["git", "-C", str(repo_root), "ls-tree", "-r", "-z", "--name-only", origin],
        ).split("\0")
    )
    for relative in (*STAGED_FILES, *BUILD_FILES, *DATA_FILES):
        current = _source_bytes(Path(repo_root) / relative)
        committed = _command(
            runner, ["git", "-C", str(repo_root), "show", revision + ":" + relative]
        ).encode("utf-8")
        if current != committed:
            raise BackendError("commit catalog compatibility source changes first")
        if relative not in original_paths:
            raise IncompatibleCatalog("catalog data recipe changed; rebuild required")
        original = _command(
            runner, ["git", "-C", str(repo_root), "show", origin + ":" + relative]
        ).encode("utf-8")
        if _recipe_source(relative, original) != _recipe_source(relative, committed):
            raise IncompatibleCatalog("catalog data recipe changed; rebuild required")


def _compatible_slot(
    catalogs_fd, catalog, bundle_path, manifest_sha256, *, repo_root, revision, runner
):
    """Find retained data without rewriting its provenance or probing Docker."""
    names = os.listdir(catalogs_fd)
    if len(names) > MAX_SLOTS:
        raise BackendError("catalog slot limit exceeded")
    bundle_id = catalog.bundle_id
    if bundle_id[:12] + "-" + revision[:12] in names:
        return revision
    for name in sorted(names):
        if re.fullmatch(bundle_id[:12] + r"-[0-9a-f]{12}", name) is None:
            continue
        descriptor = storage.private_directory(catalogs_fd, name)
        try:
            entries = os.listdir(descriptor)
            if "failure.json" in entries or "plan.json" not in entries:
                continue
            plan = storage.read_record(descriptor, "plan.json")
            # A prefix match is only a lookup hint; the full bundle/manifest and
            # every sealed path are authenticated by _prepare_or_load below.
            if (
                type(plan) is not dict
                or type(plan.get("revision")) is not str
                or REVISION.fullmatch(plan["revision"]) is None
                or name != bundle_id[:12] + "-" + plan["revision"][:12]
            ):
                raise BackendError("existing catalog plan identity mismatch")
            if (
                plan.get("bundle_path") != str(bundle_path)
                or plan.get("manifest_sha256") != manifest_sha256
                or plan.get("document_count") != len(catalog.documents)
                or "runtime.json" not in entries
                or not _completed_slot_hint(descriptor, len(catalog.documents))
            ):
                continue
            try:
                validate_compatibility(
                    plan, repo_root=repo_root, revision=revision, runner=runner
                )
            except IncompatibleCatalog:
                continue
            return plan["revision"]
        finally:
            os.close(descriptor)
    return revision


def _completed_slot_hint(work_fd, count):
    """Only select complete histories; the vector gate later proves every byte."""
    try:
        vectors = storage.private_directory(work_fd, "vector-operations")
    except FileNotFoundError:
        return False
    try:
        names = os.listdir(vectors)
        if not 2 <= len(names) <= 129 or "recipe.json" not in names:
            return False
        complete = False
        for name in names:
            if name == "recipe.json":
                continue
            if re.fullmatch(r"[0-9a-f]{32}", name) is None:
                return False
            run = storage.private_directory(vectors, name)
            try:
                if set(os.listdir(run)) != {
                    "started.json",
                    "container.json",
                    "complete.json",
                    "events",
                }:
                    return False
                record = storage.read_record(run, "complete.json")
                result = record.get("result") if type(record) is dict else None
                if (
                    type(result) is dict
                    and result.get("status") == "complete-clean"
                    and type(result.get("done_count")) is int
                    and result["done_count"] == count
                    and type(result.get("pending_count")) is int
                    and result["pending_count"] == 0
                ):
                    complete = True
            finally:
                os.close(run)
        return complete
    finally:
        os.close(vectors)


def _sealed_sources(repo_root, revision, runner):
    sources = {}
    for relative in (*STAGED_FILES, *BUILD_FILES):
        path = storage.real_path(repo_root / relative)
        current = _source_bytes(path)
        committed = _command(
            runner, ["git", "-C", str(repo_root), "show", revision + ":" + relative]
        ).encode("utf-8")
        if current != committed:
            raise BackendError(
                "commit catalog preparation source changes before deployment"
            )
        sources[relative] = committed
    return sources


def _source_bytes(path):
    """Repository files may be root-owned; pin the leaf before inspecting it."""
    parent_fd = build_storage._open_directory_chain(
        str(path.parent), "repository source"
    )
    fd = None
    try:
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in {0, os.geteuid()}
            or before.st_mode & 0o022
            or not 1 <= before.st_size <= MAX_OUTPUT_BYTES
        ):
            raise BackendError("unsafe repository source file")
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, MAX_OUTPUT_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_OUTPUT_BYTES:
                raise BackendError("repository source exceeds its byte limit")
        after = os.fstat(fd)
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            size != before.st_size
            or not storage.same_file(before, after)
            or not storage.same_file(after, current)
        ):
            raise BackendError("repository source changed while being read")
        return b"".join(chunks)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def validate_compose(
    config,
    *,
    repo_root,
    data_root,
    project,
    uid,
    gid,
    policy_source,
    sealed_build_root=None,
):
    """Validate sealed catalog policy and the isolated slot source paths."""
    if PROJECT.fullmatch(project) is None or config.get("name") != project:
        raise BackendError("catalog slot project identity mismatch")
    networks = config.get("networks")
    network = networks.get("search_backend") if type(networks) is dict else None
    if (
        type(networks) is not dict
        or set(networks) != {"search_backend"}
        or type(network) is not dict
        or not {"name", "internal"} <= set(network)
        or not set(network) <= {"name", "internal", "attachable", "ipam"}
        or network.get("name") != project + "_search_backend"
        or network.get("internal") is not True
        or ("attachable" in network and network["attachable"] is not False)
        or ("ipam" in network and network["ipam"] != {})
    ):
        raise BackendError(
            "catalog slot network must be unique, internal and non-external"
        )
    module = types.ModuleType("depositar_sealed_catalog_policy")
    module.__file__ = str(repo_root / "scripts/validate_catalog_compose.py")
    exec(compile(policy_source, module.__file__, "exec"), module.__dict__)
    if sealed_build_root is not None:
        # Retained Compose records refer to the original source checkout.
        # This is metadata only: actual builds use our sealed minimal context,
        # and startup uses immutable images with --no-build. Never rewrite the
        # existing Compose or treat this path as the current source authority.
        if (
            type(sealed_build_root) is not str
            or not Path(sealed_build_root).is_absolute()
            or ".." in Path(sealed_build_root).parts
            or str(Path(sealed_build_root)) != sealed_build_root
        ):
            raise BackendError("sealed catalog build root is invalid")
        module.PROJECT_ROOT = Path(sealed_build_root)
    module.validate(config)
    for service in config["services"].values():
        if (
            service.get("ports")
            or service.get("privileged")
            or service.get("user") != f"{uid}:{gid}"
            or service.get("deploy", {})
            .get("resources", {})
            .get("reservations", {})
            .get("devices")
            or service.get("devices")
            or service.get("gpus")
        ):
            raise BackendError(
                "catalog slot must remain CPU-only, non-root and unpublished"
            )
        for mount in service.get("volumes", []):
            if mount["type"] == "volume":
                # The closed policy above permits only the fixed index volume.
                continue
            path = Path(mount["source"])
            if data_root not in path.parents:
                raise BackendError("catalog slot mount escapes its isolated data root")
    if str(module.derive_catalog_root(config["services"]["opensearch-search"])) != str(
        data_root
    ):
        raise BackendError("catalog slot data root changed")
    for secret in config.get("secrets", {}).values():
        if data_root not in Path(secret["file"]).parents:
            raise BackendError(
                "catalog slot secret points outside its isolated data root"
            )


def _write_file(path, content):
    storage.real_path(path)
    parent, missing = path.parent, []
    while not parent.exists():
        missing.append(parent.name)
        parent = parent.parent
    parent_fd = private_io.open_directory(parent)
    try:
        for name in reversed(missing):
            child = private_io.open_child_directory(parent_fd, name, create=True)
            os.close(parent_fd)
            parent_fd = child
        fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _staged_bytes(tooling, relative):
    fd = private_io.open_directory(tooling)
    try:
        parts = Path(relative).parts
        for name in parts[:-1]:
            child = storage.private_directory(fd, name)
            os.close(fd)
            fd = child
        return storage.file_bytes(fd, parts[-1], mode=0o600, limit=MAX_OUTPUT_BYTES)
    finally:
        os.close(fd)


def _verify_staged_sources(tooling, sources):
    for name in (*STAGED_FILES, *BUILD_FILES):
        if _staged_bytes(tooling, name) != sources[name]:
            raise BackendError("sealed bootstrap source changed")


def _build_hash_image(tooling, sources, revision, runner):
    """Build only the reviewed, sealed OpenSearch context; never use host IDs."""
    _verify_staged_sources(tooling, sources)
    image = HASH_IMAGE_PREFIX + revision
    context = tooling / "deploy/docker/opensearch"
    _command(
        runner,
        [
            "docker",
            "build",
            "--network=none",
            "--pull=false",
            "--label",
            "org.opencontainers.image.revision=" + revision,
            "--build-arg",
            "OPENSEARCH_BASE_IMAGE=" + APPROVED_OPENSEARCH_BASE,
            "--tag",
            image,
            "--file",
            str(context / "Dockerfile"),
            str(context),
        ],
        timeout=300,
    )
    inspected = _command(
        runner,
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{"id":{{json .Id}},"revision":{{json (index .Config.Labels '
            '"org.opencontainers.image.revision")}}}',
            image,
        ],
    )
    metadata = metadata_source.strict_json(inspected.encode("utf-8"))
    if (
        type(metadata) is not dict
        or set(metadata) != {"id", "revision"}
        or metadata.get("revision") != revision
        or type(metadata.get("id")) is not str
        or IMAGE_ID.fullmatch(metadata["id"]) is None
    ):
        raise BackendError("password-hash image provenance mismatch")
    _verify_staged_sources(tooling, sources)
    return image, metadata["id"]


def _environment(template, *, data_root, config, uid, gid, project):
    if PROJECT.fullmatch(project) is None:
        raise BackendError("bootstrap project must be a qualified catalog slot")
    replacements = {
        "COMPOSE_PROJECT_NAME": project,
        "SEARCH_CATALOG_DATA_ROOT": str(data_root),
        "SEARCH_CATALOG_STORAGE_ALLOWED_ROOT": str(config.state_root),
        "SEARCH_CATALOG_STORAGE_REQUIRED_FS_TYPES": "",
        "SEARCH_CATALOG_RUNTIME_UID": str(uid),
        "SEARCH_CATALOG_RUNTIME_GID": str(gid),
        "SEARCH_CATALOG_MODEL_SOURCE": str(config.embedding_model_root),
    }
    found = set()
    lines = []
    for line in template.decode("utf-8").splitlines():
        key = line.split("=", 1)[0]
        if key in replacements:
            if key in found:
                raise BackendError("duplicate generated bootstrap setting")
            found.add(key)
            line = key + "=" + replacements[key]
        lines.append(line)
    if found != set(replacements):
        raise BackendError("bootstrap template is missing a required setting")
    if any(re.search(r"[\s$`,'\"\\]", value) for value in replacements.values()):
        raise BackendError("bootstrap paths contain unsupported characters")
    return ("\n".join(lines) + "\n").encode("utf-8")


def security_material_digest(data_root):
    """Seal actual small credential/config bytes, exposing one aggregate only.

    Bootstrap establishes certificate/hash validity. This read-only baseline
    detects valid-shaped secret replacement with an unchanged manifest. Model
    blobs and mutable index/dictionary files are deliberately excluded.
    """
    descriptors = []
    try:
        root_fd = private_io.open_directory(data_root)
        descriptors.append(root_fd)
        secret_fd = private_io.open_child_directory(root_fd, "secrets")
        descriptors.append(secret_fd)
        search_fd = private_io.open_child_directory(root_fd, "opensearch")
        descriptors.append(search_fd)
        config_fd = private_io.open_child_directory(search_fd, "config")
        descriptors.append(config_fd)
        if set(os.listdir(secret_fd)) != set(SECRET_NAMES) or set(
            os.listdir(config_fd)
        ) != {*SECURITY_FILES, "security-material.sha256"}:
            raise BackendError("catalog security material inventory changed")
        entries = [
            (secret_fd, "secrets/" + name, name, 0o600, 65) for name in SECRET_NAMES
        ]
        entries.extend(
            (config_fd, "opensearch/config/" + name, name, 0o400, MAX_SECURITY_BYTES)
            for name in SECURITY_FILES
        )
        entries.append(
            (
                config_fd,
                "opensearch/config/security-material.sha256",
                "security-material.sha256",
                0o600,
                MAX_SECURITY_BYTES,
            )
        )
        fingerprints = {}
        for fd, relative, name, mode, limit in entries:
            raw = private_io.read_file(fd, name, mode=mode, max_bytes=limit)
            fingerprints[relative] = hashlib.sha256(raw).hexdigest()
        for fd, relative, name, mode, limit in entries:
            raw = private_io.read_file(fd, name, mode=mode, max_bytes=limit)
            if hashlib.sha256(raw).hexdigest() != fingerprints[relative]:
                raise BackendError("catalog security material changed while reading")
        private_io.verify_directory(root_fd, data_root)
        private_io.verify_directory(secret_fd, data_root / "secrets")
        private_io.verify_directory(search_fd, data_root / "opensearch")
        private_io.verify_directory(config_fd, data_root / "opensearch/config")
        return hashlib.sha256(
            private_io.canonical({"schema_version": 1, "files": fingerprints})
        ).hexdigest()
    except (OSError, ValueError) as error:
        raise BackendError("catalog security material is unsafe or changed") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _prepare_or_load(
    config,
    *,
    repo_root,
    bundle_path,
    manifest_sha256,
    revision,
    runner,
    allow_create,
    catalog_revision=None,
):
    """Share all sealed-source checks; readonly mode cannot create a slot."""
    if (
        not isinstance(config, ProductConfig)
        or not isinstance(revision, str)
        or REVISION.fullmatch(revision) is None
    ):
        raise BackendError("invalid product configuration or source revision")
    repo_root = storage.real_path(repo_root)
    config = validate_config(config.to_dict(), repo_root=repo_root)
    if os.geteuid() == 0 or os.getegid() == 0:
        raise BackendError("catalog preparation requires the non-root product operator")
    bundle_path = storage.real_path(bundle_path)
    catalog = CatalogBundle.load(bundle_path, expected_manifest_sha256=manifest_sha256)
    runner = _runner(repo_root) if runner is None else runner
    sources = _sealed_sources(repo_root, revision, runner)
    if catalog_revision is not None:
        validate_compatibility(
            {"revision": catalog_revision},
            repo_root=repo_root,
            revision=revision,
            runner=runner,
        )
        revision = catalog_revision
    else:
        state = private_io.open_directory(config.state_root)
        catalogs = None
        try:
            try:
                catalogs = storage.private_directory(state, "catalogs")
            except FileNotFoundError:
                pass
            else:
                revision = _compatible_slot(
                    catalogs,
                    catalog,
                    bundle_path,
                    manifest_sha256,
                    repo_root=repo_root,
                    revision=revision,
                    runner=runner,
                )
            private_io.verify_directory(state, config.state_root)
        finally:
            if catalogs is not None:
                os.close(catalogs)
            os.close(state)
    work_name = catalog.bundle_id[:12] + "-" + revision[:12]
    project = "depositar-ai-catalog-" + work_name
    work_root = config.state_root / "catalogs" / work_name
    data_root, tooling = work_root / "data", work_root / "tooling"
    compose_file = work_root / "compose.json"
    identity = {
        "schema_version": 1,
        "project": project,
        "work_root": str(work_root),
        "data_root": str(data_root),
        "tooling_root": str(tooling),
        "compose_file": str(compose_file),
        "bundle_path": str(bundle_path),
        "manifest_sha256": manifest_sha256,
        "revision": revision,
        "document_count": len(catalog.documents),
        "status": "prepared",
        "source_sha256": {
            name: hashlib.sha256(raw).hexdigest() for name, raw in sources.items()
        },
    }
    state_fd = private_io.open_directory(config.state_root)
    catalogs_fd = None
    work_fd = None
    try:
        try:
            catalogs_fd = storage.private_directory(state_fd, "catalogs")
        except FileNotFoundError:
            if not allow_create:
                raise BackendError(
                    "catalog must be prepared before it can be loaded"
                ) from None
            catalogs_fd = storage.private_directory(state_fd, "catalogs", create=True)
        if work_name in os.listdir(catalogs_fd):
            work_fd = storage.private_directory(catalogs_fd, work_name)
            if "failure.json" in os.listdir(work_fd):
                raise BackendError(
                    "catalog slot is quarantined; inspect it before reuse"
                )
            plan = storage.read_record(work_fd, "plan.json")
            if (
                type(plan) is not dict
                or set(plan)
                != {*identity, "compose_sha256", "security_material_sha256"}
                or type(plan.get("schema_version")) is not int
                or type(plan.get("document_count")) is not int
                or any(plan.get(key) != value for key, value in identity.items())
            ):
                raise BackendError("existing catalog plan identity mismatch")
            data_fd = private_io.open_directory(data_root)
            os.close(data_fd)
            if plan.get("security_material_sha256") != security_material_digest(
                data_root
            ):
                raise BackendError("sealed catalog security material digest mismatch")
            compose = storage.read_record(work_fd, "compose.json")
            if hashlib.sha256(
                metadata_source.canonical(compose) + b"\n"
            ).hexdigest() != plan.get("compose_sha256"):
                raise BackendError("sealed catalog Compose digest mismatch")
            validate_compose(
                compose,
                repo_root=repo_root,
                data_root=data_root,
                project=project,
                uid=os.geteuid(),
                gid=os.getegid(),
                policy_source=sources["scripts/validate_catalog_compose.py"],
                sealed_build_root=compose.get("services", {})
                .get("fess-search", {})
                .get("build", {})
                .get("context"),
            )
            _verify_staged_sources(tooling, sources)
            env = _environment(
                sources["deploy/env/catalog.env.example"],
                data_root=data_root,
                config=config,
                uid=os.geteuid(),
                gid=os.getegid(),
                project=project,
            )
            if _staged_bytes(tooling, ".env.catalog") != env:
                raise BackendError("sealed bootstrap environment changed")
            private_io.verify_directory(state_fd, config.state_root)
            private_io.verify_directory(catalogs_fd, config.state_root / "catalogs")
            private_io.verify_directory(work_fd, work_root)
            return plan
        if not allow_create:
            raise BackendError("catalog must be prepared before it can be loaded")
        if len(os.listdir(catalogs_fd)) >= MAX_SLOTS:
            raise BackendError(
                "catalog slot limit reached; archive retained slots first"
            )
        work_fd = storage.private_directory(catalogs_fd, work_name, create=True)
        storage.write_record(work_fd, "started.json", identity)
        phase = "stage_sources"
        try:
            tooling_fd = storage.private_directory(work_fd, "tooling", create=True)
            os.close(tooling_fd)
            for name in (*STAGED_FILES, *BUILD_FILES):
                _write_file(tooling / name, sources[name])
            _write_file(
                tooling / ".env.catalog",
                _environment(
                    sources["deploy/env/catalog.env.example"],
                    data_root=data_root,
                    config=config,
                    uid=os.geteuid(),
                    gid=os.getegid(),
                    project=project,
                ),
            )
            phase = "build_hash_helper"
            hash_image, hash_image_id = _build_hash_image(
                tooling, sources, revision, runner
            )
            storage.write_record(
                work_fd,
                "hash-image.json",
                {
                    "image_id": hash_image_id,
                    "reference": hash_image,
                    "revision": revision,
                    "sources": {
                        name: hashlib.sha256(sources[name]).hexdigest()
                        for name in BUILD_FILES
                        if name.startswith("deploy/docker/opensearch/")
                    },
                    "base": APPROVED_OPENSEARCH_BASE,
                },
            )
            phase = "bootstrap"
            _command(
                runner,
                [
                    "bash",
                    "--noprofile",
                    "--norc",
                    str(tooling / "scripts/bootstrap-catalog.sh"),
                    "--isolated-project",
                    project,
                    "--hash-image",
                    hash_image,
                    "--hash-image-id",
                    hash_image_id,
                ],
                timeout=900,
            )
            data_fd = private_io.open_directory(data_root)
            os.close(data_fd)
            phase = "render_compose"
            rendered = _command(
                runner,
                [
                    "docker",
                    "compose",
                    "--project-name",
                    project,
                    "--project-directory",
                    str(repo_root / "deploy/compose"),
                    "--env-file",
                    str(tooling / ".env.catalog"),
                    "-f",
                    str(tooling / "deploy/compose/compose.catalog.yaml"),
                    "config",
                    "--format",
                    "json",
                ],
            )
            compose = metadata_source.strict_json(rendered.encode("utf-8"))
            phase = "validate_compose"
            validate_compose(
                compose,
                repo_root=repo_root,
                data_root=data_root,
                project=project,
                uid=os.geteuid(),
                gid=os.getegid(),
                policy_source=sources["scripts/validate_catalog_compose.py"],
            )
            private_io.verify_directory(state_fd, config.state_root)
            private_io.verify_directory(catalogs_fd, config.state_root / "catalogs")
            private_io.verify_directory(work_fd, work_root)
            storage.write_record(work_fd, "compose.json", compose)
            plan = {
                **identity,
                "security_material_sha256": security_material_digest(data_root),
                "compose_sha256": hashlib.sha256(
                    metadata_source.canonical(compose) + b"\n"
                ).hexdigest(),
            }
            storage.write_record(work_fd, "plan.json", plan)
            return plan
        except Exception as error:
            storage.write_record(
                work_fd,
                "failure.json",
                {
                    "schema_version": 1,
                    "status": "quarantined",
                    "error_type": type(error).__name__,
                    "phase": phase,
                    "active_modified": False,
                },
            )
            raise
    finally:
        if work_fd is not None:
            os.close(work_fd)
        if catalogs_fd is not None:
            os.close(catalogs_fd)
        os.close(state_fd)


def prepare(
    config,
    *,
    repo_root,
    bundle_path,
    manifest_sha256,
    revision,
    runner=None,
    catalog_revision=None,
):
    """Seal/bootstrap/render one new isolated slot; return a verified plan."""
    return _prepare_or_load(
        config,
        repo_root=repo_root,
        bundle_path=bundle_path,
        manifest_sha256=manifest_sha256,
        revision=revision,
        runner=runner,
        allow_create=True,
        catalog_revision=catalog_revision,
    )


def load_prepared(
    config,
    *,
    repo_root,
    bundle_path,
    manifest_sha256,
    revision,
    runner=None,
    catalog_revision=None,
):
    """Reverify an existing plan without bootstrap, render, writes or creation."""
    return _prepare_or_load(
        config,
        repo_root=repo_root,
        bundle_path=bundle_path,
        manifest_sha256=manifest_sha256,
        revision=revision,
        runner=runner,
        allow_create=False,
        catalog_revision=catalog_revision,
    )
