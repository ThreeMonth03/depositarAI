"""Rendered policies for the standalone CPU frontend."""

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.validate_search_web import validate

ROOT = Path(__file__).resolve().parents[1]


class WebsiteDeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        remote_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("WEB_", "SEARCH_API_", "COMPOSE_"))
        }
        cls.remote = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    "deploy/env/.env.search-web.example",
                    "-f",
                    "deploy/compose/compose.search-web.yaml",
                    "config",
                    "--format",
                    "json",
                ],
                cwd=ROOT,
                env=remote_env,
            )
        )

    def test_remote_wrapper_uses_its_dedicated_env_file(self):
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/search-web.sh"), "config"],
            cwd=ROOT,
            env=dict(
                os.environ,
                SEARCH_WEB_ENV_FILE=str(ROOT / "deploy/env/.env.search-web.example"),
                WEB_UPSTREAM="http://unapproved.example.org",
                WEB_UPSTREAM_CA_FILE="/unapproved/caller-ca.pem",
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("deployment policy passed", result.stdout)

    def test_remote_frontend_has_no_api_model_or_catalog_inputs(self):
        self.assertEqual({"web", "web-nginx"}, set(self.remote["services"]))
        web = self.remote["services"]["web"]
        self.assertEqual(
            {
                "WEB_ORIGIN",
                "WEB_UPSTREAM",
                "WEB_UPSTREAM_CA_FILE",
                "NVIDIA_VISIBLE_DEVICES",
            },
            set(web["environment"]),
        )
        self.assertEqual("/run/secrets/api_key", web["volumes"][0]["target"])
        self.assertEqual("", web["environment"]["WEB_UPSTREAM_CA_FILE"])
        validate(self.remote)

    def test_private_ca_override_renders_fixed_read_only_mount(self):
        value = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    "deploy/env/.env.search-web.example",
                    "-f",
                    "deploy/compose/compose.search-web.yaml",
                    "-f",
                    "deploy/compose/compose.search-web.ca.yaml",
                    "config",
                    "--format",
                    "json",
                ],
                cwd=ROOT,
                env={
                    **{
                        key: value
                        for key, value in os.environ.items()
                        if not key.startswith(("WEB_", "COMPOSE_"))
                    },
                    "WEB_UPSTREAM_CA_FILE": "/private/certificates/upstream ca.pem",
                },
            )
        )
        web = value["services"]["web"]
        self.assertEqual(
            "/run/certs/upstream-ca.pem", web["environment"]["WEB_UPSTREAM_CA_FILE"]
        )
        ca = next(
            mount
            for mount in web["volumes"]
            if mount["target"] == "/run/certs/upstream-ca.pem"
        )
        self.assertEqual("/private/certificates/upstream ca.pem", ca["source"])
        self.assertTrue(ca["read_only"])
        self.assertFalse(ca["bind"]["create_host_path"])
        validate(value)

    def test_private_ca_requires_exact_env_mount_pair_and_absolute_source(self):
        for mutation in (
            "missing-mount",
            "missing-env",
            "wrong-env",
            "wrong-target",
            "relative-source",
            "writable",
            "auto-create",
            "duplicate",
        ):
            value = copy.deepcopy(self.remote)
            web = value["services"]["web"]
            web["environment"]["WEB_UPSTREAM_CA_FILE"] = "/run/certs/upstream-ca.pem"
            mount = {
                "type": "bind",
                "source": "/private/ca.pem",
                "target": "/run/certs/upstream-ca.pem",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
            web["volumes"].append(mount)
            if mutation == "missing-mount":
                web["volumes"].pop()
            elif mutation == "missing-env":
                web["environment"]["WEB_UPSTREAM_CA_FILE"] = ""
            elif mutation == "wrong-env":
                web["environment"]["WEB_UPSTREAM_CA_FILE"] = "/arbitrary/ca.pem"
            elif mutation == "wrong-target":
                mount["target"] = "/arbitrary/ca.pem"
            elif mutation == "relative-source":
                mount["source"] = "ca.pem"
            elif mutation == "writable":
                mount["read_only"] = False
            elif mutation == "auto-create":
                mount["bind"]["create_host_path"] = True
            else:
                web["volumes"].append(copy.deepcopy(mount))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate(value)

    def test_wrapper_selects_private_ca_from_its_env_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "web.env"
            path.write_text(
                (ROOT / "deploy/env/.env.search-web.example").read_text()
                + "\nWEB_UID=1003\nWEB_GID=1003\nWEB_UPSTREAM_CA_FILE=/private/approved-ca.pem\n"
            )
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/search-web.sh"), "config"],
                cwd=ROOT,
                env=dict(
                    os.environ,
                    SEARCH_WEB_ENV_FILE=str(path),
                    WEB_UPSTREAM_CA_FILE="/unapproved/caller-ca.pem",
                ),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("deployment policy passed", result.stdout)

    def test_wrapper_rejects_relative_ca_before_compose_normalizes_its_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "web.env"
            path.write_text(
                (ROOT / "deploy/env/.env.search-web.example").read_text()
                + "\nWEB_UPSTREAM_CA_FILE=relative-ca.pem\n"
            )
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/search-web.sh"), "config"],
                cwd=ROOT,
                env=dict(os.environ, SEARCH_WEB_ENV_FILE=str(path)),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(0, result.returncode)
        self.assertIn(
            "Private upstream CA must be an absolute file path.", result.stderr
        )

    def test_remote_requires_bare_https_upstream(self):
        for upstream in (
            "http://conversation-api:8000",
            "http://private-api.example.org",
            "https://user:secret@private-api.example.org",
            "https://private-api.example.org/v1/turn",
            "https://private-api.example.org?key=secret",
            "https://private-api.example.org#fragment",
            "https://private-api.example.org:99999",
            "https://private-api.example.org\n",
        ):
            value = copy.deepcopy(self.remote)
            value["services"]["web"]["environment"]["WEB_UPSTREAM"] = upstream
            with self.subTest(upstream=upstream), self.assertRaises(ValueError):
                validate(value)

    def test_remote_rejects_extra_mounts_networks_and_backend_ports(self):
        for mutation in ("mount", "network", "port", "root-group", "privileges"):
            value = copy.deepcopy(self.remote)
            web = value["services"]["web"]
            if mutation == "mount":
                web["volumes"].append(copy.deepcopy(web["volumes"][0]))
            elif mutation == "network":
                value["networks"]["catalog"] = {"external": True}
            elif mutation == "port":
                web["ports"] = [{"host_ip": "127.0.0.1", "published": "8000"}]
            elif mutation == "root-group":
                web["user"] = "1000:0"
            else:
                web["security_opt"] = []
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate(value)

    def test_remote_wrapper_rejects_http_before_build_or_start(self):
        value = copy.deepcopy(self.remote)
        value["services"]["web"]["environment"]["WEB_UPSTREAM"] = (
            "http://conversation-api:8000"
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "config.json").write_text(json.dumps(value))
            (directory / "git").write_text("#!/bin/sh\nprintf '%040d\\n' 1\n")
            (directory / "docker").write_text(
                "#!/usr/bin/env python3\n"
                "import json,os,pathlib,sys\n"
                "root=pathlib.Path(os.environ['FAKE_WEB_ROOT'])\n"
                "with (root/'calls.jsonl').open('a') as output:\n"
                " output.write(json.dumps(sys.argv[1:])+'\\n')\n"
                "if 'config' in sys.argv:\n"
                " print((root/'config.json').read_text())\n"
            )
            for name in ("git", "docker"):
                (directory / name).chmod(0o700)
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/search-web.sh"), "up"],
                cwd=ROOT,
                env=dict(
                    os.environ,
                    PATH=str(directory) + os.pathsep + os.environ["PATH"],
                    FAKE_WEB_ROOT=str(directory),
                ),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)
            calls = [
                json.loads(line)
                for line in (directory / "calls.jsonl").read_text().splitlines()
            ]
            self.assertEqual(2, len(calls))
            self.assertTrue(all("config" in call for call in calls))

    def test_production_wrapper_rejects_old_mode_arguments(self):
        for mode in ("local", "filtered", "remote"):
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/search-web.sh"), "up", mode],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            with self.subTest(mode=mode):
                self.assertEqual(2, result.returncode)

    def test_public_or_other_port_rejected(self):
        for address, port in (
            ("0.0.0.0", "18081"),
            ("127.0.0.1", "18082"),
            ("127.0.0.1", "443"),
        ):
            value = copy.deepcopy(self.remote)
            value["services"]["web-nginx"]["ports"][0].update(
                host_ip=address, published=port
            )
            with self.assertRaises(ValueError):
                validate(value)


if __name__ == "__main__":
    unittest.main()
