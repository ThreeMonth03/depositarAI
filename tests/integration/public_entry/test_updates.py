"""Real CPU controller replacements preserve the admission plane and quotas.

The support fixture owns HTTPS/OIDC/probe services. A separate managed Compose
project owns exactly the three production entrance roles. Only disposable test
networks, images and directories are used; no host ports or GPUs are involved.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch
import uuid

from search.operations.deployment.public_entry import profile as edge
from search.operations.deployment.public_entry import host
from search.operations.deployment.public_entry import updates
from tests.integration.public_entry.test_runtime import (
    ISSUER,
    ORIGIN,
    PublicEntryRuntimeFixture,
)


class PublicEntryUpdatesRuntimeFixture(PublicEntryRuntimeFixture):
    requires_candidate_images = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        root = Path(cls.temporary.name)
        cls.managed_root = root / "managed-state"
        cls.managed_root.mkdir(mode=0o700)
        cls.managed_project = "depositar-entry-updates-test-" + uuid.uuid4().hex[:12]
        cls.managed_compose = cls.managed_root / "public-entry/compose.json"
        cls.support_id = cls.compose("ps", "--quiet", "fixture").stdout.strip()
        cls.support_before = cls.inspect(cls.support_id)
        initial = json.loads(cls.compose_file.read_text())

        # Remove only the base fixture's disposable entrance. Its TLS/OIDC/probe
        # containers remain running and will resolve the managed entrance's
        # service alias on their existing private outbound network.
        cls.compose(
            "stop", "--timeout", "2", "public-entry", "public-web", "quota-redis"
        )
        cls.compose("rm", "--force", "public-entry", "public-web", "quota-redis")

        selected = cls.networks()
        release = (
            "depositar-ai-release-"
            + uuid.uuid4().hex[:12]
            + "-"
            + uuid.uuid4().hex[:12]
        )
        cls.release_network = release + "_website"
        outbound = cls.project + "_outbound"
        # The original support container now serves OIDC only on this network.
        # Its independent private counter cannot shadow the genuine release's
        # conversation-api alias used by the website and probe.
        cls.command(["docker", "network", "disconnect", outbound, cls.support_id])
        cls.command(
            [
                "docker",
                "network",
                "connect",
                "--alias",
                "oidc.test",
                outbound,
                cls.support_id,
            ]
        )
        backend = copy.deepcopy(initial["services"]["fixture"])
        backend["networks"] = {
            "website": {"aliases": ["conversation-api"]},
            "outbound": {"aliases": ["conversation-api"]},
        }
        backend["volumes"].append(
            {
                "type": "bind",
                "source": str(root / "api-key"),
                "target": "/run/secrets/api_key",
                "read_only": True,
            }
        )
        backend["healthcheck"] = {
            "test": [
                "CMD",
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/__calls', timeout=2).read()",
            ],
            "interval": "1s",
            "timeout": "3s",
            "retries": 10,
        }
        cls.release_compose = root / "private-release-compose.json"
        cls.release_compose.write_text(
            json.dumps(
                {
                    "name": release,
                    "services": {"conversation-api": backend},
                    "networks": {
                        "website": {
                            "name": cls.release_network,
                            **selected["outbound"],
                        },
                        "outbound": {"external": True, "name": outbound},
                    },
                }
            )
        )
        cls.release_compose.chmod(0o600)
        cls.addClassCleanup(cls.remove_private_release)
        cls.private_compose(
            "up",
            "--detach",
            "--pull",
            "never",
            "--no-build",
            "--wait",
            "--wait-timeout",
            "20",
        )
        cls.fixture_id = cls.private_compose(
            "ps", "--quiet", "conversation-api"
        ).stdout.strip()
        cls.private_before = cls.inspect(cls.fixture_id)
        cls.controller_mutations = []
        cls.bootstrap_hook = None
        cls.before_create_hook = None

        original_render = edge.render_public_entry
        original_bind = host.bind_public_web

        def isolated_render(config, **kwargs):
            rendered = original_render(config, **kwargs)
            compose = rendered["compose"]
            compose["services"]["public-entry"].pop("ports", None)
            for role in ("quota-redis", "public-entry"):
                compose["services"][role]["healthcheck"].update(
                    start_period="20s", start_interval="1s"
                )
            compose["networks"]["website"] = copy.deepcopy(selected["website"])
            compose["networks"]["quota"] = copy.deepcopy(selected["quota"])
            compose["networks"]["outbound"] = {
                "external": True,
                "name": cls.project + "_outbound",
            }
            return rendered

        def isolated_bind(*args, **kwargs):
            bound = original_bind(*args, **kwargs)
            # Probe disposable BFF startup promptly without changing steady-state
            # health checks, their commands, or controller readiness deadlines.
            bound["compose"]["services"]["public-web"]["healthcheck"].update(
                start_period="20s", start_interval="1s"
            )
            return bound

        cls.project_patch = patch.object(edge, "PROJECT", cls.managed_project)
        cls.render_patch = patch.object(
            edge, "render_public_entry", side_effect=isolated_render
        )
        cls.bind_patch = patch.object(
            host, "bind_public_web", side_effect=isolated_bind
        )
        cls.project_patch.start()
        cls.render_patch.start()
        cls.bind_patch.start()
        cls.addClassCleanup(cls.project_patch.stop)
        cls.addClassCleanup(cls.render_patch.stop)
        cls.addClassCleanup(cls.bind_patch.stop)

        cls.config_path = root / "managed-entry.json"
        cls.config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "state_root": str(cls.managed_root),
                    "public_origin": ORIGIN,
                    "oidc_issuer": ISSUER,
                    "oidc_discovery": ISSUER + "/.well-known/openid-configuration",
                    "oidc_client_id": "public-entry-test",
                    "oidc_client_secret_file": str(root / "client-secret"),
                    "oidc_session_secret_file": str(root / "session-secret"),
                    "edge_secret_file": str(root / "edge-key"),
                    "redis_password_file": str(root / "redis-password"),
                    "oidc_ca_file": str(root / "tls.crt"),
                    "quota_per_account": 2,
                    "quota_global": 4,
                    "quota_window_seconds": 3600,
                }
            )
        )
        cls.config_path.chmod(0o600)
        cls.release_path = root / "managed-release.json"
        cls.release_path.write_text(
            json.dumps(
                {
                    "name": release,
                    "services": {
                        "web": {
                            "environment": {
                                "WEB_UPSTREAM": "http://conversation-api:8000"
                            },
                            "volumes": [
                                {
                                    "type": "bind",
                                    "source": str(root / "api-key"),
                                    "target": "/run/secrets/api_key",
                                    "read_only": True,
                                }
                            ],
                        },
                        "conversation-api": {"image": backend["image"]},
                    },
                    "networks": {"website": {"name": cls.release_network}},
                }
            )
        )
        cls.release_path.chmod(0o600)

        cls.image_tags = []
        cls.addClassCleanup(cls.remove_images)
        cls.addClassCleanup(cls.remove_managed_project)
        base = "depositar-entry-update-base:" + uuid.uuid4().hex
        cls.command(
            ["docker", "image", "tag", initial["services"]["public-web"]["image"], base]
        )
        cls.image_tags.append(base)
        cls.old_image = cls.derive_image(root, base, "old")
        cls.new_image = cls.broken_image = None
        if cls.requires_candidate_images:
            cls.new_image = cls.derive_image(root, base, "new")
            cls.broken_image = cls.derive_image(root, base, "broken")
            if len({cls.old_image, cls.new_image, cls.broken_image}) != 3:
                raise AssertionError(
                    "controller fixture requires distinct immutable images"
                )

    @classmethod
    def inspect(cls, identifier):
        row = json.loads(cls.command(["docker", "inspect", identifier]).stdout)[0]
        return {
            "id": row["Id"],
            "image": row["Image"],
            "started_at": row["State"]["StartedAt"],
            "restart_count": row["RestartCount"],
        }

    @classmethod
    def derive_image(cls, root, base, version):
        directory = root / ("image-" + version)
        directory.mkdir(mode=0o700)
        revision = hashlib.sha1((cls.managed_project + version).encode()).hexdigest()
        dockerfile = (
            "FROM "
            + base
            + '\nLABEL org.opencontainers.image.revision="'
            + revision
            + '"\n'
        )
        if version == "broken":
            dockerfile += 'CMD ["python", "-c", "raise SystemExit(1)"]\n'
        (directory / "Dockerfile").write_text(dockerfile)
        tag = "depositar-entry-update-" + version + ":" + uuid.uuid4().hex
        cls.image_tags.append(tag)
        cls.command(
            [
                "docker",
                "build",
                "--pull=false",
                "--network=none",
                "--tag",
                tag,
                "--iidfile",
                str(directory / "image-id"),
                str(directory),
            ],
            timeout=60,
        )
        image = (directory / "image-id").read_text().strip()
        # Both source and candidate IDs are captured before any controller call.
        if (
            cls.command(
                ["docker", "image", "inspect", tag, "--format", "{{.Id}}"]
            ).stdout.strip()
            != image
        ):
            raise AssertionError("derived fixture image identity changed")
        return image

    @classmethod
    def remove_images(cls):
        for tag in reversed(cls.image_tags):
            removed = cls.command(["docker", "image", "rm", tag], check=False)
            if removed.returncode:
                exists = cls.command(["docker", "image", "inspect", tag], check=False)
                if exists.returncode == 0:
                    raise AssertionError("disposable controller image cleanup failed")

    @classmethod
    def private_compose(cls, *arguments):
        return cls.command(
            ["docker", "compose", "-f", str(cls.release_compose), *arguments],
            timeout=30,
        )

    @classmethod
    def remove_private_release(cls):
        cls.private_compose("down", "--timeout", "2", "--remove-orphans")

    @classmethod
    def remove_managed_project(cls):
        if cls.managed_compose.exists():
            cls.command(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(cls.managed_compose),
                    "down",
                    "--timeout",
                    "2",
                    "--remove-orphans",
                ],
                timeout=30,
            )

    @classmethod
    def controller_runner(cls, argv, **kwargs):
        # Keep production health commands and readiness checks intact. The
        # intentionally exiting candidate does not need a sixty-second wait.
        argv = list(argv)
        if (
            argv[:2] == ["docker", "compose"]
            and "create" in argv
            and cls.before_create_hook is not None
        ):
            hook, cls.before_create_hook = cls.before_create_hook, None
            hook(argv)
        if argv[:2] == ["docker", "compose"] and any(
            operation in argv
            for operation in ("create", "up", "stop", "start", "down", "rm")
        ):
            cls.controller_mutations.append(argv)
        if "--wait-timeout" in argv and "--no-deps" in argv:
            profile = json.loads(Path(argv[argv.index("-f") + 1]).read_text())
            if profile["services"]["public-web"]["image"] == cls.broken_image:
                argv[argv.index("--wait-timeout") + 1] = "5"
        result = subprocess.run(argv, **kwargs)
        if (
            result.returncode == 0
            and argv[:2] == ["docker", "compose"]
            and "up" in argv
            and argv[-1] == "public-entry"
            and cls.bootstrap_hook is not None
        ):
            hook, cls.bootstrap_hook = cls.bootstrap_hook, None
            hook(argv)
        return result

    def active(self):
        return json.loads((self.managed_root / "public-entry/active.json").read_text())

    def assert_web_healthy(self, container_id):
        row = json.loads(self.command(["docker", "inspect", container_id]).stdout)[0]
        healthcheck = row["Config"]["Healthcheck"]
        self.assertEqual(healthcheck["StartInterval"], 1_000_000_000)
        self.assertEqual(healthcheck["StartPeriod"], 20_000_000_000)
        self.assertEqual(row["State"]["Health"]["Status"], "healthy")

    def assert_admission_healthy(self, runtime_ids):
        for role, retries in (("quota-redis", 6), ("public-entry", 12)):
            row = json.loads(
                self.command(["docker", "inspect", runtime_ids[role]]).stdout
            )[0]
            healthcheck = row["Config"]["Healthcheck"]
            self.assertEqual(healthcheck["StartInterval"], 1_000_000_000)
            self.assertEqual(healthcheck["StartPeriod"], 20_000_000_000)
            self.assertEqual(healthcheck["Interval"], 5_000_000_000)
            self.assertEqual(healthcheck["Timeout"], 3_000_000_000)
            self.assertEqual(healthcheck["Retries"], retries)
            self.assertEqual(row["State"]["Health"]["Status"], "healthy")

    def assert_plane_unchanged(self, original):
        state = self.active()
        self.assert_web_healthy(state["runtime_ids"]["public-web"])
        for role in ("public-entry", "quota-redis"):
            self.assertEqual(state["runtime_ids"][role], original["runtime_ids"][role])
        self.assertEqual(self.inspect(self.fixture_id), self.private_before)
        self.assertEqual(self.inspect(self.support_id), self.support_before)
        self.assertEqual(
            self.inspect(state["runtime_ids"]["quota-redis"]), self.redis_before
        )
        self.assertFalse((self.managed_root / "public-entry/transaction.json").exists())
        self.assertEqual(self.probe("access", "alice")["status"], 200)
        for account, version in (("alice", 2), ("carol", 0)):
            rejected = self.probe(
                "turn", account, {"query": "氣候", "version": version}
            )
            self.assertEqual(rejected["status"], 429)
            self.assertEqual(rejected["value"], {"error": {"code": "quota_exceeded"}})
        self.assertEqual(self.calls(), 4)
        return state

    def assert_bootstrap_denied(self, code):
        with self.assertRaisesRegex(ValueError, "^" + code + "$"):
            updates.bootstrap_public_entry(
                self.config_path,
                self.release_path,
                self.old_image,
                runner=self.controller_runner,
            )
        self.assertEqual(self.controller_mutations, [])
        self.assertFalse((self.managed_root / "public-entry/active.json").exists())
        self.assertFalse((self.managed_root / "public-entry/transaction.json").exists())
        self.assertEqual(
            self.command(
                [
                    "docker",
                    "ps",
                    "--all",
                    "--quiet",
                    "--filter",
                    "label=com.docker.compose.project=" + self.managed_project,
                ]
            ).stdout.strip(),
            "",
        )

    def wait_private_healthy(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = json.loads(
                self.command(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{json .State}}",
                        self.fixture_id,
                    ]
                ).stdout
            )
            if (
                state.get("Running")
                and not state.get("Paused")
                and state.get("Health", {}).get("Status") == "healthy"
            ):
                return
            time.sleep(0.2)
        self.fail("disposable private backend did not become healthy after unpause")

    def interrupt_started_bootstrap(self, argv, *, verify_exhaustion=False):
        # The real Compose operation has succeeded, but the controller has not
        # observed/committed it. Model a process interruption at that boundary.
        output = self.command(
            [
                "docker",
                "compose",
                "-f",
                argv[argv.index("-f") + 1],
                "ps",
                "--all",
                "--no-trunc",
                "--format",
                "json",
            ]
        ).stdout
        rows = (
            json.loads(output)
            if output.lstrip().startswith("[")
            else [json.loads(line) for line in output.splitlines() if line.strip()]
        )
        self.bootstrap_ids = {row["Service"]: row["ID"] for row in rows}
        self.assertEqual(
            set(self.bootstrap_ids), {"public-entry", "public-web", "quota-redis"}
        )
        self.assert_web_healthy(self.bootstrap_ids["public-web"])
        self.assert_admission_healthy(self.bootstrap_ids)
        self.redis_before = self.inspect(self.bootstrap_ids["quota-redis"])
        for account in ("alice", "bob", "carol"):
            self.assertEqual(self.probe("login", account)["status"], 200)
            self.assertEqual(self.probe("session", account)["status"], 200)
        for account in ("alice", "bob"):
            for version in (0, 1):
                self.assertEqual(
                    self.probe("turn", account, {"query": "氣候", "version": version})[
                        "status"
                    ],
                    200,
                )
        self.assertEqual(self.calls(), 4)
        if verify_exhaustion:
            for account, version in (("alice", 2), ("carol", 0)):
                rejected = self.probe(
                    "turn", account, {"query": "氣候", "version": version}
                )
                self.assertEqual(rejected["status"], 429)
                self.assertEqual(
                    rejected["value"], {"error": {"code": "quota_exceeded"}}
                )
            self.assertEqual(self.calls(), 4)
        raise SystemExit("disposable bootstrap interrupted before controller commit")


class PublicEntryUpdatesRuntimeTests(PublicEntryUpdatesRuntimeFixture):
    def test_controller_update_rollback_and_failed_candidate_preserve_quotas(self):
        # A stopped endpoint disappears from Docker's network membership. Both
        # that absent backend and a paused, still-attached backend must reject
        # startup before any managed Compose mutation.
        self.private_compose("stop", "--timeout", "2", "conversation-api")
        self.assert_bootstrap_denied("public_entry_backend_network_invalid")
        self.private_compose(
            "start", "--wait", "--wait-timeout", "20", "conversation-api"
        )
        self.private_before = self.inspect(self.fixture_id)
        self.private_compose("pause", "conversation-api")
        try:
            self.assert_bootstrap_denied("public_entry_backend_unhealthy")
        finally:
            self.private_compose("unpause", "conversation-api")
        self.wait_private_healthy()
        self.__class__.bootstrap_hook = self.interrupt_started_bootstrap
        with self.assertRaisesRegex(
            SystemExit, "bootstrap interrupted before controller commit"
        ):
            updates.bootstrap_public_entry(
                self.config_path,
                self.release_path,
                self.old_image,
                runner=self.controller_runner,
            )
        self.assertIsNone(self.__class__.bootstrap_hook)
        self.assertFalse((self.managed_root / "public-entry/active.json").exists())
        pending = json.loads(
            (self.managed_root / "public-entry/transaction.json").read_text()
        )
        self.assertEqual(pending["action"], "bootstrap")
        self.assertIsNone(pending["previous"])

        # Recovery must recheck the persisted private backend identity rather
        # than reopen a healthy-looking BFF while its backend is unavailable.
        self.private_compose("pause", "conversation-api")
        try:
            blocked = updates.recover_public_entry(
                self.config_path, runner=self.controller_runner
            )
            self.assertEqual(blocked["status"], "recovery_required", blocked)
            self.assertTrue(blocked["entrance_stopped"])
            state = json.loads(
                self.command(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{json .State}}",
                        self.bootstrap_ids["public-entry"],
                    ]
                ).stdout
            )
            self.assertFalse(state["Running"])
            self.assertEqual(
                self.inspect(self.bootstrap_ids["quota-redis"]), self.redis_before
            )
            self.assertFalse((self.managed_root / "public-entry/active.json").exists())
            self.assertTrue(
                (self.managed_root / "public-entry/transaction.json").is_file()
            )
        finally:
            self.private_compose("unpause", "conversation-api")
        self.wait_private_healthy()
        self.assertEqual(self.calls(), 4)
        recovered = updates.recover_public_entry(
            self.config_path, runner=self.controller_runner
        )
        self.assertEqual(recovered["status"], "public_entry_recovered", recovered)
        initial = self.active()
        self.assertEqual(initial["runtime_ids"], self.bootstrap_ids)
        self.assert_plane_unchanged(initial)

        updated = updates.update_public_entry(
            self.config_path,
            self.release_path,
            self.new_image,
            runner=self.controller_runner,
        )
        self.assertEqual(updated["status"], "public_entry_updated", updated)
        current = self.assert_plane_unchanged(initial)
        self.assertEqual(current["web_image"], self.new_image)
        self.assertNotEqual(
            current["runtime_ids"]["public-web"], initial["runtime_ids"]["public-web"]
        )
        self.assertEqual(current["previous_generation"], initial["generation"])

        rolled_back = updates.rollback_public_entry(
            self.config_path, runner=self.controller_runner
        )
        self.assertEqual(rolled_back["status"], "public_entry_rolled_back", rolled_back)
        previous = self.assert_plane_unchanged(initial)
        self.assertEqual(previous["web_image"], self.old_image)
        self.assertEqual(previous["generation"], initial["generation"])
        self.assertNotEqual(
            previous["runtime_ids"]["public-web"], current["runtime_ids"]["public-web"]
        )

        failed = updates.update_public_entry(
            self.config_path,
            self.release_path,
            self.broken_image,
            runner=self.controller_runner,
        )
        self.assertEqual(failed["status"], "update_failed_rolled_back", failed)
        restored = self.assert_plane_unchanged(initial)
        self.assertEqual(restored["web_image"], self.old_image)
        self.assertEqual(restored["generation"], initial["generation"])
        self.assertNotEqual(
            restored["runtime_ids"]["public-web"], previous["runtime_ids"]["public-web"]
        )


class PublicEntryBootstrapRecoveryRuntimeTests(PublicEntryUpdatesRuntimeFixture):
    requires_candidate_images = False

    def transaction(self):
        return json.loads(
            (self.managed_root / "public-entry/transaction.json").read_text()
        )

    def managed_quota_ids(self):
        return self.command(
            [
                "docker",
                "ps",
                "--all",
                "--no-trunc",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=" + self.managed_project,
                "--filter",
                "label=com.docker.compose.service=quota-redis",
            ]
        ).stdout.split()

    def quota_data_bytes(self):
        data = self.managed_root / "public-entry/redis-data"
        self.assertTrue(data.is_dir())
        self.assertFalse(data.is_symlink())
        self.assertEqual(data.resolve(), data)
        files = {}
        for current, directories, names in os.walk(data, followlinks=False):
            for name in directories:
                self.assertFalse((Path(current) / name).is_symlink())
            for name in names:
                path = Path(current) / name
                self.assertFalse(path.is_symlink())
                self.assertTrue(path.is_file())
                files[str(path.relative_to(data))] = path.read_bytes()
        return files

    def test_bootstrap_crashes_preserve_redis_identity_and_aof(self):
        def interrupt_before_create(argv):
            self.assertEqual(argv[-1], "quota-redis")
            self.assertIn("--no-recreate", argv)
            raise SystemExit("disposable bootstrap interrupted before Redis create")

        self.__class__.before_create_hook = interrupt_before_create
        with self.assertRaisesRegex(SystemExit, "before Redis create"):
            updates.bootstrap_public_entry(
                self.config_path,
                self.release_path,
                self.old_image,
                runner=self.controller_runner,
            )
        self.assertIsNone(self.__class__.before_create_hook)
        self.assertIsNone(self.transaction()["quota_container_id"])
        self.assertEqual(self.controller_mutations, [])
        self.assertEqual(self.managed_quota_ids(), [])
        self.assertEqual(
            self.command(
                [
                    "docker",
                    "ps",
                    "--all",
                    "--quiet",
                    "--filter",
                    "label=com.docker.compose.project=" + self.managed_project,
                ]
            ).stdout.strip(),
            "",
        )
        recovered = updates.recover_public_entry(
            self.config_path, runner=self.controller_runner
        )
        self.assertEqual(recovered["status"], "public_entry_recovered", recovered)
        self.assertEqual(
            self.managed_quota_ids(), [self.active()["runtime_ids"]["quota-redis"]]
        )
        self.assert_admission_healthy(self.active()["runtime_ids"])
        self.assertFalse((self.managed_root / "public-entry/transaction.json").exists())
        self.assertEqual(self.probe("access")["status"], 401)
        self.assertEqual(self.calls(), 0)
        for argv in self.controller_mutations:
            if "create" in argv:
                self.assertEqual(argv[-1], "quota-redis")
            if "up" in argv:
                self.assertIn("--no-deps", argv)
                self.assertNotIn("quota-redis", argv)

        # The first independent trial never admitted a turn. Preserve its
        # state, remove only its disposable project, and use a fresh project
        # for the distinct crash after quota admission scenario.
        self.remove_managed_project()
        cls = type(self)
        root = Path(self.temporary.name)
        previous_config = json.loads(self.config_path.read_text())
        cls.managed_root = root / "served-bootstrap-state"
        cls.managed_root.mkdir(mode=0o700)
        cls.managed_project = "depositar-entry-redis-test-" + uuid.uuid4().hex[:12]
        cls.managed_compose = cls.managed_root / "public-entry/compose.json"
        cls.config_path = root / "served-entry.json"
        cls.config_path.write_text(
            json.dumps({**previous_config, "state_root": str(cls.managed_root)})
        )
        cls.config_path.chmod(0o600)
        project = patch.object(edge, "PROJECT", cls.managed_project)
        project.start()
        self.addCleanup(project.stop)
        self.controller_mutations.clear()
        cls.bootstrap_hook = lambda argv: self.interrupt_started_bootstrap(
            argv, verify_exhaustion=True
        )
        with self.assertRaisesRegex(SystemExit, "before controller commit"):
            updates.bootstrap_public_entry(
                self.config_path,
                self.release_path,
                self.old_image,
                runner=self.controller_runner,
            )
        self.assertIsNone(cls.bootstrap_hook)
        pending = self.transaction()
        pinned = pending["quota_container_id"]
        self.assertEqual(pinned, self.bootstrap_ids["quota-redis"])
        self.assertEqual(self.managed_quota_ids(), [pinned])
        self.assertFalse((self.managed_root / "public-entry/active.json").exists())

        redis = json.loads(self.command(["docker", "inspect", pinned]).stdout)[0]
        labels = redis["Config"]["Labels"]
        self.assertEqual(labels["com.docker.compose.project"], self.managed_project)
        self.assertEqual(labels["com.docker.compose.service"], "quota-redis")
        data = self.managed_root / "public-entry/redis-data"
        self.assertEqual(
            [
                mount["Source"]
                for mount in redis["Mounts"]
                if mount["Destination"] == "/data"
            ],
            [str(data)],
        )
        self.command(["docker", "stop", "--time", "2", pinned])
        stopped = json.loads(self.command(["docker", "inspect", pinned]).stdout)[0]
        self.assertFalse(stopped["State"]["Running"])
        before = self.quota_data_bytes()
        self.assertTrue(
            any(name.endswith(".aof") and raw for name, raw in before.items())
        )
        self.command(["docker", "rm", pinned])
        self.assertEqual(self.managed_quota_ids(), [])
        self.controller_mutations.clear()

        rejected = updates.recover_public_entry(
            self.config_path, runner=self.controller_runner
        )
        self.assertEqual(rejected["status"], "recovery_required", rejected)
        self.assertTrue(rejected["entrance_stopped"])
        self.assertEqual(self.transaction()["quota_container_id"], pinned)
        self.assertEqual(self.transaction()["candidate"], pending["candidate"])
        self.assertEqual(self.managed_quota_ids(), [])
        self.assertFalse((self.managed_root / "public-entry/active.json").exists())
        self.assertTrue(
            before == self.quota_data_bytes(), "owned Redis AOF bytes changed"
        )
        for argv in self.controller_mutations:
            self.assertFalse(
                any(operation in argv for operation in ("create", "up", "start"))
            )
        state = json.loads(
            self.command(
                ["docker", "inspect", self.bootstrap_ids["public-entry"]]
            ).stdout
        )[0]["State"]
        self.assertFalse(state["Running"])
        self.assertEqual(self.calls(), 4)
        self.assertEqual(self.inspect(self.fixture_id), self.private_before)
        self.assertEqual(self.inspect(self.support_id), self.support_before)


if __name__ == "__main__":
    unittest.main()
