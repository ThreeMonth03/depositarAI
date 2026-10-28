"""Disposable fake API: exact netns, TLS identity and IP-reuse credential safety."""

import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from search.operations.deployment import private_api_tls as endpoint
from tests.integration.public_entry.test_runtime import PublicEntryRuntimeFixture
from tests.unit.deployment.test_private_api_tls import (
    INNER_NAME,
    OUTER_NAME,
    certificates,
)


FAKE_API = """from http.server import BaseHTTPRequestHandler, HTTPServer
import json
class Handler(BaseHTTPRequestHandler):
 calls = 0
 def log_message(self,*args): pass
 def do_GET(self):
  if self.path in ('/healthz','/__calls'): self.reply(200); return
  self.handle_request(False)
 def do_POST(self): self.handle_request(True)
 def reply(self, code):
  self.send_response(code); self.send_header('Content-Type','application/json'); self.end_headers()
  self.wfile.write(json.dumps({'calls':Handler.calls}).encode())
 def handle_request(self,turn):
  if self.headers.get('Authorization') != 'Bearer fixture-private-api-key': self.reply(401); return
  self.rfile.read(int(self.headers.get('Content-Length','0')))
  if turn: Handler.calls += 1
  self.reply(200)
HTTPServer(('0.0.0.0',8000),Handler).serve_forever()
"""


