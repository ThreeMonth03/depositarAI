"""Reviewed source closure and real-Git private staging; never operate Docker."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from search.operations.deployment import source_bundle as bundle
from search.operations.shared import private_io
from tests.unit.deployment.image_fixtures import (
    blob as blob,
    fixture_sources as fixture_sources,
    fixture_tree as fixture_tree,
)


ROOT = Path(__file__).resolve().parents[3]
REVISION = "a" * 40
IMAGE = "sha256:" + "b" * 64


class SourceBundlePlanTests(unittest.TestCase):
    def setUp(self):
        self.sources = fixture_sources()
        self.tree = fixture_tree(self.sources)

    def plan(self):
        return bundle.plan_source_bundle(REVISION, self.tree, self.sources)

    def test_four_reviewed_services_have_exact_canonical_source_maps(self):
        plan = self.plan()
        self.assertEqual(
            {"conversation-api", "web", "reranker", "web-nginx"}, set(plan["services"])
        )
        self.assertFalse(plan["images_built"])
        self.assertFalse(plan["runtime_verified"])
        self.assertEqual("source_plan_only", plan["status"])
        self.assertEqual(sum(map(len, self.sources.values())), plan["total_bytes"])
        for name, service in plan["services"].items():
            self.assertEqual(
                hashlib.sha256(private_io.canonical(service["source_map"])).hexdigest(),
                service["source_sha256"],
            )
            self.assertEqual(
                name, service["required_labels"][bundle.LABEL_PREFIX + "service"]
            )
            self.assertTrue(all("@sha256:" in base for base in service["base_images"]))
            self.assertNotIn("project", service)

    def test_reranker_has_no_api_operator_or_browser_source_dependency(self):
        sources = self.plan()["services"]["reranker"]["source_map"]
        self.assertIn("search/serialization.py", sources)
        self.assertIn("search/secrets.py", sources)
        for path in sources:
            with self.subTest(path=path):
                self.assertFalse(
                    path.startswith(
                        (
                            "search/api/",
                            "search/web/",
                            "search/operations/",
                            "search/transport/",
                        )
                    )
                )

    def test_reviewed_real_source_is_complete_without_working_tree_extras(self):
        sources = {path: (ROOT / path).read_bytes() for path in self.sources}
        tree = fixture_tree(sources)
        for path in (
            "search/api/requirements.in",
            "search/api/requirements-test.lock",
            ".env.private",
            "research/private-note.txt",
        ):
            tree[path] = {"mode": "100644", "type": "blob", "object": "c" * 40}
        plan = bundle.plan_source_bundle(REVISION, tree, sources)
        self.assertIn("search/api/shared/readiness.py", plan["files"])
        self.assertNotIn(".env.private", plan["files"])
        self.assertNotIn(
            "search/reranker/requirements.lock", plan["services"]["web"]["source_map"]
        )
        self.assertEqual(
            [bundle.NODE_BASE, bundle.PYTHON_BASE],
            plan["services"]["web"]["base_images"],
        )
        for service in plan["services"].values():
            self.assertFalse(
                any(
                    name.startswith("search/catalog/build/")
                    for name in service["source_map"]
                )
            )
            dockerfile = sources[service["dockerfile"]].decode("utf-8")
            actual_bases, stages = [], set()
            for line in dockerfile.splitlines():
                words = line.split()
                if not words or words[0] != "FROM":
                    continue
                if words[1] not in stages:
                    actual_bases.append(words[1])
                if len(words) == 4 and words[2].upper() == "AS":
                    stages.add(words[3])
            self.assertEqual(actual_bases, service["base_images"])
            self.assertEqual(
                "**", sources[service["dockerignore"]].decode("utf-8").splitlines()[0]
            )

    def test_input_order_does_not_change_identity_or_mutate_inputs(self):
        before = copy.deepcopy((self.tree, self.sources))
        forward = self.plan()
        reverse = bundle.plan_source_bundle(
            REVISION,
            dict(reversed(list(self.tree.items()))),
            dict(reversed(list(self.sources.items()))),
        )
        self.assertEqual(forward, reverse)
        self.assertEqual(before, (self.tree, self.sources))

    def test_git_mode_changes_are_bound_and_preserved(self):
        before = self.plan()
        self.tree["search/catalog/query.py"]["mode"] = "100755"
        after = self.plan()
        self.assertEqual("100755", after["files"]["search/catalog/query.py"]["mode"])
        self.assertNotEqual(before["source_map_sha256"], after["source_map_sha256"])
        self.assertNotEqual(
            before["services"]["web"]["source_sha256"],
            after["services"]["web"]["source_sha256"],
        )
        self.assertEqual(before["services"]["reranker"], after["services"]["reranker"])

    def test_changed_content_changes_only_dependent_services(self):
        before = self.plan()
        self.sources["web/src/styles.css"] += b"updated\n"
        self.tree["web/src/styles.css"]["object"] = blob(
            self.sources["web/src/styles.css"]
        )
        after = self.plan()
        self.assertNotEqual(
            before["services"]["web"]["source_sha256"],
            after["services"]["web"]["source_sha256"],
        )
        self.assertEqual(
            before["services"]["conversation-api"],
            after["services"]["conversation-api"],
        )

    def test_recipe_and_ignore_mutations_require_review_even_with_valid_git_blobs(self):
        for path in (
            "deploy/docker/web/Dockerfile",
            "deploy/docker/web/Dockerfile.dockerignore",
        ):
            with self.subTest(path=path):
                sources = dict(self.sources)
                tree = copy.deepcopy(self.tree)
                sources[path] += b"# changed\n"
                tree[path]["object"] = blob(sources[path])
                with self.assertRaisesRegex(
                    bundle.SourceBundleError, "recipe_unreviewed"
                ):
                    bundle.plan_source_bundle(REVISION, tree, sources)

    def test_new_admitted_copy_inputs_are_rejected_not_silently_omitted(self):
        for path in (
            "search/api/private.py",
            "search/api/shared/new.py",
            "search/planning/new.py",
            "search/reranker/new.py",
            "search/web/new.py",
            "web/src/private.ts",
            "web/src/search/private.ts",
            "web/src/new.tsx",
            "web/src/new.css",
        ):
            with self.subTest(path=path):
                tree = copy.deepcopy(self.tree)
                tree[path] = {"mode": "100644", "type": "blob", "object": "c" * 40}
                with self.assertRaisesRegex(
                    bundle.SourceBundleError, "unreviewed_copy_input"
                ):
                    bundle.plan_source_bundle(REVISION, tree, self.sources)

    def test_ignored_nested_files_are_not_implicitly_admitted_by_star(self):
        self.tree["search/api/unused/private.py"] = {
            "mode": "100644",
            "type": "blob",
            "object": "c" * 40,
        }
        self.assertNotIn("search/api/unused/private.py", self.plan()["files"])

    def test_committed_closure_rejects_extra_and_missing_raw_bytes(self):
        for change in ("extra", "missing"):
            sources = dict(self.sources)
            if change == "extra":
                sources[".env.private"] = b"do not copy this\n"
            else:
                sources.pop("search/catalog/query.py")
            with self.assertRaisesRegex(
                bundle.SourceBundleError, "committed_closure_invalid"
            ):
                bundle.plan_source_bundle(REVISION, self.tree, sources)

    def test_missing_tracked_source_and_selected_link_modes_rejected(self):
        tree = copy.deepcopy(self.tree)
        tree.pop("search/catalog/query.py")
        with self.assertRaisesRegex(
            bundle.SourceBundleError, "copy_closure_incomplete"
        ):
            bundle.plan_source_bundle(REVISION, tree, self.sources)
        for mode, kind in (("120000", "blob"), ("160000", "commit")):
            tree = copy.deepcopy(self.tree)
            tree["search/catalog/query.py"].update(mode=mode, type=kind)
            with self.assertRaisesRegex(bundle.SourceBundleError, "copy_mode_invalid"):
                bundle.plan_source_bundle(REVISION, tree, self.sources)

    def test_git_blob_bytes_are_checked_not_only_caller_sha256(self):
        self.sources["search/catalog/query.py"] += b"different bytes\n"
        with self.assertRaisesRegex(bundle.SourceBundleError, "git_blob_changed"):
            self.plan()

    def test_file_total_tree_limits_and_exact_types(self):
        for raw in (b"", "not bytes", b"x" * (bundle.MAX_FILE_BYTES + 1)):
            with self.subTest(kind=type(raw)):
                sources = dict(self.sources, **{"search/catalog/query.py": raw})
                with self.assertRaisesRegex(
                    bundle.SourceBundleError, "file_byte_limit"
                ):
                    bundle.plan_source_bundle(REVISION, self.tree, sources)
        with mock.patch.object(bundle, "MAX_TOTAL_BYTES", 10):
            with self.assertRaisesRegex(bundle.SourceBundleError, "total_byte_limit"):
                self.plan()
        with mock.patch.object(bundle, "MAX_TRACKED_FILES", 2):
            with self.assertRaisesRegex(bundle.SourceBundleError, "tree_limit"):
                self.plan()
        with mock.patch.object(bundle, "MAX_SOURCE_FILES", 2):
            with self.assertRaisesRegex(
                bundle.SourceBundleError, "copy_closure_incomplete"
            ):
                self.plan()

    def test_invalid_revision_path_tree_shapes_fail_with_fixed_codes(self):
        for revision in (True, "main", "a" * 39, "A" * 40, "a" * 40 + "\n"):
            with self.assertRaisesRegex(bundle.SourceBundleError, "revision_invalid"):
                bundle.plan_source_bundle(revision, self.tree, self.sources)
        for path in (
            ".",
            "../private",
            "/private",
            "search//new",
            "search/new\n",
            "search\\new",
        ):
            tree = copy.deepcopy(self.tree)
            tree[path] = {"mode": "100644", "type": "blob", "object": "c" * 40}
            with self.assertRaisesRegex(bundle.SourceBundleError, "path_invalid"):
                bundle.plan_source_bundle(REVISION, tree, self.sources)
        for field, value in (
            ("mode", []),
            ("type", []),
            ("object", True),
            ("extra", "no"),
        ):
            tree = copy.deepcopy(self.tree)
            tree["search/catalog/query.py"][field] = value
            with self.assertRaisesRegex(bundle.SourceBundleError, "tree_invalid"):
                bundle.plan_source_bundle(REVISION, tree, self.sources)

    def test_image_summary_matches_client_lease_without_release_project_identity(self):
        plan = self.plan()
        labels = dict(plan["services"]["conversation-api"]["required_labels"])
        labels["org.opencontainers.image.title"] = "public base label"
        environment = [
            "SOURCE_REVISION=" + REVISION,
            "PYTHONDONTWRITEBYTECODE=1",
            "PYTHONUNBUFFERED=1",
            "HAYSTACK_TELEMETRY_ENABLED=false",
            "HAYSTACK_CONTENT_TRACING_ENABLED=false",
            "PATH=/usr/local/bin:/usr/bin",
            "UNEXPECTED_PRIVATE_TOKEN=do-not-return-this-fixture-sentinel",
        ]
        inspection = {
            "Id": IMAGE,
            "Config": {"Labels": labels, "Env": environment},
            "RepoTags": ["not-authority:latest"],
        }
        summary = bundle.validate_built_image(
            plan, "conversation-api", IMAGE, inspection
        )
        self.assertEqual(
            {
                "schema_version",
                "revision",
                "service",
                "source_sha256",
                "image_id",
                "image_environment_sha256",
            },
            set(summary),
        )
        self.assertEqual(
            plan["services"]["conversation-api"]["source_sha256"],
            summary["source_sha256"],
        )
        self.assertEqual(
            hashlib.sha256(
                private_io.canonical(bundle.parse_image_environment(environment))
            ).hexdigest(),
            summary["image_environment_sha256"],
        )
        self.assertNotIn("UNEXPECTED_PRIVATE_TOKEN", str(summary))
        self.assertNotIn("do-not-return-this-fixture-sentinel", str(summary))

    def test_recipe_environment_flags_are_required_and_nginx_has_no_revision_env(self):
        plan = self.plan()
        for name in plan["services"]:
            environment = bundle._required_environment(name, REVISION)
            entries = [key + "=" + value for key, value in environment.items()]
            inspection = {
                "Id": IMAGE,
                "Config": {
                    "Labels": plan["services"][name]["required_labels"],
                    "Env": entries,
                },
            }
            summary = bundle.validate_built_image(plan, name, IMAGE, inspection)
            self.assertEqual(name, summary["service"])
            if name != "web-nginx":
                inspection["Config"]["Env"] = [
                    entry
                    for entry in entries
                    if not entry.startswith("SOURCE_REVISION=")
                ]
                with self.assertRaisesRegex(
                    bundle.SourceBundleError, "environment_flags_changed"
                ):
                    bundle.validate_built_image(plan, name, IMAGE, inspection)
        self.assertEqual({}, bundle._required_environment("web-nginx", REVISION))
        inspection = {
            "Id": IMAGE,
            "Config": {
                "Labels": plan["services"]["conversation-api"]["required_labels"],
                "Env": [
                    "SOURCE_REVISION=" + REVISION,
                    "PYTHONDONTWRITEBYTECODE=1",
                    "PYTHONUNBUFFERED=1",
                    "HAYSTACK_TELEMETRY_ENABLED=true",
                    "HAYSTACK_CONTENT_TRACING_ENABLED=false",
                ],
            },
        }
        with self.assertRaisesRegex(
            bundle.SourceBundleError, "environment_flags_changed"
        ):
            bundle.validate_built_image(plan, "conversation-api", IMAGE, inspection)

    def test_image_label_and_immutable_identity_mismatches_rejected(self):
        plan = self.plan()
        labels = plan["services"]["conversation-api"]["required_labels"]
        for key in labels:
            actual = dict(labels)
            actual[key] = "wrong"
            with self.assertRaisesRegex(
                bundle.SourceBundleError, "image_labels_changed"
            ):
                bundle.validate_built_image(
                    plan,
                    "conversation-api",
                    IMAGE,
                    {"Id": IMAGE, "Config": {"Labels": actual}},
                )
        for reference in ("image:latest", "sha256:" + "B" * 64, True):
            with self.assertRaisesRegex(bundle.SourceBundleError, "image_id_invalid"):
                bundle.validate_built_image(plan, "conversation-api", reference, {})
        with self.assertRaisesRegex(bundle.SourceBundleError, "image_identity_changed"):
            bundle.validate_built_image(
                plan, "conversation-api", IMAGE, {"Id": "sha256:" + "c" * 64}
            )
        with self.assertRaisesRegex(bundle.SourceBundleError, "service_invalid"):
            bundle.validate_built_image(plan, "scope-model", IMAGE, {})

    def test_tampered_plan_digests_flags_pins_and_numeric_types_rejected(self):
        for change in (
            lambda p: p.update(total_bytes=True),
            lambda p: p.update(schema_version=True),
            lambda p: p.update(images_built=True),
            lambda p: p.update(images_built=float("nan")),
            lambda p: p["services"]["web"].update(base_images=["python:latest"]),
            lambda p: p["services"]["web"].update(source_sha256="c" * 64),
            lambda p: p["files"]["search/catalog/query.py"].update(size=True),
            lambda p: p["files"]["search/catalog/query.py"].update(mode=[]),
            lambda p: p.update(extra="not allowed"),
        ):
            plan = self.plan()
            change(plan)
            with self.assertRaises(bundle.SourceBundleError):
                bundle.validate_built_image(plan, "web", IMAGE, {})


class ImageEnvironmentParserTests(unittest.TestCase):
    def test_environment_values_stay_in_memory_and_digest_is_order_independent(self):
        entries = [
            "PATH=/usr/bin",
            "SOURCE_REVISION=" + REVISION,
            "EMPTY=",
            "VALUE_WITH_EQUAL=a=b",
        ]
        forward = bundle.parse_image_environment(entries)
        reverse = bundle.parse_image_environment(list(reversed(entries)))
        self.assertEqual(forward, reverse)
        self.assertEqual("", forward["EMPTY"])
        self.assertEqual("a=b", forward["VALUE_WITH_EQUAL"])
        self.assertEqual(private_io.canonical(forward), private_io.canonical(reverse))
        self.assertEqual({}, bundle.parse_image_environment([]))

    def test_environment_types_keys_duplicates_and_control_values_rejected(self):
        for entries in (
            None,
            {},
            ("PATH=/usr/bin",),
            [True],
            ["missing-equal"],
            ["=empty-key"],
            ["1BAD=value"],
            ["BAD-KEY=value"],
            ["KEY=value\x00private"],
            ["KEY=value\nprivate"],
            ["KEY=value\rprivate"],
            ["KEY=\ud800"],
            ["KEY=first", "KEY=second"],
            ["X" * 65 + "=too-long-key"],
        ):
            with self.subTest(entries=type(entries)):
                with self.assertRaisesRegex(
                    bundle.SourceBundleError, "environment_invalid"
                ) as caught:
                    bundle.parse_image_environment(entries)
                self.assertNotIn("private", str(caught.exception))

    def test_environment_count_per_entry_bytes_and_total_limits(self):
        with self.assertRaisesRegex(bundle.SourceBundleError, "environment_invalid"):
            bundle.parse_image_environment(
                ["K" + str(index) + "=x" for index in range(129)]
            )
        with self.assertRaisesRegex(bundle.SourceBundleError, "environment_invalid"):
            bundle.parse_image_environment(["KEY=" + "x" * 4096])
        with mock.patch.object(bundle, "MAX_ENVIRONMENT_ENTRY_BYTES", 10):
            with self.assertRaisesRegex(bundle.SourceBundleError, "environment_limit"):
                bundle.parse_image_environment(["KEY=中文中文"])
        with mock.patch.object(bundle, "MAX_ENVIRONMENT_BYTES", 10):
            with self.assertRaisesRegex(bundle.SourceBundleError, "environment_limit"):
                bundle.parse_image_environment(["FIRST=1", "SECOND=2"])


class SourceBundleStageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="depositar-source-context-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "checkout"
        self.repo.mkdir(mode=0o700)
        self.sources = fixture_sources()
        for path, raw in self.sources.items():
            destination = self.repo / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        (self.repo / "search/catalog/query.py").chmod(0o755)
        self.git("init", "--quiet")
        self.git("add", ".")
        self.commit()
        self.revision = self.git("rev-parse", "HEAD").strip()
        self.destination = self.root / "staged"

    def git(self, *arguments):
        result = subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def commit(self):
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "--quiet",
            "-m",
            "Fixture",
        )

    def stage(self, runner=None):
        return bundle.stage_source_bundle(
            self.repo, self.revision, self.destination, runner=runner
        )

    def test_independent_committed_plan_is_read_only_and_matches_staged_manifest(self):
        planned = bundle.committed_source_plan(self.repo, self.revision)
        self.assertFalse(self.destination.exists())
        self.assertEqual(planned, self.stage())
        again = bundle.committed_source_plan(self.repo, self.revision)
        self.assertEqual(planned, again)

    def test_committed_plan_rejects_dirty_or_different_revision_without_artifacts(self):
        with self.assertRaisesRegex(bundle.SourceBundleError, "revision_changed"):
            bundle.committed_source_plan(self.repo, "b" * 40)
        (self.repo / "search/api/conversation/app.py").write_text("dirty source\n")
        with self.assertRaisesRegex(
            bundle.SourceBundleError, "clean_revision_required"
        ):
            bundle.committed_source_plan(self.repo, self.revision)
        self.assertFalse(self.destination.exists())

    def test_real_git_context_manifest_modes_and_exact_closure(self):
        (self.repo / ".env.private").write_text("must not enter build context\n")
        commands = []

        def runner(argv, *, timeout):
            commands.append(argv)
            return bundle._runner(argv, timeout=timeout)

        plan = self.stage(runner)
        context = self.destination / "context"
        self.assertEqual(
            {"context", "manifest.json"},
            {path.name for path in self.destination.iterdir()},
        )
        self.assertEqual(0o700, self.destination.stat().st_mode & 0o777)
        self.assertEqual(0o700, context.stat().st_mode & 0o777)
        self.assertEqual(
            0o600, (self.destination / "manifest.json").stat().st_mode & 0o777
        )
        self.assertEqual(0o755, (context / "search/api").stat().st_mode & 0o777)
        self.assertEqual(
            0o755, (context / "search/catalog/query.py").stat().st_mode & 0o777
        )
        self.assertEqual(
            0o644, (context / "search/api/conversation/app.py").stat().st_mode & 0o777
        )
        self.assertEqual(
            set(self.sources),
            {
                str(path.relative_to(context))
                for path in context.rglob("*")
                if path.is_file()
            },
        )
        self.assertEqual(
            plan, json.loads((self.destination / "manifest.json").read_text())
        )
        for path, raw in self.sources.items():
            self.assertEqual(raw, (context / path).read_bytes())
        self.assertTrue(all(command[0] == "git" for command in commands))
        self.assertTrue(
            all(command[1] == "--no-replace-objects" for command in commands)
        )
        self.assertEqual(4, sum("rev-parse" in command for command in commands))

    def test_existing_or_partial_roots_never_overwritten_or_removed(self):
        self.destination.mkdir(mode=0o700)
        marker = self.destination / "evidence"
        marker.write_bytes(b"preserve\n")
        with self.assertRaisesRegex(bundle.SourceBundleError, "staging_failed"):
            self.stage()
        self.assertEqual(b"preserve\n", marker.read_bytes())
        self.assertEqual(
            {"evidence"}, {path.name for path in self.destination.iterdir()}
        )

    def test_restrictive_umask_does_not_break_nonroot_copy_modes(self):
        previous = os.umask(0o077)
        try:
            self.stage()
        finally:
            os.umask(previous)
        context = self.destination / "context"
        self.assertEqual(0o700, context.stat().st_mode & 0o777)
        self.assertEqual(0o755, (context / "search/api").stat().st_mode & 0o777)
        self.assertEqual(
            0o644, (context / "search/api/conversation/app.py").stat().st_mode & 0o777
        )
        self.assertEqual(
            0o755, (context / "search/catalog/query.py").stat().st_mode & 0o777
        )

    def test_parent_mode_and_symlink_roots_are_rejected(self):
        self.root.chmod(0o755)
        try:
            with self.assertRaises(ValueError):
                self.stage()
        finally:
            self.root.chmod(0o700)
        self.destination.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(bundle.SourceBundleError, "host_path_invalid"):
            self.stage()
        self.assertTrue(self.destination.is_symlink())

    def test_dirty_source_untracked_production_and_wrong_sha_are_rejected_before_staging(
        self,
    ):
        (self.repo / "search/api/conversation/app.py").write_text("dirty source\n")
        with self.assertRaisesRegex(
            bundle.SourceBundleError, "clean_revision_required"
        ):
            self.stage()
        self.assertFalse(self.destination.exists())
        (self.repo / "search/api/conversation/app.py").write_bytes(
            self.sources["search/api/conversation/app.py"]
        )
        (self.repo / "search/new-untracked.py").write_text("source not committed\n")
        with self.assertRaisesRegex(
            bundle.SourceBundleError, "clean_revision_required"
        ):
            self.stage()
        (self.repo / "search/new-untracked.py").unlink()
        with self.assertRaisesRegex(bundle.SourceBundleError, "revision_changed"):
            bundle.stage_source_bundle(self.repo, REVISION, self.destination)

    def test_replace_refs_cannot_seal_a_different_tree_under_the_same_revision(self):
        # Normal Git presents replacement B as commit A, and core.filemode=false
        # makes the original working-file mode irrelevant. Every source read,
        # including the shared clean guard, must bypass refs/replace.
        self.git("config", "core.filemode", "false")
        self.git("update-index", "--chmod=+x", "search/api/conversation/app.py")
        self.commit()
        replacement = self.git("rev-parse", "HEAD").strip()
        self.git("replace", self.revision, replacement)
        self.git("update-ref", "HEAD", self.revision)
        self.assertEqual("", self.git("status", "--porcelain", "--", "search"))
        with self.assertRaisesRegex(
            bundle.SourceBundleError, "clean_revision_required"
        ):
            self.stage()
        self.assertFalse(self.destination.exists())
        # Restoring the original literal index permits staging A even while its
        # unused replacement ref still exists; A's 0644 mode is not B's 0755.
        self.git("--no-replace-objects", "read-tree", self.revision)
        plan = self.stage()
        self.assertEqual(
            "100644", plan["files"]["search/api/conversation/app.py"]["mode"]
        )
        self.assertEqual(
            0o644,
            (self.destination / "context/search/api/conversation/app.py").stat().st_mode
            & 0o777,
        )

    def test_source_change_after_staging_retains_partial_evidence_without_manifest(
        self,
    ):
        real_write = bundle._write_source
        changed = False

        def change_after_write(*arguments):
            nonlocal changed
            real_write(*arguments)
            if not changed:
                changed = True
                (self.repo / "search/api/conversation/app.py").write_text(
                    "changed during stage\n"
                )

        with mock.patch.object(bundle, "_write_source", side_effect=change_after_write):
            with self.assertRaisesRegex(
                bundle.SourceBundleError, "clean_revision_required"
            ):
                self.stage()
        self.assertTrue((self.destination / "context").is_dir())
        self.assertFalse((self.destination / "manifest.json").exists())

    def test_poststage_file_and_extra_closure_tampering_are_rejected(self):
        real_write = bundle._write_source
        count = 0

        def add_unexpected_file(*arguments):
            nonlocal count
            real_write(*arguments)
            count += 1
            if count == 1:
                (self.destination / "context/extra-private.txt").write_text(
                    "not allowed\n"
                )

        with mock.patch.object(
            bundle, "_write_source", side_effect=add_unexpected_file
        ):
            with self.assertRaisesRegex(
                bundle.SourceBundleError, "staged_closure_changed"
            ):
                self.stage()
        self.assertTrue((self.destination / "context/extra-private.txt").exists())
        self.assertFalse((self.destination / "manifest.json").exists())

    def test_stage_verification_detects_source_bytes_mode_and_link_replacement(self):
        real_verify = bundle._verify_context
        target = self.destination / "context/search/api/conversation/app.py"

        def change_before_verify(*arguments, **keywords):
            target.write_bytes(b"unexpected copied bytes\n")
            return real_verify(*arguments, **keywords)

        with mock.patch.object(
            bundle, "_verify_context", side_effect=change_before_verify
        ):
            with self.assertRaisesRegex(
                bundle.SourceBundleError, "staged_source_changed"
            ):
                self.stage()
        self.assertFalse((self.destination / "manifest.json").exists())
        self.assertTrue(target.exists())

        context = self.destination / "context"
        descriptor = private_io.open_directory(context)
        try:
            tree = fixture_tree(self.sources)
            tree["search/catalog/query.py"]["mode"] = "100755"
            records = bundle.plan_source_bundle(self.revision, tree, self.sources)[
                "files"
            ]
            target.write_bytes(self.sources["search/api/conversation/app.py"])
            target.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "file_unsafe"):
                bundle._verify_context(descriptor, records)
            target.chmod(0o644)
            # A second link outside the context leaves its exact name closure
            # unchanged, but still invalidates the single-link source proof.
            os.link(target, self.root / "source-hardlink")
            with self.assertRaisesRegex(ValueError, "file_unsafe"):
                bundle._verify_context(descriptor, records)
        finally:
            os.close(descriptor)

    def test_git_runner_errors_and_output_limits_do_not_leak_private_stderr(self):
        def failed(argv, *, timeout):
            return subprocess.CompletedProcess(
                argv, 1, b"", b"private credentials must never appear"
            )

        with self.assertRaisesRegex(
            bundle.SourceBundleError, "clean_revision_required"
        ) as caught:
            self.stage(failed)
        self.assertNotIn("credentials", str(caught.exception))
        self.assertFalse(self.destination.exists())
        git = bundle._Git(
            self.repo,
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"x" * 11, b""),
        )
        with self.assertRaisesRegex(bundle.SourceBundleError, "git_output_limit"):
            git.raw(["git"], max_bytes=10)


class BoundedSourceGitRunnerTests(unittest.TestCase):
    def test_binary_output_and_nonzero_exit_keep_completed_process_contract(self):
        argv = [
            sys.executable,
            "-c",
            "import os; os.write(1, bytes([0,255,10])); "
            "os.write(2, bytes([128,0])); raise SystemExit(7)",
        ]
        result = bundle._runner(argv, timeout=2)
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertIs(result.args, argv)
        self.assertEqual(7, result.returncode)
        self.assertEqual(b"\x00\xff\n", result.stdout)
        self.assertEqual(b"\x80\x00", result.stderr)

    def test_pipe_cap_counts_stdout_and_stderr_together(self):
        argv = [
            sys.executable,
            "-c",
            "import os; os.write(1,b'abcd'); os.write(2,b'efgh')",
        ]
        with mock.patch.object(bundle, "MAX_GIT_OUTPUT_BYTES", 8):
            result = bundle._runner(argv, timeout=2)
        self.assertEqual((b"abcd", b"efgh"), (result.stdout, result.stderr))
        with (
            mock.patch.object(bundle, "MAX_GIT_OUTPUT_BYTES", 7),
            self.assertRaisesRegex(
                bundle.SourceBundleError, "^source_bundle_git_output_limit$"
            ),
        ):
            bundle._runner(argv, timeout=2)

    def test_eof_before_leader_exit_keeps_direct_and_git_wait_error_policy(self):
        original_popen = subprocess.Popen
        argv = [
            sys.executable,
            "-c",
            "import os,time; os.close(1); os.close(2); time.sleep(10)",
        ]
        for through_git in (False, True):
            created = []

            def spawn(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                process.wait = mock.Mock(wraps=process.wait)
                created.append(process)
                return process

            with (
                self.subTest(through_git=through_git),
                mock.patch.object(bundle.subprocess, "Popen", side_effect=spawn),
            ):
                if through_git:
                    git = bundle._Git(
                        Path("/fixture"),
                        lambda _argv, **_kwargs: bundle._runner(argv, timeout=1),
                    )
                    with self.assertRaisesRegex(
                        bundle.SourceBundleError, "^source_bundle_git_failed$"
                    ):
                        git.raw(["git", "--version"])
                else:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        bundle._runner(argv, timeout=1)
                self.assertEqual(1, len(created))
                process = created[0]
                # Prove EOF reached the final wait, rather than timing out while
                # reading pipes and observing only the five-second cleanup wait.
                self.assertLessEqual(
                    process.wait.call_args_list[0].kwargs["timeout"], 1
                )
                self.assertIsNotNone(process.returncode)
                self.assertTrue(process.stdout.closed)
                self.assertTrue(process.stderr.closed)

    def test_call_budget_includes_maximum_closure_and_all_staging_clean_checks(self):
        git = bundle._Git(
            Path("/fixture"),
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"ok", b""),
        )
        for _ in range(bundle.MAX_SOURCE_FILES + 9):
            self.assertEqual(b"ok", git.raw(["git"]))
        self.assertEqual(b"ok", git.raw(["git"]))
        with self.assertRaisesRegex(bundle.SourceBundleError, "budget_exceeded"):
            git.raw(["git"])

    def test_live_output_capture_stops_at_bound(self):
        with mock.patch.object(bundle, "MAX_GIT_OUTPUT_BYTES", 4096):
            with self.assertRaisesRegex(bundle.SourceBundleError, "git_output_limit"):
                bundle._runner(
                    [sys.executable, "-c", "import os; os.write(1, b'x' * 65536)"],
                    timeout=2,
                )

    def test_live_timeout_stops_process_group(self):
        with self.assertRaisesRegex(bundle.SourceBundleError, "git_timeout"):
            bundle._runner(
                [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1
            )

    def test_exited_leader_does_not_leave_child_holding_capture_pipe(self):
        program = (
            "import os, time; child = os.fork(); "
            "time.sleep(10) if child == 0 else os._exit(0)"
        )
        with mock.patch.object(bundle.os, "killpg", wraps=os.killpg) as kill:
            with self.assertRaisesRegex(bundle.SourceBundleError, "git_timeout"):
                bundle._runner([sys.executable, "-c", program], timeout=0.1)
        self.assertEqual(1, kill.call_count)

    def test_git_tree_duplicate_and_nonterminated_output_rejected(self):
        row = b"100644 blob " + b"a" * 40 + b"\tREADME.md\x00"
        for raw in (row + row, row[:-1]):
            git = bundle._Git(
                Path("/fixture"),
                lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, raw, b""),
            )
            with self.assertRaisesRegex(bundle.SourceBundleError, "git_tree_invalid"):
                git.tree(REVISION)


if __name__ == "__main__":
    unittest.main()
