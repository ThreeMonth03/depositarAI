"""One Compose website release updates two images and retains quotas/backend."""

import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.deployment.public_entry import host, updates
from tests.unit.deployment.public_entry import test_preflight as preflight_fixture

OLD, NEW, FRONT, NEW_FRONT = ("sha256:" + c * 64 for c in "abcf")
REVISIONS = {OLD: "1" * 40, FRONT: "1" * 40, NEW: "2" * 40, NEW_FRONT: "2" * 40}


class ComposeRunner:
    def __init__(self):
        self.commands = []
        self.images = None
        self.redis_starts = 0
        self.entrance = False
        self.fail_image = None

    def __call__(self, argv, **_kwargs):
        argv = list(argv)
        self.commands.append(argv)
        path = Path(argv[argv.index("-f") + 1])
        config = json.loads(path.read_text())
        command = argv[argv.index("-f") + 2 :]
        if command[0] == "ps":
            return SimpleNamespace(stdout=b"[]", stderr=b"", returncode=0)
        if command[0] == "stop":
            self.entrance = False
        if command[0] == "up" and command[-1] == "quota-redis":
            self.redis_starts += 1
        if command[0] == "up" and command[-2:] == ["public-web", "public-frontend"]:
            self.images = tuple(
                config["services"][role]["image"]
                for role in ("public-web", "public-frontend")
            )
            if self.fail_image in self.images:
                raise subprocess.CalledProcessError(1, argv)
        if command[0] == "up" and command[-1] == "public-entry":
            self.entrance = True
        return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)


class PublicEntryUpdatesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        raw = dict(
            schema_version=1,
            state_root=str(self.state),
            public_origin="https://public.example.org",
            oidc_issuer="https://id.example.org",
            oidc_discovery="https://id.example.org/discovery",
            oidc_client_id="test",
        )
        for i, key in enumerate(
            (
                "oidc_client_secret_file",
                "oidc_session_secret_file",
                "edge_secret_file",
                "redis_password_file",
            )
        ):
            path = self.root / key
            path.write_text("client-secret" if i == 0 else str(i) * 64)
            path.chmod(0o600)
            raw[key] = str(path)
        self.config_path = self.root / "entry.json"
        self.config_path.write_text(json.dumps(raw))
        self.config_path.chmod(0o600)
        self.key = self.root / "api-key"
        self.key.write_text("9" * 64)
        self.key.chmod(0o600)
        project = "depositar-ai-release-" + "a" * 12 + "-" + "b" * 12
        self.release = {
            "name": project,
            "networks": {"website": {"name": project + "_website"}},
            "services": {
                "conversation-api": {"image": "sha256:" + "d" * 64},
                "web": {
                    "environment": {"WEB_UPSTREAM": "http://conversation-api:8000"},
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(self.key),
                            "target": "/run/secrets/api_key",
                            "read_only": True,
                        }
                    ],
                },
            },
        }
        self.release_path = self.root / "release.json"
        self.release_path.write_text(json.dumps(self.release))
        self.release_path.chmod(0o600)
        self.runner = ComposeRunner()
        self.smoke = lambda _config: None
        patcher = patch.object(updates.preflight, "verify_bound_backend")
        self.backend = patcher.start()
        self.addCleanup(patcher.stop)
        self.base = self.state / "public-entry"

    def inspector(self, image):
        return {
            "Id": image,
            "Labels": {
                "org.opencontainers.image.revision": REVISIONS[image],
                host.CAPABILITY
                if image in (OLD, NEW)
                else host.FRONTEND_CAPABILITY: "1"
                if image in (FRONT, NEW_FRONT)
                else "2",
                host.CAPABILITY: "2",
                host.CONVERSATION_CAPABILITY: "3",
            },
        }

    def invoke(self, function, web=OLD, frontend=FRONT):
        kwargs = dict(
            runner=self.runner, image_inspector=self.inspector, smoke_probe=self.smoke
        )
        if function is updates.rollback_public_entry:
            return function(self.config_path, **kwargs)
        return function(self.config_path, self.release_path, web, frontend, **kwargs)

    def active(self):
        return json.loads((self.base / "active.json").read_text())

    def test_pair_update_and_explicit_rollback_preserve_redis_and_private_backend(self):
        started = self.invoke(updates.bootstrap_public_entry)
        self.assertEqual(started["status"], "public_entry_started")
        self.assertEqual(self.runner.redis_starts, 1)
        baseline = self.release_path.read_bytes()
        count = len(self.runner.commands)
        updated = self.invoke(updates.update_public_entry, NEW, NEW_FRONT)
        self.assertEqual(updated["status"], "public_entry_updated")
        self.assertEqual(
            (updated["web_image"], updated["web_nginx_image"]), (NEW, NEW_FRONT)
        )
        self.assertEqual(updated["previous_generation"], started["generation"])
        self.assertEqual(self.runner.images, (NEW, NEW_FRONT))
        for command in self.runner.commands[count:]:
            self.assertNotIn("quota-redis", command)
            self.assertNotIn("down", command)
            self.assertNotIn("--remove-orphans", command)
        rolled = self.invoke(updates.rollback_public_entry)
        self.assertEqual(rolled["status"], "public_entry_rolled_back")
        self.assertEqual(self.runner.images, (OLD, FRONT))
        self.assertEqual(self.runner.redis_starts, 1)
        self.assertEqual(self.release_path.read_bytes(), baseline)
        self.assertGreaterEqual(self.backend.call_count, 6)

    def test_failed_candidate_closes_entrance_and_waits_for_manual_rollback(self):
        self.invoke(updates.bootstrap_public_entry)
        original = self.active()
        self.runner.fail_image = NEW_FRONT
        failed = self.invoke(updates.update_public_entry, NEW, NEW_FRONT)
        self.assertEqual(failed["status"], "deployment_failed")
        self.assertFalse(self.runner.entrance)
        self.assertEqual(self.active(), original)
        self.assertTrue((self.base / "pending.json").exists())
        self.assertEqual(self.runner.images, (NEW, NEW_FRONT))
        with self.assertRaisesRegex(ValueError, "pending_operation"):
            self.invoke(updates.update_public_entry, NEW, NEW_FRONT)
        self.runner.fail_image = None
        rolled = self.invoke(updates.rollback_public_entry)
        self.assertEqual(rolled["status"], "public_entry_rolled_back")
        self.assertEqual(self.runner.images, (OLD, FRONT))
        self.assertFalse((self.base / "pending.json").exists())

    def test_partial_interruption_keeps_a_durable_rollback_target(self):
        self.invoke(updates.bootstrap_public_entry)
        original = self.active()

        def interrupted(_config):
            raise KeyboardInterrupt

        self.smoke = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(updates.update_public_entry, NEW, NEW_FRONT)
        self.assertFalse(self.runner.entrance)
        pending = json.loads((self.base / "pending.json").read_text())
        self.assertEqual(pending["previous"], original["generation"])
        self.smoke = lambda _config: None
        self.assertEqual(
            self.invoke(updates.rollback_public_entry)["status"],
            "public_entry_rolled_back",
        )
        self.assertEqual(self.runner.images, (OLD, FRONT))

    def pending_cleanup_failure(self, error, *, after_unlink=False):
        initial = self.invoke(updates.bootstrap_public_entry)
        original_remove = updates._remove

        def fail_remove(root, name):
            if after_unlink:
                original_remove(root, name)
            raise error

        with patch.object(updates, "_remove", side_effect=fail_remove):
            if isinstance(error, KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.invoke(updates.update_public_entry, NEW, NEW_FRONT)
            else:
                failed = self.invoke(updates.update_public_entry, NEW, NEW_FRONT)
                self.assertEqual(failed["status"], "deployment_failed")
        self.assertFalse(self.runner.entrance)
        self.assertEqual(self.active()["web_image"], NEW)
        self.assertEqual(self.active()["previous_generation"], initial["generation"])
        self.assertEqual((self.base / "pending.json").exists(), not after_unlink)
        if not after_unlink:
            pending = json.loads((self.base / "pending.json").read_text())
            self.assertEqual(pending["candidate"], self.active()["generation"])
            self.assertEqual(pending["previous"], initial["generation"])
        count = len(self.runner.commands)
        rolled = self.invoke(updates.rollback_public_entry)
        self.assertEqual(rolled["status"], "public_entry_rolled_back")
        self.assertEqual(self.runner.images, (OLD, FRONT))
        self.assertEqual(self.runner.redis_starts, 1)
        self.assertFalse((self.base / "pending.json").exists())
        self.assertTrue(self.runner.entrance)
        self.assertFalse(
            any("quota-redis" in command for command in self.runner.commands[count:])
        )

    def test_pending_unlink_failure_after_active_publication_can_roll_back(self):
        self.pending_cleanup_failure(OSError("unlink failed"))

    def test_pending_fsync_failure_after_unlink_can_roll_back(self):
        self.pending_cleanup_failure(OSError("fsync failed"), after_unlink=True)

    def test_interruption_after_active_publication_can_roll_back(self):
        self.pending_cleanup_failure(KeyboardInterrupt())

    def test_inconsistent_active_and_pending_generations_refuse_rollback(self):
        initial = self.invoke(updates.bootstrap_public_entry)
        for candidate in (initial["generation"], "f" * 64):
            pending = {
                "schema_version": 1,
                "candidate": candidate,
                "previous": "e" * 64,
            }
            updates._write(self.base, "pending.json", pending)
            count = len(self.runner.commands)
            with (
                self.subTest(candidate=candidate),
                self.assertRaisesRegex(ValueError, "snapshot_invalid"),
            ):
                self.invoke(updates.rollback_public_entry)
            self.assertEqual(self.runner.commands[count:], [])
            self.assertEqual(self.active()["generation"], initial["generation"])

    def bootstrap_cleanup_failure(self, error, *, after_unlink=False):
        original_remove = updates._remove

        def fail_remove(root, name):
            if after_unlink:
                original_remove(root, name)
            raise error

        with patch.object(updates, "_remove", side_effect=fail_remove):
            if isinstance(error, KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.invoke(updates.bootstrap_public_entry)
            else:
                failed = self.invoke(updates.bootstrap_public_entry)
                self.assertEqual(failed["status"], "deployment_failed")
        self.assertFalse(self.runner.entrance)
        self.assertIsNone(self.active()["previous_generation"])
        self.assertEqual((self.base / "pending.json").exists(), not after_unlink)
        sentinel = self.base / "redis-data/quotas"
        sentinel.write_text("keep")
        count = len(self.runner.commands)
        with self.assertRaisesRegex(ValueError, "profile_changed"):
            self.invoke(updates.bootstrap_public_entry, NEW, NEW_FRONT)
        self.assertEqual(self.runner.commands[count:], [])
        resumed = self.invoke(updates.bootstrap_public_entry)
        self.assertEqual(resumed["status"], "public_entry_started")
        self.assertEqual(self.runner.images, (OLD, FRONT))
        self.assertTrue(self.runner.entrance)
        self.assertIsNone(self.active()["previous_generation"])
        self.assertFalse((self.base / "pending.json").exists())
        self.assertEqual(sentinel.read_text(), "keep")
        redis_commands = [
            call for call in self.runner.commands if call[-1] == "quota-redis"
        ]
        self.assertEqual(len(redis_commands), 2)
        self.assertTrue(all("--no-recreate" in call for call in redis_commands))

    def test_bootstrap_pending_unlink_failure_can_retry_the_same_pair(self):
        self.bootstrap_cleanup_failure(OSError("unlink failed"))

    def test_bootstrap_pending_fsync_failure_can_retry_the_same_pair(self):
        self.bootstrap_cleanup_failure(OSError("fsync failed"), after_unlink=True)

    def test_bootstrap_interruption_after_active_write_can_retry_the_same_pair(self):
        self.bootstrap_cleanup_failure(KeyboardInterrupt())

    def test_bootstrap_refuses_unrelated_active_pending_generations(self):
        initial = self.invoke(updates.bootstrap_public_entry)
        updates._write(
            self.base,
            "pending.json",
            {
                "schema_version": 1,
                "candidate": "f" * 64,
                "previous": None,
            },
        )
        count = len(self.runner.commands)
        with self.assertRaisesRegex(ValueError, "pending_operation"):
            self.invoke(updates.bootstrap_public_entry)
        self.assertEqual(self.runner.commands[count:], [])
        self.assertEqual(self.active()["generation"], initial["generation"])

    def test_same_revision_pair_is_required_before_mutation(self):
        with self.assertRaisesRegex(ValueError, "frontend_image_invalid"):
            self.invoke(updates.bootstrap_public_entry, OLD, NEW_FRONT)
        self.assertEqual(self.runner.commands, [])

    def test_unhealthy_private_backend_prevents_cpu_mutation(self):
        self.backend.side_effect = ValueError("backend_unhealthy")
        with self.assertRaisesRegex(ValueError, "backend_unhealthy"):
            self.invoke(updates.bootstrap_public_entry)
        self.assertFalse(any("up" in call for call in self.runner.commands))

    def reject_legacy_api(self):
        _profile, _release, network, api = preflight_fixture.fixture()
        api["Image"] = self.release["services"]["conversation-api"]["image"]
        api["Mounts"][0]["Source"] = str(self.key)
        api["Labels"][host.CONVERSATION_CAPABILITY] = "2"
        metadata = preflight_fixture.MetadataRunner(network, [api])
        self.backend.side_effect = lambda bound, **_options: (
            updates.preflight.verify_private_backend(
                bound, self.release, runner=metadata
            )
        )

    def test_local_legacy_api_prevents_first_container_mutation(self):
        self.reject_legacy_api()
        with self.assertRaisesRegex(
            updates.preflight.PublicEntryPreflightError, "contract_changed$"
        ):
            self.invoke(updates.bootstrap_public_entry)
        self.assertFalse(
            any(
                operation in command
                for command in self.runner.commands
                for operation in ("up", "stop", "start", "down", "rm")
            )
        )
        self.assertFalse((self.base / "pending.json").exists())

    def test_local_legacy_api_blocks_update_and_rollback_before_mutation(self):
        self.invoke(updates.bootstrap_public_entry)
        self.invoke(updates.update_public_entry, NEW, NEW_FRONT)
        self.reject_legacy_api()
        count = len(self.runner.commands)
        for action in (updates.update_public_entry, updates.rollback_public_entry):
            with (
                self.subTest(action=action.__name__),
                self.assertRaisesRegex(
                    updates.preflight.PublicEntryPreflightError, "contract_changed$"
                ),
            ):
                self.invoke(action)
        self.assertEqual(self.runner.commands[count:], [])
        self.assertTrue(self.runner.entrance)

    def test_local_legacy_bff_snapshot_blocks_update_and_rollback_before_mutation(self):
        self.invoke(updates.bootstrap_public_entry)
        self.invoke(updates.update_public_entry, NEW, NEW_FRONT)
        original = self.inspector

        def old_contract(image):
            proof = original(image)
            if image in (OLD, NEW):
                proof["Labels"][host.CONVERSATION_CAPABILITY] = "2"
            return proof

        self.inspector = old_contract
        count = len(self.runner.commands)
        for action in (updates.update_public_entry, updates.rollback_public_entry):
            with (
                self.subTest(action=action.__name__),
                self.assertRaisesRegex(ValueError, "profile_changed$"),
            ):
                self.invoke(action)
        self.assertEqual(self.runner.commands[count:], [])

    def test_unchanged_pair_does_not_recreate_services(self):
        self.invoke(updates.bootstrap_public_entry)
        count = len(self.runner.commands)
        result = self.invoke(updates.update_public_entry)
        self.assertEqual(result["status"], "public_entry_unchanged")
        self.assertEqual(self.runner.commands[count:], [])

    def test_secret_or_configuration_drift_rejects_update(self):
        self.invoke(updates.bootstrap_public_entry)
        self.key.write_text("8" * 64)
        count = len(self.runner.commands)
        with self.assertRaisesRegex(ValueError, "material_changed"):
            self.invoke(updates.update_public_entry, NEW, NEW_FRONT)
        self.assertEqual(self.runner.commands[count:], [])

    def test_rollback_requires_a_recorded_previous_version(self):
        self.invoke(updates.bootstrap_public_entry)
        with self.assertRaisesRegex(ValueError, "no_previous_version"):
            self.invoke(updates.rollback_public_entry)

    def test_operations_cannot_run_concurrently(self):
        descriptor = os.open(
            self.state / ".public-entry-operation.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaisesRegex(ValueError, "operation_locked"):
            self.invoke(updates.bootstrap_public_entry)
        self.assertEqual(self.runner.commands, [])

    def test_smoke_failure_never_reports_a_successful_release(self):
        self.smoke = lambda _config: (_ for _ in ()).throw(ValueError("unhealthy"))
        result = self.invoke(updates.bootstrap_public_entry)
        self.assertEqual(result["status"], "deployment_failed")
        self.assertFalse(self.runner.entrance)
        self.assertFalse((self.base / "active.json").exists())
        self.assertTrue((self.base / "pending.json").exists())

    def test_first_boot_can_retry_only_the_same_sealed_pair_without_resetting_redis(
        self,
    ):
        self.runner.fail_image = FRONT
        self.assertEqual(
            self.invoke(updates.bootstrap_public_entry)["status"], "deployment_failed"
        )
        sentinel = self.base / "redis-data/keep-existing-quotas"
        sentinel.write_text("keep")
        count = len(self.runner.commands)
        with self.assertRaisesRegex(ValueError, "profile_changed"):
            self.invoke(updates.bootstrap_public_entry, NEW, NEW_FRONT)
        self.assertEqual(self.runner.commands[count:], [])
        self.runner.fail_image = None
        self.assertEqual(
            self.invoke(updates.bootstrap_public_entry)["status"],
            "public_entry_started",
        )
        self.assertEqual(sentinel.read_text(), "keep")
        redis_commands = [
            call for call in self.runner.commands if call[-1] == "quota-redis"
        ]
        self.assertEqual(len(redis_commands), 2)
        self.assertTrue(all("--no-recreate" in call for call in redis_commands))

    def test_loopback_smoke_verifies_home_and_bundled_assets_without_redirects(self):
        config = SimpleNamespace(
            loopback_port=18084, public_origin="https://public.example.org:9443"
        )

        def response(body, mime):
            return SimpleNamespace(
                status=200, read=lambda _limit: body, getheader=lambda *_: mime
            )

        replies = [
            response(b'{"status":"ok"}', "application/json"),
            response(
                b'<div id="root"></div><script src="/assets/index-test.js"></script><link href="/assets/index-test.css">',
                "text/html",
            ),
            response(b"body{}", "text/css"),
            response(b"console.log(1)", "application/javascript"),
        ]
        with patch.object(updates.http.client, "HTTPConnection") as factory:
            connection = factory.return_value
            connection.getresponse.side_effect = replies
            updates._smoke(config)
            self.assertEqual(
                [call.args[1] for call in connection.request.call_args_list],
                ["/healthz", "/", "/assets/index-test.css", "/assets/index-test.js"],
            )
            self.assertTrue(
                all(
                    call.kwargs["headers"]["Host"] == "public.example.org:9443"
                    for call in connection.request.call_args_list
                )
            )
            connection.close.assert_called_once()
        with patch.object(updates.http.client, "HTTPConnection") as factory:
            factory.return_value.getresponse.return_value = SimpleNamespace(
                status=302, read=lambda _: b""
            )
            with self.assertRaisesRegex(ValueError, "http_acceptance_failed"):
                updates._smoke(config)
            self.assertEqual(factory.return_value.request.call_count, 1)
