"""Verify a trusted product payload and restore source, without using Docker.

Large assets remain in the original private package, an explicit dependency
which a later installer must revalidate. This is never installed authority.
"""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import stat
import sys

from scripts.ci import source_artifact
from search.catalog.bundle import ARTIFACT_NAMES, MANIFEST_NAME, CatalogBundle
from search.operations.deployment import product_package, source_bundle
from search.operations.deployment.images import build as image_build
from search.operations.shared import private_io


ROOT = Path(__file__).resolve().parents[3]
ERROR_CODES = frozenset(
    "product_restore_" + code
    for code in (
        "anchors_invalid",
        "manifest_digest_mismatch",
        "manifest_invalid",
        "external_models_unsupported",
        "inventory_invalid",
        "asset_unsafe",
        "asset_digest_mismatch",
        "checksums_invalid",
        "archive_invalid",
        "models_invalid",
        "metadata_invalid",
        "destination_unsafe",
        "destination_exists",
        "source_restore_failed",
        "source_closure_invalid",
        "dependency_changed",
        "publication_failed",
        "failed",
    )
)
MAX_ENTRIES = 1024
MAX_TOTAL_BYTES = 3 * product_package.MAX_ARCHIVE_BYTES
GIT_ENVIRONMENT = (
    "PATH=/usr/local/bin:/usr/bin:/bin",
    "LANG=C.UTF-8",
    "LC_ALL=C.UTF-8",
    "GIT_CONFIG_NOSYSTEM=1",
    "GIT_CONFIG_GLOBAL=/dev/null",
    "GIT_CONFIG_SYSTEM=/dev/null",
    "GIT_TERMINAL_PROMPT=0",
    "GIT_NO_REPLACE_OBJECTS=1",
)
PLAN_KEYS = frozenset(
    "schema_version kind revision source images models metadata catalog "
    "credentials_included payload_self_contained install_complete "
    "runtime_install_verified registry_identity_restore_verified active_changed".split()
)


class ProductRestoreError(ValueError):
    """Only closed diagnostic codes, never paths or subprocess output."""


def _fail(code):
    raise ProductRestoreError("product_restore_" + code) from None


def _safe_run(arguments, *, timeout):
    # Reuse the bounded pipe/deadline runner. env -i also closes Git's global
    # configuration, which merely removing inherited GIT_* would not do.
    return source_bundle._runner(
        ["/usr/bin/env", "-i", *GIT_ENVIRONMENT, *map(str, arguments)],
        timeout=timeout,
    )


def _python(arguments, *, timeout):
    result = _safe_run([sys.executable, "-I", "-S", "-B", *arguments], timeout=timeout)
    if result.returncode != 0:
        raise ValueError("offline_child_failed")
    return image_build._json(result.stdout)


def _restore_source(package, revision, destination):
    return _python(
        [source_artifact.__file__, "restore", package, revision, destination],
        timeout=300,
    )


