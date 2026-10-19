"""Image updates retain admission state and recover partial CPU replacements."""

import copy
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.deployment.public_entry import profile as edge
from search.operations.deployment.public_entry import host
from search.operations.deployment.public_entry import updates
from search.operations.deployment.public_entry import preflight
from tests.unit.deployment.public_entry.test_preflight import API_IMAGE as BACKEND_IMAGE
from tests.unit.deployment.public_entry.test_preflight import fixture as backend_fixture


OLD_IMAGE = "sha256:" + "a" * 64
NEW_IMAGE = "sha256:" + "b" * 64
ENTRY_ID, REDIS_ID = "c" * 64, "d" * 64
REDIS_IMAGE_ID = "sha256:" + "f" * 64


class ComposeRunner:
    """Failure injection models replacement before a Docker client reports failure."""

    def __init__(self, base):
        self.base = base
        self.mutations = []
        self.pending_at_mutation = []
        self.fail_images = {}
        self.web_image = None
        self.web_id = None
        self.entry_running = False
        self.entry_created = False
        self.redis_created = False
        self.redis_running = False
        self.serial = 0
        self.on_replace = None
        self.on_observe = None
        self.before_mutation = None
        self.after_mutation = None
        self.fail_stops = 0
        self.redis_id = REDIS_ID
        self.redis_overrides = {}
        self.queries = []
        _, _, self.backend_network, self.backend = backend_fixture()
        # The backend fixture's default ID would collide with quota Redis.
        self.backend["Id"] = "e" * 64
        self.backend_network["Containers"] = {self.backend["Id"]: {}}
        self.backend_failure = None
        self.metadata_queries = []

    @staticmethod
    def result(value=b"", *, code=0):
        return SimpleNamespace(returncode=code, stdout=value, stderr=b"")

    def redis_metadata(self):
        profile = json.loads((self.base / "compose.json").read_text())
        service = profile["services"]["quota-redis"]
        row = {
            "Id": self.redis_id,
            "Image": REDIS_IMAGE_ID,
            "Config": {
                "Labels": {
                    "com.docker.compose.project": profile["name"],
                    "com.docker.compose.service": "quota-redis",
                }
            },
            "State": {
                "Status": "running" if self.redis_running else "created",
                "Running": self.redis_running,
                "Paused": False,
                "Restarting": False,
                "Dead": False,
                "StartedAt": "2026-01-01T00:00:00Z"
                if self.redis_running
                else "0001-01-01T00:00:00Z",
                "Health": {"Status": "healthy" if self.redis_running else "starting"},
            },
            "RestartCount": 0,
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": mount["source"],
                    "Destination": mount["target"],
                    "RW": not mount["read_only"],
                }
                for mount in service["volumes"]
            ],
            "NetworkSettings": {
                "Networks": {
                    profile["networks"][name].get(
                        "name", profile["name"] + "_" + name
                    ): {}
                    for name in service["networks"]
                },
                "Ports": {"6379/tcp": None},
            },
            "HostConfig": {"PortBindings": {}},
        }
        return {**row, **self.redis_overrides}

    def service_rows(self, roles):
        return [
            {"Service": role, "ID": identifier}
            for role, identifier, exists in (
                ("quota-redis", self.redis_id, self.redis_created),
                ("public-entry", ENTRY_ID, self.entry_created),
                ("public-web", self.web_id, self.web_id is not None),
            )
            if exists and role in roles
        ]

    def __call__(self, argv, **_kwargs):
        argv = list(argv)
        if argv[:3] == ["docker", "image", "inspect"]:
            self.metadata_queries.append(argv)
            return self.result((REDIS_IMAGE_ID + "\n").encode())
        if argv[:2] == ["docker", "inspect"] and self.backend["Id"] not in argv:
            self.metadata_queries.append(argv)
            if not self.redis_created or self.redis_id not in argv:
                return self.result(code=1)
            value = self.redis_metadata()
            return self.result(
                json.dumps(value if "--format" in argv else [value]).encode()
            )
        if argv[:3] == ["docker", "network", "inspect"] or argv[:2] == [
            "docker",
            "inspect",
        ]:
            self.metadata_queries.append(argv)
            if self.backend_failure:
                return self.backend_failure
            value = (
                self.backend_network
                if argv[:3] == ["docker", "network", "inspect"]
                else self.backend
            )
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(value).encode(), stderr=b""
            )
        if argv[:2] == ["docker", "ps"]:
            self.queries.append(argv)
            if (
                "label=com.docker.compose.project=" + edge.PROJECT not in argv
                or "--no-trunc" not in argv
            ):
                raise AssertionError("discovery must target only this Compose project")
            all_states = "--all" in argv or "-a" in argv
            roles = {
                role
                for role in updates.SERVICES
                if "label=com.docker.compose.service=" + role in argv
            } or updates.SERVICES
            rows = self.service_rows(roles)
            identifiers = [
                row["ID"]
                for row in rows
                if all_states
                or row["Service"] == "public-entry"
                and self.entry_running
                or row["Service"] == "quota-redis"
                and self.redis_running
                or row["Service"] == "public-web"
            ]
            return self.result(
                "".join(identifier + "\n" for identifier in identifiers).encode()
            )
        if argv[:2] == ["docker", "compose"] and "ps" in argv:
            self.queries.append(argv)
            roles = {
                role for role in updates.SERVICES if role in argv
            } or updates.SERVICES
            rows = self.service_rows(roles)
            return self.result(json.dumps(rows).encode())
        operation = next(
            (
                name
                for name in ("create", "up", "stop", "start", "down", "rm")
                if name in argv
            ),
            None,
        )
        if operation is None:
            return SimpleNamespace(returncode=0, stdout=b"[]\n", stderr=b"")
        self.mutations.append(argv)
        transaction = self.base / "transaction.json"
        try:
            pending = (
                json.loads(transaction.read_text()) if transaction.exists() else None
            )
        except ValueError:
            pending = {"malformed_receipt_preserved": True}
        self.pending_at_mutation.append(pending)
        if self.before_mutation:
            self.before_mutation(argv)
        if operation in {"down", "rm"}:
            raise AssertionError("ordinary updates must not remove deployment state")
        if operation == "stop":
            if "quota-redis" in argv:
                raise AssertionError("ordinary updates must not stop quota Redis")
            if self.fail_stops:
                self.fail_stops -= 1
                raise subprocess.CalledProcessError(
                    1, argv, stderr=b"fixture stop failure"
                )
            self.entry_running = False
        elif operation == "create":
            if (
                "quota-redis" not in argv
                or "public-entry" in argv
                or "public-web" in argv
            ):
                raise AssertionError("bootstrap may create only quota Redis")
            if self.redis_created:
                raise AssertionError("quota Redis must never be implicitly recreated")
            self.redis_created = True
        elif operation == "start":
            if "quota-redis" in argv:
                if not self.redis_created:
                    raise AssertionError("start must not create a missing quota Redis")
                self.redis_running = True
            else:
                if not self.entry_created:
                    raise AssertionError("start must not create a missing entrance")
                self.entry_running = True
        else:
            if "quota-redis" in argv or "--no-deps" not in argv:
                raise AssertionError(
                    "website up must not recreate Redis through dependencies"
                )
            if not self.redis_created or not self.redis_running:
                raise AssertionError("website requires the existing quota Redis")
            if (
                pending
                and pending.get("action") == "bootstrap"
                and pending.get("quota_container_id") != self.redis_id
            ):
                raise AssertionError(
                    "Redis identity must be durable before any entrance can start"
                )
            compose = json.loads(Path(argv[argv.index("-f") + 1]).read_text())
            self.web_image = compose["services"]["public-web"]["image"]
            if "--no-recreate" not in argv or self.web_id is None:
                self.serial += 1
                self.web_id = f"{self.serial:064x}"
            if "public-entry" in argv:
                self.entry_created = self.entry_running = True
            if self.on_replace:
                self.on_replace(self.web_image)
            remaining = self.fail_images.get(self.web_image, 0)
            if remaining:
                self.fail_images[self.web_image] = remaining - 1
                raise subprocess.CalledProcessError(1, argv, stderr=b"fixture failure")
        if self.after_mutation:
            self.after_mutation(argv)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def observe(self, snapshot, _runner):
        if not self.entry_running or not self.redis_running or not self.web_id:
            raise updates.PublicEntryUpdateError("public_entry_runtime_changed")
        if snapshot["web_image"] != self.web_image:
            raise updates.PublicEntryUpdateError("public_entry_runtime_changed")
        if self.on_observe:
            self.on_observe(snapshot["web_image"])
        return {
            "public-entry": ENTRY_ID,
            "quota-redis": self.redis_id,
            "public-web": self.web_id,
        }


