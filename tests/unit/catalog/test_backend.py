"""Isolated catalog preparation uses sealed sources and no running services."""

from __future__ import annotations

import copy
import ast
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from search.catalog.build import builder
from search.catalog.errors import CatalogError
from search.operations.catalog import backend
from search.operations.config import GPUConfig, ProductConfig
from tests.test_validate_search_compose import (
    CANARY_ROOT,
    RUNTIME_GID,
    RUNTIME_UID,
    valid_config,
)
from tests.unit.operations.test_metadata_sync import dataset


ROOT = Path(__file__).resolve().parents[3]
REVISION = "a" * 40
HASH_IMAGE_ID = "sha256:" + "c" * 64


def adapted_config(data_root, project):
    original = valid_config()
    raw = json.dumps(original)
    raw = raw.replace(CANARY_ROOT, str(data_root))
    raw = raw.replace(f"{RUNTIME_UID}:{RUNTIME_GID}", f"{os.geteuid()}:{os.getegid()}")
    raw = raw.replace(f"uid={RUNTIME_UID}", f"uid={os.geteuid()}")
    raw = raw.replace(f"gid={RUNTIME_GID}", f"gid={os.getegid()}")
    config = json.loads(raw)
    config["services"]["fess-search"]["environment"]["FESS_JAVA_OPTS"] = config[
        "services"
    ]["fess-search"]["environment"]["FESS_JAVA_OPTS"].replace(
        "max_documents_per_run=10000", "max_documents_per_run=256"
    )
    config["name"] = project
    config["networks"]["search_backend"]["name"] = project + "_search_backend"
    config["networks"]["search_backend"]["attachable"] = False
    config["networks"]["search_backend"]["ipam"] = {}
    for name, value in config["secrets"].items():
        value["name"] = project + "_" + name
    return config


class CatalogBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="depositar-backend-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        roots = {}
        for name in ("state", "api-models", "embedding-models", "reranker-models"):
            roots[name] = self.root / name
            roots[name].mkdir(mode=0o700)
        uuids = tuple(f"GPU-0000000{i}-0000-4000-8000-000000000001" for i in range(4))
        self.config = ProductConfig(
            1,
            roots["state"],
            roots["api-models"],
            roots["embedding-models"],
            roots["reranker-models"],
            GPUConfig(uuids[0], (uuids[1], uuids[2]), uuids[3]),
        )
        source = self.root / "snapshot.jsonl"
        source.write_text(json.dumps(dataset()) + "\n")
        bundles = self.root / "bundles"
        bundles.mkdir(mode=0o700)
        self.bundle = builder.build_bundle(source, bundles)
        self.calls = []
        self.compose_mutation = None
        self.fail_bootstrap = False
        self.fail_hash_build = False
        self.hash_metadata_mutation = None
        self.hash_context_mutation = False
        self.dirty_source = None
        self.original_sources = {}
        self.missing_history = False
        self.missing_origin_files = set()
        self.unreadable_origin_files = set()

    def runner(self, argv, *, timeout):
        self.calls.append((argv, timeout))
        if argv[:1] == ["git"]:
            if "ls-tree" in argv:
                if self.missing_history:
                    return subprocess.CompletedProcess(argv, 1, "", "missing old tree")
                paths = (
                    *backend.STAGED_FILES,
                    *backend.BUILD_FILES,
                    *backend.DATA_FILES,
                )
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "\0".join(p for p in paths if p not in self.missing_origin_files)
                    + "\0",
                    "",
                )
            revision, relative = argv[-1].split(":", 1)
            if self.missing_history and revision == REVISION:
                return subprocess.CompletedProcess(argv, 1, "", "missing old object")
            if revision == REVISION and relative in self.unreadable_origin_files:
                return subprocess.CompletedProcess(argv, 1, "", "unreadable old blob")
            content = self.original_sources.get(
                (revision, relative), (ROOT / relative).read_text()
            )
            if relative == self.dirty_source:
                content += "\n# Not committed\n"
            return subprocess.CompletedProcess(argv, 0, content, "")
        if argv[:1] == ["bash"]:
            receipt = json.loads(
                (Path(argv[3]).parents[2] / "hash-image.json").read_text()
            )
            self.assertEqual(argv[7], receipt["reference"])
            self.assertEqual(argv[9], receipt["image_id"])
            if self.fail_bootstrap:
                return subprocess.CompletedProcess(argv, 1, "", "private failure")
            self.assertEqual("--isolated-project", argv[4])
            tooling = Path(argv[3]).parents[1]
            values = dict(
                line.split("=", 1)
                for line in (tooling / ".env.search-canary").read_text().splitlines()
                if line and not line.startswith("#")
            )
            self.assertEqual(argv[5], values["COMPOSE_PROJECT_NAME"])
            self.assertEqual("--hash-image", argv[6])
            self.assertRegex(
                argv[7], r"\Adepositar-opensearch-search-hash:[0-9a-f]{40}\Z"
            )
            self.assertEqual(argv[5].rsplit("-", 1)[1], argv[7].split(":")[1][:12])
            self.assertEqual(["--hash-image-id", HASH_IMAGE_ID], argv[8:])
            data = Path(values["SEARCH_CANARY_DATA_ROOT"])
            data.mkdir(mode=0o700)
            for name in ("state", "secrets", "opensearch", "opensearch/config"):
                (data / name).mkdir(mode=0o700)
            (data / "state/operations.lock").touch(mode=0o600)
            for number, name in enumerate(backend.SECRET_NAMES, 1):
                secret = data / "secrets" / name
                secret.write_text(
                    str(number) * (32 if name == "fess_app_cipher_key" else 64) + "\n"
                )
                secret.chmod(0o600)
            for name in (*backend.SECURITY_FILES, "security-material.sha256"):
                target = data / "opensearch/config" / name
                target.write_text("nonsecret fake bootstrap material: " + name + "\n")
                target.chmod(0o600 if name.endswith(".sha256") else 0o400)
            return subprocess.CompletedProcess(argv, 0, "isolated bootstrap\n", "")
        if argv[:2] == ["docker", "build"]:
            self.assertEqual(300, timeout)
            self.assertIn("--network=none", argv)
            self.assertIn("--pull=false", argv)
            context = Path(argv[-1])
            self.assertEqual(
                str(context / "Dockerfile"), argv[argv.index("--file") + 1]
            )
            self.assertEqual(
                "OPENSEARCH_BASE_IMAGE=" + backend.APPROVED_OPENSEARCH_BASE,
                argv[argv.index("--build-arg") + 1],
            )
            self.assertEqual("tooling", context.parents[2].name)
            for name in ("Dockerfile", ".dockerignore", "opensearch_healthcheck.py"):
                self.assertEqual(
                    (ROOT / "deploy/docker/opensearch" / name).read_bytes(),
                    (context / name).read_bytes(),
                )
            if self.hash_context_mutation:
                (context / "Dockerfile").write_text("unsealed build source\n")
            return subprocess.CompletedProcess(
                argv, int(self.fail_hash_build), "", "private build failure"
            )
        if argv[:3] == ["docker", "image", "inspect"]:
            revision = argv[-1].split(":", 1)[1]
            metadata = {"id": HASH_IMAGE_ID, "revision": revision}
            if self.hash_metadata_mutation:
                self.hash_metadata_mutation(metadata)
            return subprocess.CompletedProcess(argv, 0, json.dumps(metadata), "")
        if argv[:2] == ["docker", "compose"]:
            self.assertEqual(["config", "--format", "json"], argv[-3:])
            env = Path(argv[argv.index("--env-file") + 1])
            project = argv[argv.index("--project-name") + 1]
            config = adapted_config(env.parent.parent / "data", project)
            if self.compose_mutation:
                self.compose_mutation(config)
            return subprocess.CompletedProcess(argv, 0, json.dumps(config), "")
        self.fail("unexpected runtime command: " + repr(argv))

    def prepare(self, **options):
        return backend.prepare(
            self.config,
            repo_root=ROOT,
            bundle_path=Path(self.bundle["bundle_path"]),
            manifest_sha256=self.bundle["manifest_sha256"],
            revision=options.pop("revision", REVISION),
            runner=self.runner,
            **options,
        )

    def test_prepares_sealed_cpu_slot_without_starting_or_switching(self):
        result = self.prepare()
        name = self.bundle["bundle_id"][:12] + "-" + REVISION[:12]
        self.assertEqual("depositar-ai-catalog-" + name, result["project"])
        self.assertEqual(
            self.config.state_root / "catalogs" / name, Path(result["work_root"])
        )
        self.assertEqual(
            (1, "prepared", 1),
            (result["schema_version"], result["status"], result["document_count"]),
        )
        self.assertEqual(
            result, json.loads((Path(result["work_root"]) / "plan.json").read_text())
        )
        self.assertEqual(
            set((*backend.STAGED_FILES, *backend.BUILD_FILES)),
            set(result["source_sha256"]),
        )
        compose = json.loads(Path(result["compose_file"]).read_text())
        self.assertTrue(compose["networks"]["search_backend"]["internal"])
        self.assertFalse((self.config.state_root / "active.json").exists())
        self.assertEqual(1, sum(argv[0] == "bash" for argv, _ in self.calls))
        self.assertEqual(3, sum(argv[0] == "docker" for argv, _ in self.calls))
        self.assertEqual(
            900, next(timeout for argv, timeout in self.calls if argv[0] == "bash")
        )
        for name in (*backend.STAGED_FILES, *backend.BUILD_FILES):
            staged = Path(result["tooling_root"]) / name
            self.assertEqual((ROOT / name).read_bytes(), staged.read_bytes())
            self.assertEqual(0o600, staged.stat().st_mode & 0o777)

    def test_repeat_verifies_sealed_plan_without_bootstrap_or_render(self):
        first = self.prepare()
        self.calls.clear()
        self.assertEqual(first, self.prepare())
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))

    def test_volume_profile_seals_one_external_index_without_creating_a_volume(self):
        self.config = replace(
            self.config, schema_version=2, catalog_storage="docker-volume"
        )
        with patch.object(Path, "home", return_value=self.root):
            plan = self.prepare()
            self.calls.clear()
            self.assertEqual(plan, self.prepare())
        compose = json.loads(Path(plan["compose_file"]).read_text())
        self.assertEqual(
            {"catalog-index": {"name": plan["project"] + "-index", "external": True}},
            compose["volumes"],
        )
        mount = next(
            value
            for value in compose["services"]["opensearch-search"]["volumes"]
            if value["target"] == "/usr/share/opensearch/data"
        )
        self.assertEqual(
            {
                "type": "volume",
                "source": "catalog-index",
                "target": "/usr/share/opensearch/data",
                "volume": {"nocopy": True},
            },
            mount,
        )
        env = (Path(plan["tooling_root"]) / ".env.search-canary").read_text()
        self.assertIn("SEARCH_CANARY_CATALOG_STORAGE=docker-volume\n", env)
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))
        self.assertFalse((Path(plan["work_root"]) / "volume.json").exists())
        self.config = replace(self.config, schema_version=1, catalog_storage="bind")
        with self.assertRaises(ValueError):
            self.prepare()

    def test_volume_profile_rejects_changed_bootstrap_data_bind_before_replacement(
        self,
    ):
        self.config = replace(
            self.config, schema_version=2, catalog_storage="docker-volume"
        )

        def change_data_mount(compose):
            mount = next(
                value
                for value in compose["services"]["opensearch-search"]["volumes"]
                if value["target"] == "/usr/share/opensearch/data"
            )
            mount["source"] = "/etc/opensearch/data"

        self.compose_mutation = change_data_mount
        with self.assertRaises(ValueError):
            self.prepare()

    def test_legacy_bind_profile_keeps_home_state_forbidden(self):
        with patch.object(Path, "home", return_value=self.root):
            with self.assertRaisesRegex(
                backend.BackendError, "outside the operator home"
            ):
                self.prepare()
        self.assertEqual([], self.calls)

    def test_hash_helper_receipt_seals_exact_sources_and_immutable_build_identity(self):
        prepared = self.prepare()
        receipt = Path(prepared["work_root"]) / "hash-image.json"
        sources = (
            "deploy/docker/opensearch/Dockerfile",
            "deploy/docker/opensearch/.dockerignore",
            "deploy/docker/opensearch/opensearch_healthcheck.py",
        )
        self.assertEqual(
            {
                "image_id": HASH_IMAGE_ID,
                "reference": "depositar-opensearch-search-hash:" + REVISION,
                "revision": REVISION,
                "base": backend.APPROVED_OPENSEARCH_BASE,
                "sources": {
                    name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                    for name in sources
                },
            },
            json.loads(receipt.read_text()),
        )
        self.assertEqual(0o600, receipt.stat().st_mode & 0o777)

    def test_loading_legacy_slot_does_not_fabricate_hash_image_receipt(self):
        prepared = self.prepare()
        receipt = Path(prepared["work_root"]) / "hash-image.json"
        receipt.unlink()
        self.calls.clear()
        self.assertEqual(prepared, self.load_prepared())
        self.assertEqual(prepared, self.prepare())
        self.assertFalse(receipt.exists())
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))

    def load_prepared(self):
        return backend.load_prepared(
            self.config,
            repo_root=ROOT,
            bundle_path=Path(self.bundle["bundle_path"]),
            manifest_sha256=self.bundle["manifest_sha256"],
            revision=REVISION,
            runner=self.runner,
        )

    def test_readonly_load_never_creates_a_missing_slot(self):
        with self.assertRaisesRegex(backend.BackendError, "must be prepared"):
            self.load_prepared()
        self.assertFalse((self.config.state_root / "catalogs").exists())
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))

    def test_readonly_load_reuses_exact_repeat_validation(self):
        prepared = self.prepare()
        self.calls.clear()
        self.assertEqual(prepared, self.load_prepared())
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))
        staged = Path(prepared["tooling_root"]) / backend.STAGED_FILES[0]
        staged.write_bytes(staged.read_bytes() + b"# unsealed\n")
        with self.assertRaisesRegex(backend.BackendError, "source changed"):
            self.load_prepared()

    def test_readonly_load_cannot_reuse_a_quarantined_slot(self):
        self.fail_bootstrap = True
        with self.assertRaises(backend.BackendError):
            self.prepare()
        self.calls.clear()
        with self.assertRaisesRegex(backend.BackendError, "quarantined"):
            self.load_prepared()
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))

    def test_repeat_detects_valid_hex_secret_changed_with_unchanged_manifest(self):
        result = self.prepare()
        directory = Path(result["data_root"])
        manifest = directory / "opensearch/config/security-material.sha256"
        original_manifest = manifest.read_bytes()
        secret = directory / "secrets/fess_initial_admin_password"
        secret.write_text("f" * 64 + "\n")
        with self.assertRaisesRegex(
            backend.BackendError, "security material digest mismatch"
        ):
            self.load_prepared()
        self.assertEqual(original_manifest, manifest.read_bytes())
        self.assertNotIn(
            "f" * 64, (Path(result["work_root"]) / "plan.json").read_text()
        )

    def test_repeat_detects_static_security_file_byte_replacement(self):
        result = self.prepare()
        path = Path(result["data_root"]) / "opensearch/config/roles_mapping.yml"
        path.chmod(0o600)
        path.write_text("different role bindings\n")
        path.chmod(0o400)
        with self.assertRaisesRegex(
            backend.BackendError, "security material digest mismatch"
        ):
            self.load_prepared()

    def test_security_material_fifo_symlink_and_extra_entries_fail_closed(self):
        result = self.prepare()
        data = Path(result["data_root"])
        path = data / "opensearch/config/node.pem"
        raw = path.read_bytes()
        path.unlink()
        os.mkfifo(path, 0o400)
        with self.assertRaisesRegex(
            backend.BackendError, "security material is unsafe"
        ):
            self.load_prepared()
        path.unlink()
        path.symlink_to(data / "opensearch/config/root-ca.pem")
        with self.assertRaisesRegex(
            backend.BackendError, "security material is unsafe"
        ):
            self.load_prepared()
        path.unlink()
        path.write_bytes(raw)
        path.chmod(0o400)
        extra = data / "secrets/unexpected"
        extra.write_text("not a reviewed secret\n")
        extra.chmod(0o600)
        with self.assertRaisesRegex(
            backend.BackendError, "security material is unsafe"
        ):
            self.load_prepared()

    def test_new_revision_uses_a_different_slot_and_project(self):
        first = self.prepare()
        second = self.prepare(revision="b" * 40)
        self.assertNotEqual(first["project"], second["project"])
        self.assertNotEqual(first["work_root"], second["work_root"])
        self.assertTrue(Path(first["compose_file"]).exists())

    def complete_hint(self, plan):
        """Selection fixture only; runtime/vector authority is tested separately."""
        root = Path(plan["work_root"])
        backend._write_file(root / "runtime.json", b"{}\n")
        vectors = root / "vector-operations"
        vectors.mkdir(mode=0o700)
        backend._write_file(vectors / "recipe.json", b"{}\n")
        run = vectors / ("1" * 32)
        run.mkdir(mode=0o700)
        (run / "events").mkdir(mode=0o700)
        for name in ("started.json", "container.json"):
            backend._write_file(run / name, b"{}\n")
        backend._write_file(
            run / "complete.json",
            json.dumps(
                {
                    "result": {
                        "status": "complete-clean",
                        "done_count": plan["document_count"],
                        "pending_count": 0,
                    }
                }
            ).encode(),
        )

    def test_complete_catalog_survives_unrelated_app_commit_without_any_docker_call(
        self,
    ):
        first = self.prepare()
        self.complete_hint(first)
        plan_path = Path(first["work_root"]) / "plan.json"
        before = plan_path.read_bytes()
        self.calls.clear()
        second = self.prepare(revision="b" * 40)
        self.assertEqual(first, second)
        self.assertEqual(before, plan_path.read_bytes())
        self.assertEqual(REVISION, second["revision"])
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))
        self.assertEqual(1, len(list((self.config.state_root / "catalogs").iterdir())))

    def test_query_and_storage_changes_keep_catalog_and_original_provenance(
        self,
    ):
        first = self.prepare()
        self.complete_hint(first)
        plan_path = Path(first["work_root"]) / "plan.json"
        before = plan_path.read_bytes()
        for relative in (
            "search/api/retrieval/index.py",
            "search/api/retrieval/pipeline.py",
            "search/api/retrieval/query.py",
            "search/operations/catalog/volumes.py",
            "search/operations/catalog/volume_policy.py",
        ):
            with self.subTest(relative=relative):
                self.original_sources.clear()
                self.original_sources[(REVISION, relative)] = (
                    ROOT / relative
                ).read_text() + "\n# Different storage lifecycle helper\n"
                self.calls.clear()
                loaded = backend.load_prepared(
                    self.config,
                    repo_root=ROOT,
                    bundle_path=Path(first["bundle_path"]),
                    manifest_sha256=first["manifest_sha256"],
                    revision="b" * 40,
                    runner=self.runner,
                )
                self.assertEqual(first, loaded)
                self.assertEqual(REVISION, loaded["revision"])
                self.assertEqual(before, plan_path.read_bytes())
                self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))
                self.assertEqual(
                    1, len(list((self.config.state_root / "catalogs").iterdir()))
                )

    def test_auto_selection_skips_partial_slots_and_keeps_current_partial_slot(self):
        first = self.prepare()
        complete = self.prepare(revision="c" * 40)
        self.complete_hint(complete)
        self.calls.clear()
        self.assertEqual(complete, self.prepare(revision="b" * 40))
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))
        current = self.prepare(revision="b" * 40, catalog_revision="b" * 40)
        self.assertNotEqual(first["work_root"], current["work_root"])
        self.assertNotEqual(complete["work_root"], current["work_root"])
        self.calls.clear()
        self.assertEqual(current, self.prepare(revision="b" * 40))
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))

    def test_data_closure_covers_every_importer_image_source(self):
        from search.operations.catalog.administration import ADMIN_FILES

        self.assertLessEqual(set(ADMIN_FILES), set(backend.DATA_FILES))

    def test_completed_catalog_reuses_original_compose_from_a_new_source_checkout(self):
        first = self.prepare()
        self.complete_hint(first)
        relocated = self.root / "new-checkout"
        relocated.mkdir(mode=0o700)
        for relative in (
            *backend.STAGED_FILES,
            *backend.BUILD_FILES,
            *backend.DATA_FILES,
        ):
            path = relocated / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / relative).read_bytes())
        compose_path = Path(first["compose_file"])
        before = compose_path.read_bytes()
        self.calls.clear()
        loaded = backend.load_prepared(
            self.config,
            repo_root=relocated,
            bundle_path=Path(first["bundle_path"]),
            manifest_sha256=first["manifest_sha256"],
            revision="b" * 40,
            runner=self.runner,
        )
        self.assertEqual(first, loaded)
        self.assertEqual(before, compose_path.read_bytes())
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))

    def test_retained_compose_build_root_must_be_normalized_and_coherent(self):
        first = self.prepare()
        compose = json.loads(Path(first["compose_file"]).read_text())
        arguments = dict(
            repo_root=ROOT,
            data_root=Path(first["data_root"]),
            project=first["project"],
            uid=os.geteuid(),
            gid=os.getegid(),
            policy_source=(ROOT / "scripts/validate_search_compose.py").read_bytes(),
        )
        for path in ("relative", "/tmp/source/../other", "/tmp//source", True):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(backend.BackendError, "build root"),
            ):
                backend.validate_compose(compose, **arguments, sealed_build_root=path)
        changed = copy.deepcopy(compose)
        changed["services"]["opensearch-search"]["build"]["context"] = "/different"
        with self.assertRaisesRegex(ValueError, "build context changed"):
            backend.validate_compose(changed, **arguments, sealed_build_root=str(ROOT))

    def test_data_model_mapping_or_image_change_misses_the_old_catalog(self):
        for relative in (
            "search/catalog/bundle.py",
            "search/catalog/embedding.py",
            "search/operations/catalog/projection.py",
            "deploy/docker/fess/Dockerfile",
        ):
            with self.subTest(relative=relative):
                # Restore a new empty state root for each independent recipe.
                with tempfile.TemporaryDirectory(dir=self.root) as directory:
                    config = self.config
                    self.config = ProductConfig(
                        **{**config.__dict__, "state_root": Path(directory)}
                    )
                    try:
                        first = self.prepare()
                        self.complete_hint(first)
                        self.original_sources[(REVISION, relative)] = (
                            ROOT / relative
                        ).read_text() + "\n# Different original recipe\n"
                        second = self.prepare(revision="b" * 40)
                        self.assertNotEqual(first["project"], second["project"])
                    finally:
                        self.config = config
                        self.original_sources.clear()

    def test_explicit_origin_can_prepare_a_fresh_compatible_package_slot(self):
        result = self.prepare(revision="b" * 40, catalog_revision=REVISION)
        self.assertEqual(REVISION, result["revision"])
        self.assertTrue(result["project"].endswith(REVISION[:12]))
        self.assertEqual(
            result, self.prepare(revision="b" * 40, catalog_revision=REVISION)
        )

    def test_changed_vector_profile_misses_while_orchestration_change_reuses(self):
        first = self.prepare()
        self.complete_hint(first)
        relative = "search/operations/catalog/vectors.py"
        original = (ROOT / relative).read_text()
        self.original_sources[(REVISION, relative)] = original.replace(
            '"work_seconds": 1200', '"work_seconds": 900'
        )
        with self.assertRaises(backend.IncompatibleCatalog):
            backend.validate_compatibility(
                first, repo_root=ROOT, revision="b" * 40, runner=self.runner
            )
        self.original_sources[(REVISION, relative)] = (
            original + "\n# host optimization\n"
        )
        self.assertEqual(first, self.prepare(revision="b" * 40))

    def test_vector_profile_has_no_unsealed_external_constant_dependency(self):
        tree = ast.parse((ROOT / "search/operations/catalog/vectors.py").read_text())
        recipe = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_recipe"
        )
        local = {argument.arg for argument in recipe.args.args}
        for node in ast.walk(recipe):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                local.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                local.update(alias.asname or alias.name for alias in node.names)
        loaded = {
            node.id
            for node in ast.walk(recipe)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        self.assertEqual({"any", "execution", "RuntimeError"}, loaded - local)

    def test_explicit_incompatible_origin_refuses_before_slot_or_docker_mutations(self):
        relative = "search/catalog/embedding.py"
        self.original_sources[(REVISION, relative)] = (
            ROOT / relative
        ).read_text() + "\n# model changed\n"
        with self.assertRaisesRegex(backend.IncompatibleCatalog, "recipe changed"):
            self.prepare(revision="b" * 40, catalog_revision=REVISION)
        self.assertFalse((self.config.state_root / "catalogs").exists())
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))

    def test_missing_origin_history_cannot_turn_into_a_cache_miss(self):
        first = self.prepare()
        self.complete_hint(first)
        self.missing_history = True
        self.calls.clear()
        with self.assertRaises(backend.BackendError) as error:
            self.prepare(revision="b" * 40)
        self.assertNotIsInstance(error.exception, backend.IncompatibleCatalog)
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))
        self.assertEqual(1, len(list((self.config.state_root / "catalogs").iterdir())))

    def test_readable_old_tree_without_new_recipe_input_is_a_cache_miss(self):
        first = self.prepare()
        self.complete_hint(first)
        self.missing_origin_files.add("search/catalog/embedding.py")
        second = self.prepare(revision="b" * 40)
        self.assertNotEqual(first["project"], second["project"])
        self.assertEqual(2, len(list((self.config.state_root / "catalogs").iterdir())))

    def test_explicit_old_tree_without_new_recipe_input_requires_rebuild(self):
        self.missing_origin_files.add("search/catalog/embedding.py")
        with self.assertRaisesRegex(backend.IncompatibleCatalog, "rebuild required"):
            self.prepare(revision="b" * 40, catalog_revision=REVISION)
        self.assertFalse((self.config.state_root / "catalogs").exists())
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))

    def test_origin_blob_read_error_is_not_misclassified_as_a_recipe_cache_miss(self):
        first = self.prepare()
        self.complete_hint(first)
        self.unreadable_origin_files.add("search/catalog/embedding.py")
        self.calls.clear()
        with self.assertRaises(backend.BackendError) as error:
            self.prepare(revision="b" * 40)
        self.assertNotIsInstance(error.exception, backend.IncompatibleCatalog)
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))
        self.assertEqual(1, len(list((self.config.state_root / "catalogs").iterdir())))

    def test_cached_plan_still_requires_unchanged_security_material(self):
        first = self.prepare()
        self.complete_hint(first)
        path = Path(first["data_root"]) / "opensearch/config/roles_mapping.yml"
        path.chmod(0o600)
        path.write_text("changed role mapping\n")
        path.chmod(0o400)
        self.calls.clear()
        with self.assertRaisesRegex(
            backend.BackendError, "security material digest mismatch"
        ):
            self.prepare(revision="b" * 40)
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))

    def test_dirty_bootstrap_source_fails_before_any_slot_is_created(self):
        self.dirty_source = "scripts/bootstrap-search-canary.sh"
        with self.assertRaisesRegex(backend.BackendError, "commit"):
            self.prepare()
        self.assertFalse((self.config.state_root / "catalogs").exists())
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))

    def test_dirty_build_source_fails_before_bootstrap(self):
        self.dirty_source = "deploy/docker/opensearch/Dockerfile"
        with self.assertRaisesRegex(backend.BackendError, "commit"):
            self.prepare()
        self.assertFalse((self.config.state_root / "catalogs").exists())

    def test_bootstrap_failure_is_quarantined_and_cannot_be_reused(self):
        self.fail_bootstrap = True
        with self.assertRaises(backend.BackendError):
            self.prepare()
        slot = next((self.config.state_root / "catalogs").iterdir())
        failure = json.loads((slot / "failure.json").read_text())
        self.assertEqual("quarantined", failure["status"])
        self.assertEqual("bootstrap", failure["phase"])
        self.assertFalse(failure["active_modified"])
        self.assertNotIn("private failure", (slot / "failure.json").read_text())
        self.assertFalse((slot / "plan.json").exists())
        self.assertEqual(
            HASH_IMAGE_ID,
            json.loads((slot / "hash-image.json").read_text())["image_id"],
        )
        self.fail_bootstrap = False
        with self.assertRaisesRegex(backend.BackendError, "quarantined"):
            self.prepare()

    def test_failed_hash_build_is_quarantined_without_bootstrap(self):
        self.fail_hash_build = True
        with self.assertRaises(backend.BackendError):
            self.prepare()
        slot = next((self.config.state_root / "catalogs").iterdir())
        failure = json.loads((slot / "failure.json").read_text())
        self.assertEqual("build_hash_helper", failure["phase"])
        self.assertNotIn("private build failure", json.dumps(failure))
        self.assertTrue(all(argv[0] != "bash" for argv, _ in self.calls))
        self.assertFalse((slot / "plan.json").exists())
        self.assertFalse((slot / "hash-image.json").exists())

    def test_hash_image_identity_and_revision_are_verified_before_bootstrap(self):
        for number, mutation in enumerate(
            (
                lambda value: value.update(revision="b" * 40),
                lambda value: value.update(id="sha256:short"),
                lambda value: value.update(id=True),
                lambda value: value.update(extra="unreviewed"),
            )
        ):
            with self.subTest(mutation=mutation):
                self.hash_metadata_mutation = mutation
                with self.assertRaisesRegex(backend.BackendError, "provenance"):
                    self.prepare(revision=f"{number:012x}" + "0" * 28)
                self.assertTrue(all(argv[0] != "bash" for argv, _ in self.calls))
                self.assertTrue(
                    all(
                        not (slot / "hash-image.json").exists()
                        for slot in (self.config.state_root / "catalogs").iterdir()
                    )
                )

    def test_sealed_hash_build_context_is_checked_again_after_build(self):
        self.hash_context_mutation = True
        with self.assertRaisesRegex(backend.BackendError, "source changed"):
            self.prepare()
        self.assertTrue(all(argv[0] != "bash" for argv, _ in self.calls))

    def test_render_rejects_ports_and_retains_failure_evidence(self):
        self.compose_mutation = lambda c: c["services"]["fess-search"].update(
            ports=["8080:8080"]
        )
        with self.assertRaises(ValueError):
            self.prepare()
        slot = next((self.config.state_root / "catalogs").iterdir())
        self.assertTrue((slot / "failure.json").exists())
        self.assertFalse((slot / "plan.json").exists())

    def test_sealed_plan_and_staged_sources_cannot_be_rewritten(self):
        result = self.prepare()
        path = Path(result["tooling_root"]) / backend.STAGED_FILES[0]
        path.write_bytes(path.read_bytes() + b"\n# mutated\n")
        with self.assertRaisesRegex(backend.BackendError, "source changed"):
            self.prepare()

    def test_repeat_checks_sealed_build_sources_too(self):
        result = self.prepare()
        path = Path(result["tooling_root"]) / backend.BUILD_FILES[0]
        path.write_bytes(path.read_bytes() + b"\n# mutated\n")
        with self.assertRaisesRegex(backend.BackendError, "source changed"):
            self.prepare()

    def test_sealed_environment_cannot_be_rewritten(self):
        result = self.prepare()
        path = Path(result["tooling_root"]) / ".env.search-canary"
        path.write_bytes(path.read_bytes() + b"SECRET=not-allowed\n")
        with self.assertRaisesRegex(backend.BackendError, "environment changed"):
            self.prepare()

    def test_compose_digest_and_plan_identity_are_verified(self):
        result = self.prepare()
        compose_path = Path(result["compose_file"])
        compose = json.loads(compose_path.read_text())
        compose["services"]["fess-search"]["ports"] = ["8080:8080"]
        compose_path.write_text(json.dumps(compose))
        with self.assertRaisesRegex(backend.BackendError, "digest mismatch"):
            self.prepare()

    def test_plan_schema_requires_exact_integer_not_boolean(self):
        result = self.prepare()
        path = Path(result["work_root"]) / "plan.json"
        original = json.loads(path.read_text())
        for name in ("schema_version", "document_count"):
            altered = {**original, name: True}
            path.write_text(json.dumps(altered))
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(backend.BackendError, "identity mismatch"),
            ):
                self.prepare()

    def test_work_directory_replacement_during_render_cannot_publish_plan(self):
        replaced = []

        def replace_work(config):
            data_root = Path(
                config["services"]["opensearch-search"]["volumes"][0]["source"]
            ).parents[1]
            work = data_root.parent
            saved = work.with_name(work.name + "-saved")
            work.rename(saved)
            work.mkdir(mode=0o700)
            replaced.extend((work, saved))

        self.compose_mutation = replace_work
        with self.assertRaisesRegex(ValueError, "directory_changed"):
            self.prepare()
        work, saved = replaced
        self.assertFalse((work / "plan.json").exists())
        self.assertFalse((saved / "plan.json").exists())
        self.assertTrue((saved / "failure.json").exists())

    def test_invalid_revision_or_manifest_fails_before_commands(self):
        for revision in ("main", "a" * 39, "A" * 40, True):
            with (
                self.subTest(revision=revision),
                self.assertRaises(backend.BackendError),
            ):
                self.prepare(revision=revision)
        with self.assertRaises(CatalogError):
            backend.prepare(
                self.config,
                repo_root=ROOT,
                bundle_path=Path(self.bundle["bundle_path"]),
                manifest_sha256="0" * 64,
                revision=REVISION,
                runner=self.runner,
            )
        self.assertEqual([], self.calls)

    def test_slot_retention_stops_without_deleting_existing_data(self):
        slots = self.config.state_root / "catalogs"
        slots.mkdir(mode=0o700)
        for number in range(backend.MAX_SLOTS):
            (slots / f"retained-{number}").mkdir(mode=0o700)
        before = set(slots.iterdir())
        with self.assertRaisesRegex(backend.BackendError, "limit"):
            self.prepare()
        self.assertEqual(before, set(slots.iterdir()))
        self.assertTrue(all(argv[0] == "git" for argv, _ in self.calls))

    def test_only_exact_isolated_project_network_secrets_and_cpu_are_accepted(self):
        project = "depositar-ai-catalog-" + "a" * 12 + "-" + "b" * 12
        data = self.root / "data"
        config = adapted_config(data, project)
        policy = (ROOT / "scripts/validate_search_compose.py").read_bytes()
        arguments = dict(
            repo_root=ROOT,
            data_root=data,
            project=project,
            uid=os.geteuid(),
            gid=os.getegid(),
            policy_source=policy,
        )
        backend.validate_compose(config, **arguments)
        mutations = (
            lambda c: c.update(name="depositar-search-canary"),
            lambda c: c["networks"]["search_backend"].update(internal=False),
            lambda c: c["networks"]["search_backend"].update(name="shared-network"),
            lambda c: c["networks"]["search_backend"].update(external=True),
            lambda c: c["networks"]["search_backend"].update(attachable=True),
            lambda c: c["networks"]["search_backend"].update(ipam={"driver": "custom"}),
            lambda c: next(iter(c["secrets"].values())).update(name="shared-secret"),
            lambda c: next(iter(c["secrets"].values())).update(
                file="/run/active-secret"
            ),
            lambda c: c["services"]["fess-search"].update(ports=["80:8080"]),
            lambda c: c["services"]["opensearch-search"]["volumes"][0].update(
                source="/run/active-index"
            ),
            lambda c: c["services"]["fess-search"]["build"].update(
                context="/run/other-source"
            ),
            lambda c: c["services"]["opensearch-search"]["environment"].update(
                {"cluster.name": project}
            ),
        )
        for number, mutate in enumerate(mutations):
            altered = copy.deepcopy(config)
            mutate(altered)
            with self.subTest(mutation=number), self.assertRaises(ValueError):
                backend.validate_compose(altered, **arguments)

    def test_repository_source_reader_refuses_fifo_before_blocking(self):
        path = self.root / "repository-source"
        os.mkfifo(path, 0o600)
        real_open = os.open

        def guarded_open(name, flags, *args, **kwargs):
            if Path(name).name == path.name:
                self.assertTrue(flags & os.O_NONBLOCK)
            return real_open(name, flags, *args, **kwargs)

        with patch.object(os, "open", side_effect=guarded_open):
            with self.assertRaisesRegex(backend.BackendError, "unsafe"):
                backend._source_bytes(path)

    def test_repository_sources_refuse_links_and_writable_files(self):
        path = self.root / "repository-source"
        path.write_bytes(b"sealed source\n")
        path.chmod(0o666)
        with self.assertRaises(backend.BackendError):
            backend._source_bytes(path)
        path.chmod(0o600)
        linked = self.root / "hardlink"
        os.link(path, linked)
        with self.assertRaises(backend.BackendError):
            backend._source_bytes(path)
        linked.unlink()
        linked.symlink_to(path)
        with self.assertRaises(OSError):
            backend._source_bytes(linked)


if __name__ == "__main__":
    unittest.main()