# The authenticated packaged revision owns its historical COPY closure and
# catalog compatibility policy. Import it in another interpreter, never into
# this process's already-loaded controller modules. Only Git is invoked here.
SOURCE_PROOF = r"""
import hashlib, json, os, subprocess, sys
from pathlib import Path
root, payload = map(Path, sys.argv[1:3])
expected_manifest = sys.argv[3]
# Trusted consumer policy: hash the committed builder closure independently of
# packaged builder code. The package cannot shrink or replace these inputs.
metadata_builder_files = (
    "search/__init__.py",
    "search/catalog/__init__.py",
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
    "search/catalog/policy.py",
    "search/catalog/resources.py",
)
sys.path.insert(0, str(root))
from search.operations.deployment import source_bundle, product_package
from search.operations.catalog import backend, execution, administration
from search.operations.shared import private_io
environment = [
    "PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8", "LC_ALL=C.UTF-8",
    "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null",
    "GIT_CONFIG_SYSTEM=/dev/null", "GIT_TERMINAL_PROMPT=0",
    "GIT_NO_REPLACE_OBJECTS=1",
]
def run(argv, *, timeout):
    if argv[0] != "git":
        raise ValueError("git_only")
    return source_bundle._runner(
        ["/usr/bin/env", "-i", *environment, "git", "--no-replace-objects", *argv[1:]],
        timeout=timeout,
    )
def text_run(argv, *, timeout):
    result = run(argv, timeout=timeout)
    return subprocess.CompletedProcess(argv, result.returncode,
        result.stdout.decode("utf-8"), result.stderr.decode("utf-8"))
payload_fd = private_io.open_directory(payload)
def read_record(name, expected):
    raw = private_io.read_file(payload_fd, name, mode=0o600,
        max_bytes=product_package.MAX_RECORD_BYTES)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("package_changed")
    private_io.verify_directory(payload_fd, payload)
    return json.loads(raw)
plan = read_record("package.json", expected_manifest)
revision, origin = plan["revision"], plan["catalog"]["revision"]
app = source_bundle.committed_source_plan(root, revision, runner=run)
backend.validate_compatibility({"revision": origin}, repo_root=root,
    revision=revision, runner=text_run)
git = source_bundle._Git(root, run)
tree = git.tree(origin)
names = set(administration.ADMIN_FILES)
for role in ("fess-search", "opensearch-search"):
    names.update(execution.service_sources(role))
names.update(metadata_builder_files)
blobs = {}
for name in sorted(names):
    if tree[name]["mode"] not in {"100644", "100755"} or tree[name]["type"] != "blob":
        raise ValueError("source_mode")
    blobs[name] = git.raw(["git", "-C", str(root), "show", origin + ":" + name],
        max_bytes=source_bundle.MAX_FILE_BYTES)
metadata_builder_digest = hashlib.sha256(json.dumps(
    {name: hashlib.sha256(blobs[name]).hexdigest() for name in metadata_builder_files},
    ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
).encode("utf-8")).hexdigest()
catalog = {}
for role in product_package.CATALOG_ROLES:
    paths = administration.ADMIN_FILES if role == "catalog-admin" else execution.service_sources(
        "opensearch-search" if role == "opensearch-hash" else role)
    catalog[role] = execution.digest_record({name: hashlib.sha256(blobs[name]).hexdigest()
        for name in paths})
receipt = plan["catalog_image_receipt"]
if receipt is not None:
    product_package._catalog_images(plan, read_record(receipt["path"], receipt["sha256"]))
git.clean(revision)
private_io.verify_directory(payload_fd, payload)
os.close(payload_fd)
print(json.dumps({
    "revision": revision, "catalog_revision": origin,
    "source_tree": git.raw(["git", "-C", str(root), "rev-parse", revision + "^{tree}"]).decode().strip(),
    "app_sources": {role: value["source_sha256"] for role, value in app["services"].items()},
    "catalog_sources": catalog,
    "metadata_builder_sha256": metadata_builder_digest,
}, sort_keys=True))
"""


def _source_proof(source, package, manifest, manifest_sha256):
    try:
        proof = _python(
            ["-c", SOURCE_PROOF, source, package, manifest_sha256], timeout=300
        )
        product_package._keys(
            proof,
            {
                "revision",
                "catalog_revision",
                "source_tree",
                "app_sources",
                "catalog_sources",
                "metadata_builder_sha256",
            },
            "source_proof_invalid",
        )
        if (
            proof["revision"] != manifest["revision"]
            or proof["catalog_revision"] != manifest["catalog"]["revision"]
            or source_bundle.REVISION.fullmatch(proof["source_tree"]) is None
            or proof["app_sources"]
            != {
                role: manifest["images"][role]["source_sha256"]
                for role in image_build.SERVICES
            }
            or proof["catalog_sources"]
            != {
                role: manifest["images"][role]["source_sha256"]
                for role in product_package.CATALOG_ROLES
            }
        ):
            _fail("source_closure_invalid")
        return proof
    except Exception:
        _fail("source_closure_invalid")


def _manifest(raw, revision):
    try:
        value = image_build._json(raw)
        product_package._keys(
            value,
            PLAN_KEYS | {"status", "image_archive", "catalog_image_receipt"},
            "manifest_invalid",
        )
        plan = product_package._validate_plan({key: value[key] for key in PLAN_KEYS})
        if value["revision"] != revision or value["status"] != "packaged":
            _fail("manifest_invalid")
        archive = value["image_archive"]
        product_package._keys(
            archive, {"path", "bytes", "sha256", "format"}, "archive_invalid"
        )
        if (
            archive["path"] != "images.tar"
            or type(archive["bytes"]) is not int
            or not 1 <= archive["bytes"] <= product_package.MAX_ARCHIVE_BYTES
            or archive["format"]
            not in {"docker-save-classic", "docker-save-oci-linux-amd64"}
        ):
            _fail("manifest_invalid")
        product_package._hash(archive["sha256"])
        receipt = value["catalog_image_receipt"]
        if receipt is not None:
            product_package._keys(receipt, {"path", "sha256"}, "receipt_invalid")
            if receipt != {
                "path": "catalog-images.json",
                "sha256": plan["catalog"]["image_receipt_sha256"],
            }:
                _fail("manifest_invalid")
        if plan["models"]["delivery"] != "bundled":
            _fail("external_models_unsupported")
        return value
    except ProductRestoreError:
        raise
    except Exception:
        _fail("manifest_invalid")


