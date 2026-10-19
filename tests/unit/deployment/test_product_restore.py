"""Offline product consumer tests: real Git, metadata, archives and model pins."""

from __future__ import annotations

from contextlib import ExitStack
import copy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from scripts import prepare_search_models
from search.catalog.build import builder, identity
from search.catalog import embedding as embedding_policy
from search.catalog.bundle import CatalogBundle
from search.operations import doctor
from search.operations.deployment import product_package, product_restore, source_bundle
from search.operations.shared import private_io
from search.reranker import model as reranker_model
from tests.unit.deployment.test_product_package import FakeDocker, record
from tests.unit.operations.test_metadata_sync import dataset


ROOT = Path(__file__).resolve().parents[3]
APP_PATHS = {
    role: "fixture/" + role + ".py" for role in product_package.image_build.SERVICES
}
CATALOG_PATHS = {
    "fess-search": ("fixture/fess.py",),
    "opensearch-search": ("fixture/opensearch.py",),
    "catalog-admin": ("fixture/admin.py",),
    "opensearch-hash": ("fixture/opensearch.py",),
}

# This tiny historical policy deliberately has different inputs from the
# consumer's RECIPES. Its subprocess still reads real committed Git objects.
HISTORICAL_SOURCE = (
    """
import hashlib, subprocess
from pathlib import Path
from search.operations.shared import private_io
from search.operations.shared.source_identity import require_clean_revision
MAX_FILE_BYTES = 2 * 1024**2
APP_PATHS = %r
def _runner(argv, *, timeout):
    if argv[0] != "/usr/bin/env" or "git" not in argv:
        raise ValueError("fixture_git_only")
    return subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL,
        timeout=timeout, check=False)
class _Git:
    def __init__(self, repo, runner):
        self.repo, self.runner = Path(repo), runner
    def raw(self, argv, *, max_bytes=2 * 1024**2):
        result = self.runner(argv, timeout=10)
        if result.returncode or len(result.stdout) > max_bytes:
            raise ValueError("fixture_git_failed")
        return result.stdout
    def tree(self, revision):
        rows = self.raw(["git", "-C", str(self.repo), "ls-tree", "-rz", revision])
        result = {}
        for row in rows.split(b"\\0"):
            if row:
                metadata, name = row.split(b"\\t", 1)
                mode, kind, object_id = metadata.decode().split()
                result[name.decode()] = {"mode": mode, "type": kind, "object": object_id}
        return result
    def clean(self, revision):
        def text(argv, *, timeout):
            raw = self.raw(argv)
            return subprocess.CompletedProcess(argv, 0, raw.decode(), "")
        if require_clean_revision(self.repo, runner=text) != revision:
            raise ValueError("fixture_not_clean")
def committed_source_plan(root, revision, runner=None):
    git = _Git(root, runner)
    git.clean(revision)
    tree, services = git.tree(revision), {}
    for role, path in APP_PATHS.items():
        raw = git.raw(["git", "-C", str(root), "show", revision + ":" + path])
        sources = {path: {"mode": tree[path]["mode"], "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}}
        services[role] = {"source_sha256": hashlib.sha256(private_io.canonical(sources)).hexdigest()}
    return {"services": services}
"""
    % APP_PATHS
)

HISTORICAL_BACKEND = """
DATA_FILES = %r
def validate_compatibility(plan, *, repo_root, revision, runner=None):
    origin = plan["revision"]
    for path in DATA_FILES:
        def blob(sha):
            result = runner(["git", "-C", str(repo_root), "show", sha + ":" + path], timeout=10)
            if result.returncode:
                raise ValueError("fixture_missing_history")
            return result.stdout
        if blob(origin) != blob(revision):
            raise ValueError("fixture_catalog_incompatible")
""" % (
    identity.SOURCE_FILES
    + tuple(sorted({paths[0] for paths in CATALOG_PATHS.values()})),
)


class ProductRestoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-product-restore-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "historical-source"
        self.repo.mkdir(mode=0o700)
        self.package = self.root / "payload"
        self.package.mkdir(mode=0o700)
        self.destination = self.root / "restored"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self._source_fixture()
        self._metadata_fixture()
        self._model_fixture()
        self._package_fixture()

    def write(self, root, name, raw):
        path = root / name
        for parent in reversed(path.parent.parents):
            if root == parent or root in parent.parents:
                parent.mkdir(mode=0o700, exist_ok=True)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o600)
        return path

    def git(self, *arguments):
        result = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(self.repo), *map(str, arguments)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=False,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
        self.assertEqual(result.returncode, 0, "fixture Git command failed")
        return result.stdout

    def commit(self):
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "--quiet",
            "-m",
            "Synthetic historical source",
        )
        return self.git("rev-parse", "HEAD").decode().strip()

    def _source_fixture(self):
        files = {
            "README.md": b"Historical committed source fixture.\n",
            "search/operations/deployment/source_bundle.py": HISTORICAL_SOURCE.encode(),
            "search/operations/deployment/product_package.py": (
                "MAX_RECORD_BYTES = 1024**2\nCATALOG_ROLES = "
                + repr(product_package.CATALOG_ROLES)
                + "\n"
            ).encode(),
            "search/operations/catalog/backend.py": HISTORICAL_BACKEND.encode(),
            "search/operations/catalog/execution.py": (
                "import hashlib\nfrom search.operations.shared import private_io\n"
                "def digest_record(value):\n    return hashlib.sha256(private_io.canonical(value)).hexdigest()\n"
                "def service_sources(role):\n"
                "    return ('fixture/opensearch.py',) if role == 'opensearch-search' else ('fixture/fess.py',)\n"
            ).encode(),
            "search/operations/catalog/administration.py": b"ADMIN_FILES = ('fixture/admin.py',)\n",
            "search/operations/shared/private_io.py": (
                ROOT / "search/operations/shared/private_io.py"
            ).read_bytes(),
            "search/operations/shared/source_identity.py": (
                ROOT / "search/operations/shared/source_identity.py"
            ).read_bytes(),
            "scripts/depositarai.py": (
                "import argparse\nparser = argparse.ArgumentParser()\n"
                "parser.add_argument('--config', required=True)\nparser.parse_args()\n"
            ).encode(),
        }
        for name in (
            "search/operations/__init__.py",
            "search/operations/shared/__init__.py",
            "search/operations/catalog/__init__.py",
            "search/operations/deployment/__init__.py",
        ):
            files[name] = b'"""Synthetic historical package."""\n'
        for name in identity.SOURCE_FILES:
            files[name] = (ROOT / name).read_bytes()
        for name in (
            *APP_PATHS.values(),
            *(paths[0] for paths in CATALOG_PATHS.values()),
        ):
            files[name] = ("# historical committed " + name + "\n").encode()
        for name, raw in files.items():
            self.write(self.repo, name, raw)
        self.git("init", "--quiet")
        self.catalog_revision = self.commit()
        self.write(
            self.repo, APP_PATHS["web"], b"# application changed after catalog\n"
        )
        self.revision = self.commit()
        self.source_tree = self.git("rev-parse", "HEAD^{tree}").decode().strip()
        archive, bundle, revision = product_package.source_names(self.revision)[:-1]
        source = self.package / "source"
        source.mkdir(mode=0o700)
        self.git(
            "archive",
            "--format=tar.gz",
            "--prefix=depositar-ai/",
            "--output=" + str(source / archive),
            self.revision,
        )
        self.git("bundle", "create", source / bundle, "HEAD")
        self.write(source, revision, (self.revision + "\n").encode())
        for name in (archive, bundle):
            (source / name).chmod(0o600)
        sums = "".join(
            record((source / name).read_bytes())["sha256"] + "  " + name + "\n"
            for name in (archive, bundle, revision)
        )
        self.write(source, "SHA256SUMS", sums.encode())

    def _metadata_fixture(self):
        snapshot = self.write(
            self.root, "snapshot.jsonl", (json.dumps(dataset()) + "\n").encode()
        )
        bundles = self.root / "bundles"
        bundles.mkdir(mode=0o700)
        built = builder.build_bundle(snapshot, bundles)
        sealed = Path(built["bundle_path"])
        self.metadata = {
            "bundle_id": built["bundle_id"],
            "manifest_sha256": built["manifest_sha256"],
            "document_count": 1,
            "files": {},
        }
        for name in (*product_package.ARTIFACT_NAMES, product_package.MANIFEST_NAME):
            raw = (sealed / name).read_bytes()
            self.write(self.package, "metadata/" + name, raw)
            self.metadata["files"][name] = record(raw)

    def _ollama_fixture(self, role):
        model = "fixture-" + role + ":unit"
        blobs = [(role + " config").encode(), (role + " model layer").encode()]
        descriptors = [
            {"digest": "sha256:" + record(raw)["sha256"], "size": len(raw)}
            for raw in blobs
        ]
        raw = private_io.canonical(
            {"schemaVersion": 2, "config": descriptors[0], "layers": descriptors[1:]}
        )
        inventory = {
            "manifests/registry.ollama.ai/library/" + model.replace(":", "/"): raw
        }
        inventory.update(
            {"blobs/sha256-" + record(blob)["sha256"]: blob for blob in blobs}
        )
        for name, value in inventory.items():
            self.write(self.package, "models/" + role + "/" + name, value)
        return (
            model,
            record(raw)["sha256"],
            {name: record(value) for name, value in inventory.items()},
        )

    def _model_fixture(self):
        api, api_digest, api_files = self._ollama_fixture("api")
        embedding, embedding_digest, embedding_files = self._ollama_fixture("embedding")
        raw = b"small fixed reranker weights"
        self.write(self.package, "models/reranker/model.safetensors", raw)
        self.models = {
            "api": api_files,
            "embedding": embedding_files,
            "reranker": {"model.safetensors": record(raw)},
        }
        self.stack.enter_context(
            mock.patch.object(prepare_search_models, "PINS", {api: api_digest})
        )
        self.stack.enter_context(
            mock.patch.object(embedding_policy, "MODEL", embedding)
        )
        self.stack.enter_context(
            mock.patch.object(embedding_policy, "MODEL_DIGEST", embedding_digest)
        )
        self.stack.enter_context(
            mock.patch.object(
                reranker_model,
                "MODEL_HASHES",
                {"model.safetensors": record(raw)["sha256"]},
            )
        )

    def _package_fixture(self):
        sources = {
            name: record((self.package / "source" / name).read_bytes())
            for name in product_package.source_names(self.revision)
        }
        catalog = {
            "project": "depositar-ai-catalog-"
            + self.metadata["bundle_id"][:12]
            + "-"
            + self.catalog_revision[:12],
            "revision": self.catalog_revision,
            "bundle_id": self.metadata["bundle_id"],
            "manifest_sha256": self.metadata["manifest_sha256"],
            "compose_sha256": "c" * 64,
            "image_receipt_sha256": "d" * 64,
        }
        environment_digest = record(private_io.canonical({"PATH": "/usr/bin"}))[
            "sha256"
        ]
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w"):
            pass
        layer = stream.getvalue()
        images, configs = {}, {}
        for role in product_package.IMAGE_ROLES:
            digest = None
            if role in APP_PATHS:
                name = APP_PATHS[role]
                raw = self.git("show", self.revision + ":" + name)
                digest = record(
                    private_io.canonical(
                        {
                            name: {
                                "mode": "100644",
                                "size": len(raw),
                                "sha256": record(raw)["sha256"],
                            }
                        }
                    )
                )["sha256"]
            elif role in CATALOG_PATHS:
                digest = record(
                    private_io.canonical(
                        {
                            name: record(
                                self.git("show", self.catalog_revision + ":" + name)
                            )["sha256"]
                            for name in CATALOG_PATHS[role]
                        }
                    )
                )["sha256"]
            config = private_io.canonical(
                {
                    "architecture": "amd64",
                    "os": "linux",
                    "config": {
                        "Env": ["PATH=/usr/bin"],
                        "Labels": {"fixture-role": role},
                    },
                    "rootfs": {
                        "type": "layers",
                        "diff_ids": ["sha256:" + record(layer)["sha256"]],
                    },
                }
            )
            identifier = "sha256:" + record(config)["sha256"]
            configs[identifier] = config
            images[role] = {
                "image_id": identifier,
                "image_environment_sha256": environment_digest,
            }
            images[role].update(
                {"reference": product_package.composition.OLLAMA_IMAGE}
                if role == "ollama"
                else {
                    "revision": self.catalog_revision
                    if role in CATALOG_PATHS
                    else self.revision,
                    "source_sha256": digest,
                }
            )
        plan = product_package.plan_product_package(
            self.revision,
            source=sources,
            images=images,
            models=self.models,
            metadata=self.metadata,
            catalog=catalog,
        )
        self.docker = FakeDocker(
            plan, {identifier: {} for identifier in configs}, configs, layer
        )
        self.make_archive()
        self.manifest = {
            **plan,
            "status": "packaged",
            "image_archive": {
                "path": "images.tar",
                **record((self.package / "images.tar").read_bytes()),
                "format": "docker-save-classic",
            },
            "catalog_image_receipt": None,
        }
        self.publish_manifest()

    def make_archive(self):
        client = self.root / "empty-docker-client"
        client.mkdir(mode=0o700, exist_ok=True)
        target = self.package / "images.tar"
        target.unlink(missing_ok=True)
        self.docker(
            [
                "docker",
                "--host",
                "fixture-never-executed",
                "--config",
                str(client),
                "image",
                "save",
                "--output",
                str(target),
                *self.docker.images,
            ],
            timeout=1,
            environment={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
        )
        target.chmod(0o600)

    def publish_manifest(self):
        raw = private_io.canonical(self.manifest)
        self.write(self.package, "package.json", raw)
        self.anchor = record(raw)["sha256"]
        records = {
            "package.json": self.anchor,
            "images.tar": self.manifest["image_archive"]["sha256"],
        }
        for category in ("source", "metadata"):
            records.update(
                {
                    category + "/" + name: value["sha256"]
                    for name, value in self.manifest[category]["files"].items()
                }
            )
        for role, files in self.manifest["models"]["files"].items():
            records.update(
                {
                    "models/" + role + "/" + name: value["sha256"]
                    for name, value in files.items()
                }
            )
        self.write(
            self.package,
            "SHA256SUMS",
            "".join(
                digest + "  " + name + "\n" for name, digest in sorted(records.items())
            ).encode(),
        )

    def restore(self, **overrides):
        arguments = {
            "expected_revision": self.revision,
            "expected_manifest_sha256": self.anchor,
            "destination": self.destination,
        }
        arguments.update(overrides)
        return product_restore.restore_product(self.package, **arguments)

    def rejected(self, code=None, **overrides):
        with self.assertRaises(product_restore.ProductRestoreError) as caught:
            self.restore(**overrides)
        message = str(caught.exception)
        self.assertIn(message, product_restore.ERROR_CODES)
        if code:
            self.assertEqual(message, "product_restore_" + code)
        self.assertNotIn(str(self.root), message)
        self.assertFalse((self.destination / "restore.json").exists())

    def test_real_offline_restore_uses_packaged_policy_and_sanitized_children(self):
        original_popen = subprocess.Popen
        commands = []

        def spawn(argv, **kwargs):
            commands.append(argv)
            self.assertEqual(argv[:2], ["/usr/bin/env", "-i"])
            self.assertIn("GIT_CONFIG_GLOBAL=/dev/null", argv)
            self.assertIn("GIT_NO_REPLACE_OBJECTS=1", argv)
            self.assertNotIn("docker", argv)
            return original_popen(argv, **kwargs)

        with (
            mock.patch.object(subprocess, "Popen", side_effect=spawn),
            mock.patch.object(source_bundle, "RECIPES", {}),
            mock.patch.dict(
                os.environ,
                {"GIT_DIR": "/must-not-be-used", "PYTHONPATH": "/must-not-be-imported"},
            ),
            mock.patch.object(
                doctor, "_ollama_files", wraps=doctor._ollama_files
            ) as model_check,
        ):
            result = self.restore()
        self.assertEqual(sum("-I" in command for command in commands), 2)
        self.assertEqual(model_check.call_count, 2)
        self.assertEqual(result["status"], "package_restored")
        self.assertEqual(result["source_tree"], self.source_tree)
        self.assertEqual(result["catalog_revision"], self.catalog_revision)
        self.assertNotEqual(self.catalog_revision, self.revision)
        self.assertEqual(
            json.loads((self.destination / "restore.json").read_bytes()), result
        )
        self.assertEqual(result["source_restore"]["status"], "restored")
        self.assertEqual(
            (self.destination / "source" / APP_PATHS["web"]).read_bytes(),
            b"# application changed after catalog\n",
        )
        bundle = CatalogBundle.load(
            Path(result["metadata_directory"]),
            expected_manifest_sha256=self.metadata["manifest_sha256"],
        )
        self.assertEqual(len(bundle.documents), 1)
        self.assertEqual(
            stat.S_IMODE(Path(result["metadata_directory"]).stat().st_mode), 0o500
        )
        for field in (
            "payload_verified",
            "image_archive_verified",
            "pinned_models_verified",
            "source_closures_verified",
            "package_dependency_revalidation_required",
        ):
            self.assertIs(result[field], True)
        for field in (
            "payload_assets_copied",
            "image_imported",
            "registry_identity_restore_verified",
            "runtime_install_verified",
            "install_complete",
            "deployment_performed",
            "active_changed",
        ):
            self.assertIs(result[field], False)
        self.assertEqual(result["retained_package"], str(self.package))
        self.assertFalse((self.destination / "models").exists())
        self.assertFalse((self.destination / "images.tar").exists())

    def test_external_anchor_is_checked_before_json_or_assets(self):
        self.write(self.package, "package.json", b"invalid json sentinel")
        self.rejected("manifest_digest_mismatch")
        self.assertFalse(self.destination.exists())

    def test_invalid_anchor_or_revision_never_creates_destination(self):
        for arguments in (
            {"expected_revision": "bad"},
            {"expected_manifest_sha256": "bad"},
            {"expected_revision": "f" * 40},
        ):
            with self.subTest(arguments=arguments):
                self.rejected(**arguments)
                self.assertFalse(self.destination.exists())

    def test_closed_manifest_rejects_extra_fields_and_install_claims(self):
        original = copy.deepcopy(self.manifest)
        for field, value in (
            ("unexpected", "sentinel"),
            ("install_complete", True),
            ("runtime_install_verified", True),
        ):
            with self.subTest(field=field):
                self.manifest = {**copy.deepcopy(original), field: value}
                self.publish_manifest()
                self.rejected("manifest_invalid")
                self.assertFalse(self.destination.exists())

    def test_external_models_are_explicitly_unsupported(self):
        self.manifest["models"]["delivery"] = "external"
        self.manifest["payload_self_contained"] = False
        self.publish_manifest()
        self.rejected("external_models_unsupported")
        self.assertFalse(self.destination.exists())

    def test_unknown_or_missing_payload_is_rejected(self):
        unexpected = self.write(self.package, "extra.bin", b"unexpected")
        self.rejected("inventory_invalid")
        unexpected.unlink()
        (self.package / "source/SOURCE_REVISION").unlink()
        self.rejected("inventory_invalid")
        self.assertFalse(self.destination.exists())

    def test_missing_completion_manifest_is_not_adopted(self):
        (self.package / "package.json").unlink()
        self.rejected()
        self.assertFalse(self.destination.exists())

    def test_tampered_asset_is_rejected_even_with_valid_manifest_anchor(self):
        target = self.package / "models/reranker/model.safetensors"
        self.write(
            self.package,
            str(target.relative_to(self.package)),
            b"x" * target.stat().st_size,
        )
        self.rejected("asset_digest_mismatch")
        self.assertFalse(self.destination.exists())

    def test_symlink_and_hardlink_assets_are_not_followed(self):
        target = self.package / "models/reranker/model.safetensors"
        raw = target.read_bytes()
        outside = self.write(self.root, "outside-weights", raw)
        target.unlink()
        target.symlink_to(outside)
        self.rejected("asset_unsafe")
        target.unlink()
        os.link(outside, target)
        self.rejected("asset_unsafe")
        self.assertEqual(outside.read_bytes(), raw)
        self.assertFalse(self.destination.exists())

    def test_nonprivate_file_and_directory_modes_are_rejected(self):
        target = self.package / "models/reranker/model.safetensors"
        target.chmod(0o644)
        self.rejected("asset_unsafe")
        target.chmod(0o600)
        target.parent.chmod(0o750)
        self.rejected()
        self.assertFalse(self.destination.exists())

    def test_checksums_cannot_add_duplicate_records(self):
        target = self.package / "SHA256SUMS"
        target.write_bytes(
            target.read_bytes() + target.read_bytes().splitlines(keepends=True)[0]
        )
        self.rejected("checksums_invalid")
        self.assertFalse(self.destination.exists())

    def test_archive_content_is_verified_beyond_its_outer_digest(self):
        self.docker.corrupt_layer = True
        self.make_archive()
        self.manifest["image_archive"].update(
            record((self.package / "images.tar").read_bytes())
        )
        self.publish_manifest()
        self.rejected("archive_invalid")
        self.assertFalse(self.destination.exists())

    def test_real_ollama_pin_validation_rejects_reanchored_bad_manifest(self):
        name = next(
            name for name in self.models["api"] if name.startswith("manifests/")
        )
        raw = b'{"schemaVersion":0}'
        self.write(self.package, "models/api/" + name, raw)
        self.manifest["models"]["files"]["api"][name] = record(raw)
        self.publish_manifest()
        self.rejected("models_invalid")
        self.assertFalse(self.destination.exists())

    def test_metadata_semantics_are_checked_after_outer_hashes(self):
        name = product_package.ARTIFACT_NAMES[0]
        raw = b"not a gzip metadata artifact"
        self.write(self.package, "metadata/" + name, raw)
        self.manifest["metadata"]["files"][name] = record(raw)
        self.publish_manifest()
        self.rejected("metadata_invalid")
        self.assertTrue(self.destination.exists())
        self.assertFalse((self.destination / "source").exists())

    def test_late_source_failure_preserves_partial_output_and_requires_new_path(self):
        def fail(*_args):
            raise ValueError("private-subprocess-output-sentinel")

        self.rejected("source_restore_failed", source_restorer=fail)
        self.assertTrue(self.destination.exists())
        self.assertTrue((self.destination / "metadata").exists())
        self.rejected("destination_exists")

    def test_source_hash_claim_must_match_packaged_committed_policy(self):
        self.manifest["images"]["web"]["source_sha256"] = "f" * 64
        self.publish_manifest()
        self.rejected("source_closure_invalid")
        self.assertTrue((self.destination / "source/.git").exists())

    def test_metadata_closure_hashes_git_blobs_without_importing_digest_code(self):
        guard = """
import sys
class RejectBuilder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "search.catalog.build.identity":
            raise RuntimeError("packaged_digest_code_must_not_execute")
sys.meta_path.insert(0, RejectBuilder())
"""
        proof = product_restore._python(
            [
                "-c",
                guard + product_restore.SOURCE_PROOF,
                self.repo,
                self.package,
                self.anchor,
            ],
            timeout=300,
        )
        self.assertEqual(proof["metadata_builder_sha256"], identity.source_digest())
        self.assertEqual(proof["catalog_revision"], self.catalog_revision)

    def test_dependency_mutation_after_source_restore_prevents_receipt(self):
        def mutate(package, revision, destination):
            result = product_restore._restore_source(package, revision, destination)
            target = self.package / "models/reranker/model.safetensors"
            target.write_bytes(b"x" * target.stat().st_size)
            return result

        self.rejected("dependency_changed", source_restorer=mutate)
        self.assertTrue((self.destination / "source/.git").exists())

    def test_source_mutation_after_historical_proof_prevents_receipt(self):
        original = product_restore._source_proof

        def mutate(source, *arguments):
            result = original(source, *arguments)
            (source / "README.md").write_bytes(b"Late tracked source drift.\n")
            return result

        with mock.patch.object(product_restore, "_source_proof", side_effect=mutate):
            self.rejected()
        self.assertTrue((self.destination / "source/.git").exists())

    def test_sealed_metadata_mutation_after_proof_prevents_receipt(self):
        original = product_restore._source_proof

        def mutate(*arguments):
            result = original(*arguments)
            target = (
                self.destination
                / "metadata"
                / ("sha256-" + self.metadata["bundle_id"])
                / product_package.ARTIFACT_NAMES[0]
            )
            target.chmod(0o600)
            target.write_bytes(b"x" * target.stat().st_size)
            target.chmod(0o400)
            return result

        with mock.patch.object(product_restore, "_source_proof", side_effect=mutate):
            self.rejected("dependency_changed")

    def test_publication_failure_never_claims_completed_restore(self):
        with mock.patch.object(
            private_io,
            "write_json",
            side_effect=OSError("private-publication-output-sentinel"),
        ):
            self.rejected()
        self.assertTrue((self.destination / "source/.git").exists())

    def test_linked_package_directory_is_not_followed(self):
        target = self.package / "models/reranker"
        outside = self.root / "outside-reranker"
        target.rename(outside)
        target.symlink_to(outside, target_is_directory=True)
        self.rejected()
        self.assertEqual(
            (outside / "model.safetensors").read_bytes(),
            b"small fixed reranker weights",
        )
        self.assertFalse(self.destination.exists())

    def test_fifo_asset_is_rejected_without_opening_or_blocking(self):
        target = self.package / "models/reranker/model.safetensors"
        target.unlink()
        os.mkfifo(target, 0o600)
        with mock.patch.object(
            product_restore, "_open_asset", wraps=product_restore._open_asset
        ) as opened:
            self.rejected("asset_unsafe")
        opened.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_archive_replacement_between_hash_and_parser_is_rejected(self):
        original = product_restore._open_asset
        archive = self.package / "images.tar"
        opened = 0

        def replace(root_fd, name):
            nonlocal opened
            if name == "images.tar":
                opened += 1
                if opened == 2:
                    raw = archive.read_bytes()
                    archive.rename(self.root / "original-images.tar")
                    self.write(self.package, "images.tar", raw)
            return original(root_fd, name)

        with (
            mock.patch.object(product_restore, "_open_asset", side_effect=replace),
            mock.patch.object(
                product_package, "_check_archive", wraps=product_package._check_archive
            ) as parsed,
        ):
            self.rejected("dependency_changed")
        parsed.assert_not_called()
        self.assertEqual(opened, 2)
        self.assertFalse(self.destination.exists())

    def test_existing_destination_and_package_overlap_are_rejected(self):
        self.destination.mkdir(mode=0o700)
        sentinel = self.write(self.destination, "keep", b"preserve existing output")
        self.rejected("destination_exists")
        self.assertEqual(sentinel.read_bytes(), b"preserve existing output")
        self.rejected("destination_unsafe", destination=self.package / "restored")
        self.assertFalse((self.package / "restored").exists())

    def test_concurrent_destination_creation_is_never_adopted(self):
        original = product_restore._models
        sentinel = b"Concurrent owner output must be preserved.\n"

        def create(root, manifest):
            original(root, manifest)
            self.destination.mkdir(mode=0o700)
            self.write(self.destination, "sentinel", sentinel)

        with mock.patch.object(product_restore, "_models", side_effect=create):
            self.rejected("destination_exists")
        self.assertEqual(
            set(self.destination.iterdir()), {self.destination / "sentinel"}
        )
        self.assertEqual((self.destination / "sentinel").read_bytes(), sentinel)


if __name__ == "__main__":
    unittest.main()
