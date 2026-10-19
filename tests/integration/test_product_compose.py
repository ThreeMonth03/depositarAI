"""Check plans with the real Compose parser, without starting containers."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest

from search.operations.deployment.composition import (
    QueryBindingPaths,
    render_composition,
)
from search.operations.catalog.policy import _memory_bytes
from tests.unit.deployment.test_composition import REVISION, catalog, product


@unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI is not installed")
class ProductComposeIntegrationTests(unittest.TestCase):
    def render(self, plane):
        config = product()
        plan = catalog(config)
        composition = render_composition(
            config,
            plan,
            QueryBindingPaths.for_catalog(config, plan),
            REVISION,
            uid=1000,
            gid=1000,
            inference_source_sha256="6" * 64,
        )[plane]
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--project-directory",
                "/tmp",
                "--file",
                "-",
                "config",
                "--format",
                "json",
            ],
            input=json.dumps(composition, allow_nan=False),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(0, result.returncode, f"Compose rejected {plane} plan")
        normalized = json.loads(result.stdout)
        self.assertEqual(composition["name"], normalized["name"])
        self.assertEqual(set(composition["services"]), set(normalized["services"]))
        for name, expected in composition["services"].items():
            actual = normalized["services"][name]
            for key in (
                "image",
                "runtime",
                "user",
                "read_only",
                "cap_drop",
                "security_opt",
                "environment",
                "pids_limit",
            ):
                self.assertEqual(expected[key], actual[key], (plane, name, key))
            for key in ("mem_limit", "memswap_limit"):
                self.assertEqual(
                    _memory_bytes(expected[key]), _memory_bytes(actual[key])
                )
            self.assertEqual(float(expected["cpus"]), float(actual["cpus"]))
        return normalized

    def test_inference_parser_preserves_disjoint_uuid_requests(self):
        services = self.render("inference")["services"]
        uuids = []
        for service in services.values():
            requests = service["deploy"]["resources"]["reservations"]["devices"]
            self.assertEqual(1, len(requests))
            identifiers = requests[0]["device_ids"]
            self.assertEqual(
                ",".join(identifiers), service["environment"]["NVIDIA_VISIBLE_DEVICES"]
            )
            self.assertEqual("nvidia", service["runtime"])
            self.assertNotIn("ports", service)
            uuids.extend(identifiers)
        self.assertEqual(4, len(uuids))
        self.assertEqual(4, len(set(uuids)))

    def test_cpu_release_parser_preserves_no_ports_or_gpus(self):
        services = self.render("release")["services"]
        for service in services.values():
            self.assertEqual("runc", service["runtime"])
            self.assertEqual("void", service["environment"]["NVIDIA_VISIBLE_DEVICES"])
            self.assertNotIn("ports", service)
            self.assertNotIn("deploy", service)

    def test_gateway_parser_preserves_single_loopback_binding(self):
        service = self.render("gateway")["services"]["web-nginx"]
        ports = service["ports"]
        self.assertEqual(1, len(ports))
        self.assertEqual("127.0.0.1", ports[0]["host_ip"])
        self.assertEqual("18083", str(ports[0]["published"]))
        self.assertEqual(8080, ports[0]["target"])
        self.assertEqual("tcp", ports[0]["protocol"])
        self.assertEqual(
            ["-c", "/etc/nginx/routes/active.conf", "-g", "daemon off;"],
            service["command"],
        )


if __name__ == "__main__":
    unittest.main()
