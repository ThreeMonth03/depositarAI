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

    def test_update_closes_admission_on_failed_or_interrupted_health_check(self):
        for outcome, exit_code in (("success", 0), ("failed", 42), ("terminated", 143)):
            with (
                self.subTest(outcome=outcome),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                (directory / "config.json").write_text(json.dumps(self.remote))
                (directory / "git").write_text(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = rev-parse ]; then printf '%040d\\n' 1; fi\n"
                )
                (directory / "docker").write_text(
                    "#!/usr/bin/env python3\n"
                    "import json,os,pathlib,signal,sys\n"
                    "root=pathlib.Path(os.environ['FAKE_WEB_ROOT'])\n"
                    "with (root/'calls.jsonl').open('a') as output:\n"
                    " output.write(json.dumps(sys.argv[1:])+'\\n')\n"
                    "if 'config' in sys.argv:\n"
                    " print((root/'config.json').read_text())\n"
                    "if 'build' in sys.argv: sys.stdin.buffer.read()\n"
                    "if 'up' in sys.argv:\n"
                    " outcome=os.environ['FAKE_DEPLOY_OUTCOME']\n"
                    " if outcome=='failed': sys.exit(42)\n"
                    " if outcome=='terminated': os.kill(os.getppid(),signal.SIGTERM)\n"
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
                        FAKE_DEPLOY_OUTCOME=outcome,
                    ),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(exit_code, result.returncode, result.stderr)
                calls = [
                    json.loads(line)
                    for line in (directory / "calls.jsonl").read_text().splitlines()
                ]
                operations = [
                    "stop" if "stop" in call else "up"
                    for call in calls
                    if "stop" in call or "up" in call
                ]
                expected = ["stop", "up"]
                if exit_code:
                    expected.append("stop")
                self.assertEqual(expected, operations)

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


class IntranetDeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("WEB_", "SEARCH_API_", "COMPOSE_"))
        }
        cls.rendered = json.loads(
            subprocess.check_output(
                [
                    "docker",
                    "compose",
                    "--project-name",
                    "depositar-search-intranet",
                    "--env-file",
                    "deploy/env/.env.search-web-intranet.example",
                    "-f",
                    "deploy/compose/compose.search-web.yaml",
                    "-f",
                    "deploy/compose/compose.search-web.intranet.yaml",
                    "config",
                    "--format",
                    "json",
                ],
                cwd=ROOT,
                env=environment,
            )
        )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.access_file = Path(temporary.name) / "intranet-allow.conf"
        self.config = copy.deepcopy(self.rendered)
        self.config["services"]["web-nginx"]["volumes"][0]["source"] = str(
            self.access_file
        )

    def test_rendered_entry_is_opt_in_and_separate_from_private_demo(self):
        with self.assertRaises(ValueError):
            validate(self.config)
        self.assertFalse(self.access_file.exists())
        validate(self.config, intranet=True, prepare=True)
        validate(self.config, intranet=True)
        self.assertEqual("depositar-search-intranet", self.config["name"])
        web = self.config["services"]["web"]
        nginx = self.config["services"]["web-nginx"]
        self.assertEqual("intranet-http", web["environment"]["WEB_ACCESS_MODE"])
        self.assertEqual("10.20.30.40", nginx["ports"][0]["host_ip"])
        self.assertEqual("18082", nginx["ports"][0]["published"])
        self.assertEqual("10.20.30.0/24", nginx["environment"]["WEB_ALLOWED_CIDRS"])
        self.assertEqual(
            "# Direct TCP peers only. Loopback is for container health checks.\n"
            "allow 127.0.0.1;\nallow ::1;\nallow 10.20.30.0/24;\ndeny all;\n",
            self.access_file.read_text(),
        )
        self.assertEqual(0o444, self.access_file.stat().st_mode & 0o777)

    def test_entry_rejects_origin_bind_mount_and_runtime_drift_before_writing(self):
        for mutation in (
            "project",
            "missing-mode",
            "public-origin",
            "bind-ip",
            "bind-port",
            "udp",
            "extra-port",
            "backend-port",
            "extra-network",
            "missing-mount",
            "extra-mount",
            "mount-target",
            "mount-relative",
            "mount-name",
            "mount-writable",
            "mount-auto-create",
        ):
            value = copy.deepcopy(self.config)
            web = value["services"]["web"]
            nginx = value["services"]["web-nginx"]
            mount = nginx["volumes"][0]
            if mutation == "project":
                value["name"] = "depositar-search-web"
            elif mutation == "missing-mode":
                web["environment"].pop("WEB_ACCESS_MODE")
            elif mutation == "public-origin":
                web["environment"]["WEB_ORIGIN"] = "http://8.8.8.8:18082"
            elif mutation == "bind-ip":
                nginx["ports"][0]["host_ip"] = "0.0.0.0"
            elif mutation == "bind-port":
                nginx["ports"][0]["published"] = "18081"
            elif mutation == "udp":
                nginx["ports"][0]["protocol"] = "udp"
            elif mutation == "extra-port":
                nginx["ports"].append(copy.deepcopy(nginx["ports"][0]))
            elif mutation == "backend-port":
                web["ports"] = [{"target": 8000, "published": "18083"}]
            elif mutation == "extra-network":
                value["networks"]["other-service"] = {"external": True}
            elif mutation == "missing-mount":
                nginx["volumes"] = []
            elif mutation == "extra-mount":
                nginx["volumes"].append(copy.deepcopy(mount))
            elif mutation == "mount-target":
                mount["target"] = "/etc/nginx/nginx.conf"
            elif mutation == "mount-relative":
                mount["source"] = "intranet-allow.conf"
            elif mutation == "mount-name":
                mount["source"] = str(self.access_file.parent / "other.conf")
            elif mutation == "mount-writable":
                mount["read_only"] = False
            elif mutation == "mount-auto-create":
                mount["bind"]["create_host_path"] = True
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate(value, intranet=True, prepare=True)
            self.assertFalse(self.access_file.exists(), mutation)

    def test_intranet_requires_explicit_canonical_private_client_cidrs(self):
        for cidrs in (
            None,
            "",
            "0.0.0.0/0",
            "140.109.21.56/32",
            "127.0.0.1/32",
            "169.254.0.0/16",
            "10.20.30.40/24",
            "10.20.30.0/24, 192.168.56.0/24",
            "10.20.30.0/24,10.20.30.0/24",
            "10.20.30.0/24;allow all",
            "10.20.30.0/24\ndeny all",
        ):
            value = copy.deepcopy(self.config)
            value["services"]["web-nginx"]["environment"]["WEB_ALLOWED_CIDRS"] = cidrs
            with self.subTest(cidrs=cidrs), self.assertRaises(ValueError):
                validate(value, intranet=True, prepare=True)
            self.assertFalse(self.access_file.exists())

    def test_verification_rejects_missing_tampered_and_symlink_admission_file(self):
        with self.assertRaises(OSError):
            validate(self.config, intranet=True)
        validate(self.config, intranet=True, prepare=True)
        self.access_file.chmod(0o600)
        self.access_file.write_text("allow all;\n")
        with self.assertRaises(ValueError):
            validate(self.config, intranet=True)
        target = self.access_file.parent / "other-service.conf"
        target.write_text("leave this service alone\n")
        self.access_file.unlink()
        self.access_file.symlink_to(target)
        for prepare in (False, True):
            with self.subTest(prepare=prepare), self.assertRaises(ValueError):
                validate(self.config, intranet=True, prepare=prepare)
        self.assertEqual("leave this service alone\n", target.read_text())

    def test_cli_prepares_only_after_complete_policy_and_sanitizes_failure(self):
        command = [
            "python3",
            str(ROOT / "scripts/validate_search_web.py"),
            "--intranet",
            "--prepare-access",
        ]
        invalid = copy.deepcopy(self.config)
        invalid["services"]["web"]["volumes"][0]["source"] = "/secret/api-key"
        invalid["services"]["web"]["ports"] = [{"target": 8000}]
        failed = subprocess.run(
            command,
            input=json.dumps(invalid),
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, failed.returncode)
        self.assertEqual("Intranet website deployment policy failed.\n", failed.stderr)
        self.assertNotIn("/secret/api-key", failed.stdout + failed.stderr)
        self.assertFalse(self.access_file.exists())
        passed = subprocess.run(
            command,
            input=json.dumps(self.config),
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, passed.returncode, passed.stderr)
        self.assertTrue(self.access_file.exists())
        with self.assertRaises(ValueError):
            validate(self.config, prepare=True)

    def test_wrapper_uses_intranet_env_without_mutating_private_configuration(self):
        path = self.access_file.parent / "web.env"
        path.write_text(
            (ROOT / "deploy/env/.env.search-web-intranet.example").read_text()
            + f"\nWEB_ACCESS_FILE={self.access_file}\n"
        )
        private_source = (ROOT / "deploy/compose/compose.search-web.yaml").read_bytes()
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/search-web.sh"), "config"],
            cwd=ROOT,
            env=dict(
                os.environ,
                SEARCH_WEB_MODE="intranet-http",
                SEARCH_WEB_ENV_FILE=str(path),
                WEB_BIND_IP="0.0.0.0",
                WEB_ALLOWED_CIDRS="0.0.0.0/0",
                WEB_ACCESS_FILE="/unapproved/intranet-allow.conf",
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Intranet website deployment policy passed", result.stdout)
        self.assertIn("allow 10.20.30.0/24;", self.access_file.read_text())
        self.assertNotIn("0.0.0.0/0", self.access_file.read_text())
        self.assertEqual(
            private_source,
            (ROOT / "deploy/compose/compose.search-web.yaml").read_bytes(),
        )

    def wrapper_with_fake_docker(self, command, *, mode=None):
        directory = self.access_file.parent
        calls_file = directory / "calls.jsonl"
        calls_file.unlink(missing_ok=True)
        (directory / "config.json").write_text(json.dumps(self.config))
        docker = directory / "docker"
        docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json,os,pathlib,sys\n"
            "root=pathlib.Path(os.environ['FAKE_WEB_ROOT'])\n"
            "with (root/'calls.jsonl').open('a') as output:\n"
            " output.write(json.dumps(sys.argv[1:])+'\\n')\n"
            "if 'config' in sys.argv:\n"
            " print((root/'config.json').read_text())\n"
        )
        docker.chmod(0o700)
        environment = dict(
            os.environ,
            PATH=str(directory) + os.pathsep + os.environ["PATH"],
            FAKE_WEB_ROOT=str(directory),
            SEARCH_WEB_ENV_FILE=str(directory / "intranet.env"),
        )
        environment.pop("SEARCH_WEB_MODE", None)
        if mode is not None:
            environment["SEARCH_WEB_MODE"] = mode
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/search-web.sh"), command],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
        return result, calls

    def test_missing_intranet_mode_rejects_down_and_status_before_runtime_commands(
        self,
    ):
        for command in ("down", "status"):
            with self.subTest(command=command):
                result, calls = self.wrapper_with_fake_docker(command)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(
                    "Website origin does not match SEARCH_WEB_MODE.", result.stderr
                )
                self.assertEqual(1, len(calls))
                self.assertIn("config", calls[0])
                self.assertNotIn("down", calls[0])
                self.assertNotIn("ps", calls[0])

    def test_correct_intranet_down_does_not_require_intact_admission_file(self):
        for admission in ("missing", "tampered"):
            with self.subTest(admission=admission):
                if admission == "tampered":
                    self.access_file.write_text("allow all;\n")
                result, calls = self.wrapper_with_fake_docker(
                    "down", mode="intranet-http"
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(2, len(calls))
                self.assertIn("config", calls[0])
                self.assertEqual("down", calls[1][-1])
                for call in calls:
                    index = call.index("--project-name")
                    self.assertEqual("depositar-search-intranet", call[index + 1])
                if admission == "missing":
                    self.assertFalse(self.access_file.exists())
                else:
                    self.assertEqual("allow all;\n", self.access_file.read_text())


if __name__ == "__main__":
    unittest.main()
