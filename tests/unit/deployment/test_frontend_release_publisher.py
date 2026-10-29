"""The release producer must preserve tested images and never overwrite a release."""

import hashlib
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "frontend_release_publisher", ROOT / "scripts/ci/frontend-release.py"
)
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
REVISION = "a" * 40
MANIFESTS = {"web": "sha256:" + "b" * 64, "web-nginx": "sha256:" + "c" * 64}
CONFIGS = {"web": "sha256:" + "d" * 64, "web-nginx": "sha256:" + "e" * 64}
SHARED = "search.operations.deployment.frontend_artifact"


class PublisherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="frontend-publisher-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tested = self.root / "tested.json"
        self.output = self.root / "release"
        self.calls = []
        self.current = dict(MANIFESTS)
        self.tag_created = False
        self.published = False
        self.incomplete_assets = False
        self.bad_remote_digest = False
        self.changed_after_save = False
        self.existing_release = False
        self.api_denied = False
        self.dirty = False
        self.validated_archives = []
        self.environment = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": publisher.REPOSITORY,
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": REVISION,
            "EXPECTED_RELEASE_REVISION": REVISION,
            "RUNNER_ENVIRONMENT": "github-hosted",
            "GITHUB_WORKFLOW_REF": publisher.REPOSITORY
            + "/"
            + publisher.WORKFLOW
            + "@refs/heads/main",
        }
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, self.environment))
        stack.enter_context(mock.patch.object(publisher, "_command", self.command))
        shared = SimpleNamespace(
            describe_archive=self.describe,
            validate_manifest=lambda value, **_: value,
            load_manifest=lambda path, **_: json.loads(path.read_text()),
            validate_archive=self.validate_archive,
        )
        stack.enter_context(mock.patch.dict(sys.modules, {SHARED: shared}))

    def describe(self, path, revision, role):
        self.assertEqual(revision, REVISION)
        return {
            "archive": path.name,
            "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "archive_bytes": path.stat().st_size,
            "platform_manifest": MANIFESTS[role],
            "image_config": CONFIGS[role],
        }

    def validate_archive(self, path, entry, revision, role):
        self.validated_archives.append((path.name, role, revision))
        self.assertEqual(entry, self.describe(path, revision, role))

    @staticmethod
    def response(args, value, code=0):
        return subprocess.CompletedProcess(args, code, json.dumps(value), "")

    def command(self, args, *, timeout=120):
        self.calls.append(args)
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, REVISION + "\n", "")
        if args[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(args, int(self.dirty), "", "")
        if args[:3] == ["docker", "image", "inspect"]:
            role = "web-nginx" if "web-nginx:" in args[-1] else "web"
            return self.response(
                args,
                [
                    {
                        "Id": self.current[role],
                        "Os": "linux",
                        "Architecture": "amd64",
                        "Descriptor": {
                            "digest": self.current[role],
                            "mediaType": "application/vnd.oci.image.manifest.v1+json",
                            "platform": {"architecture": "amd64", "os": "linux"},
                        },
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": REVISION,
                                "io.depositar.ai.public-entry.version": "2",
                                "io.depositar.ai.conversation.version": "4",
                                "io.depositar.ai.frontend.version": "1",
                            }
                        },
                    }
                ],
            )
        if args[:3] == ["docker", "image", "tag"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["docker", "image", "save"]:
            path = Path(args[args.index("--output") + 1])
            path.write_bytes(b"synthetic archive for " + path.name.encode())
            if self.changed_after_save:
                self.current["web"] = "sha256:" + "f" * 64
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["gh", "release", "upload"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["gh", "api"]:
            route = args[2].removeprefix("repos/" + publisher.REPOSITORY + "/")
            if self.api_denied:
                return self.response(args, {"status": 403}, 1)
            if route.startswith("releases/tags/"):
                return (
                    self.response(args, {"id": 27}, 0)
                    if self.existing_release
                    else self.response(args, {"status": "404"}, 1)
                )
            if route.startswith("git/ref/tags/"):
                return (
                    self.response(args, {"object": {"type": "commit", "sha": REVISION}})
                    if self.tag_created
                    else self.response(args, {"status": 404}, 1)
                )
            if route == "git/refs":
                self.tag_created = True
                return self.response(
                    args, {"object": {"type": "commit", "sha": REVISION}}
                )
            if route == "releases":
                return self.response(args, {"id": 27, "draft": True})
            if route == "releases/27":
                if "PATCH" in args:
                    self.published = True
                    return self.response(args, {"draft": False})
                assets = [
                    {
                        "name": p.name,
                        "size": p.stat().st_size,
                        "digest": "sha256:"
                        + hashlib.sha256(p.read_bytes()).hexdigest(),
                    }
                    for p in self.output.iterdir()
                ]
                if self.incomplete_assets:
                    assets.pop()
                if self.bad_remote_digest:
                    assets[0]["digest"] = "sha256:" + "0" * 64
                return self.response(
                    args,
                    {
                        "draft": True,
                        "tag_name": "frontend-" + REVISION,
                        "assets": assets,
                    },
                )
        self.fail(f"unexpected external command: {args}")

    def prepare(self, *, signed=False):
        publisher.capture(self.tested)
        publisher.export(self.tested, self.output)
        if signed:
            (self.output / publisher.BUNDLE).write_text('{"mediaType":"test bundle"}')

    def test_export_preserves_tested_manifests_and_distinguishes_config_digests(self):
        self.prepare()
        manifest = json.loads((self.output / publisher.MANIFEST).read_text())
        for role in publisher.ROLES:
            self.assertEqual(
                manifest["images"][role]["platform_manifest"], MANIFESTS[role]
            )
            self.assertEqual(manifest["images"][role]["image_config"], CONFIGS[role])
        saves = [x for x in self.calls if x[:3] == ["docker", "image", "save"]]
        self.assertEqual(len(saves), 2)
        self.assertTrue(all(x[3:5] == ["--platform", "linux/amd64"] for x in saves))
        self.assertEqual(
            {x[-1] for x in saves},
            {f"depositar-search-{r}:{REVISION}" for r in publisher.ROLES},
        )
        self.assertFalse(
            any("build" in x or "pull" in x or "push" in x for x in self.calls)
        )
        publisher.verify(self.output)
        self.assertEqual(len(self.validated_archives), 2)

    def test_fork_branch_other_events_and_dirty_source_fail_before_docker(self):
        for key, value in [
            ("GITHUB_REPOSITORY", "attacker/fork"),
            ("GITHUB_REF", "refs/heads/feature"),
            ("GITHUB_EVENT_NAME", "pull_request"),
            ("GITHUB_SHA", "b" * 40),
            ("EXPECTED_RELEASE_REVISION", "b" * 40),
            ("EXPECTED_RELEASE_REVISION", ""),
            ("GITHUB_ACTIONS", "false"),
            ("RUNNER_ENVIRONMENT", "self-hosted"),
            (
                "GITHUB_WORKFLOW_REF",
                "ThreeMonth03/depositarAI/.github/workflows/other.yml@refs/heads/main",
            ),
        ]:
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: value}):
                with self.assertRaises(ValueError):
                    publisher.capture(self.tested)
        self.dirty = True
        with self.assertRaises(ValueError):
            publisher.capture(self.tested)
        self.assertFalse(any(x[0] == "docker" for x in self.calls))

    def test_existing_capture_cannot_be_overwritten(self):
        publisher.capture(self.tested)
        before = self.tested.read_bytes()
        with self.assertRaises(FileExistsError):
            publisher.capture(self.tested)
        self.assertEqual(self.tested.read_bytes(), before)

    def test_missing_or_stale_receipt_and_tag_drift_prevent_export(self):
        with self.assertRaises(ValueError):
            publisher.export(self.tested, self.output)
        publisher.capture(self.tested)
        self.current["web"] = "sha256:" + "f" * 64
        with self.assertRaises(ValueError):
            publisher.export(self.tested, self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse(any(x[:3] == ["docker", "image", "tag"] for x in self.calls))

    def test_tag_drift_during_save_never_produces_a_manifest(self):
        publisher.capture(self.tested)
        self.changed_after_save = True
        with self.assertRaises(ValueError):
            publisher.export(self.tested, self.output)
        self.assertFalse((self.output / publisher.MANIFEST).exists())

    def test_extra_files_fail_before_attestation_or_publication(self):
        self.prepare()
        (self.output / "private.env").write_text("not for publication")
        with self.assertRaises(ValueError):
            publisher.verify(self.output)
        self.assertFalse(any(x[0] == "gh" for x in self.calls))

    def test_publication_is_draft_upload_verify_then_publish(self):
        self.prepare(signed=True)
        publisher.publish(self.output)
        mutations = [
            x
            for x in self.calls
            if x[0] == "gh" and ("--method" in x or x[1:3] == ["release", "upload"])
        ]
        self.assertIn("git/refs", mutations[0][2])
        self.assertTrue(mutations[1][2].endswith("/releases"))
        self.assertIn("draft=true", mutations[1])
        self.assertEqual(mutations[2][1:3], ["release", "upload"])
        self.assertIn("PATCH", mutations[3])
        self.assertIn("draft=false", mutations[3])
        self.assertTrue(self.published)
        self.assertFalse(any("--clobber" in x for x in self.calls))

    def test_existing_release_tag_or_api_denial_never_mutates_github(self):
        self.prepare(signed=True)
        for attribute in ["existing_release", "tag_created", "api_denied"]:
            with self.subTest(attribute=attribute):
                setattr(self, attribute, True)
                before = len(self.calls)
                with self.assertRaises(ValueError):
                    publisher.publish(self.output)
                self.assertFalse(any("--method" in x for x in self.calls[before:]))
                setattr(self, attribute, False)

    def test_incomplete_or_changed_remote_assets_leave_release_unpublished(self):
        self.prepare(signed=True)
        for attribute in ["incomplete_assets", "bad_remote_digest"]:
            with self.subTest(attribute=attribute):
                setattr(self, attribute, True)
                self.tag_created = False
                with self.assertRaises(ValueError):
                    publisher.publish(self.output)
                self.assertFalse(self.published)
                setattr(self, attribute, False)


class PublisherWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / ".github/workflows/frontend-release.yml").read_text()
        self.build, self.publish = self.source.split("\n  publish:\n", 1)

    def test_only_dispatched_original_main_can_reach_hosted_publisher(self):
        self.assertIn("on:\n  workflow_dispatch:\n", self.source)
        self.assertEqual(
            self.source.count("github.repository == 'ThreeMonth03/depositarAI'"), 2
        )
        self.assertEqual(self.source.count("github.ref == 'refs/heads/main'"), 2)
        self.assertIn("needs: [build-test]", self.publish)
        self.assertNotIn("always()", self.publish)
        for forbidden in [
            "pull_request",
            "workflow_run",
            "self-hosted",
            "secrets.",
            "ssh ",
        ]:
            self.assertNotIn(forbidden, self.source)
        for action in re.findall(r"uses: (\S+)", self.source):
            self.assertRegex(action, r"^[a-zA-Z0-9_./-]+@[0-9a-f]{40}$")

    def test_test_job_cannot_sign_publish_or_rebuild_after_tests(self):
        self.assertIn("permissions:\n  contents: read", self.build)
        for permission in ["contents: write", "id-token: write", "attestations: write"]:
            self.assertNotIn(permission, self.build)
            self.assertIn(permission, self.publish)
        ordered = [
            "run: bash scripts/test-search-web.sh",
            "frontend-release.py export",
            "uses: actions/upload-artifact@",
        ]
        positions = [self.build.index(x) for x in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("CI_FRONTEND_TESTED_IMAGES_FILE:", self.build)
        self.assertIn('"containerd-snapshotter":true', self.build)
        self.assertNotIn("docker build", self.publish)
        self.assertIn("frontend-release.py verify", self.publish)
        self.assertLess(
            self.publish.index("frontend-release.py verify"),
            self.publish.index("uses: actions/attest-build-provenance@"),
        )
        self.assertIn("steps.provenance.outputs.bundle-path", self.publish)
        self.assertNotIn("run-id:", self.publish)


if __name__ == "__main__":
    unittest.main()
