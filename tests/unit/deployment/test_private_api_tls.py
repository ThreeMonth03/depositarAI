"""TLS identity and unchanged-backend admission for the private API endpoint."""

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.deployment import private_api_tls as endpoint
from tests.unit.deployment.public_entry.test_preflight import fixture

OUTER_NAME = "api.example.test"
INNER_NAME = "api-inner.example.test"


def certificates(root, *, outer_name=OUTER_NAME, inner_name=INNER_NAME):
    """Disposable, locally signed test identities; never use product material."""

    def run(*args):
        subprocess.run(
            ["openssl", *args], check=True, capture_output=True, timeout=15, umask=0o077
        )

    run(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(root / "ca.key"),
        "-out",
        str(root / "ca.pem"),
        "-days",
        "3",
        "-subj",
        "/CN=Disposable private API fixture CA",
    )
    for role, name in (("inner", inner_name), ("outer", outer_name)):
        run(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(root / (role + ".key")),
            "-out",
            str(root / (role + ".csr")),
            "-subj",
            "/CN=" + name,
        )
        ext = root / (role + ".ext")
        ext.write_text("subjectAltName=DNS:" + name + "\nextendedKeyUsage=serverAuth\n")
        run(
            "x509",
            "-req",
            "-in",
            str(root / (role + ".csr")),
            "-CA",
            str(root / "ca.pem"),
            "-CAkey",
            str(root / "ca.key"),
            "-CAcreateserial",
            "-out",
            str(root / (role + ".crt")),
            "-days",
            "3",
            "-extfile",
            str(ext),
        )
    for path in root.iterdir():
        if path.is_file():
            path.chmod(0o600)


