"""The existing update/rollback contract also applies to the protected edge."""

import hashlib
import json
import os
import stat
import subprocess

from search.operations.deployment.public_entry import host, updates
from tests.unit.deployment.public_entry import test_updates as fixture


ALL_SERVICES = updates.SERVICES | updates.ANTIBOT_SERVICES
ANTIBOT_ASSETS = frozenset(
    {"antibot-nginx.conf", "antibot-errors.conf", "antibot-policy.yaml"}
)


def operation(argv):
    return argv[argv.index("-f") + 2 :]


class ProtectedComposeRunner(fixture.ComposeRunner):
    def __init__(self):
        super().__init__()
        self.running = set()
        self.fail_service = None

    def __call__(self, argv, **kwargs):
        result = super().__call__(argv, **kwargs)
        command = operation(list(argv))
        roles = set(command) & ALL_SERVICES
        if command[0] == "stop":
            self.running.difference_update(roles)
        elif command[0] == "up":
            self.running.update(roles)
            if self.fail_service in roles:
                self.running.discard(self.fail_service)
                raise subprocess.CalledProcessError(1, list(argv))
        return result


class AntibotUpdatesTests(fixture.PublicEntryUpdatesTests):
    def setUp(self):
        super().setUp()
        raw = json.loads(self.config_path.read_text())
        raw.update(access_mode="mixed", antibot_enabled=True)
        for name, value in (
            ("guest_session_secret_file", "7" * 64),
            ("antibot_signing_key_file", "6" * 64),
        ):
            path = self.root / name
            path.write_text(value)
            path.chmod(0o600)
            raw[name] = str(path)
        self.config_path.write_text(json.dumps(raw))
        self.runner = ProtectedComposeRunner()

    def assert_closed(self):
        self.assertFalse(self.runner.entrance)
        self.assertNotIn("public-edge", self.runner.running)
        self.assertNotIn("public-entry", self.runner.running)

    def assert_activation_order(self, commands):
        actions = [operation(argv) for argv in commands]
        stop = next(i for i, call in enumerate(actions) if call[0] == "stop")
        self.assertEqual(actions[stop][-2:], ["public-edge", "public-entry"])
        web = next(
            i
            for i, call in enumerate(actions)
            if call[0] == "up" and call[-2:] == ["public-web", "public-frontend"]
        )
        edge = next(
            i
            for i, call in enumerate(actions)
            if call[0] == "up" and call[-2:] == ["antibot", "public-edge"]
        )
        gateway = next(
            i
            for i, call in enumerate(actions)
            if call[0] == "up" and call[-1] == "public-entry"
        )
        self.assertLess(stop, web)
        self.assertLess(web, edge)
        self.assertLess(edge, gateway)
        self.assertTrue(all("down" not in call for call in actions))

    def test_protected_assets_are_sealed_and_all_services_have_lifecycle_ownership(
        self,
    ):
        started = self.invoke(updates.bootstrap_public_entry)
        prepared = json.loads((self.base / "prepared.json").read_text())
        self.assertTrue(ANTIBOT_ASSETS <= set(prepared["files"]))
        snapshot = updates._read_snapshot(self.base, started["generation"])
        self.assertEqual(snapshot["asset_sha256"], prepared["files"])
        self.assertEqual(set(snapshot["compose"]["services"]), ALL_SERVICES)
        for name in ANTIBOT_ASSETS:
            path = self.base / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                prepared["files"][name],
            )
            before = path.read_bytes()
            count = len(self.runner.commands)
            path.write_bytes(before + b"\n# changed\n")
            try:
                with (
                    self.subTest(asset=name),
                    self.assertRaisesRegex(ValueError, "preparation_changed"),
                ):
                    self.invoke(
                        updates.update_public_entry, fixture.NEW, fixture.NEW_FRONT
                    )
                self.assertEqual(self.runner.commands[count:], [])
            finally:
                path.write_bytes(before)
        managed = set()
        for argv in self.runner.commands:
            command = operation(argv)
            if command[0] in {"up", "stop"}:
                managed.update(set(command) & ALL_SERVICES)
        self.assertEqual(managed, ALL_SERVICES)
        self.assertEqual(self.runner.running, ALL_SERVICES)

    def test_bootstrap_update_and_rollback_start_edge_before_gateway(self):
        for function, web, frontend in (
            (updates.bootstrap_public_entry, fixture.OLD, fixture.FRONT),
            (updates.update_public_entry, fixture.NEW, fixture.NEW_FRONT),
            (updates.rollback_public_entry, fixture.OLD, fixture.FRONT),
        ):
            count = len(self.runner.commands)
            self.invoke(function, web, frontend)
            self.assert_activation_order(self.runner.commands[count:])
            self.assertTrue({"public-edge", "public-entry"} <= self.runner.running)

    def test_antibot_and_quota_state_remain_private_and_survive_failure_and_rollback(
        self,
    ):
        self.invoke(updates.bootstrap_public_entry)
        sentinels = {}
        for directory, name in (
            ("antibot-data", "anubis.bdb"),
            ("redis-data", "quota-state"),
        ):
            parent = self.base / directory
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
            self.assertEqual(parent.stat().st_uid, os.geteuid())
            path = parent / name
            path.write_bytes(b"existing protected admission state")
            path.chmod(0o600)
            sentinels[path] = (path.read_bytes(), path.stat().st_ino)
        self.invoke(updates.update_public_entry, fixture.NEW, fixture.NEW_FRONT)
        self.invoke(updates.rollback_public_entry)
        self.runner.fail_image = fixture.NEW_FRONT
        failed = self.invoke(
            updates.update_public_entry, fixture.NEW, fixture.NEW_FRONT
        )
        self.assertEqual(failed["status"], "deployment_failed")
        self.assert_closed()
        self.runner.fail_image = None
        self.invoke(updates.rollback_public_entry)
        for path, (content, inode) in sentinels.items():
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.runner.redis_starts, 1)

    def test_extra_secret_drift_rejects_update_and_rollback_before_mutation(self):
        self.invoke(updates.bootstrap_public_entry)
        self.invoke(updates.update_public_entry, fixture.NEW, fixture.NEW_FRONT)
        raw = json.loads(self.config_path.read_text())
        for field in ("guest_session_secret_file", "antibot_signing_key_file"):
            path = self.root / field
            self.assertEqual(str(path), raw[field])
            original = path.read_bytes()
            path.write_text("5" * 64)
            try:
                for function in (
                    updates.update_public_entry,
                    updates.rollback_public_entry,
                ):
                    count = len(self.runner.commands)
                    with (
                        self.subTest(field=field, function=function.__name__),
                        self.assertRaisesRegex(ValueError, "material_changed"),
                    ):
                        self.invoke(function, fixture.OLD, fixture.FRONT)
                    self.assertEqual(self.runner.commands[count:], [])
            finally:
                path.write_bytes(original)

    def test_first_boot_refuses_existing_unowned_antibot_state(self):
        host.prepare_public_entry(
            self.config_path,
            self.release_path,
            fixture.OLD,
            fixture.FRONT,
            image_inspector=self.inspector,
        )
        (self.base / "antibot-data" / "unowned.bdb").write_bytes(b"unowned state")
        with self.assertRaisesRegex(ValueError, "runtime_changed"):
            self.invoke(updates.bootstrap_public_entry)
        self.assertTrue(
            all(operation(call)[0] == "ps" for call in self.runner.commands)
        )

    def test_failed_challenge_service_start_closes_edge_and_can_resume_same_bootstrap(
        self,
    ):
        self.runner.fail_service = "antibot"
        failed = self.invoke(updates.bootstrap_public_entry)
        self.assertEqual(failed["status"], "deployment_failed")
        self.assert_closed()
        retained = self.base / "antibot-data" / "anubis.bdb"
        retained.write_bytes(b"partially initialized challenge state")
        retained.chmod(0o600)
        self.runner.fail_service = None
        resumed = self.invoke(updates.bootstrap_public_entry)
        self.assertEqual(resumed["status"], "public_entry_started")
        self.assertEqual(
            retained.read_bytes(), b"partially initialized challenge state"
        )
        self.assertTrue({"public-edge", "public-entry"} <= self.runner.running)