def _inventory(root_fd, files):
    directories = {""}
    for name in files:
        directories.update(
            str(parent) for parent in Path(name).parents if str(parent) != "."
        )
    result = {}

    def walk(fd, prefix):
        if len(result) >= MAX_ENTRIES:
            _fail("inventory_invalid")
        result[prefix] = private_io.identity(os.fstat(fd))
        with os.scandir(fd) as entries:
            for entry in entries:
                name = entry.name
                relative = prefix + "/" + name if prefix else name
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if relative in directories:
                    child = private_io.open_child_directory(fd, name)
                    try:
                        walk(child, relative)
                    finally:
                        os.close(child)
                elif relative in files:
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or info.st_nlink != 1
                        or stat.S_IMODE(info.st_mode) != 0o600
                    ):
                        _fail("asset_unsafe")
                    result[relative] = private_io.identity(info)
                else:
                    _fail("inventory_invalid")
                if len(result) > MAX_ENTRIES:
                    _fail("inventory_invalid")

    walk(root_fd, "")
    if set(result) != directories | files:
        _fail("inventory_invalid")
    return result


def _open_asset(root_fd, name):
    with ExitStack() as stack:
        parent = root_fd
        for part in name.split("/")[:-1]:
            parent = private_io.open_child_directory(parent, part)
            stack.callback(os.close, parent)
        return os.open(
            name.split("/")[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent,
        )


def _stream(root_fd, name, record, snapshot, output=None):
    descriptor = _open_asset(root_fd, name)
    try:
        before = os.fstat(descriptor)
        if (
            private_io.identity(before) != snapshot[name]
            or before.st_size != record["bytes"]
        ):
            _fail("asset_unsafe")
        digest, count = hashlib.sha256(), 0
        while part := os.read(descriptor, 1024**2):
            count += len(part)
            if count > record["bytes"]:
                _fail("asset_digest_mismatch")
            digest.update(part)
            if output is not None:
                output.write(part)
        if count != record["bytes"] or digest.hexdigest() != record["sha256"]:
            _fail("asset_digest_mismatch")
        if private_io.identity(os.fstat(descriptor)) != snapshot[name]:
            _fail("dependency_changed")
    finally:
        os.close(descriptor)


def _models(root, manifest):
    try:
        from scripts.prepare_search_models import PINS
        from search.catalog.embedding import MODEL, MODEL_DIGEST
        from search.operations import doctor
        from search.reranker.model import MODEL_HASHES

        expected = manifest["models"]["files"]
        for role, pins in (("api", PINS), ("embedding", {MODEL: MODEL_DIGEST})):
            if doctor._ollama_files(root / "models" / role, pins) != expected[role]:
                _fail("models_invalid")
        if {
            name: item["sha256"] for name, item in expected["reranker"].items()
        } != MODEL_HASHES:
            _fail("models_invalid")
    except Exception:
        _fail("models_invalid")


def _metadata(package_fd, root_fd, destination, manifest, snapshot):
    target = destination / "metadata" / ("sha256-" + manifest["metadata"]["bundle_id"])
    try:
        with ExitStack() as stack:
            parent = private_io.open_child_directory(root_fd, "metadata", create=True)
            stack.callback(os.close, parent)
            child = private_io.open_child_directory(parent, target.name, create=True)
            stack.callback(os.close, child)
            for name, record in manifest["metadata"]["files"].items():
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=child,
                )
                with os.fdopen(fd, "wb") as output:
                    _stream(package_fd, "metadata/" + name, record, snapshot, output)
                    output.flush()
                    os.fchmod(output.fileno(), 0o400)
                    os.fsync(output.fileno())
            os.fchmod(child, 0o500)
            os.fsync(child)
            os.fsync(parent)
            private_io.verify_directory(root_fd, destination)
            private_io.verify_directory(parent, target.parent)
            private_io.verify_directory(child, target, mode=0o500)
        bundle = CatalogBundle.load(
            target, expected_manifest_sha256=manifest["metadata"]["manifest_sha256"]
        )
        if (
            bundle.bundle_id != manifest["metadata"]["bundle_id"]
            or len(bundle.documents) != manifest["metadata"]["document_count"]
        ):
            _fail("metadata_invalid")
        return target, _sealed_metadata(target)
    except Exception:
        _fail("metadata_invalid")


