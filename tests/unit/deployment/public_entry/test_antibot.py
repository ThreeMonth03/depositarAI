"""Policy and runtime boundary checks for the optional anonymous browser gate."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from search.operations.deployment.public_entry.antibot import (
    ANUBIS_IMAGE,
    NETWORK,
    render_antibot,
)


class AntibotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-antibot-unit-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = SimpleNamespace(
            antibot_enabled=True,
            antibot_signing_key_file=root / "signing-key",
            state_root=root,
            public_origin="https://public.example.org:9443",
            trusted_proxy_cidrs=("192.0.2.0/24",),
            loopback_port=18084,
        )

    def plan(self):
        return render_antibot(self.config, uid=1000, gid=1000)

    def test_disabled_has_no_assets_containers_or_network_changes(self):
        self.config.antibot_enabled = False
        plan = self.plan()
        self.assertEqual(plan["assets"], {})
        self.assertEqual(plan["services"], {})
        self.assertEqual(plan["networks"], {})
        self.assertEqual(plan["state_directories"], [])
        self.assertEqual(plan["apisix_overrides"], {})
        self.assertEqual(plan["entry_service"], "public-entry")

    def test_only_outer_entry_publishes_loopback_and_all_services_are_cpu_bounded(self):
        plan = self.plan()
        self.assertEqual(plan["networks"], {NETWORK: {"internal": True}})
        ports = plan["services"]["public-edge"]["ports"]
        self.assertEqual(ports[0]["host_ip"], "127.0.0.1")
        self.assertEqual(ports[0]["published"], "18084")
        self.assertNotIn("ports", plan["services"]["antibot"])
        self.assertEqual(plan["services"]["antibot"]["image"], ANUBIS_IMAGE)
        self.assertRegex(ANUBIS_IMAGE, r"v1\.28\.1@sha256:[0-9a-f]{64}$")
        for service in plan["services"].values():
            self.assertEqual(service["runtime"], "runc")
            self.assertEqual(service["environment"]["NVIDIA_VISIBLE_DEVICES"], "void")
            self.assertTrue(service["read_only"])
            self.assertLessEqual(service["cpus"], 0.5)
            self.assertNotIn("devices", service)

    def test_restart_uses_durable_store_fixed_signing_key_and_private_metrics(self):
        plan = self.plan()
        policy = json.loads(plan["assets"]["antibot-policy.yaml"])
        self.assertEqual(policy["store"]["backend"], "bbolt")
        self.assertEqual(policy["store"]["parameters"]["path"], "/data/anubis.bdb")
        self.assertEqual(policy["metrics"]["bind"], "127.0.0.1:9090")
        self.assertFalse(policy["metrics"]["debug"])
        env = plan["services"]["antibot"]["environment"]
        self.assertEqual(env["JWT_RESTRICTION_HEADER"], "X-Real-IP")
        self.assertEqual(env["REDIRECT_DOMAINS"], "public.example.org:9443")
        self.assertEqual(env["COOKIE_SECURE"], "true")
        self.assertEqual(env["COOKIE_HTTP_ONLY"], "true")
        self.assertEqual(env["COOKIE_EXPIRATION_TIME"], "24h")
        mounts = plan["services"]["antibot"]["volumes"]
        self.assertEqual([v["target"] for v in mounts if not v["read_only"]], ["/data"])
        self.assertEqual(plan["state_directories"], ["antibot-data"])
        self.assertNotIn("ED25519_PRIVATE_KEY_HEX", env)

    def test_no_user_agent_or_api_allowlist_can_skip_required_challenge(self):
        plan = self.plan()
        policy = json.loads(plan["assets"]["antibot-policy.yaml"])
        self.assertEqual(len(policy["bots"]), 1)
        self.assertEqual(policy["bots"][0]["action"], "CHALLENGE")
        self.assertEqual(policy["bots"][0]["path_regex"], ".*")
        self.assertNotIn("user_agent_regex", policy["bots"][0])
        nginx = plan["assets"]["antibot-nginx.conf"]
        self.assertIn("auth_request /_antibot_check;", nginx)
        self.assertIn(
            '"code":"challenge_required","verification_path":"/verify"', nginx
        )
        self.assertIn("error_page 401 403 = @challenge_required;", nginx)
        self.assertIn("location = /_antibot_check {\n            internal;", nginx)
        self.assertIn("proxy_pass_request_body off;", nginx)
        self.assertIn(
            "location = /.within.website/x/cmd/anubis/api/check { return 404; }", nginx
        )

    def test_verification_redirect_is_fixed_and_cookie_is_not_replayed_as_api_post(
        self,
    ):
        nginx = self.plan()["assets"]["antibot-nginx.conf"]
        verify = nginx.split("location = /verify {", 1)[1].split("\n        }", 1)[0]
        self.assertIn("if ($request_method != GET)", verify)
        self.assertIn("proxy_pass $antibot/?redir=/;", verify)
        self.assertNotIn("$args", verify)
        self.assertNotIn("$request_body", verify)
        # The challenge gate only wraps creation/turn/login; callback still reaches APISIX.
        self.assertNotIn("location = /auth/callback", nginx)
        self.assertIn("location / { proxy_pass $gateway; }", nginx)
        self.assertIn("if ($request_method = POST) { return 418; }", nginx)

    def test_forwarded_identity_is_overwritten_and_public_gateway_has_no_direct_port(
        self,
    ):
        plan = self.plan()
        nginx = plan["assets"]["antibot-nginx.conf"]
        self.assertIn("set_real_ip_from 192.0.2.0/24;", nginx)
        self.assertIn("proxy_set_header X-Real-IP $remote_addr;", nginx)
        self.assertIn("proxy_set_header X-Forwarded-For '';", nginx)
        self.assertIn("proxy_set_header X-Anubis-Status '';", nginx)
        self.assertNotIn("@@", nginx)
        overrides = plan["apisix_overrides"]
        self.assertEqual(overrides["ports"], [])
        self.assertEqual(overrides["real_ip_from"], ["public-edge"])
        self.assertEqual(overrides["add_networks"], [NETWORK])

    def test_enabled_requires_dedicated_key_and_nonroot_container_identity(self):
        for key in (None, "inline-secret", Path("relative")):
            self.config.antibot_signing_key_file = key
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "key_required"),
            ):
                self.plan()
        self.config.antibot_signing_key_file = self.config.state_root / "signing-key"
        with self.assertRaisesRegex(ValueError, "operator_invalid"):
            render_antibot(self.config, uid=0, gid=os.getegid())


if __name__ == "__main__":
    unittest.main()
