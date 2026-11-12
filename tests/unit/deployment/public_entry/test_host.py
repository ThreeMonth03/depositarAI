"""The public entrance is a separate CPU deployment with a guarded image."""

import copy
import errno
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.deployment.public_entry import host


IMAGE = "sha256:" + "a" * 64
FRONTEND = "sha256:" + "f" * 64
API_IMAGE = "sha256:" + "d" * 64
REVISION = "b" * 40


class PublicEntryBindingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.key = self.root / "api-key"
        self.key.write_text("c" * 64 + "\n")
        self.key.chmod(0o600)
        self.config = SimpleNamespace(
            state_root=self.root,
            public_origin="https://public.example.org",
            edge_secret_file=self.root / "edge-key",
        )
        self.frontend_proof = {
            "Id": FRONTEND,
            "Labels": {
                host.FRONTEND_CAPABILITY: "1",
                host.CAPABILITY: "2",
                "org.opencontainers.image.revision": REVISION,
            },
        }
        self.proof = {
            "Id": IMAGE,
            "Labels": {
                host.CAPABILITY: "2",
                host.CONVERSATION_CAPABILITY: "4",
                "org.opencontainers.image.revision": REVISION,
            },
        }
        name = "depositar-ai-release-" + "a" * 12 + "-" + "b" * 12
        self.release = {
            "name": name,
            "services": {
                "web": {
                    "environment": {
                        "WEB_UPSTREAM": "http://conversation-api:8000"
                    },
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(self.key),
                            "target": "/run/secrets/api_key",
                            "read_only": True,
                        }
                    ],
                },
                "conversation-api": {"image": API_IMAGE},
            },
            "networks": {"website": {"name": name + "_website"}},
        }
        self.rendered = {
            "state_directories": ["redis-data"],
            "compose": {
                "name": "depositar-ai-public-entry",
                "services": {"public-entry": {"depends_on": {}}},
                "networks": {"website": {"internal": True}},
            },
            "assets": {"config.yaml": "{}\n", "apisix.yaml": "{}\n#END\n"},
        }

    def bind(self, **kwargs):
        return host.bind_public_web(
            self.config,
            self.rendered,
            kwargs.get("release", self.release),
            IMAGE,
            kwargs.get("proof", self.proof),
            FRONTEND,
            kwargs.get("frontend_proof", self.frontend_proof),
        )

    def test_binding_preserves_private_release_and_publishes_no_backend_port(
        self,
    ):
        before = copy.deepcopy((self.rendered, self.release))
        result = self.bind()
        self.assertEqual(before, (self.rendered, self.release))
        web = result["compose"]["services"]["public-web"]
        self.assertEqual(web["image"], IMAGE)
        self.assertEqual(
            web["environment"]["WEB_ORIGIN"], self.config.public_origin
        )
        self.assertEqual(
            web["environment"]["WEB_EDGE_SECRET_FILE"],
            "/run/secrets/public_edge_key",
        )
        self.assertEqual(web["runtime"], "runc")
        self.assertEqual(
            web["networks"],
            {"website": {"aliases": ["web"]}, "existing-release": {}},
        )
        frontend = result["compose"]["services"]["public-frontend"]
        self.assertEqual(frontend["image"], FRONTEND)
        self.assertEqual(frontend["networks"], ["website"])
        self.assertNotIn("volumes", frontend)
        self.assertNotIn("ports", frontend)
        self.assertNotIn("ports", web)
        self.assertNotIn("deploy", web)
        self.assertEqual(web["user"], f"{os.geteuid()}:{os.getegid()}")
        self.assertTrue(all(mount["read_only"] for mount in web["volumes"]))
        self.assertEqual(
            result["compose"]["x-depositar-plan"]["private_backend"],
            {
                "schema_version": 1,
                "release_project": self.release["name"],
                "network_name": self.release["networks"]["website"]["name"],
                "api_image": API_IMAGE,
            },
        )

    def test_binding_backend_identity_preserves_existing_plan_metadata(
        self,
    ):
        self.rendered["compose"]["x-depositar-plan"] = {
            "schema_version": 1,
            "status": "public_entry_planned",
            "quota_counts": "authenticated_turn_attempts",
        }
        result = self.bind()
        plan = result["compose"]["x-depositar-plan"]
        self.assertEqual(plan["status"], "public_entry_prepared")
        self.assertEqual(plan["quota_counts"], "authenticated_turn_attempts")
        self.assertEqual(plan["private_backend"]["api_image"], API_IMAGE)
        self.assertNotIn(
            "private_backend", self.rendered["compose"]["x-depositar-plan"]
        )

    def test_private_api_image_must_be_immutable_before_offline_binding(self):
        for image in (
            None,
            True,
            1,
            "mutable:latest",
            "sha256:" + "d" * 63,
            "sha256:" + "D" * 64,
        ):
            release = copy.deepcopy(self.release)
            release["services"]["conversation-api"]["image"] = image
            with (
                self.subTest(image=image),
                self.assertRaisesRegex(
                    ValueError, "^public_entry_release_invalid$"
                ),
            ):
                self.bind(release=release)
        del release["services"]["conversation-api"]["image"]
        with self.assertRaisesRegex(
            ValueError, "^public_entry_release_invalid$"
        ):
            self.bind(release=release)

    def test_old_or_unbound_image_cannot_start_public_website(self):
        for proof in (
            None,
            {},
            {**self.proof, "Id": "sha256:" + "d" * 64},
            {**self.proof, "Labels": {}},
            {
                **self.proof,
                "Labels": {
                    host.CAPABILITY: "2",
                    "org.opencontainers.image.revision": "development",
                },
            },
        ):
            with (
                self.subTest(proof=proof),
                self.assertRaisesRegex(ValueError, "web_image_invalid"),
            ):
                self.bind(proof=proof)

    def test_local_bff_requires_current_wire_contract_before_binding(self):
        for version in (None, "1", "2", "3", 4, True):
            proof = copy.deepcopy(self.proof)
            if version is None:
                proof["Labels"].pop(host.CONVERSATION_CAPABILITY)
            else:
                proof["Labels"][host.CONVERSATION_CAPABILITY] = version
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(
                    ValueError, "^public_entry_web_image_invalid$"
                ),
            ):
                self.bind(proof=proof)
        self.assertNotIn("public-web", self.rendered["compose"]["services"])

    def test_frontend_requires_capability_and_same_revision(self):
        for proof in (
            None,
            {},
            {"Id": FRONTEND, "Labels": ["invalid"]},
            {**self.frontend_proof, "Id": IMAGE},
            {
                "Id": FRONTEND,
                "Labels": {
                    host.FRONTEND_CAPABILITY: "1",
                    host.CAPABILITY: "2",
                    "org.opencontainers.image.revision": "c" * 40,
                },
            },
            {
                "Id": FRONTEND,
                "Labels": {"org.opencontainers.image.revision": REVISION},
            },
        ):
            with (
                self.subTest(proof=proof),
                self.assertRaisesRegex(ValueError, "frontend_image_invalid"),
            ):
                self.bind(frontend_proof=proof)

    def test_unexpected_release_network_and_published_api_are_rejected(self):
        for mutate in (
            lambda release: release["services"].update(
                {"conversation-api": None}
            ),
            lambda release: release["networks"]["website"].update(
                name="unrelated"
            ),
            lambda release: release["services"]["conversation-api"].update(
                ports=[8000]
            ),
            lambda release: release["services"]["web"]["volumes"][0].update(
                read_only=False
            ),
            lambda release: release["services"]["web"]["environment"].update(
                WEB_UPSTREAM="http://attacker:8000"
            ),
        ):
            release = copy.deepcopy(self.release)
            mutate(release)
            with self.assertRaisesRegex(ValueError, "release_invalid"):
                self.bind(release=release)

    def test_unsafe_or_malformed_api_key_is_rejected(self):
        self.key.write_text("invalid")
        with self.assertRaisesRegex(ValueError, "api_key_invalid"):
            self.bind()
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):
            self.bind()

    def prepare(self):
        release_file = self.root / "release.json"
        if not release_file.exists():
            release_file.write_text(json.dumps(self.release))
            release_file.chmod(0o600)
        with (
            patch.object(
                host.public_entry,
                "load_public_entry_config",
                return_value=self.config,
            ),
            patch.object(
                host.public_entry,
                "render_public_entry",
                return_value=self.rendered,
            ),
        ):
            return host.prepare_public_entry(
                self.root / "entry.json",
                release_file,
                IMAGE,
                FRONTEND,
                image_inspector=lambda image: self.proof
                if image == IMAGE
                else self.frontend_proof,
            )

    def test_prepare_reverifies_output_and_rejects_drift_without_overwrite(
        self,
    ):
        report = self.prepare()
        self.assertFalse(report["deployment_performed"])
        self.assertFalse(report["public_readiness_verified"])
        self.assertFalse(report["active_changed"])
        self.assertEqual(self.prepare(), report)
        path = Path(report["compose_file"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        prepared = json.loads(path.read_text())
        self.assertEqual(
            prepared["x-depositar-plan"]["private_backend"]["api_image"],
            API_IMAGE,
        )
        path.write_text("tampered")
        with self.assertRaisesRegex(ValueError, "preparation_changed"):
            self.prepare()
        self.assertEqual(path.read_text(), "tampered")

    def test_offline_prepare_missing_api_identity_publishes_no_state(
        self,
    ):
        del self.release["services"]["conversation-api"]["image"]
        with (
            patch.object(host.subprocess, "run") as runner,
            self.assertRaisesRegex(
                ValueError, "^public_entry_release_invalid$"
            ),
        ):
            self.prepare()
        runner.assert_not_called()
        self.assertFalse((self.root / "public-entry").exists())
        self.assertEqual(list(self.root.glob(".public-entry-staging-*")), [])

    def test_invalid_image_reference_never_runs_docker(self):
        with (
            patch.object(host.subprocess, "run") as runner,
            self.assertRaises(ValueError),
        ):
            host.inspect_web_image("mutable:latest")
        runner.assert_not_called()

    def test_interrupted_preparation_can_retry_without_deleting_state(self):
        with patch.object(
            host.private_io,
            "write_file",
            side_effect=OSError("disk interrupted"),
        ):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertFalse((self.root / "public-entry").exists())
        self.assertTrue(list(self.root.glob(".public-entry-staging-*")))
        report = self.prepare()
        self.assertTrue(Path(report["compose_file"]).is_file())

    def test_operator_destination_created_during_staging_is_preserved(self):
        original = host.private_io.write_json

        def concurrent(*args, **kwargs):
            original(*args, **kwargs)
            destination = self.root / "public-entry"
            destination.mkdir(mode=0o700)
            (destination / "operator-sentinel").write_text("keep")

        with patch.object(
            host.private_io, "write_json", side_effect=concurrent
        ):
            with self.assertRaisesRegex(ValueError, "destination_exists"):
                self.prepare()
        self.assertEqual(
            (self.root / "public-entry/operator-sentinel").read_text(), "keep"
        )

    def test_saved_report_failure_precedes_assets_without_overwrite(self):
        report = self.prepare()
        prepared = self.root / "public-entry"
        saved = prepared / "prepared.json"
        original = host.private_io.read_file
        for raw, message in (
            (
                json.dumps({**report, "status": "changed"}).encode(),
                "public_entry_preparation_changed",
            ),
            (b'{"status":1,"status":2}\n', "duplicate JSON key"),
        ):
            with self.subTest(message=message):
                saved.write_bytes(raw)
                before = {
                    path.name: path.read_bytes()
                    for path in prepared.iterdir()
                    if path.is_file()
                }
                reads = []

                def read(descriptor, name, **kwargs):
                    if name in {
                        "prepared.json",
                        *self.rendered["assets"],
                        "compose.json",
                    }:
                        reads.append(name)
                    return original(descriptor, name, **kwargs)

                with (
                    patch.object(
                        host.private_io, "read_file", side_effect=read
                    ),
                    self.assertRaisesRegex(ValueError, "^" + message + "$"),
                ):
                    self.prepare()
                self.assertEqual(reads, ["prepared.json"])
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in prepared.iterdir()
                        if path.is_file()
                    },
                    before,
                )

    def test_non_compose_asset_drift_preserves_read_order_and_files(self):
        self.prepare()
        prepared = self.root / "public-entry"
        (prepared / "apisix.yaml").write_bytes(b"changed asset\n")
        before = {
            path.name: path.read_bytes()
            for path in prepared.iterdir()
            if path.is_file()
        }
        original = host.private_io.read_file
        reads = []

        def read(descriptor, name, **kwargs):
            if name in {
                "prepared.json",
                *self.rendered["assets"],
                "compose.json",
            }:
                reads.append(name)
            return original(descriptor, name, **kwargs)

        with (
            patch.object(host.private_io, "read_file", side_effect=read),
            self.assertRaisesRegex(
                ValueError, "^public_entry_preparation_changed$"
            ),
        ):
            self.prepare()
        self.assertEqual(
            reads, ["prepared.json", "config.yaml", "apisix.yaml"]
        )
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in prepared.iterdir()
                if path.is_file()
            },
            before,
        )

    def test_existing_retry_releases_descriptors_and_lock_after_read_failure(
        self,
    ):
        report = self.prepare()
        original_read = host.private_io.read_file
        original_open = host.private_io.open_directory
        original_flock = host.fcntl.flock
        lock_path = self.root / ".public-entry-prepare.lock"
        for fail_read in (False, True):
            with self.subTest(fail_read=fail_read):
                parent = lock = None
                resources = {}
                failure = PermissionError("existing asset unavailable")

                def open_directory(path, **kwargs):
                    nonlocal parent
                    parent = original_open(path, **kwargs)
                    return parent

                def flock(descriptor, operation):
                    nonlocal lock
                    original_flock(descriptor, operation)
                    lock = descriptor

                def read(descriptor, name, **kwargs):
                    if name in {
                        "prepared.json",
                        *self.rendered["assets"],
                        "compose.json",
                    }:
                        resources.update(
                            parent=parent, preparation=descriptor, lock=lock
                        )
                        for owned in resources.values():
                            os.fstat(owned)
                        with lock_path.open("rb") as competing:
                            with self.assertRaises(BlockingIOError):
                                original_flock(
                                    competing.fileno(),
                                    host.fcntl.LOCK_EX | host.fcntl.LOCK_NB,
                                )
                    if fail_read and name == "apisix.yaml":
                        raise failure
                    result = original_read(descriptor, name, **kwargs)
                    os.fstat(descriptor)
                    return result

                with (
                    patch.object(
                        host.private_io,
                        "open_directory",
                        side_effect=open_directory,
                    ),
                    patch.object(host.fcntl, "flock", side_effect=flock),
                    patch.object(
                        host.private_io, "read_file", side_effect=read
                    ),
                ):
                    if fail_read:
                        with self.assertRaises(PermissionError) as caught:
                            self.prepare()
                        self.assertIs(caught.exception, failure)
                    else:
                        self.assertEqual(self.prepare(), report)
                self.assertEqual(len(set(resources.values())), 3)
                for name, descriptor in resources.items():
                    with (
                        self.subTest(resource=name),
                        self.assertRaises(OSError) as caught,
                    ):
                        os.fstat(descriptor)
                    self.assertEqual(caught.exception.errno, errno.EBADF)
                with lock_path.open("rb") as competing:
                    original_flock(
                        competing.fileno(),
                        host.fcntl.LOCK_EX | host.fcntl.LOCK_NB,
                    )
                self.assertEqual(self.prepare(), report)


if __name__ == "__main__":
    unittest.main()