def _sealed_metadata(path):
    descriptor = private_io.open_directory(path, mode=0o500)
    try:
        names = {*ARTIFACT_NAMES, MANIFEST_NAME}
        if set(os.listdir(descriptor)) != names:
            _fail("metadata_invalid")
        identities = {"": private_io.identity(os.fstat(descriptor))}
        for name in names:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o400
            ):
                _fail("metadata_invalid")
            identities[name] = private_io.identity(info)
        raw = private_io.read_file(
            descriptor,
            MANIFEST_NAME,
            mode=0o400,
            max_bytes=product_package.MAX_RECORD_BYTES,
        )
        return identities, image_build._json(raw)
    finally:
        os.close(descriptor)


def _recheck_directory(descriptor, path):
    fresh = private_io.open_directory(path)
    try:
        if private_io.identity(os.fstat(fresh)) != private_io.identity(
            os.fstat(descriptor)
        ):
            _fail("dependency_changed")
    finally:
        os.close(fresh)


def _destination(package, destination):
    path = Path(destination)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path.resolve() != path
        or path in {Path("/"), Path.home()}
        or any(
            path == root or path in root.parents or root in path.parents
            for root in (package, ROOT)
        )
    ):
        _fail("destination_unsafe")
    if path.exists() or path.is_symlink():
        _fail("destination_exists")
    return path