@unittest.skipUnless(
    os.environ.get("DEPOSITAR_TEST_PRIVATE_API_TLS_RUNTIME") == "1",
    "explicit isolated Docker opt-in required",
)
class PrivateApiTlsRuntimeTests(unittest.TestCase):
    isolated = False

    def endpoint_runner(self, argv, **kwargs):
        kwargs["check"] = False
        result = subprocess.run(argv, **kwargs)
        if result.returncode:
            diagnostic = result.stderr.decode(errors="replace")[-2000:]
            if "up" in argv:
                logs = self.run_command(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(self.output / "compose.json"),
                        "logs",
                        "--tail",
                        "12",
                        "inner",
                        "outer",
                    ],
                    check=False,
                )
                diagnostic += logs.stdout[-3000:]
            raise AssertionError("disposable endpoint command failed: " + diagnostic)
        return result

    def run_command(self, argv, *, check=True, timeout=40):
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
        if check and result.returncode:
            raise AssertionError(
                "isolated command failed: "
                + " ".join(argv[:4])
                + "\n"
                + result.stderr[-2000:]
            )
        return result

    def inspect(self, identifier):
        return json.loads(
            self.run_command(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{json .NetworkSettings}}",
                    identifier,
                ]
            ).stdout
        )

    def start(
        self, role, *, extra=(), command=None, name=None, project=None, network=None
    ):
        name = name or self.project + "-" + role
        labels = [
            "--label",
            "com.docker.compose.project=" + (project or self.project),
            "--label",
            "com.docker.compose.service=" + role,
        ]
        if role == "conversation-api":
            labels += ["--label", "io.depositar.ai.conversation.version=4"]
        argv = [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--runtime",
            "runc",
            "--user",
            f"{os.geteuid()}:{os.getegid()}",
            "--network",
            network or self.network,
            "--memory",
            "64m",
            "--cpus",
            "0.25",
            "--pids-limit",
            "32",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,noexec,size=8m",
            "--env",
            "NVIDIA_VISIBLE_DEVICES=void",
            "--health-cmd",
            "python -c 'pass'",
            "--health-interval",
            "1s",
            "--health-timeout",
            "1s",
            "--health-retries",
            "10",
            *labels,
            *extra,
            "--entrypoint",
            "python",
            self.python_image,
            *(command or ["-c", "import time;time.sleep(3600)"]),
        ]
        identifier = self.run_command(argv).stdout.strip()
        self.created.append(identifier)
        return identifier

    def request(
        self, method, path, *, source="127.0.0.1", headers=None, body=None, context=None
    ):
        conn = http.client.HTTPSConnection(
            "127.0.0.2",
            443,
            context=context or self.context,
            timeout=8,
            source_address=(source, 0),
        )
        # Preserve verification against the certificate SAN while connecting only
        # to this test-owned loopback listener.
        raw = socket.create_connection(
            ("127.0.0.2", 443), timeout=8, source_address=(source, 0)
        )
        conn.sock = (context or self.context).wrap_socket(
            raw, server_hostname=self.config["outer_server_name"]
        )
        conn.request(
            method,
            path,
            body=body,
            headers={
                "Host": self.config["outer_server_name"],
                "Authorization": "Bearer fixture-private-api-key",
                **(headers or {}),
            },
        )
        response = conn.getresponse()
        value = response.read()
        conn.close()
        return response.status, value

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="private-api-tls-runtime-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.created = []
        self.extra_networks = []
        self.project = (
            "depositar-ai-release-"
            + uuid.uuid4().hex[:12]
            + "-"
            + uuid.uuid4().hex[:12]
        )
        self.network = self.project + "_website"
        self.gateway_project = (
            "depositar-private-api-test-gateway-" + uuid.uuid4().hex[:10]
        )
        self.endpoint_project = "depositar-private-api-tls-" + uuid.uuid4().hex[:12]
        self.output = self.root / "prepared"
        self.python_image = PublicEntryRuntimeFixture.image(
            "DEPOSITAR_TEST_PUBLIC_FIXTURE_IMAGE", "depositar-search-api:ci"
        )
        nginx_image = PublicEntryRuntimeFixture.image(
            "DEPOSITAR_TEST_PUBLIC_TLS_IMAGE", endpoint.NGINX_IMAGE
        )
        network_specs = PublicEntryRuntimeFixture.networks()
        subnet = network_specs["website"]["ipam"]["config"][0]["subnet"]
        self.run_command(
            [
                "docker",
                "network",
                "create",
                "--internal",
                "--subnet",
                subnet,
                "--label",
                "com.docker.compose.project=" + self.project,
                "--label",
                "com.docker.compose.network=website",
                self.network,
            ]
        )

        def cleanup():
            compose = self.output / "compose.json"
            if compose.exists():
                self.run_command(
                    ["docker", "compose", "-f", str(compose), "down", "--timeout", "3"],
                    check=False,
                )
            if self.created:
                self.run_command(
                    ["docker", "rm", "--force", *reversed(self.created)], check=False
                )
            self.run_command(
                ["docker", "network", "rm", self.network, *self.extra_networks],
                check=False,
            )

        self.addCleanup(cleanup)
        self.server_names = {
            "outer_server_name": OUTER_NAME,
            "inner_server_name": INNER_NAME,
        }
        certificates(
            self.root,
            outer_name=self.server_names["outer_server_name"],
            inner_name=self.server_names["inner_server_name"],
        )
        self.context = ssl.create_default_context(cafile=str(self.root / "ca.pem"))
        key = self.root / "service-key"
        key.write_text("fixture-private-api-key")
        key.chmod(0o600)
        script = self.root / "fake-api.py"
        script.write_text(FAKE_API)
        script.chmod(0o600)
        self.api = self.start(
            "conversation-api",
            extra=[
                "--network-alias",
                "conversation-api",
                "--mount",
                f"type=bind,src={key},dst=/run/secrets/api_key,readonly",
                "--mount",
                f"type=bind,src={script},dst=/fixture.py,readonly",
            ],
            command=["/fixture.py"],
        )
        self.start("web")
        self.start("filtered-embedding")
        self.release_ids = list(self.created)
        routes = self.root / "routes"
        routes.mkdir(mode=0o700)
        active = routes / "active.conf"
        active.write_text("unchanged test native gateway")
        active.chmod(0o600)
        gateway_network = self.network
        if self.isolated:
            gateway_network = self.gateway_project + "_website"
            self.run_command(
                [
                    "docker",
                    "network",
                    "create",
                    "--internal",
                    "--subnet",
                    network_specs["quota"]["ipam"]["config"][0]["subnet"],
                    gateway_network,
                ]
            )
            self.extra_networks.append(gateway_network)
        self.gateway = self.start(
            "web-nginx",
            project=self.gateway_project,
            network=gateway_network,
            extra=["--mount", f"type=bind,src={routes},dst=/etc/nginx/routes,readonly"],
        )
        self.gateway_before = self.inspect(self.gateway)
        release = {
            "name": self.project,
            "networks": {
                "website": {
                    "name": self.network,
                    "internal": True,
                    "attachable": False,
                }
            },
            "services": {
                "conversation-api": {
                    "image": self.python_image,
                    "networks": ["website"],
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(key),
                            "target": "/run/secrets/api_key",
                            "read_only": True,
                        }
                    ],
                },
                "web": {"image": self.python_image, "networks": ["website"]},
                "filtered-embedding": {
                    "image": self.python_image,
                    "networks": ["website"],
                },
            },
        }
        release_path = self.root / "release.json"
        release_path.write_text(json.dumps(release))
        release_path.chmod(0o600)
        self.config = {
            **self.server_names,
            "schema_version": 1,
            "release_compose": str(release_path),
            "api_container_id": self.api,
            "nginx_image": nginx_image,
            "bind_address": "127.0.0.2",
            "listen_port": 443,
            "allowed_source": "127.0.0.1/32",
            "ca_file": str(self.root / "ca.pem"),
            "outer_certificate_file": str(self.root / "outer.crt"),
            "outer_key_file": str(self.root / "outer.key"),
            "inner_certificate_file": str(self.root / "inner.crt"),
            "inner_key_file": str(self.root / "inner.key"),
        }
        if self.isolated:
            self.config.update(schema_version=2, backend_mode="isolated-release")
        config_path = self.root / "endpoint.json"
        config_path.write_text(json.dumps(self.config))
        config_path.chmod(0o600)
        self.policy = patch.object(
            endpoint.composition, "GATEWAY_PROJECT", self.gateway_project
        )
        self.policy.start()
        self.addCleanup(self.policy.stop)
        for _ in range(20):
            health = self.run_command(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Health.Status}}",
                    *self.release_ids,
                ]
            ).stdout.splitlines()
            if health == ["healthy"] * 3:
                break
            time.sleep(0.25)
        endpoint.prepare(config_path, self.output, project=self.endpoint_project)
        self.plan_path = self.output / "plan.json"
        self.before = self.inspect(self.api)

    def test_tls_auth_routes_netns_stop_and_ip_reuse_do_not_leak_credentials(self):
        endpoint.operate("up", self.plan_path, runner=self.endpoint_runner)
        for role, worker_uid in (("outer", "101"), ("inner", str(os.geteuid()))):
            identifier = self.run_command(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(self.output / "compose.json"),
                    "ps",
                    "--quiet",
                    role,
                ]
            ).stdout.strip()
            processes = self.run_command(
                ["docker", "top", identifier, "-eo", "pid,uid,args"]
            ).stdout.splitlines()
            self.assertTrue(
                any(
                    line.split()[1] == worker_uid and "nginx: worker process" in line
                    for line in processes[1:]
                ),
                processes,
            )
        status, _ = self.request("GET", "/v1/version")
        self.assertEqual(status, 200)
        self.assertEqual(
            self.request(
                "POST",
                "/v1/turn",
                body=b"{}",
                headers={"Content-Type": "application/json"},
            )[0],
            200,
        )
        for method, path, code in (
            ("GET", "/v1/turn", 405),
            ("HEAD", "/v1/version", 405),
            ("POST", "/v1/version", 405),
            ("GET", "/v1/version?x=1", 400),
            ("GET", "/v1/%76ersion", 400),
            ("GET", "/", 404),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path)[0], code)
        self.assertEqual(
            self.request(
                "GET", "/v1/version", headers={"Authorization": "Bearer forged"}
            )[0],
            401,
        )
        self.assertEqual(self.request("GET", "/v1/version", source="127.0.0.3")[0], 403)
        self.assertEqual(
            self.request(
                "POST",
                "/v1/turn",
                body=b"x" * 8193,
                headers={"Content-Type": "application/json"},
            )[0],
            413,
        )
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.request("GET", "/v1/version", context=ssl.create_default_context())
        self.assertEqual(self.before, self.inspect(self.api))
        self.assertEqual(
            endpoint.check(self.plan_path)["backend"]["peer_ids"],
            sorted(self.release_ids if self.isolated else self.created),
        )
        if self.isolated:
            binding = endpoint.check(self.plan_path)
            self.assertEqual(binding["schema_version"], 2)
            self.assertEqual(binding["backend"]["backend_mode"], "isolated-release")
            self.assertNotIn("active_route", binding["backend"])
            self.assertEqual(self.gateway_before, self.inspect(self.gateway))
        calls = json.loads(self.request("GET", "/v1/version")[1])["calls"]
        self.assertEqual(calls, 1)
        endpoint.operate("stop", self.plan_path, runner=self.endpoint_runner)
        endpoint.operate("up", self.plan_path, runner=self.endpoint_runner)
        self.assertEqual(json.loads(self.request("GET", "/v1/version")[1])["calls"], 1)
        # Only the disposable fake API is stopped. An IP-reused server presents
        # a same-CA certificate for the WRONG name; no HTTP header may reach it.
        ip = self.before["Networks"][self.network]["IPAddress"]
        self.run_command(["docker", "stop", "--time", "1", self.api])
        self.run_command(
            ["docker", "network", "disconnect", "--force", self.network, self.api],
            check=False,
        )
        received = self.root / "received"
        received.mkdir(mode=0o700)
        bad = self.root / "foreign.py"
        bad.write_text(
            "from http.server import HTTPServer,BaseHTTPRequestHandler\nimport ssl\nclass H(BaseHTTPRequestHandler):\n def do_GET(self):\n  open('/received/http','w').write('received')\n  self.send_response(200);self.end_headers()\n def log_message(self,*a):pass\ns=HTTPServer(('0.0.0.0',8443),H)\nc=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);c.load_cert_chain('/tls/outer.crt','/tls/outer.key');s.socket=c.wrap_socket(s.socket,server_side=True);s.serve_forever()\n"
        )
        bad.chmod(0o600)
        self.start(
            "foreign",
            extra=[
                "--ip",
                ip,
                "--mount",
                f"type=bind,src={self.root / 'outer.crt'},dst=/tls/outer.crt,readonly",
                "--mount",
                f"type=bind,src={self.root / 'outer.key'},dst=/tls/outer.key,readonly",
                "--mount",
                f"type=bind,src={received},dst=/received",
                "--mount",
                f"type=bind,src={bad},dst=/foreign.py,readonly",
            ],
            command=["/foreign.py"],
        )
        # Demonstrate a live, reachable TLS server for its actual SAN before
        # testing the outer proxy's different required inner identity.
        deadline = time.monotonic() + 6
        while True:
            try:
                raw = socket.create_connection((ip, 8443), timeout=1)
                with self.context.wrap_socket(
                    raw, server_hostname=self.config["outer_server_name"]
                ):
                    pass
                break
            except (ConnectionError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        self.assertEqual(self.request("GET", "/v1/version")[0], 502)
        self.assertFalse(
            (received / "http").exists(),
            "outer must verify TLS before sending Authorization",
        )
        with self.assertRaises(ValueError):
            endpoint.check(self.plan_path)
        endpoint.operate("stop", self.plan_path)
        self.assertEqual(self.gateway_before, self.inspect(self.gateway))


class IsolatedReleaseTlsRuntimeTests(PrivateApiTlsRuntimeTests):
    """A second, unchanged gateway stays on its own bridge throughout the test."""

    isolated = True
