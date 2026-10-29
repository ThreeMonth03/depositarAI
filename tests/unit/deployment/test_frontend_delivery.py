"""Frontend host transactions with fake Docker/downloads and the real checker."""

import copy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.deployment import frontend_check as check
from search.operations.deployment import frontend_delivery as delivery
from tests.unit.deployment.test_frontend_check import (
    API,
    CATALOG,
    FRONT,
    IDS,
    NGINX_IMAGE,
    WEB_IMAGE,
    Runner,
)


NEXT = "a" * 40
NEXT_IMAGES = {"web": "sha256:" + "b" * 64, "web-nginx": "sha256:" + "c" * 64}


class HostRunner(Runner):
    def __init__(self, root):
        super().__init__(root, mode="intranet-http")
        self.original = copy.deepcopy(self.config)
        self.templates = copy.deepcopy(self.rows)
        self.fail_health = False
        self.foreign_after_failure = False
        self.generation = 0
        for role, manifest in NEXT_IMAGES.items():
            old = WEB_IMAGE if role == "web" else NGINX_IMAGE
            image = copy.deepcopy(self.images[old])
            image["Id"] = image["Descriptor"]["digest"] = manifest
            image["Config"]["Labels"]["org.opencontainers.image.revision"] = NEXT
            image["Config"]["Env"] = [
                x.replace(FRONT, NEXT) for x in image["Config"]["Env"]
            ]
            self.images[manifest] = image
            self.locators[delivery._locator(NEXT, role)] = manifest
        for name, row in self.networks.items():
            row["Id"] = hashlib.sha256(name.encode()).hexdigest()
        for row in self.rows.values():
            for name, attachment in row["NetworkSettings"]["Networks"].items():
                attachment["NetworkID"] = self.networks[name]["Id"]
        self.templates = copy.deepcopy(self.rows)

    def __call__(self, argv, *, environment, timeout):
        args = argv[5:]
        if args[0] == "compose" and args[-3:] == ["config", "--format", "json"]:
            self.config = copy.deepcopy(self.original)
            for role, service in self.config["services"].items():
                service["image"] = delivery._locator(
                    environment["SOURCE_REVISION"], role
                )
            return super().__call__(argv, environment=environment, timeout=timeout)
        if args[0] == "exec" and args[2:] == ["cat", "/etc/nginx/intranet-allow.conf"]:
            self.calls.append(list(argv))
            assert (
                self.rows[args[1]]["Config"]["Labels"]["com.docker.compose.service"]
                == "web-nginx"
            )
            return SimpleNamespace(returncode=0, stdout=self.mounted_acl, stderr=b"")
        if args[:2] == ["image", "inspect"]:
            reference = args[-1]
            assert reference in self.locators, (
                "Docker29 does not resolve bare platform digests"
            )
            manifest = self.locators[reference]
            self.calls.append(list(argv))
            self.environments.append(dict(environment))
            self.assert_local(argv, timeout)
            assert args[2:4] == ["--platform", "linux/amd64"]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(self.images[manifest]).encode(),
                stderr=b"",
            )
        if (
            args[0] == "load"
            or args[0] == "tag"
            or (args[0] == "compose" and "config" not in args)
        ):
            self.calls.append(list(argv))
            if args[0] == "tag":
                self.locators[args[2]] = args[1]
            elif args[0] == "compose" and "stop" in args:
                for row in self.rows.values():
                    if (
                        row["Config"]["Labels"]["com.docker.compose.service"]
                        == "web-nginx"
                    ):
                        row["State"]["Running"] = False
            elif args[0] == "compose" and "up" in args:
                composition = json.loads(Path(args[args.index("-f") + 1]).read_text())
                self.generation += 1
                self.rows = {}
                for role in delivery.ROLES:
                    service = composition["services"][role]
                    image = self.images[self.locators[service["image"]]]
                    row = copy.deepcopy(self.templates[IDS[role]])
                    identifier = hashlib.sha256(
                        f"{role}-{self.generation}".encode()
                    ).hexdigest()
                    row["Id"], row["Image"] = identifier, image["Id"]
                    row["ImageManifestDescriptor"] = copy.deepcopy(image["Descriptor"])
                    row["Config"] = copy.deepcopy(image["Config"])
                    row["Config"]["Env"] += [
                        k + "=" + v for k, v in service["environment"].items()
                    ]
                    row["Config"]["User"] = service["user"]
                    row["Config"]["Labels"].update(
                        {
                            "com.docker.compose.project": self.project,
                            "com.docker.compose.service": role,
                            **service.get("labels", {}),
                        }
                    )
                    self.rows[identifier] = row
                self.refresh_networks()
                if self.fail_health:
                    self.fail_health = False
                    next(iter(self.rows.values()))["State"]["Health"]["Status"] = (
                        "unhealthy"
                    )
                    if self.foreign_after_failure:
                        next(iter(self.rows.values()))["Config"]["Labels"][
                            delivery.TRANSACTION_LABEL
                        ] = "f" * 32
                    return SimpleNamespace(
                        returncode=23, stdout=b"", stderr=b"private health diagnostic"
                    )
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return super().__call__(argv, environment=environment, timeout=timeout)

    def refresh_networks(self):
        for name, network in self.networks.items():
            network["Containers"] = {
                identifier: {}
                for identifier, row in self.rows.items()
                if name in row["NetworkSettings"]["Networks"]
            }

    def mutations(self):
        return [
            c[5:]
            for c in self.calls
            if c[5] in ("load", "tag")
            or (c[5] == "compose" and ("up" in c[5:] or "stop" in c[5:]))
        ]


class FrontendDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="frontend-delivery-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "delivery"
        self.state.mkdir(mode=0o700)
        self.frontend = self.root / "frontend"
        self.frontend.mkdir(mode=0o700)
        self.monitor = self.root / "monitor"
        self.monitor.mkdir(mode=0o700)
        lock = self.monitor / ".check.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        gh = self.root / "gh"
        gh.write_text("#!/bin/sh\nexit 0\n")
        gh.chmod(0o700)
        env = self.frontend / "web.env"
        env.write_text("WEB_UID=1003\n")
        env.chmod(0o600)
        self.canonical = self.frontend / "frontend-check.json"
        self.initial = {
            "schema_version": 3,
            "deployment_mode": "intranet-http",
            "conversation_contract_version": 4,
            "frontend_env_file": str(env),
            "frontend_revision": FRONT,
            "api_revision": API,
            "catalog_manifest_sha256": CATALOG,
            "web_platform_manifest": WEB_IMAGE,
            "web_nginx_platform_manifest": NGINX_IMAGE,
        }
        self.canonical.write_text(json.dumps(self.initial))
        self.canonical.chmod(0o600)
        self.settings = {
            "schema_version": 1,
            "repository": delivery.REPOSITORY,
            "repository_id": 1425391257,
            "state_root": str(self.state),
            "frontend_check_file": str(self.canonical),
            "gh_binary": str(gh),
            "monitor_lock_file": str(lock),
        }
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(self.settings))
        self.config.chmod(0o600)
        self.runner = HostRunner(self.frontend)
        self.fetches = 0
        self.manifest = {
            "revision": NEXT,
            "conversation_contract_version": 4,
            "images": {
                role: {
                    "archive": role + ".oci.tar",
                    "platform_manifest": manifest,
                    "image_config": "sha256:" + "d" * 64,
                    "archive_sha256": hashlib.sha256(role.encode()).hexdigest(),
                    "archive_bytes": len(role),
                }
                for role, manifest in NEXT_IMAGES.items()
            },
        }
        self.addCleanup(patch.stopall)
        patch.object(check, "_homepage", return_value=2).start()
        self.fetch = patch(
            "search.operations.deployment.frontend_artifact.fetch_release",
            side_effect=self.fetch_release,
        ).start()
        patch(
            "search.operations.deployment.frontend_artifact.validate_archive",
            side_effect=self.validate_archive,
        ).start()

    def fetch_release(
        self, revision, repository, destination, gh_binary, *, repository_id
    ):
        self.fetches += 1
        self.assertEqual(revision, NEXT)
        self.assertEqual(repository, delivery.REPOSITORY)
        self.assertEqual(repository_id, 1425391257)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
        for role in delivery.ROLES:
            (destination / (role + ".oci.tar")).write_bytes(role.encode())
        return copy.deepcopy(self.manifest)

    def validate_archive(self, path, entry, revision, role):
        self.assertEqual(path.read_bytes(), role.encode())
        self.assertEqual(entry["platform_manifest"], NEXT_IMAGES[role])
        self.assertEqual(revision, NEXT)

    def run_delivery(self, operation="deploy", revision=NEXT):
        return delivery.operate(self.config, operation, revision, runner=self.runner)

    def test_failed_attestation_never_calls_docker_or_changes_canonical(self):
        self.fetch.side_effect = ValueError("secret remote diagnostic")
        with self.assertRaisesRegex(
            delivery.FrontendDeliveryError, "artifact_verification_failed$"
        ):
            self.run_delivery()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(json.loads(self.canonical.read_text()), self.initial)
        self.assertFalse((self.state / "intent.json").exists())

    def test_success_and_repeat_deploy_preserve_single_previous_without_restart(self):
        result = self.run_delivery()
        self.assertEqual(
            result, {"status": "deployed", "revision": NEXT, "previous_revision": FRONT}
        )
        mutations = self.runner.mutations()
        self.assertEqual(sum("up" in c for c in mutations), 1)
        self.assertTrue(
            all(
                "--no-build" in c and c[c.index("--pull") + 1] == "never"
                for c in mutations
                if "up" in c
            )
        )
        self.assertEqual(self.run_delivery()["status"], "unchanged")
        self.assertEqual(self.runner.mutations(), mutations)
        self.assertEqual(self.fetches, 1)
        saved = json.loads((self.state / "state.json").read_text())
        self.assertEqual(
            saved["current"]["composition"]["services"]["web"]["image"],
            delivery._locator(NEXT, "web"),
        )
        self.assertEqual(saved["previous"]["profile"], self.initial)
        self.assertFalse((self.state / "intent.json").exists())
        self.assertFalse(
            any(p.name.startswith(".download-") for p in self.state.iterdir())
        )

    def test_failed_health_restores_exact_previous_profile_and_records_new_runtime(
        self,
    ):
        self.runner.fail_health = True
        with self.assertRaisesRegex(
            delivery.FrontendDeliveryError, "cutover_failed_rolled_back$"
        ):
            self.run_delivery()
        self.assertEqual(json.loads(self.canonical.read_text()), self.initial)
        saved = json.loads((self.state / "state.json").read_text())
        self.assertEqual(saved["current"]["profile"], self.initial)
        self.assertNotEqual(
            saved["current"]["runtime"]["web"]["container_id"], IDS["web"]
        )
        self.assertFalse((self.state / "intent.json").exists())
        self.assertEqual(sum("up" in c for c in self.runner.mutations()), 2)

    def test_api_contract_mismatch_refuses_before_any_cutover_or_image_import(self):
        self.runner.proof = {"error": "upstream contract mismatch"}
        with self.assertRaises(check.FrontendCheckError):
            self.run_delivery()
        self.assertEqual(self.runner.mutations(), [])
        self.assertEqual(json.loads(self.canonical.read_text()), self.initial)

    def test_explicit_rollback_switches_only_frontend_and_keeps_one_previous(self):
        self.run_delivery()
        result = self.run_delivery("rollback", None)
        self.assertEqual(
            result,
            {"status": "rolled_back", "revision": FRONT, "previous_revision": NEXT},
        )
        self.assertEqual(json.loads(self.canonical.read_text()), self.initial)
        saved = json.loads((self.state / "state.json").read_text())
        self.assertEqual(saved["previous"]["profile"]["frontend_revision"], NEXT)
        self.assertTrue(
            all(
                "depositar-search-intranet" in c
                for c in self.runner.mutations()
                if c[0] == "compose"
            )
        )

    def test_foreign_container_prevents_blind_rollback_and_intent_can_recover(self):
        self.runner.fail_health = True
        self.runner.foreign_after_failure = True
        with self.assertRaisesRegex(
            delivery.FrontendDeliveryError, "recovery_required$"
        ):
            self.run_delivery()
        self.assertTrue((self.state / "intent.json").exists())
        self.assertEqual(sum("stop" in c for c in self.runner.mutations()), 1)
        self.assertEqual(
            self.run_delivery("status", None)["status"], "recovery_required"
        )
        # Simulate the operator removing the unrelated replacement, not the tool.
        self.runner.rows = {
            k: r
            for k, r in self.runner.rows.items()
            if r["Config"]["Labels"].get(delivery.TRANSACTION_LABEL) != "f" * 32
        }
        self.runner.refresh_networks()
        result = self.run_delivery("rollback", None)
        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["revision"], FRONT)
        self.assertFalse((self.state / "intent.json").exists())

    def test_status_is_read_only_and_initial_adoption_requires_checker_success(self):
        self.assertEqual(self.run_delivery("status", None)["revision"], FRONT)
        self.assertEqual(self.runner.mutations(), [])
        self.assertFalse((self.state / "state.json").exists())
        self.runner.proof = {"error": "unhealthy baseline"}
        with self.assertRaises(check.FrontendCheckError):
            self.run_delivery(revision=FRONT)
        self.assertFalse((self.state / "state.json").exists())

    def test_wrong_repository_identity_or_unsafe_config_does_not_start_fetch(self):
        for key, value in (
            ("repository_id", True),
            ("repository", "other/repo"),
            ("state_root", str(self.frontend)),
            ("extra", "private"),
        ):
            settings = {**self.settings, key: value}
            self.config.write_text(json.dumps(settings))
            with (
                self.subTest(key=key),
                self.assertRaises(delivery.FrontendDeliveryError),
            ):
                self.run_delivery()
        self.assertEqual(self.fetches, 0)
        self.assertEqual(self.runner.calls, [])

    def test_busy_contender_cannot_remove_current_owners_scratch_profile(self):
        scratch = self.state / ".profile.json"
        scratch.write_bytes(b"private profile under active check")
        scratch.chmod(0o600)
        before = scratch.stat()
        lock = self.state / ".delivery.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(delivery.FrontendDeliveryError, "busy$"):
                self.run_delivery()
            self.assertEqual(
                scratch.read_bytes(), b"private profile under active check"
            )
            self.assertEqual(
                (scratch.stat().st_dev, scratch.stat().st_ino),
                (before.st_dev, before.st_ino),
            )
            self.assertEqual(self.runner.calls, [])
            self.assertEqual(self.fetches, 0)
        finally:
            os.close(fd)

    def test_cli_failure_never_reflects_private_diagnostics(self):
        output = io.StringIO()
        with (
            patch.object(
                delivery, "operate", side_effect=ValueError("PRIVATE_SENTINEL")
            ),
            patch("sys.stdout", output),
        ):
            self.assertEqual(delivery.main(["--config", str(self.config), "status"]), 1)
        self.assertEqual(
            json.loads(output.getvalue()), {"error": "frontend_delivery_failed"}
        )

    def test_cli_reports_fixed_recovery_action_without_child_output(self):
        output = io.StringIO()
        with (
            patch.object(
                delivery,
                "operate",
                side_effect=delivery.FrontendDeliveryError(
                    "frontend_delivery_recovery_required"
                ),
            ),
            patch("sys.stdout", output),
        ):
            self.assertEqual(
                delivery.main(["--config", str(self.config), "rollback"]), 1
            )
        self.assertEqual(
            json.loads(output.getvalue()),
            {"error": "frontend_delivery_recovery_required"},
        )


if __name__ == "__main__":
    unittest.main()