def restore_product(
    package_directory: Path,
    *,
    expected_revision: str,
    expected_manifest_sha256: str,
    destination: Path,
    source_restorer=None,
) -> dict:
    """Consume an externally authenticated package; retain all large assets.

    The optional source_restorer is an internal test seam. The default invokes
    the existing real-Git restorer in a sanitized isolated Python subprocess.
    Failure preserves a new partial destination and never publishes restore.json.
    """
    try:
        if (
            type(expected_revision) is not str
            or source_bundle.REVISION.fullmatch(expected_revision) is None
            or type(expected_manifest_sha256) is not str
            or source_bundle.DIGEST.fullmatch(expected_manifest_sha256) is None
            or (source_restorer is not None and not callable(source_restorer))
        ):
            _fail("anchors_invalid")
        package = Path(package_directory)
        destination = _destination(package, destination)
        with ExitStack() as stack:
            package_fd = private_io.open_directory(package)
            stack.callback(os.close, package_fd)
            raw = private_io.read_file(
                package_fd,
                "package.json",
                mode=0o600,
                max_bytes=product_package.MAX_RECORD_BYTES,
            )
            if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
                _fail("manifest_digest_mismatch")
            manifest = _manifest(raw, expected_revision)
            assets = {
                directory + "/" + name: item
                for directory in ("source", "metadata")
                for name, item in manifest[directory]["files"].items()
            }
            assets.update(
                {
                    "models/" + role + "/" + name: item
                    for role, files in manifest["models"]["files"].items()
                    for name, item in files.items()
                }
            )
            assets["images.tar"] = {
                key: manifest["image_archive"][key] for key in ("bytes", "sha256")
            }
            assets["package.json"] = {
                "bytes": len(raw),
                "sha256": expected_manifest_sha256,
            }
            if manifest["catalog_image_receipt"] is not None:
                receipt = private_io.read_file(
                    package_fd,
                    "catalog-images.json",
                    mode=0o600,
                    max_bytes=product_package.MAX_RECORD_BYTES,
                )
                # Historical source/file policy is checked by the restored
                # revision's _catalog_images, not this controller's policy.
                assets["catalog-images.json"] = {
                    "bytes": len(receipt),
                    "sha256": manifest["catalog"]["image_receipt_sha256"],
                }
            if sum(item["bytes"] for item in assets.values()) > MAX_TOTAL_BYTES:
                _fail("inventory_invalid")
            names = set(assets) | {"SHA256SUMS"}
            snapshot = _inventory(package_fd, names)
            checksums = private_io.read_file(
                package_fd,
                "SHA256SUMS",
                mode=0o600,
                max_bytes=product_package.MAX_RECORD_BYTES,
            )
            expected = "".join(
                item["sha256"] + "  " + name + "\n"
                for name, item in sorted(assets.items())
            ).encode("ascii")
            if checksums != expected:
                _fail("checksums_invalid")
            for name, record in assets.items():
                _stream(package_fd, name, record, snapshot)
            descriptor = _open_asset(package_fd, "images.tar")
            try:
                if private_io.identity(os.fstat(descriptor)) != snapshot["images.tar"]:
                    _fail("dependency_changed")
                if (
                    product_package._check_archive(descriptor, manifest["images"])
                    != manifest["image_archive"]["format"]
                ):
                    _fail("archive_invalid")
                if private_io.identity(os.fstat(descriptor)) != snapshot["images.tar"]:
                    _fail("dependency_changed")
            except ProductRestoreError:
                raise
            except Exception:
                _fail("archive_invalid")
            finally:
                os.close(descriptor)
            _models(package, manifest)
            parent_fd = private_io.open_directory(destination.parent)
            stack.callback(os.close, parent_fd)
            try:
                os.mkdir(destination.name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                _fail("destination_exists")
            os.fsync(parent_fd)
            root_fd = private_io.open_child_directory(parent_fd, destination.name)
            stack.callback(os.close, root_fd)
            metadata, sealed_metadata = _metadata(
                package_fd, root_fd, destination, manifest, snapshot
            )
            restore = _restore_source if source_restorer is None else source_restorer
            try:
                restored = restore(
                    package / "source", expected_revision, destination / "source"
                )
                expected = {
                    "schema_version": 1,
                    "status": "restored",
                    "source_revision": expected_revision,
                    "tracked_file_count": restored["tracked_file_count"],
                    "digests_verified": True,
                    "archive_matches_git_tree": True,
                    "clean_git_checkout": True,
                    "cli_help_verified": True,
                    "runtime_restored": False,
                    "deployment_performed": False,
                }
                if (
                    type(restored["tracked_file_count"]) is not int
                    or not 1
                    <= restored["tracked_file_count"]
                    <= source_artifact.MAX_FILES
                    or private_io.canonical(restored) != private_io.canonical(expected)
                ):
                    _fail("source_restore_failed")
            except Exception:
                _fail("source_restore_failed")
            source_fd = private_io.open_directory(destination / "source")
            stack.callback(os.close, source_fd)
            proof = _source_proof(
                destination / "source", package, manifest, expected_manifest_sha256
            )
            if _sealed_metadata(metadata) != sealed_metadata:
                _fail("dependency_changed")
            builder = sealed_metadata[1]["builder"]
            if builder["source_sha256"] != proof["metadata_builder_sha256"]:
                _fail("source_closure_invalid")
            if (
                snapshot != _inventory(package_fd, names)
                or private_io.read_file(
                    package_fd,
                    "package.json",
                    mode=0o600,
                    max_bytes=product_package.MAX_RECORD_BYTES,
                )
                != raw
            ):
                _fail("dependency_changed")
            _recheck_directory(package_fd, package)
            _recheck_directory(parent_fd, destination.parent)
            _recheck_directory(root_fd, destination)
            # A clean Git source is re-proven at publication, independently of
            # the historical policy subprocess and its earlier clean check.
            git = source_bundle._Git(destination / "source", _safe_run)
            git.clean(expected_revision)
            if (
                git.raw(
                    [
                        "git",
                        "-C",
                        str(destination / "source"),
                        "status",
                        "--porcelain",
                        "--untracked-files=all",
                    ]
                )
                or git.raw(
                    [
                        "git",
                        "-C",
                        str(destination / "source"),
                        "rev-parse",
                        "--abbrev-ref",
                        "HEAD",
                    ]
                ).strip()
                != b"HEAD"
            ):
                _fail("dependency_changed")
            _recheck_directory(source_fd, destination / "source")
            if _sealed_metadata(metadata) != sealed_metadata:
                _fail("dependency_changed")
            result = {
                "schema_version": 1,
                "status": "package_restored",
                "revision": expected_revision,
                "catalog_revision": manifest["catalog"]["revision"],
                "source_tree": proof["source_tree"],
                "package_manifest_sha256": expected_manifest_sha256,
                "retained_package": str(package),
                "source_directory": str(destination / "source"),
                "metadata_directory": str(metadata),
                "source_restore": restored,
                "payload_verified": True,
                "image_archive_verified": True,
                "pinned_models_verified": True,
                "source_closures_verified": True,
                "payload_assets_copied": False,
                "package_dependency_revalidation_required": True,
                "image_imported": False,
                "registry_identity_restore_verified": False,
                "runtime_install_verified": False,
                "install_complete": False,
                "deployment_performed": False,
                "active_changed": False,
            }
            private_io.write_json(
                root_fd,
                "restore.json",
                result,
                max_bytes=product_package.MAX_RECORD_BYTES,
            )
            return result
    except ProductRestoreError:
        raise
    except Exception:
        _fail("failed")
