"""Real Compose pair updates preserve OIDC admission and durable quotas.

The support fixture owns HTTPS/OIDC/probe services. A managed Compose project
owns the production admission, Redis, Nginx frontend and Django BFF roles. Failed
candidates leave admission stopped until an explicit operator rollback. Only
disposable networks, images and directories are used; no host ports or GPUs.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
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
            "stop",
            "--timeout",
            "2",
            "public-entry",
            "public-frontend",
            "public-web",
            "quota-redis",
        )
        cls.compose(
            "rm",
            "--force",
            "public-entry",
            "public-frontend",
            "public-web",
            "quota-redis",
        )

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
        backend_labels = json.loads(
            cls.command(
                [
                    "docker",
                    "image",
                    "inspect",
                    backend["image"],
                    "--format",
                    "{{json .Config.Labels}}",
                ]
            ).stdout
        )
        if (backend_labels or {}).get(host.CONVERSATION_CAPABILITY) != "3":
            raise AssertionError(
                "rebuild the CPU API fixture image for wire contract 3"
            )
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
        cls.compose_mutations = []

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
            # health checks, their commands, or deployment readiness deadlines.
            for role in ("public-web", "public-frontend"):
                bound["compose"]["services"][role]["healthcheck"].update(
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
                    "quota_window_seconds": cls.quota_window_seconds,
                    "session_window_seconds": cls.quota_window_seconds,
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
        bases = {}
        for role in ("public-web", "public-frontend"):
            base = "depositar-entry-update-base-" + role + ":" + uuid.uuid4().hex
            cls.command(
                ["docker", "image", "tag", initial["services"][role]["image"], base]
            )
            cls.image_tags.append(base)
            bases[role] = base
        cls.old_pair = cls.derive_pair(root, bases, "old")
        cls.new_pair = cls.derive_pair(root, bases, "new")
        cls.broken_pair = cls.derive_pair(root, bases, "broken")
        if len(set(cls.old_pair + cls.new_pair + cls.broken_pair)) != 6:
            raise AssertionError("pair fixture requires six distinct immutable images")

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
    def derive_pair(cls, root, bases, version):
        revision = hashlib.sha1((cls.managed_project + version).encode()).hexdigest()
        images = []
        for role, base in bases.items():
            directory = root / ("image-" + version + "-" + role)
            directory.mkdir(mode=0o700)
            dockerfile = (
                "FROM "
                + base
                + "\n"
                + 'LABEL org.opencontainers.image.revision="'
                + revision
                + '"\n'
                + "ENV SOURCE_REVISION="
                + revision
                + "\n"
            )
            if version == "broken" and role == "public-frontend":
                dockerfile += 'ENTRYPOINT ["/bin/sh", "-c", "exit 1"]\nCMD []\n'
            (directory / "Dockerfile").write_text(dockerfile)
            tag = "depositar-entry-" + version + "-" + role + ":" + uuid.uuid4().hex
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
            actual = cls.command(
                ["docker", "image", "inspect", tag, "--format", "{{.Id}}"]
            ).stdout.strip()
            if actual != image:
                raise AssertionError("derived fixture image identity changed")
            images.append(image)
        return tuple(images)

    @classmethod
    def remove_images(cls):
        for tag in reversed(cls.image_tags):
            removed = cls.command(["docker", "image", "rm", tag], check=False)
            if removed.returncode:
                exists = cls.command(["docker", "image", "inspect", tag], check=False)
                if exists.returncode == 0:
                    raise AssertionError("disposable pair image cleanup failed")

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
    def compose_runner(cls, argv, **kwargs):
        argv = list(argv)
        if argv[:2] == ["docker", "compose"]:
            if any(
                operation in argv for operation in ("up", "stop", "start", "down", "rm")
            ):
                cls.compose_mutations.append(argv)
            if "--wait-timeout" in argv and "--no-deps" in argv:
                profile = json.loads(Path(argv[argv.index("-f") + 1]).read_text())
                if (
                    profile["services"]["public-frontend"]["image"]
                    == cls.broken_pair[1]
                ):
                    # Keep real health checks; only shorten the intentional crash wait.
                    argv[argv.index("--wait-timeout") + 1] = "5"
        return subprocess.run(argv, **kwargs)

    @classmethod
    def smoke(cls, config):
        # Probe the actual managed entrance inside its private network. No host
        # port, alternate static server or no-op acceptance hook is required.
        script = (
            "import http.client,json\n"
            "connection=http.client.HTTPConnection('public-entry',9080,timeout=5)\n"
            "for path in ('/healthz','/'):\n"
            " connection.request('GET',path,headers={'Host':"
            + repr(config.public_origin.split("://", 1)[1])
            + "})\n"
            " response=connection.getresponse(); body=response.read(262145)\n"
            " assert response.status==200 and len(body)<=262144\n"
            " if path=='/healthz': assert json.loads(body)=={'status':'ok'}\n"
            " else: assert b'<div id=\"root\"></div>' in body\n"
            "connection.close()\n"
        )
        cls.compose("exec", "--no-TTY", "probe", "python", "-c", script)

    def active(self):
        return json.loads((self.managed_root / "public-entry/active.json").read_text())

    def runtime(self):
        output = self.command(
            [
                "docker",
                "compose",
                "-f",
                str(self.managed_compose),
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
        self.assertEqual(
            {row["Service"] for row in rows},
            {"public-entry", "quota-redis", "public-web", "public-frontend"},
        )
        return {row["Service"]: self.inspect(row["ID"]) for row in rows}

    def assert_unchanged_dependencies(self, original):
        current = self.runtime()
        self.assertEqual(current["quota-redis"], original["quota-redis"])
        self.assertEqual(current["public-entry"]["id"], original["public-entry"]["id"])
        self.assertEqual(
            current["public-entry"]["image"], original["public-entry"]["image"]
        )
        self.assertEqual(self.inspect(self.fixture_id), self.private_before)
        self.assertEqual(self.inspect(self.support_id), self.support_before)
        return current

    def assert_pair(self, images):
        state, current = self.active(), self.runtime()
        for role, field, image in (
            ("public-web", "web_image", images[0]),
            ("public-frontend", "web_nginx_image", images[1]),
        ):
            self.assertEqual(state[field], image)
            self.assertEqual(current[role]["image"], image)
            row = json.loads(
                self.command(["docker", "inspect", current[role]["id"]]).stdout
            )[0]
            self.assertEqual(row["State"]["Health"]["Status"], "healthy")
            self.assertEqual(
                row["Config"]["Labels"]["org.opencontainers.image.revision"],
                state["revision"],
            )
        self.assertFalse((self.managed_root / "public-entry/pending.json").exists())
        self.assertEqual(self.probe("access")["status"], 401)
        self.assertEqual(self.probe("access", "alice")["status"], 200)
        return state, current

    def assert_quotas_retained(self):
        for account in ("alice", "carol"):
            rejected = self.probe("turn", account, {"query": "氣候", "version": 0})
            self.assertEqual(rejected["status"], 429)
            self.assertEqual(rejected["value"], {"error": {"code": "quota_exceeded"}})
        self.assertEqual(self.calls(), 4)


class PublicEntryUpdatesRuntimeTests(PublicEntryUpdatesRuntimeFixture):
    def test_pair_update_manual_rollback_and_failed_frontend_preserve_quotas(self):
        started = updates.bootstrap_public_entry(
            self.config_path,
            self.release_path,
            *self.old_pair,
            runner=self.compose_runner,
            smoke_probe=self.smoke,
        )
        self.assertEqual(started["status"], "public_entry_started", started)
        for account in ("alice", "bob", "carol"):
            self.assertEqual(self.probe("login", account)["status"], 200)
            self.assertEqual(self.probe("session", account)["status"], 200)
        for account in ("alice", "bob"):
            for version in range(2):
                self.assertEqual(
                    self.probe("turn", account, {"query": "氣候", "version": version})[
                        "status"
                    ],
                    200,
                )
        self.assert_quotas_retained()
        original_state, original_runtime = self.assert_pair(self.old_pair)

        # A frontend from a different revision is rejected before touching the
        # healthy entrance or accepting a half-updated website.
        self.__class__.compose_mutations.clear()
        with self.assertRaisesRegex(ValueError, "public_entry_frontend_image_invalid"):
            updates.update_public_entry(
                self.config_path,
                self.release_path,
                self.new_pair[0],
                self.old_pair[1],
                runner=self.compose_runner,
                smoke_probe=self.smoke,
            )
        self.assertEqual(self.compose_mutations, [])
        self.assertEqual(self.active(), original_state)
        self.assert_unchanged_dependencies(original_runtime)

        updated = updates.update_public_entry(
            self.config_path,
            self.release_path,
            *self.new_pair,
            runner=self.compose_runner,
            smoke_probe=self.smoke,
        )
        self.assertEqual(updated["status"], "public_entry_updated", updated)
        current_state, current_runtime = self.assert_pair(self.new_pair)
        self.assertEqual(
            current_state["previous_generation"], original_state["generation"]
        )
        for role in ("public-web", "public-frontend"):
            self.assertNotEqual(
                current_runtime[role]["id"], original_runtime[role]["id"]
            )
        self.assert_unchanged_dependencies(original_runtime)
        self.assert_quotas_retained()

        rolled_back = updates.rollback_public_entry(
            self.config_path, runner=self.compose_runner, smoke_probe=self.smoke
        )
        self.assertEqual(rolled_back["status"], "public_entry_rolled_back", rolled_back)
        previous_state, previous_runtime = self.assert_pair(self.old_pair)
        self.assertEqual(previous_state["generation"], original_state["generation"])
        for role in ("public-web", "public-frontend"):
            self.assertNotEqual(
                previous_runtime[role]["id"], current_runtime[role]["id"]
            )
        self.assert_unchanged_dependencies(original_runtime)
        self.assert_quotas_retained()

        failed = updates.update_public_entry(
            self.config_path,
            self.release_path,
            *self.broken_pair,
            runner=self.compose_runner,
            smoke_probe=self.smoke,
        )
        self.assertEqual(failed["status"], "deployment_failed", failed)
        self.assertTrue(failed["entrance_stop_requested"])
        self.assertTrue(failed["manual_rollback_required"])
        self.assertEqual(self.active(), previous_state)
        self.assertTrue((self.managed_root / "public-entry/pending.json").is_file())
        entrance_id = self.runtime()["public-entry"]["id"]
        entrance = json.loads(self.command(["docker", "inspect", entrance_id]).stdout)[
            0
        ]
        self.assertFalse(entrance["State"]["Running"])
        self.assertIn(self.probe("access", "alice")["status"], (502, 503, 504))
        self.assert_unchanged_dependencies(original_runtime)
        self.assertEqual(self.calls(), 4)

        restored = updates.rollback_public_entry(
            self.config_path, runner=self.compose_runner, smoke_probe=self.smoke
        )
        self.assertEqual(restored["status"], "public_entry_rolled_back", restored)
        self.assert_pair(self.old_pair)
        self.assert_unchanged_dependencies(original_runtime)
        self.assert_quotas_retained()


if __name__ == "__main__":
    unittest.main()