class PublicEntryInspectionTests(unittest.TestCase):
    """Exercise the real Docker metadata checks without mocking the observer."""

    def setUp(self):
        self.identifiers = {
            "public-entry": ENTRY_ID,
            "quota-redis": REDIS_ID,
            "public-web": "e" * 64,
        }
        self.profile = {
            "name": edge.PROJECT,
            "services": {
                role: {"image": OLD_IMAGE, "volumes": [], "networks": ["quota"]}
                for role in self.identifiers
            },
            "networks": {"quota": {"name": "fixture-quota"}},
        }
        self.rows = {
            role: {
                "Id": identifier,
                "Image": OLD_IMAGE,
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": edge.PROJECT,
                        "com.docker.compose.service": role,
                    }
                },
                "State": {
                    "Running": True,
                    "Paused": False,
                    "Restarting": False,
                    "Dead": False,
                    "Health": {"Status": "healthy"},
                },
                "Mounts": [],
                "NetworkSettings": {"Networks": {"fixture-quota": {}}},
            }
            for role, identifier in self.identifiers.items()
        }
        self.calls = []

    def runner(self, command, **_kwargs):
        self.calls.append(list(command))
        if command == ["docker", "inspect", *self.identifiers.values()]:
            raw = json.dumps(list(self.rows.values())).encode()
        elif command == [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            OLD_IMAGE,
        ]:
            raw = (OLD_IMAGE + "\n").encode()
        else:
            raise AssertionError("inspection must not mutate Docker")
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")

    def inspect(self, *, healthy=True):
        return updates._inspect_containers(
            {"compose": self.profile},
            self.runner,
            self.identifiers,
            healthy=healthy,
        )

    def test_running_healthy_containers_pass_the_actual_metadata_observer(self):
        result = self.inspect()
        self.assertEqual(set(self.identifiers.values()), set(result))
        self.assertEqual(len(self.identifiers) + 1, len(self.calls))

    def test_paused_restarting_or_dead_peers_cannot_reuse_stale_healthy_status(self):
        original = copy.deepcopy(self.rows)
        for role in self.rows:
            for flag in ("Paused", "Restarting", "Dead"):
                with self.subTest(role=role, flag=flag):
                    self.rows = copy.deepcopy(original)
                    self.rows[role]["State"][flag] = True
                    with self.assertRaisesRegex(
                        updates.PublicEntryUpdateError, "^public_entry_runtime_changed$"
                    ):
                        self.inspect()

    def test_missing_lifecycle_flags_are_not_treated_as_false(self):
        original = copy.deepcopy(self.rows)
        for role in self.rows:
            for flag in ("Paused", "Restarting", "Dead"):
                with self.subTest(role=role, flag=flag):
                    self.rows = copy.deepcopy(original)
                    del self.rows[role]["State"][flag]
                    with self.assertRaisesRegex(
                        updates.PublicEntryUpdateError, "^public_entry_runtime_changed$"
                    ):
                        self.inspect()

    def test_created_containers_still_pass_profile_inspection_before_startup(self):
        for row in self.rows.values():
            row["State"].update(Status="created", Running=False)
            del row["State"]["Health"]
        result = self.inspect(healthy=False)
        self.assertEqual(set(self.identifiers.values()), set(result))


class PublicEntryUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-entry-update-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.base = self.state / "public-entry"
        self.raw = {
            "schema_version": 1,
            "state_root": str(self.state),
            "public_origin": "https://public.example.org",
            "oidc_issuer": "https://identity.example.org",
            "oidc_discovery": "https://identity.example.org/.well-known/openid-configuration",
            "oidc_client_id": "public-update-fixture",
        }
        for index, name in enumerate(
            (
                "oidc_client_secret_file",
                "oidc_session_secret_file",
                "edge_secret_file",
                "redis_password_file",
            )
        ):
            path = self.root / name
            path.write_text(
                "fixture-secret\n" if index == 0 else str(index) * 64 + "\n"
            )
            path.chmod(0o600)
            self.raw[name] = str(path)
        self.config_path = self.root / "entry.json"
        self.write_config()
        self.api_key = self.root / "api-key"
        self.api_key.write_text("e" * 64 + "\n")
        self.api_key.chmod(0o600)
        release_name = "depositar-ai-release-" + "a" * 12 + "-" + "b" * 12
        self.release = {
            "name": release_name,
            "services": {
                "web": {
                    "environment": {"WEB_UPSTREAM": "http://conversation-api:8000"},
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(self.api_key),
                            "target": "/run/secrets/api_key",
                            "read_only": True,
                        }
                    ],
                },
                "conversation-api": {"image": BACKEND_IMAGE},
            },
            "networks": {"website": {"name": release_name + "_website"}},
        }
        self.release_path = self.root / "release.json"
        self.release_path.write_text(json.dumps(self.release))
        self.release_path.chmod(0o600)
        self.runner = ComposeRunner(self.base)
        self.runner.backend["Mounts"][0]["Source"] = str(self.api_key)
        observation = patch.object(updates, "_observe", side_effect=self.runner.observe)
        observation.start()
        self.addCleanup(observation.stop)

    def write_config(self):
        self.config_path.write_text(json.dumps(self.raw))
        self.config_path.chmod(0o600)

    @staticmethod
    def inspect(image):
        return {
            "Id": image,
            "Labels": {
                host.CAPABILITY: "1",
                "org.opencontainers.image.revision": "f" * 40,
            },
        }

    def bootstrap(self):
        result = updates.bootstrap_public_entry(
            self.config_path,
            self.release_path,
            OLD_IMAGE,
            runner=self.runner,
            image_inspector=self.inspect,
        )
        self.runner.mutations.clear()
        self.runner.pending_at_mutation.clear()
        return result

    def update(self, image=NEW_IMAGE, config_path=None):
        return updates.update_public_entry(
            config_path or self.config_path,
            self.release_path,
            image,
            runner=self.runner,
            image_inspector=self.inspect,
        )

    def rollback(self):
        return updates.rollback_public_entry(
            self.config_path,
            runner=self.runner,
            image_inspector=self.inspect,
        )

    def recover(self):
        return updates.recover_public_entry(
            self.config_path,
            runner=self.runner,
            image_inspector=self.inspect,
        )

    def active(self):
        return json.loads((self.base / "active.json").read_text())

    def snapshot(self, generation=None):
        generation = generation or self.active()["generation"]
        return json.loads(
            (self.base / "versions" / generation / "snapshot.json").read_text()
        )

    def replace_recorded_backend_binding(self, binding):
        """Model an integrity-consistent legacy receipt, not a corrupt JSON file."""
        state = self.active()
        snapshot = self.snapshot()
        profile = snapshot["compose"]
        if binding is None:
            del profile["x-depositar-plan"]["private_backend"]
        else:
            profile["x-depositar-plan"]["private_backend"] = binding
        compose_bytes = updates.private_io.canonical(profile)
        (self.base / "compose.json").write_bytes(compose_bytes)
        prepared_path = self.base / "prepared.json"
        prepared = json.loads(prepared_path.read_text())
        prepared["files"]["compose.json"] = hashlib.sha256(compose_bytes).hexdigest()
        prepared_path.write_bytes(updates.private_io.canonical(prepared))
        fields = {
            "schema_version": snapshot["schema_version"],
            "compose": profile,
            "web_image": snapshot["web_image"],
            "asset_sha256": prepared["files"],
            "material_sha256": snapshot["material_sha256"],
        }
        generation = hashlib.sha256(updates.private_io.canonical(fields)).hexdigest()
        directory = self.base / "versions" / generation
        directory.mkdir(mode=0o700, exist_ok=True)
        compose_path = directory / "compose.json"
        snapshot_path = directory / "snapshot.json"
        compose_path.write_bytes(compose_bytes)
        snapshot_path.write_bytes(
            updates.private_io.canonical(
                {**fields, "generation": generation, "compose_file": str(compose_path)}
            )
        )
        compose_path.chmod(0o600)
        snapshot_path.chmod(0o600)
        changed = {**state, "generation": generation, "compose_file": str(compose_path)}
        (self.base / "active.json").write_bytes(updates.private_io.canonical(changed))
        transaction_path = self.base / "transaction.json"
        if transaction_path.exists():
            transaction = json.loads(transaction_path.read_text())
            transaction["previous"] = changed
            transaction_path.write_bytes(updates.private_io.canonical(transaction))
        return changed

    def assert_only_web_replaced(self):
        for argv in self.runner.mutations:
            self.assertNotIn("quota-redis", argv)
            if "up" in argv:
                self.assertIn("--no-deps", argv)
                self.assertEqual(argv[argv.index("--pull") + 1], "never")
                self.assertIn("public-web", argv)
        self.assertTrue(all(self.runner.pending_at_mutation))

    def test_update_and_rollback_keep_previous_files_and_quota_storage(self):
        self.bootstrap()
        before = self.active()
        old = self.snapshot()
        old_bytes = Path(old["compose_file"]).read_bytes()
        sentinel = self.base / "redis-data" / "quota-sentinel"
        sentinel.write_bytes(b"do not reset admission state")
        self.update()
        current = self.active()
        self.assertNotEqual(current["generation"], before["generation"])
        self.assertEqual(current["previous_generation"], before["generation"])
        self.assertEqual(current["material_sha256"], before["material_sha256"])
        self.assertEqual(current["runtime_ids"]["quota-redis"], REDIS_ID)
        self.assertEqual(current["runtime_ids"]["public-entry"], ENTRY_ID)
        self.assertEqual(self.snapshot()["web_image"], NEW_IMAGE)
        self.assertEqual(Path(old["compose_file"]).read_bytes(), old_bytes)
        self.assertEqual(sentinel.read_bytes(), b"do not reset admission state")
        self.assertFalse((self.base / "transaction.json").exists())
        self.rollback()
        self.assertEqual(self.active()["generation"], before["generation"])
        self.assertEqual(self.runner.web_image, OLD_IMAGE)
        self.assertEqual(sentinel.read_bytes(), b"do not reset admission state")
        self.assert_only_web_replaced()

    def test_partial_candidate_failure_restores_old_generation(self):
        self.bootstrap()
        before = self.active()
        self.runner.fail_images[NEW_IMAGE] = 1
        result = self.update()
        self.assertEqual(result["status"], "update_failed_rolled_back")
        self.assertEqual(self.active()["generation"], before["generation"])
        self.assertEqual(self.runner.web_image, OLD_IMAGE)
        self.assertTrue(self.runner.entry_running)
        self.assertFalse((self.base / "transaction.json").exists())
        self.assert_only_web_replaced()

    def make_recovery_required(self):
        self.runner.fail_images.update({NEW_IMAGE: 1, OLD_IMAGE: 1})
        return self.update()

    def test_failed_rollback_stops_entrance_and_explicit_recovery_restores_old(self):
        self.bootstrap()
        before = self.active()
        self.make_recovery_required()
        transaction = json.loads((self.base / "transaction.json").read_text())
        self.assertEqual(transaction["status"], "recovery_required")
        self.assertFalse(self.runner.entry_running)
        self.assertTrue(self.runner.redis_running)
        self.assertEqual(self.active()["generation"], before["generation"])
        self.runner.fail_images.clear()
        self.recover()
        self.assertEqual(self.active()["generation"], before["generation"])
        self.assertEqual(self.runner.web_image, OLD_IMAGE)
        self.assertTrue(self.runner.entry_running)
        self.assertFalse((self.base / "transaction.json").exists())
        self.assert_only_web_replaced()

    def test_failed_emergency_stop_is_reported_without_claiming_closed_entrance(self):
        self.bootstrap()
        before = self.active()
        self.runner.fail_stops = 3
        result = self.update()
        self.assertEqual(result["status"], "recovery_required")
        self.assertFalse(result["entrance_stopped"])
        self.assertTrue(self.runner.entry_running)
        self.assertTrue(self.runner.redis_running)
        transaction = json.loads((self.base / "transaction.json").read_text())
        self.assertFalse(transaction["entrance_stopped"])
        self.assertEqual(self.active(), before)
        self.recover()
        self.assertFalse((self.base / "transaction.json").exists())
        self.assertTrue(self.runner.entry_running)

    def test_unexpected_redis_replacement_is_not_accepted_as_preserved_state(self):
        self.bootstrap()
        before = self.active()

        def replace_redis_externally(image):
            if image == NEW_IMAGE:
                self.runner.redis_id = "9" * 64

        self.runner.on_replace = replace_redis_externally
        result = self.update()
        self.assertEqual(result["status"], "recovery_required")
        self.assertFalse(self.runner.entry_running)
        self.assertEqual(self.active(), before)
        self.assertEqual(self.active()["runtime_ids"]["quota-redis"], REDIS_ID)
        self.assertTrue((self.base / "transaction.json").exists())
        self.assert_only_web_replaced()

    def test_pending_recovery_blocks_all_ordinary_operations_before_mutation(self):
        self.bootstrap()
        self.make_recovery_required()
        for operation in (self.update, self.rollback, self.bootstrap):
            self.runner.mutations.clear()
            with (
                self.subTest(operation=operation.__name__),
                self.assertRaises(updates.PublicEntryUpdateError),
            ):
                operation()
            self.assertEqual(self.runner.mutations, [])
            self.assertFalse(self.runner.entry_running)

    def test_stopped_baseline_is_not_silently_resumed(self):
        self.bootstrap()
        before = self.active()
        self.runner.entry_running = False
        with self.assertRaises(updates.PublicEntryUpdateError):
            self.update()
        self.assertEqual(self.runner.mutations, [])
        self.assertEqual(self.active(), before)
        self.assertFalse(self.runner.entry_running)

    def test_changed_identity_quota_or_same_path_secrets_are_rejected_before_docker(
        self,
    ):
        self.bootstrap()
        original = copy.deepcopy(self.raw)
        before = self.active()
        for changed in (
            {"quota_per_account": 9},
            {"public_origin": "https://different.example.org"},
            {"oidc_issuer": "https://different-identity.example.org"},
            {
                "network_subnets": {
                    "website": "10.253.251.0/28",
                    "quota": "10.253.251.16/28",
                    "outbound": "10.253.251.32/28",
                }
            },
        ):
            self.raw = {**original, **changed}
            self.write_config()
            with (
                self.subTest(changed=changed),
                self.assertRaises(updates.PublicEntryUpdateError),
            ):
                self.update()
            self.assertEqual(self.runner.mutations, [])
            self.assertEqual(self.active(), before)
        self.raw = original
        self.write_config()
        for path in (Path(self.raw["edge_secret_file"]), self.api_key):
            previous = path.read_bytes()
            path.write_text("0" * 64 + "\n")
            with (
                self.subTest(secret=path.name),
                self.assertRaises(updates.PublicEntryUpdateError),
            ):
                self.update()
            self.assertEqual(self.runner.mutations, [])
            path.write_bytes(previous)

    def test_identical_config_copy_keeps_effective_material_and_runtime_pins(self):
        self.bootstrap()
        before = self.active()
        other = self.root / "other-entry.json"
        other.write_bytes(self.config_path.read_bytes())
        other.chmod(0o600)
        self.update(config_path=other)
        self.assertEqual(self.active()["material_sha256"], before["material_sha256"])
        self.assertEqual(self.active()["runtime_ids"]["quota-redis"], REDIS_ID)
        self.assert_only_web_replaced()

    def test_bootstrap_remains_idempotent_after_image_update(self):
        self.bootstrap()
        self.update()
        before = self.active()
        self.runner.mutations.clear()
        updates.bootstrap_public_entry(
            self.config_path,
            self.release_path,
            NEW_IMAGE,
            runner=self.runner,
            image_inspector=self.inspect,
        )
        self.assertEqual(self.runner.mutations, [])
        self.assertEqual(self.active(), before)

    def test_first_bootstrap_rejects_backend_before_pending_or_container_mutation(self):
        original_network = copy.deepcopy(self.runner.backend_network)
        original_api = copy.deepcopy(self.runner.backend)
        for case, code in (
            ("missing", "inspection_failed"),
            ("public_network", "network_invalid"),
            ("unhealthy", "unhealthy"),
        ):
            self.runner.backend_failure = None
            self.runner.backend_network = copy.deepcopy(original_network)
            self.runner.backend = copy.deepcopy(original_api)
            if case == "missing":
                self.runner.backend_failure = SimpleNamespace(
                    returncode=1, stdout=b"", stderr=b"fixture missing network"
                )
            elif case == "public_network":
                self.runner.backend_network["Internal"] = False
            else:
                self.runner.backend["State"]["Running"] = False
            with (
                self.subTest(case=case),
                self.assertRaisesRegex(
                    preflight.PublicEntryPreflightError,
                    "^public_entry_backend_" + code + "$",
                ),
            ):
                self.bootstrap()
            self.assertEqual(self.runner.mutations, [])
            self.assertFalse((self.base / "transaction.json").exists())
            self.assertFalse((self.base / "active.json").exists())

    def test_bootstrap_uses_sealed_backend_when_release_file_changes_after_prepare(
        self,
    ):
        prepare = host.prepare_public_entry

        def changed_after_prepare(*args, **kwargs):
            result = prepare(*args, **kwargs)
            changed = copy.deepcopy(self.release)
            changed["services"]["conversation-api"]["image"] = NEW_IMAGE
            self.release_path.write_text(json.dumps(changed))
            return result

        with patch.object(
            host, "prepare_public_entry", side_effect=changed_after_prepare
        ):
            result = self.bootstrap()
        self.assertEqual(result["status"], "public_entry_started")
        self.assertEqual(
            self.snapshot()["compose"]["x-depositar-plan"]["private_backend"],
            {
                "schema_version": 1,
                "release_project": self.release["name"],
                "network_name": self.release["networks"]["website"]["name"],
                "api_image": BACKEND_IMAGE,
            },
        )
        self.assertTrue(self.runner.entry_running)

    def test_missing_or_invalid_sealed_binding_refuses_ordinary_mutation(self):
        self.bootstrap()
        valid = self.snapshot()["compose"]["x-depositar-plan"]["private_backend"]
        for binding in (
            None,
            {**valid, "schema_version": True},
            {**valid, "api_image": "latest"},
        ):
            with self.subTest(binding=binding):
                before = self.replace_recorded_backend_binding(binding)
                for operation in (self.update, self.bootstrap):
                    self.runner.mutations.clear()
                    with self.assertRaisesRegex(
                        (
                            updates.PublicEntryUpdateError,
                            preflight.PublicEntryPreflightError,
                        ),
                        "^public_entry_(profile_changed|backend_plan_invalid)$",
                    ):
                        operation()
                    self.assertEqual(self.runner.mutations, [])
                    self.assertEqual(self.active(), before)
                    self.assertFalse((self.base / "transaction.json").exists())
                with self.assertRaisesRegex(
                    preflight.PublicEntryPreflightError,
                    "^public_entry_backend_plan_invalid$",
                ):
                    self.recover()
                self.assertEqual(self.runner.mutations, [])
                self.assertEqual(self.active(), before)
                self.assertFalse((self.base / "transaction.json").exists())
                self.replace_recorded_backend_binding(valid)

    def test_invalid_sealed_binding_in_pending_recovery_closes_candidate(self):
        self.leave_candidate_pending()
        valid = self.snapshot()["compose"]["x-depositar-plan"]["private_backend"]
        sentinel = self.base / "redis-data" / "quota-sentinel"
        sentinel.write_bytes(b"preserve quota history")
        for binding in (None, {**valid, "network_name": "wrong-network"}):
            with self.subTest(binding=binding):
                before = self.replace_recorded_backend_binding(binding)
                self.runner.mutations.clear()
                result = self.recover()
                self.assertEqual(result["status"], "recovery_required")
                self.assertTrue(result["entrance_stopped"])
                self.assertFalse(self.runner.entry_running)
                self.assertTrue(self.runner.redis_running)
                self.assertEqual(self.active(), before)
                self.assertFalse(
                    any(
                        "up" in argv or "start" in argv
                        for argv in self.runner.mutations
                    )
                )
                self.assertTrue((self.base / "transaction.json").exists())
                self.assertEqual(sentinel.read_bytes(), b"preserve quota history")
                self.replace_recorded_backend_binding(valid)
        self.recover()
        self.assertTrue(self.runner.entry_running)
        self.assertFalse((self.base / "transaction.json").exists())

    def test_interrupted_bootstrap_backend_failure_retains_state_until_healed(self):
        def interrupted(_image):
            raise KeyboardInterrupt("fixture bootstrap interruption")

        self.runner.on_replace = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.bootstrap()
        self.runner.on_replace = None
        transaction = json.loads((self.base / "transaction.json").read_text())
        web_id = self.runner.web_id
        sentinel = self.base / "redis-data" / "quota-sentinel"
        sentinel.write_bytes(b"preserve quota history")
        self.assertTrue(self.runner.entry_running)
        self.runner.backend["State"]["Health"]["Status"] = "unhealthy"
        self.runner.mutations.clear()
        result = self.recover()
        self.assertEqual(result["status"], "recovery_required")
        self.assertTrue(result["entrance_stopped"])
        self.assertFalse(self.runner.entry_running)
        self.assertTrue(self.runner.redis_running)
        self.assertFalse((self.base / "active.json").exists())
        self.assertEqual(
            json.loads((self.base / "transaction.json").read_text())["candidate"],
            transaction["candidate"],
        )
        self.assertFalse(
            any("up" in argv or "start" in argv for argv in self.runner.mutations)
        )
        self.assertEqual(sentinel.read_bytes(), b"preserve quota history")
        self.runner.backend["State"]["Health"]["Status"] = "healthy"
        result = self.recover()
        self.assertEqual(result["status"], "public_entry_recovered")
        self.assertEqual(self.active()["generation"], transaction["candidate"])
        self.assertEqual(self.runner.web_id, web_id)
        self.assertEqual(self.active()["runtime_ids"]["quota-redis"], REDIS_ID)
        self.assertEqual(sentinel.read_bytes(), b"preserve quota history")
        self.assertFalse((self.base / "transaction.json").exists())
        for argv in self.runner.mutations:
            if "up" in argv:
                self.assertIn("--no-recreate", argv)

    def test_backend_drift_during_web_up_prevents_entry_start_and_automatic_rollback(
        self,
    ):
        self.bootstrap()
        before = self.active()

        def drift(image):
            if image == NEW_IMAGE:
                self.runner.backend["Image"] = NEW_IMAGE

        self.runner.on_replace = drift
        result = self.update()
        self.assertEqual(result["status"], "recovery_required")
        self.assertTrue(result["entrance_stopped"])
        self.assertFalse(self.runner.entry_running)
        self.assertTrue(self.runner.redis_running)
        self.assertFalse(any("start" in argv for argv in self.runner.mutations))
        self.assertEqual(self.active(), before)
        self.assertTrue((self.base / "transaction.json").exists())
        self.assert_only_web_replaced()

    def test_backend_drift_after_candidate_observation_cannot_commit_success(self):
        self.bootstrap()
        before = self.active()

        def drift(image):
            if image == NEW_IMAGE:
                self.runner.backend["State"]["Running"] = False

        self.runner.on_observe = drift
        result = self.update()
        self.assertEqual(result["status"], "recovery_required")
        self.assertFalse(self.runner.entry_running)
        self.assertTrue(self.runner.redis_running)
        self.assertEqual(self.active(), before)
        self.assertTrue((self.base / "transaction.json").exists())

    def test_backend_drift_after_automatic_rollback_observation_cannot_commit(self):
        self.bootstrap()
        before = self.active()
        self.runner.fail_images[NEW_IMAGE] = 1

        def drift(image):
            if image == OLD_IMAGE and any(
                "up" in argv for argv in self.runner.mutations
            ):
                self.runner.backend["State"]["Running"] = False

        self.runner.on_observe = drift
        result = self.update()
        self.assertEqual(result["status"], "recovery_required")
        self.assertFalse(self.runner.entry_running)
        self.assertEqual(self.active(), before)
        self.assertTrue((self.base / "transaction.json").exists())

    def test_backend_drift_after_bootstrap_observation_cannot_publish_active(self):
        self.runner.on_observe = lambda _image: self.runner.backend["State"].update(
            Running=False
        )
        result = self.bootstrap()
        self.assertEqual(result["status"], "recovery_required")
        self.assertFalse(self.runner.entry_running)
        self.assertTrue(self.runner.redis_running)
        self.assertFalse((self.base / "active.json").exists())
        self.assertTrue((self.base / "transaction.json").exists())

    def test_backend_drift_after_recovery_observation_cannot_publish_active(self):
        self.runner.fail_images[OLD_IMAGE] = 1
        self.bootstrap()
        self.runner.on_observe = lambda _image: self.runner.backend["State"].update(
            Running=False
        )
        result = self.recover()
        self.assertEqual(result["status"], "recovery_required")
        self.assertFalse(self.runner.entry_running)
        self.assertTrue(self.runner.redis_running)
        self.assertFalse((self.base / "active.json").exists())
        self.assertTrue((self.base / "transaction.json").exists())

    def test_bootstrap_rejects_config_edit_between_load_and_preparation(self):
        prepare = host.prepare_public_entry

        def changed_before_prepare(*args, **kwargs):
            self.raw["quota_per_account"] = 9
            self.write_config()
            return prepare(*args, **kwargs)

        with patch.object(
            host, "prepare_public_entry", side_effect=changed_before_prepare
        ):
            with self.assertRaisesRegex(
                updates.PublicEntryUpdateError, "material_changed"
            ):
                self.bootstrap()
        self.assertEqual(self.runner.mutations, [])
        self.assertFalse((self.base / "active.json").exists())
        self.assertFalse((self.base / "transaction.json").exists())
        # The fully prepared B configuration can be explicitly retried; the
        # interrupted A request never acquires a mixed A/B running receipt.
        self.bootstrap()
        self.assertEqual(self.active()["web_image"], OLD_IMAGE)

    def test_same_image_update_is_idempotent_without_container_mutation(self):
        self.bootstrap()
        before = self.active()
        self.update(OLD_IMAGE)
        self.assertEqual(self.runner.mutations, [])
        self.assertEqual(self.active(), before)

    def test_missing_previous_image_prevents_mutation_and_preserves_active(self):
        self.bootstrap()
        before = self.active()

        def missing_old(image):
            if image == OLD_IMAGE:
                raise FileNotFoundError("fixture image is absent")
            return PublicEntryUpdateTests.inspect(image)

        with patch.object(self, "inspect", side_effect=missing_old):
            with self.assertRaises(FileNotFoundError):
                self.update()
        self.assertEqual(self.runner.mutations, [])
        self.assertEqual(self.active(), before)

    def test_interrupted_snapshot_staging_can_retry_without_publishing_partial_version(
        self,
    ):
        self.bootstrap()
        before = self.active()
        versions = self.base / "versions"
        published = {
            path.name for path in versions.iterdir() if not path.name.startswith(".")
        }
        write_json = updates.private_io.write_json

        def interrupt(descriptor, name, value, **kwargs):
            if name == "snapshot.json" and value["web_image"] == NEW_IMAGE:
                raise OSError("fixture staging write interruption")
            return write_json(descriptor, name, value, **kwargs)

        with patch.object(updates.private_io, "write_json", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.update()
        self.assertEqual(self.runner.mutations, [])
        self.assertEqual(self.active(), before)
        self.assertFalse((self.base / "transaction.json").exists())
        self.assertEqual(
            {path.name for path in versions.iterdir() if not path.name.startswith(".")},
            published,
        )
        self.assertTrue(list(versions.glob(".staging-*")))
        self.update()
        self.assertEqual(self.active()["web_image"], NEW_IMAGE)

    def test_interruption_before_active_publication_requires_explicit_recovery(self):
        self.bootstrap()
        before = self.active()
        write_json = updates.private_io.write_json

        def interrupt(descriptor, name, value, **kwargs):
            if name == "active.json" and value["web_image"] == NEW_IMAGE:
                raise KeyboardInterrupt("fixture process interruption")
            return write_json(descriptor, name, value, **kwargs)

        with patch.object(updates.private_io, "write_json", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.update()
        self.assertEqual(self.runner.web_image, NEW_IMAGE)
        self.assertEqual(self.active(), before)
        self.assertTrue((self.base / "transaction.json").exists())
        self.runner.mutations.clear()
        with self.assertRaises(updates.PublicEntryUpdateError):
            self.update()
        self.assertEqual(self.runner.mutations, [])
        self.recover()
        self.assertEqual(self.active()["generation"], before["generation"])
        self.assertEqual(self.runner.web_image, OLD_IMAGE)
        self.assertFalse((self.base / "transaction.json").exists())

    def test_interruption_after_active_publication_still_restores_transaction_previous(
        self,
    ):
        self.bootstrap()
        before = self.active()
        remove = updates._remove

        def interrupt(root, name):
            if name == "transaction.json":
                raise KeyboardInterrupt("fixture process interruption")
            return remove(root, name)

        with patch.object(updates, "_remove", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.update()
        self.assertEqual(self.active()["web_image"], NEW_IMAGE)
        self.assertTrue((self.base / "transaction.json").exists())
        self.recover()
        self.assertEqual(self.active()["generation"], before["generation"])
        self.assertEqual(self.runner.web_image, OLD_IMAGE)
        self.assertFalse((self.base / "transaction.json").exists())

    def leave_candidate_pending(self):
        self.bootstrap()
        before = self.active()
        write_json = updates.private_io.write_json

        def interrupt(descriptor, name, value, **kwargs):
            if name == "active.json" and value["web_image"] == NEW_IMAGE:
                raise KeyboardInterrupt("fixture process interruption")
            return write_json(descriptor, name, value, **kwargs)

        with patch.object(updates.private_io, "write_json", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.update()
        self.assertTrue(self.runner.entry_running)
        self.assertEqual(self.runner.web_image, NEW_IMAGE)
        return before

    def assert_emergency_closed(self, report):
        self.assertEqual(report["status"], "recovery_required")
        self.assertTrue(report["entrance_stopped"])
        self.assertFalse(self.runner.entry_running)
        self.assertTrue(self.runner.redis_running)
        self.assertTrue(self.runner.queries)
        stops = [
            argv for argv in self.runner.mutations if argv[:2] == ["docker", "stop"]
        ]
        self.assertTrue(stops)
        for argv in stops:
            self.assertIn(ENTRY_ID, argv)
            self.assertNotIn(REDIS_ID, argv)

    def test_missing_previous_history_still_closes_running_candidate(self):
        for filename in ("snapshot.json", "compose.json"):
            with self.subTest(filename=filename):
                before = self.leave_candidate_pending()
                path = self.base / "versions" / before["generation"] / filename
                original = path.read_bytes()
                path.unlink()
                self.assert_emergency_closed(self.recover())
                self.assertEqual(self.active(), before)
                self.assertTrue((self.base / "transaction.json").exists())
                path.write_bytes(original)
                path.chmod(0o600)
                self.recover()
                self.assertEqual(self.runner.web_image, OLD_IMAGE)

    def test_corrupt_previous_history_still_closes_running_candidate(self):
        for filename in ("snapshot.json", "compose.json"):
            with self.subTest(filename=filename):
                before = self.leave_candidate_pending()
                path = self.base / "versions" / before["generation"] / filename
                original = path.read_bytes()
                path.write_bytes(b"{invalid history")
                self.assert_emergency_closed(self.recover())
                self.assertEqual(path.read_bytes(), b"{invalid history")
                self.assertEqual(self.active(), before)
                path.write_bytes(original)
                self.recover()
                self.assertEqual(self.runner.web_image, OLD_IMAGE)

    def test_malformed_transaction_is_preserved_while_candidate_is_closed(self):
        before = self.leave_candidate_pending()
        path = self.base / "transaction.json"
        malformed = b'{"operator_note":"private-secret-sentinel", invalid'
        path.write_bytes(malformed)
        report = self.recover()
        self.assert_emergency_closed(report)
        self.assertEqual(path.read_bytes(), malformed)
        self.assertEqual(self.active(), before)
        self.assertNotIn("sentinel", json.dumps(report))
        self.runner.mutations.clear()
        with self.assertRaises(updates.PublicEntryUpdateError):
            self.update()
        self.assertEqual(self.runner.mutations, [])

    def test_removed_secret_does_not_prevent_emergency_close_after_interruption(self):
        before = self.leave_candidate_pending()
        path = Path(self.raw["edge_secret_file"])
        original = path.read_bytes()
        path.unlink()
        self.assert_emergency_closed(self.recover())
        self.assertEqual(self.active(), before)
        self.assertTrue((self.base / "transaction.json").exists())
        path.write_bytes(original)
        path.chmod(0o600)
        self.recover()
        self.assertEqual(self.runner.web_image, OLD_IMAGE)
        self.assertFalse((self.base / "transaction.json").exists())

    def test_damaged_history_and_failed_emergency_stop_report_uncertainty(self):
        before = self.leave_candidate_pending()
        path = self.base / "versions" / before["generation"] / "snapshot.json"
        original = path.read_bytes()
        path.unlink()
        self.runner.fail_stops = 1
        report = self.recover()
        self.assertEqual(report["status"], "recovery_required")
        self.assertFalse(report["entrance_stopped"])
        self.assertTrue(self.runner.entry_running)
        self.assertTrue(self.runner.redis_running)
        self.assertEqual(self.active(), before)
        self.assertFalse(
            json.loads((self.base / "transaction.json").read_text())["entrance_stopped"]
        )
        path.write_bytes(original)
        path.chmod(0o600)
        self.recover()
        self.assertEqual(self.runner.web_image, OLD_IMAGE)
        self.assertFalse((self.base / "transaction.json").exists())

    def test_unknown_runtime_and_unclaimed_redis_data_cannot_be_bootstrapped(self):
        host.prepare_public_entry(
            self.config_path,
            self.release_path,
            OLD_IMAGE,
            image_inspector=self.inspect,
        )
        with patch.object(updates, "_project_present", return_value=True):
            with self.assertRaises(updates.PublicEntryUpdateError):
                self.bootstrap()
        self.assertEqual(self.runner.mutations, [])
        sentinel = self.base / "redis-data" / "unknown-history"
        sentinel.write_bytes(b"preserve")
        with self.assertRaises(updates.PublicEntryUpdateError):
            self.bootstrap()
        self.assertEqual(self.runner.mutations, [])
        self.assertEqual(sentinel.read_bytes(), b"preserve")

    def test_failed_first_bootstrap_recovery_never_recreates_quota_services(self):
        self.runner.fail_images[OLD_IMAGE] = 1
        result = updates.bootstrap_public_entry(
            self.config_path,
            self.release_path,
            OLD_IMAGE,
            runner=self.runner,
            image_inspector=self.inspect,
        )
        self.assertEqual(result["status"], "recovery_required")
        self.assertFalse((self.base / "active.json").exists())
        self.assertFalse(self.runner.entry_running)
        self.assertTrue(self.runner.redis_running)
        candidate = json.loads((self.base / "transaction.json").read_text())[
            "candidate"
        ]
        web_id = self.runner.web_id
        self.runner.mutations.clear()
        self.recover()
        self.assertEqual(self.active()["generation"], candidate)
        self.assertEqual(self.runner.web_id, web_id)
        self.assertEqual(self.active()["runtime_ids"]["quota-redis"], REDIS_ID)
        self.assertFalse((self.base / "transaction.json").exists())
        for argv in self.runner.mutations:
            if "up" in argv:
                self.assertIn("--no-recreate", argv)
                self.assertEqual(argv[argv.index("--pull") + 1], "never")

    def interrupt_bootstrap_at_redis_create(self, *, after=False):
        def interrupt(argv):
            if "create" in argv:
                raise KeyboardInterrupt("fixture Redis creation interruption")

        attribute = "after_mutation" if after else "before_mutation"
        setattr(self.runner, attribute, interrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.bootstrap()
        setattr(self.runner, attribute, None)
        transaction = json.loads((self.base / "transaction.json").read_text())
        self.assertIn("quota_container_id", transaction)
        self.assertIsNone(transaction["quota_container_id"])
        self.assertFalse(self.runner.entry_created)
        self.assertIsNone(self.runner.web_id)
        self.assertFalse(self.runner.redis_running)
        return transaction

    def pending_bootstrap_with_quota_history(self):
        self.runner.fail_images[OLD_IMAGE] = 1
        result = self.bootstrap()
        self.assertEqual(result["status"], "recovery_required")
        transaction = json.loads((self.base / "transaction.json").read_text())
        self.assertEqual(transaction["quota_container_id"], REDIS_ID)
        sentinel = self.base / "redis-data" / "quota-sentinel.aof"
        sentinel.write_bytes(b"preserve admitted turn counters")
        self.runner.mutations.clear()
        self.runner.pending_at_mutation.clear()
        return transaction, sentinel

    def assert_bootstrap_recovery_refused(self, transaction, sentinel=None):
        result = self.recover()
        self.assertEqual(result["status"], "recovery_required")
        self.assertTrue(result["entrance_stopped"])
        self.assertFalse(self.runner.entry_running)
        self.assertFalse((self.base / "active.json").exists())
        current = json.loads((self.base / "transaction.json").read_text())
        self.assertEqual(current["candidate"], transaction["candidate"])
        self.assertEqual(
            current.get("quota_container_id"), transaction.get("quota_container_id")
        )
        self.assertFalse(
            any(
                "create" in argv or "up" in argv or "start" in argv
                for argv in self.runner.mutations
            )
        )
        if sentinel is not None:
            self.assertEqual(sentinel.read_bytes(), b"preserve admitted turn counters")

    def test_early_bootstrap_crash_before_redis_creation_can_complete_recovery(self):
        transaction = self.interrupt_bootstrap_at_redis_create()
        self.assertFalse(self.runner.redis_created)
        self.runner.mutations.clear()
        result = self.recover()
        self.assertEqual(result["status"], "public_entry_recovered")
        self.assertEqual(self.active()["generation"], transaction["candidate"])
        self.assertEqual(self.active()["runtime_ids"]["quota-redis"], REDIS_ID)
        self.assertTrue(self.runner.entry_running)
        self.assertFalse((self.base / "transaction.json").exists())
        self.assertEqual(sum("create" in argv for argv in self.runner.mutations), 1)
        for argv, pending in zip(
            self.runner.mutations,
            self.runner.pending_at_mutation[-len(self.runner.mutations) :],
        ):
            if "up" in argv:
                self.assertEqual(pending["quota_container_id"], REDIS_ID)
                self.assertIn("--no-deps", argv)

    def test_early_bootstrap_crash_after_redis_creation_adopts_only_never_started_container(
        self,
    ):
        self.interrupt_bootstrap_at_redis_create(after=True)
        self.assertTrue(self.runner.redis_created)
        self.runner.mutations.clear()
        result = self.recover()
        self.assertEqual(result["status"], "public_entry_recovered")
        self.assertEqual(self.active()["runtime_ids"]["quota-redis"], REDIS_ID)
        self.assertFalse(any("create" in argv for argv in self.runner.mutations))
        self.assertTrue(self.runner.redis_running)
        self.assertTrue(self.runner.entry_running)

    def test_null_identity_cannot_adopt_a_previously_started_redis(self):
        transaction = self.interrupt_bootstrap_at_redis_create(after=True)
        created_state = self.runner.redis_metadata()["State"]
        for changed in (
            {"Status": "running", "Running": True},
            {"Status": "exited", "StartedAt": "2026-01-01T00:00:00Z"},
        ):
            with self.subTest(changed=changed):
                self.runner.redis_overrides = {"State": {**created_state, **changed}}
                self.runner.mutations.clear()
                self.assert_bootstrap_recovery_refused(transaction)

    def test_null_identity_with_existing_website_or_aof_history_refuses_creation(self):
        transaction = self.interrupt_bootstrap_at_redis_create()
        self.runner.entry_created = True
        self.runner.mutations.clear()
        self.assert_bootstrap_recovery_refused(transaction)
        self.runner.entry_created = False
        sentinel = self.base / "redis-data" / "quota-sentinel.aof"
        sentinel.write_bytes(b"preserve admitted turn counters")
        self.runner.mutations.clear()
        self.assert_bootstrap_recovery_refused(transaction, sentinel)
        self.assertFalse(self.runner.redis_created)

    def test_pinned_missing_redis_is_not_recreated_and_original_identity_can_recover(
        self,
    ):
        transaction, sentinel = self.pending_bootstrap_with_quota_history()
        self.runner.redis_created = self.runner.redis_running = False
        self.assert_bootstrap_recovery_refused(transaction, sentinel)
        self.runner.redis_created = True
        self.runner.redis_running = False
        result = self.recover()
        self.assertEqual(result["status"], "public_entry_recovered")
        self.assertEqual(self.active()["runtime_ids"]["quota-redis"], REDIS_ID)
        self.assertEqual(sentinel.read_bytes(), b"preserve admitted turn counters")
        self.assertFalse(any("create" in argv for argv in self.runner.mutations))

    def test_pinned_replaced_redis_is_refused_without_discarding_aof_or_transaction(
        self,
    ):
        transaction, sentinel = self.pending_bootstrap_with_quota_history()
        self.runner.redis_id = "9" * 64
        self.assert_bootstrap_recovery_refused(transaction, sentinel)
        self.assertTrue(self.runner.redis_running)
        self.runner.redis_id = REDIS_ID
        result = self.recover()
        self.assertEqual(result["status"], "public_entry_recovered")
        self.assertEqual(sentinel.read_bytes(), b"preserve admitted turn counters")
        self.assertFalse(any("create" in argv for argv in self.runner.mutations))

    def test_pinned_redis_metadata_drift_blocks_bootstrap_recovery(self):
        transaction, sentinel = self.pending_bootstrap_with_quota_history()
        valid = self.runner.redis_metadata()
        for changed in (
            {"Image": OLD_IMAGE},
            {"Config": {"Labels": {"com.docker.compose.project": "wrong-project"}}},
            {"HostConfig": {"PortBindings": {"6379/tcp": [{"HostPort": "6379"}]}}},
            {"Mounts": [{**valid["Mounts"][0], "RW": True}, *valid["Mounts"][1:]]},
            {"NetworkSettings": {"Networks": {"unrelated-network": {}}}},
        ):
            with self.subTest(changed=changed):
                self.runner.redis_overrides = changed
                self.runner.mutations.clear()
                self.assert_bootstrap_recovery_refused(transaction, sentinel)
        self.runner.redis_overrides = {}
        self.assertEqual(self.recover()["status"], "public_entry_recovered")

    def test_legacy_or_invalid_bootstrap_quota_identity_receipts_are_preserved(self):
        transaction, sentinel = self.pending_bootstrap_with_quota_history()
        path = self.base / "transaction.json"
        for identity in ("missing", True, 123, "latest", "a" * 63, "A" * 64):
            with self.subTest(identity=identity):
                changed = {**transaction, "quota_container_id": identity}
                if identity == "missing":
                    del changed["quota_container_id"]
                raw = updates.private_io.canonical(changed)
                path.write_bytes(raw)
                self.runner.mutations.clear()
                self.assert_bootstrap_recovery_refused(changed, sentinel)
                self.assertEqual(path.read_bytes(), raw)

    def test_failed_durable_redis_identity_write_never_starts_entrance(self):
        write = updates._write

        def fail_identity(root, name, value):
            if (
                name == "transaction.json"
                and value.get("quota_container_id") == REDIS_ID
            ):
                raise OSError("fixture identity write failure")
            return write(root, name, value)

        with patch.object(updates, "_write", side_effect=fail_identity):
            result = self.bootstrap()
        self.assertEqual(result["status"], "recovery_required")
        self.assertTrue(self.runner.redis_created)
        self.assertFalse(self.runner.redis_running)
        self.assertFalse(self.runner.entry_created)
        self.assertIsNone(self.runner.web_id)
        transaction = json.loads((self.base / "transaction.json").read_text())
        self.assertIsNone(transaction["quota_container_id"])
        self.assertFalse((self.base / "active.json").exists())
        self.runner.mutations.clear()
        result = self.recover()
        self.assertEqual(result["status"], "public_entry_recovered")
        self.assertFalse(any("create" in argv for argv in self.runner.mutations))

    def test_published_redis_identity_survives_write_error_and_cannot_be_recreated(
        self,
    ):
        write = updates._write
        failed = False

        def publish_then_fail(root, name, value):
            nonlocal failed
            write(root, name, value)
            if (
                name == "transaction.json"
                and value.get("quota_container_id") == REDIS_ID
                and not failed
            ):
                failed = True
                raise OSError("fixture post-publication directory fsync failure")

        with patch.object(updates, "_write", side_effect=publish_then_fail):
            result = self.bootstrap()
        self.assertEqual(result["status"], "recovery_required")
        transaction = json.loads((self.base / "transaction.json").read_text())
        self.assertEqual(transaction["quota_container_id"], REDIS_ID)
        self.assertFalse(self.runner.redis_running)
        self.assertFalse(self.runner.entry_created)
        self.assertIsNone(self.runner.web_id)
        self.runner.redis_created = False
        self.runner.mutations.clear()
        self.assert_bootstrap_recovery_refused(transaction)
        self.runner.redis_created = True
        self.assertEqual(self.recover()["status"], "public_entry_recovered")
        self.assertFalse(any("create" in argv for argv in self.runner.mutations))

    def test_unlinked_transaction_fsync_error_restores_pin_and_requires_recovery(self):
        remove = updates._remove

        def unlink_then_fail(root, name):
            remove(root, name)
            if name == "transaction.json":
                raise OSError("fixture post-unlink directory fsync failure")

        with patch.object(updates, "_remove", side_effect=unlink_then_fail):
            result = self.bootstrap()
        self.assertEqual(result["status"], "recovery_required")
        self.assertTrue(result["entrance_stopped"])
        self.assertFalse(self.runner.entry_running)
        self.assertTrue(self.runner.redis_running)
        transaction = json.loads((self.base / "transaction.json").read_text())
        self.assertEqual(transaction["quota_container_id"], REDIS_ID)
        self.assertEqual(transaction["status"], "recovery_required")
        before = self.active()
        web_id = self.runner.web_id
        self.runner.mutations.clear()
        with self.assertRaisesRegex(
            updates.PublicEntryUpdateError, "^public_entry_pending_operation$"
        ):
            self.bootstrap()
        self.assertEqual(self.runner.mutations, [])
        self.assertEqual(self.active(), before)
        self.assertEqual(self.recover()["status"], "public_entry_recovered")
        self.assertEqual(self.active()["generation"], transaction["candidate"])
        self.assertEqual(self.active()["runtime_ids"]["quota-redis"], REDIS_ID)
        self.assertEqual(self.runner.web_id, web_id)
        self.assertFalse((self.base / "transaction.json").exists())
        self.assertFalse(any("create" in argv for argv in self.runner.mutations))

    def test_redis_drift_after_bootstrap_observation_cannot_publish_success(self):
        def drift(_image):
            sentinel = self.base / "redis-data" / "quota-sentinel.aof"
            sentinel.write_bytes(b"preserve admitted turn counters")
            self.runner.redis_id = "9" * 64

        self.runner.on_observe = drift
        result = self.bootstrap()
        self.assertEqual(result["status"], "recovery_required")
        self.assertFalse(self.runner.entry_running)
        self.assertFalse((self.base / "active.json").exists())
        transaction = json.loads((self.base / "transaction.json").read_text())
        self.assertEqual(transaction["quota_container_id"], REDIS_ID)
        self.assertEqual(
            (self.base / "redis-data" / "quota-sentinel.aof").read_bytes(),
            b"preserve admitted turn counters",
        )

    def test_redis_drift_after_recovery_observation_cannot_publish_success(self):
        transaction, sentinel = self.pending_bootstrap_with_quota_history()
        self.runner.on_observe = lambda _image: setattr(
            self.runner, "redis_id", "9" * 64
        )
        result = self.recover()
        self.assertEqual(result["status"], "recovery_required")
        self.assertFalse(self.runner.entry_running)
        self.assertFalse((self.base / "active.json").exists())
        self.assertEqual(
            json.loads((self.base / "transaction.json").read_text())[
                "quota_container_id"
            ],
            transaction["quota_container_id"],
        )
        self.assertEqual(sentinel.read_bytes(), b"preserve admitted turn counters")

    def test_changed_gateway_plugin_or_image_cannot_reset_counter_namespace(self):
        self.bootstrap()
        render = edge.render_public_entry

        def changed(config, **kwargs):
            result = render(config, **kwargs)
            route = next(
                item
                for item in result["apisix_routes"]["routes"]
                if item["id"] == "turn"
            )
            route["plugins"]["limit-count"]["_meta"]["error_response"] = {
                "error": {"code": "changed"}
            }
            result["assets"]["apisix.yaml"] = (
                json.dumps(result["apisix_routes"]) + "\n#END\n"
            )
            return result

        with (
            patch.object(edge, "render_public_entry", side_effect=changed),
            self.assertRaises(updates.PublicEntryUpdateError),
        ):
            self.update()
        self.assertEqual(self.runner.mutations, [])
        with (
            patch.object(edge, "APISIX_IMAGE", "sha256:" + "9" * 64),
            self.assertRaises(updates.PublicEntryUpdateError),
        ):
            self.update()
        self.assertEqual(self.runner.mutations, [])

    def test_profile_comparison_preserves_candidate_image_and_both_inputs(self):
        self.bootstrap()
        base = self.snapshot()["compose"]
        candidate = copy.deepcopy(base)
        candidate["services"]["public-web"]["image"] = NEW_IMAGE
        originals = copy.deepcopy((base, candidate))
        self.assertTrue(updates._same_profile_except_web_image(candidate, base))
        self.assertEqual((base, candidate), originals)

    def test_snapshot_web_image_mismatch_is_rejected_before_inspection(self):
        self.bootstrap()
        snapshot = self.snapshot()
        snapshot["web_image"] = NEW_IMAGE
        config = edge.load_public_entry_config(self.config_path)
        with (
            patch.object(self, "inspect") as inspector,
            self.assertRaisesRegex(
                updates.PublicEntryUpdateError, "^public_entry_profile_changed$"
            ),
        ):
            updates._verify(config, self.base, snapshot, inspector)
        inspector.assert_not_called()
        self.assertEqual(self.runner.mutations, [])

    def test_non_image_web_changes_are_rejected_before_mutation(self):
        self.bootstrap()
        before = self.active()
        bind = host.bind_public_web
        for field, value in (
            ("read_only", False),
            ("ports", ["18083:8000"]),
            ("networks", ["quota"]),
            ("environment", {"WEB_UPSTREAM": "http://other-backend:8000"}),
        ):
            with self.subTest(field=field):

                def changed(*args, **kwargs):
                    result = bind(*args, **kwargs)
                    result["compose"]["services"]["public-web"][field] = value
                    return result

                with (
                    patch.object(host, "bind_public_web", side_effect=changed),
                    self.assertRaisesRegex(
                        updates.PublicEntryUpdateError, "^public_entry_profile_changed$"
                    ),
                ):
                    self.update()
                self.assertEqual(self.runner.mutations, [])
                self.assertEqual(self.active(), before)
                self.assertFalse((self.base / "transaction.json").exists())

    def test_tampered_active_snapshot_refuses_update_without_overwrite(self):
        self.bootstrap()
        compose = Path(self.snapshot()["compose_file"])
        compose.write_text("operator content must remain")
        with self.assertRaises(ValueError):
            self.update()
        self.assertEqual(self.runner.mutations, [])
        self.assertEqual(compose.read_text(), "operator content must remain")

    def test_secret_drift_after_candidate_replacement_closes_entrance(self):
        self.bootstrap()
        before = self.active()

        def drift(image):
            if image == NEW_IMAGE:
                Path(self.raw["edge_secret_file"]).write_text("0" * 64 + "\n")

        self.runner.on_replace = drift
        self.update()
        self.assertFalse(self.runner.entry_running)
        self.assertTrue(self.runner.redis_running)
        self.assertEqual(self.active()["generation"], before["generation"])
        self.assertEqual(
            json.loads((self.base / "transaction.json").read_text())["status"],
            "recovery_required",
        )

    def test_one_operation_lock_blocks_concurrent_mutation(self):
        self.bootstrap()
        path = self.state / ".public-entry-operation.lock"
        with path.open("a+b") as stream:
            path.chmod(0o600)
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises((OSError, ValueError)):
                self.update()
        self.assertEqual(self.runner.mutations, [])


if __name__ == "__main__":
    unittest.main()