class Runner:
    def __init__(self, network, rows):
        self.network, self.rows, self.calls = network, rows, []
        self.runtime = []
        self.owned = None

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] == "openssl":
            return subprocess.run(argv, **kwargs)
        if argv[:3] == ["docker", "network", "inspect"]:
            value = [self.network]
        elif argv[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(
                returncode=0, stdout=("sha256:" + "f" * 64 + "\n").encode(), stderr=b""
            )
        elif argv[:2] == ["docker", "inspect"]:
            value = [self.owned] if '"NetworkMode"' in argv[3] else self.rows
        elif argv[:2] == ["docker", "compose"] and "ps" in argv:
            value = self.runtime
        else:
            raise AssertionError("unexpected mutation")
        raw = (
            json.dumps(value).encode()
            if "ps" in argv
            else b"\n".join(json.dumps(row).encode() for row in value)
        )
        return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")


class PrivateApiTlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certdir = tempfile.TemporaryDirectory(prefix="private-api-cert-unit-")
        certificates(Path(cls.certdir.name))

    @classmethod
    def tearDownClass(cls):
        cls.certdir.cleanup()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="private-api-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("ca.pem", "outer.crt", "outer.key", "inner.crt", "inner.key"):
            p = self.root / name
            p.write_bytes((Path(self.certdir.name) / name).read_bytes())
            p.chmod(0o600)
        _, release, network, api = fixture()
        self.api = api
        api["Pid"], api["StartedAt"], api["SandboxID"] = (
            1234,
            "2026-10-19T00:00:00Z",
            "a" * 64,
        )
        api["Networks"][network["Name"]]["IPAddress"] = "172.30.0.4"
        key = self.root / "service-key"
        key.write_text("private-api-key-must-not-be-read")
        key.chmod(0o600)
        api["Mounts"][0]["Source"] = str(key)
        self.rows = [api]
        routes = self.root / "routes"
        routes.mkdir(mode=0o700)
        (routes / "active.conf").write_text("unchanged private gateway")
        (routes / "active.conf").chmod(0o600)
        for index, role in enumerate(("web", "filtered-embedding", "web-nginx"), 1):
            row = copy.deepcopy(api)
            row["Id"] = str(index) * 64
            row["Labels"]["com.docker.compose.service"] = role
            row["Networks"][network["Name"]]["Aliases"] = [role]
            if role == "web-nginx":
                row["Labels"]["com.docker.compose.project"] = (
                    endpoint.composition.GATEWAY_PROJECT
                )
                row["Mounts"] = [
                    {
                        "Type": "bind",
                        "Source": str(routes),
                        "Destination": "/etc/nginx/routes",
                        "RW": False,
                    }
                ]
            self.rows.append(row)
            network["Containers"][row["Id"]] = {"Name": role}
        release["services"]["conversation-api"]["volumes"] = [
            {
                "type": "bind",
                "source": str(key),
                "target": "/run/secrets/api_key",
                "read_only": True,
            }
        ]
        self.release = self.root / "release.json"
        self.release.write_text(json.dumps(release))
        self.release.chmod(0o600)
        self.config = {
            "schema_version": 1,
            "release_compose": str(self.release),
            "api_container_id": api["Id"],
            "nginx_image": "sha256:" + "f" * 64,
            "bind_address": "127.0.0.2",
            "listen_port": 18443,
            "allowed_source": "127.0.0.1/32",
            "outer_server_name": OUTER_NAME,
            "inner_server_name": INNER_NAME,
            "ca_file": str(self.root / "ca.pem"),
            "outer_certificate_file": str(self.root / "outer.crt"),
            "outer_key_file": str(self.root / "outer.key"),
            "inner_certificate_file": str(self.root / "inner.crt"),
            "inner_key_file": str(self.root / "inner.key"),
        }
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)
        self.runner = Runner(network, self.rows)
        self.output = self.root / "prepared"

    def prepare(self):
        return endpoint.prepare(self.config_path, self.output, runner=self.runner)

    def test_prepare_and_check_never_read_or_mount_service_key(self):
        original = endpoint._read

        def guarded(path):
            self.assertNotEqual(Path(path).name, "service-key")
            return original(path)

        with patch.object(endpoint, "_read", side_effect=guarded):
            self.prepare()
            endpoint.check(self.output / "plan.json", runner=self.runner)
        raw = (self.output / "plan.json").read_text()
        self.assertNotIn("private-api-key-must-not-be-read", raw)
        compose = json.loads((self.output / "compose.json").read_text())
        self.assertNotIn("networks", compose)
        self.assertNotIn("ports", compose["services"]["outer"])
        self.assertEqual(
            compose["services"]["inner"]["network_mode"], "container:" + self.api["Id"]
        )
        self.assertNotIn("ca.key", json.dumps(compose))
        self.assertNotIn("service-key", json.dumps(compose))
        outer = compose["services"]["outer"]
        inner = compose["services"]["inner"]
        self.assertEqual(outer["user"], "0:0")
        self.assertEqual(
            set(outer["cap_add"]), {"SETUID", "SETGID", "DAC_OVERRIDE", "CHOWN"}
        )
        self.assertEqual(inner["user"], f"{os.geteuid()}:{os.getegid()}")
        self.assertNotIn("cap_add", inner)
        for service in (outer, inner):
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertTrue(service["read_only"])
            self.assertEqual(service["security_opt"], ["no-new-privileges:true"])

    def test_tls_configs_have_exact_methods_verified_inner_tls_and_no_retry(self):
        self.prepare()
        outer = (self.output / "outer.conf").read_text()
        inner = (self.output / "inner.conf").read_text()
        self.assertIn("listen 172.30.0.4:8443 ssl", inner)
        self.assertIn("proxy_ssl_verify on", outer)
        self.assertIn("proxy_ssl_name " + self.config["inner_server_name"], outer)
        self.assertIn("allow 127.0.0.1/32; deny all", outer)
        for text in (outer, inner):
            self.assertIn("$request_method != GET", text)
            self.assertIn("$request_method != POST", text)
            self.assertIn("$request_uri !=", text)
            self.assertIn("proxy_next_upstream off", text)
            self.assertNotIn("Authorization", text)

    def test_deployment_names_bind_certificate_verification_proxy_and_plan(self):
        self.config.update(
            outer_server_name="tenant-api.example.test",
            inner_server_name="tenant-inner.example.test",
        )
        certificates(
            self.root,
            outer_name=self.config["outer_server_name"],
            inner_name=self.config["inner_server_name"],
        )
        self.config_path.write_text(json.dumps(self.config))
        self.prepare()
        plan = endpoint.check(self.output / "plan.json", runner=self.runner)
        outer = (self.output / "outer.conf").read_text()
        inner = (self.output / "inner.conf").read_text()
        for role, text in (("outer", outer), ("inner", inner)):
            name = self.config[role + "_server_name"]
            self.assertEqual(plan["config"][role + "_server_name"], name)
            self.assertIn("server_name " + name + ";", text)
            self.assertIn("if ($host != " + name + ")", text)
            self.assertTrue(
                any(
                    "-verify_hostname" in call
                    and call[call.index("-verify_hostname") + 1] == name
                    for call in self.runner.calls
                )
            )
        self.assertIn("proxy_ssl_name tenant-inner.example.test;", outer)
        self.assertIn("proxy_set_header Host tenant-inner.example.test;", outer)
        plan["config"]["inner_server_name"] = "different-inner.example.test"
        (self.output / "plan.json").write_text(json.dumps(plan))
        with self.assertRaises(ValueError):
            endpoint.check(self.output / "plan.json", runner=self.runner)

    def test_cn_fallback_and_wildcard_san_do_not_replace_explicit_identity(self):
        source = Path(self.certdir.name)
        for san in (None, "*.example.test"):
            with self.subTest(san=san):
                extension = self.root / "replacement.ext"
                extension.write_text(
                    "extendedKeyUsage=serverAuth\n"
                    + ("subjectAltName=DNS:" + san + "\n" if san else "")
                )
                subprocess.run(
                    [
                        "openssl",
                        "x509",
                        "-req",
                        "-in",
                        str(source / "outer.csr"),
                        "-CA",
                        str(source / "ca.pem"),
                        "-CAkey",
                        str(source / "ca.key"),
                        "-CAserial",
                        str(source / "ca.srl"),
                        "-out",
                        str(self.root / "outer.crt"),
                        "-days",
                        "3",
                        "-extfile",
                        str(extension),
                    ],
                    capture_output=True,
                    check=True,
                    timeout=10,
                )
                with self.assertRaises(endpoint.EndpointError):
                    self.prepare()
                self.assertFalse(self.output.exists())
        self.assertFalse(any(call[0] == "docker" for call in self.runner.calls))

    def test_server_names_are_required_distinct_bounded_dns_names(self):
        for field in ("outer_server_name", "inner_server_name"):
            missing = dict(self.config)
            del missing[field]
            with self.subTest(field=field, missing=True), self.assertRaises(ValueError):
                endpoint.validate_config(missing)
            for bad in (
                None,
                123,
                "",
                "localhost",
                "API.example.test",
                "api.example.test.",
                ".api.example.test",
                "api..example.test",
                "api_example.test",
                "-api.example.test",
                "api-.example.test",
                "*.example.test",
                "127.0.0.1",
                "::1",
                "https://api.example.test",
                "api.example.test:443",
                "api.example.test;return 200",
                "api.example.test\nserver {}",
                "api.example.test\x00",
                "api.\u4f8b\u5b50.test",
                "a" * 64 + ".example.test",
                ".".join(["a" * 63] * 4),
                self.config[
                    "inner_server_name"
                    if field == "outer_server_name"
                    else "outer_server_name"
                ],
            ):
                with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                    endpoint.validate_config({**self.config, field: bad})
        longest = ".".join(["a" * 63] * 3 + ["b" * 61])
        self.assertEqual(len(longest), 253)
        self.assertEqual(
            endpoint.validate_config({**self.config, "outer_server_name": longest})[
                "outer_server_name"
            ],
            longest,
        )

    def test_backend_restart_changed_id_or_ip_and_extra_peer_refuse(self):
        self.prepare()
        for field, bad in (
            ("Pid", 999),
            ("StartedAt", "later"),
            ("SandboxID", "b" * 64),
        ):
            previous = self.api[field]
            self.api[field] = bad
            with self.subTest(field=field), self.assertRaises(endpoint.EndpointError):
                endpoint.check(self.output / "plan.json", runner=self.runner)
            self.api[field] = previous
        network_name = self.runner.network["Name"]
        self.api["Networks"][network_name]["IPAddress"] = "172.30.0.5"
        with self.assertRaises(endpoint.EndpointError):
            endpoint.check(self.output / "plan.json", runner=self.runner)
        self.api["Networks"][network_name]["IPAddress"] = "172.30.0.4"
        self.runner.network["Containers"]["9" * 64] = {"Name": "foreign"}
        with self.assertRaises(ValueError):
            endpoint.check(self.output / "plan.json", runner=self.runner)

    def test_leaf_key_mismatch_fails_before_writing(self):
        (self.root / "inner.key").write_bytes((self.root / "outer.key").read_bytes())
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_expired_leaf_fails_before_writing(self):
        source = Path(self.certdir.name)
        subprocess.run(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(source / "inner.csr"),
                "-CA",
                str(source / "ca.pem"),
                "-CAkey",
                str(source / "ca.key"),
                "-CAserial",
                str(source / "ca.srl"),
                "-out",
                str(self.root / "inner.crt"),
                "-days",
                "0",
                "-extfile",
                str(source / "inner.ext"),
            ],
            capture_output=True,
            check=True,
            timeout=10,
        )
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_material_and_active_route_changes_refuse(self):
        self.prepare()
        route = self.root / "routes" / "active.conf"
        route.write_text("changed")
        with self.assertRaises(endpoint.EndpointError):
            endpoint.check(self.output / "plan.json", runner=self.runner)
        self.assertFalse(any("up" in call for call in self.runner.calls))

    def test_unknown_existing_runtime_is_not_replaced_or_stopped(self):
        self.prepare()
        self.runner.runtime = [{"Service": "outer", "ID": "9" * 64}]
        self.runner.owned = {
            "Id": "9" * 64,
            "Image": "sha256:" + "f" * 64,
            "Labels": {},
            "Mounts": [],
            "NetworkMode": "host",
            "Running": True,
            "Healthy": "healthy",
        }
        for action in ("up", "stop"):
            with self.subTest(action=action), self.assertRaises(endpoint.EndpointError):
                endpoint.operate(action, self.output / "plan.json", runner=self.runner)
        self.assertFalse(
            any(
                any(x in call for x in ("up", "stop", "rm"))
                for call in self.runner.calls
            )
        )

    def test_stop_does_not_require_live_backend_or_current_certificate(self):
        self.prepare()
        self.runner.network["Containers"].clear()
        (self.root / "outer.crt").unlink()
        result = endpoint.operate("stop", self.output / "plan.json", runner=self.runner)
        self.assertEqual(result["status"], "private_api_tls_stopped")

    def test_invalid_config_ip_image_path_and_source_refuse(self):
        for field, bad in (
            ("bind_address", "0.0.0.0"),
            ("bind_address", "127.0.0.1;return 200"),
            ("allowed_source", "0.0.0.0/0"),
            ("nginx_image", "nginx:latest"),
            ("outer_key_file", "relative"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                endpoint.validate_config({**self.config, field: bad})


if __name__ == "__main__":
    unittest.main()
