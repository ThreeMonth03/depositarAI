"""Opt-in CPU-only Nginx peer ACL acceptance, without published host ports."""

from ipaddress import IPv4Address, IPv4Network
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid

from tests.integration.public_entry import test_runtime as runtime_fixture


HEALTH_SERVER = """from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
 def log_message(self, *args): pass
 def do_GET(self):
  self.send_response(200); self.end_headers()
  self.wfile.write(b'{"status":"ok"}')
HTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
"""

CLIENT = """import http.client, json, sys
connection = http.client.HTTPConnection('entry', 8080, timeout=5)
connection.request('GET', sys.argv[1], headers=json.loads(sys.argv[2]))
peer = connection.sock.getsockname()[0]
response = connection.getresponse()
print(json.dumps({'peer':peer, 'status':response.status,
                 'body':response.read(4096).decode()}))
connection.close()
"""


@unittest.skipUnless(
    os.environ.get("DEPOSITAR_TEST_INTRANET_WEB_RUNTIME") == "1",
    "explicit isolated CPU Docker opt-in required",
)
class IntranetWebRuntimeTests(unittest.TestCase):
    def command(self, *arguments, check=True, timeout=25):
        result = subprocess.run(
            ["docker", *arguments],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode:
            self.fail("owned intranet fixture command failed: " + result.stderr[-1500:])
        return result

    def run_container(
        self, name, image, *arguments, network, alias=None, detach=True, command=()
    ):
        self.containers.append(name)
        options = [
            "run",
            "--name",
            name,
            "--runtime",
            "runc",
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            f"{os.geteuid()}:{os.getegid()}",
            "--cpus",
            "0.25",
            "--memory",
            "64m",
            "--pids-limit",
            "32",
            "--tmpfs",
            "/tmp:rw,nosuid,noexec,nodev,size=16m,mode=1777",
            "--env",
            "NVIDIA_VISIBLE_DEVICES=void",
        ]
        options += ["--detach"] if detach else ["--rm"]
        if alias:
            options += ["--network-alias", alias]
        return self.command(*options, *arguments, image, *command)

    def request(self, network, path="/", headers=None):
        name = f"{self.prefix}-client-{len(self.containers)}"
        result = self.run_container(
            name,
            self.python_image,
            "--entrypoint",
            "python",
            network=network,
            detach=False,
            command=("-c", CLIENT, path, json.dumps(headers or {})),
        )
        return json.loads(result.stdout)

    def cleanup(self):
        if self.containers:
            self.command("rm", "--force", *self.containers, check=False)
        for network in reversed(self.networks):
            self.command("network", "rm", network, check=False)
        remaining = self.command(
            "ps", "--all", "--quiet", "--filter", "name=^/" + self.prefix + "-"
        )
        self.assertFalse(remaining.stdout.strip(), "owned fixture containers remain")
        for network in self.networks:
            self.assertNotEqual(
                self.command("network", "inspect", network, check=False).returncode,
                0,
                "owned fixture network remains",
            )

    def test_admission_uses_tcp_peer_and_preserves_loopback_health(self):
        self.prefix = "depositar-intranet-acl-" + uuid.uuid4().hex[:16]
        self.containers = []
        self.networks = []
        self.python_image = self.command(
            "image", "inspect", "--format", "{{.Id}}", "depositar-search-api:ci"
        ).stdout.strip()
        nginx_image = self.command(
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            os.environ.get(
                "DEPOSITAR_TEST_INTRANET_NGINX_IMAGE", "depositar-search-web-nginx:ci"
            ),
        ).stdout.strip()
        allowed, denied = self.prefix + "-allowed", self.prefix + "-denied"
        nginx = self.prefix + "-nginx"
        with tempfile.TemporaryDirectory(prefix=self.prefix + "-") as directory:
            try:
                # Reuse the existing allocator instead of consuming exhausted
                # default Docker pools or removing any unrelated network.
                specs = runtime_fixture.PublicEntryRuntimeFixture.networks()
                for network, role in ((allowed, "website"), (denied, "quota")):
                    self.networks.append(network)
                    subnet = specs[role]["ipam"]["config"][0]["subnet"]
                    self.command(
                        "network", "create", "--internal", "--subnet", subnet, network
                    )
                subnet = self.command(
                    "network",
                    "inspect",
                    "--format",
                    "{{(index .IPAM.Config 0).Subnet}}",
                    allowed,
                ).stdout.strip()
                access = Path(directory) / "intranet-allow.conf"
                access.write_text(
                    f"allow 127.0.0.1;\nallow ::1;\nallow {subnet};\ndeny all;\n"
                )
                access.chmod(0o444)
                self.run_container(
                    self.prefix + "-web",
                    self.python_image,
                    "--entrypoint",
                    "python",
                    network=allowed,
                    alias="web",
                    command=("-u", "-c", HEALTH_SERVER),
                )
                self.run_container(
                    nginx,
                    nginx_image,
                    "--mount",
                    f"type=bind,source={access},target=/etc/nginx/intranet-allow.conf,readonly",
                    network=allowed,
                    alias="entry",
                )
                self.command("network", "connect", "--alias", "entry", denied, nginx)
                health = None
                for _ in range(30):
                    health = self.command(
                        "exec",
                        nginx,
                        "wget",
                        "-q",
                        "-O",
                        "-",
                        "http://127.0.0.1:8080/healthz",
                        check=False,
                    )
                    if health.returncode == 0:
                        break
                    time.sleep(0.2)
                self.assertEqual(
                    health.returncode, 0, "loopback health did not become ready"
                )
                self.assertEqual(json.loads(health.stdout), {"status": "ok"})
                homepage = self.request(allowed)
                self.assertEqual(homepage["status"], 200)
                self.assertIn('<div id="root"></div>', homepage["body"])
                self.assertIn(IPv4Address(homepage["peer"]), IPv4Network(subnet))
                blocked = self.request(denied)
                self.assertEqual(blocked["status"], 403)
                self.assertNotIn(IPv4Address(blocked["peer"]), IPv4Network(subnet))
                spoofed = {
                    "X-Forwarded-For": homepage["peer"],
                    "X-Real-IP": "127.0.0.1",
                    "Forwarded": "for=127.0.0.1",
                }
                for path in ("/", "/api/access", "/healthz"):
                    with self.subTest(path=path):
                        self.assertEqual(
                            self.request(denied, path, spoofed)["status"], 403
                        )
                self.assertEqual(self.request(allowed, "/healthz")["status"], 200)
            finally:
                self.cleanup()


if __name__ == "__main__":
    unittest.main()
