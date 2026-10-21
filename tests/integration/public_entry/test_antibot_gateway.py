"""Real optional Anubis -> APISIX -> Redis -> BFF acceptance, without a GPU."""

import json
import os
import subprocess
import time
import unittest

from tests.integration.public_entry import test_runtime as runtime_fixture


@unittest.skipUnless(
    os.environ.get("DEPOSITAR_TEST_ANTIBOT_RUNTIME") == "1",
    "combined browser gate requires an explicit isolated CPU test opt-in",
)
class AntibotGatewayTests(runtime_fixture.PublicEntryRuntimeFixture):
    mixed = True
    antibot = True

    @classmethod
    def compose(cls, *arguments, timeout=30):
        try:
            return super().compose(*arguments, timeout=timeout)
        except (AssertionError, subprocess.TimeoutExpired) as error:
            if not arguments or arguments[0] != "up":
                raise
            # Only this disposable fixture's health state/logs are inspected.
            # Never include full inspect output, environment or mounted files.
            prefix = [
                "docker",
                "compose",
                "--project-name",
                cls.project,
                "--file",
                str(cls.compose_file),
            ]
            logs = cls.command(
                [
                    *prefix,
                    "logs",
                    "--no-color",
                    "--tail",
                    "15",
                    "antibot",
                    "public-edge",
                ],
                timeout=10,
                check=False,
            ).stdout[-4000:]
            identifiers = cls.command(
                [
                    *prefix,
                    "ps",
                    "--all",
                    "--quiet",
                    "antibot",
                    "public-edge",
                    "public-entry",
                ],
                timeout=10,
                check=False,
            ).stdout.split()
            health = []
            if identifiers:
                inspected = cls.command(
                    ["docker", "inspect", *identifiers],
                    timeout=10,
                    check=False,
                )
                try:
                    records = json.loads(inspected.stdout)
                except ValueError:
                    records = []
                for record in records:
                    state = record.get("State", {})
                    checks = state.get("Health", {})
                    health.append(
                        {
                            "name": record.get("Name"),
                            "status": state.get("Status"),
                            "exit_code": state.get("ExitCode"),
                            "health": checks.get("Status"),
                            "checks": [
                                {
                                    "exit_code": check.get("ExitCode"),
                                    "output": check.get("Output", "")[-1000:],
                                }
                                for check in checks.get("Log", [])[-3:]
                            ],
                        }
                    )
            diagnostic = str(error) + "\n" + logs + "\n" + json.dumps(health)
            for secret in ("fixture-oidc-client-secret", *(c * 64 for c in "abcdef")):
                diagnostic = diagnostic.replace(secret, "[fixture-secret-redacted]")
            raise AssertionError(diagnostic) from None

    def test_anonymous_and_oidc_admission_use_challenge_gate_and_durable_quotas(self):
        self.compose("up", "--detach", "--wait", "antibot", timeout=45)
        self.assertEqual(self.probe("access", "guest")["status"], 200)
        denied = self.probe("session", "guest")
        self.assertEqual(denied["status"], 403)
        self.assertEqual(
            denied["value"]["error"],
            {"code": "challenge_required", "verification_path": "/verify"},
        )
        self.assertEqual(self.calls(), 0)

        self.assertEqual(self.probe("verify", "guest")["status"], 200)
        self.assertEqual(self.probe("session", "guest")["status"], 200)
        self.assertEqual(
            self.probe("turn", "guest", {"query": "氣候", "version": 0})["status"],
            200,
        )
        self.assertEqual(self.calls(), 1)
        self.assertEqual(
            self.probe("turn", "guest", {"query": "氣候", "version": 1})["status"],
            429,
        )
        self.assertEqual(self.calls(), 1)

        self.assertEqual(self.probe("verify", "alice")["status"], 200)
        self.assertEqual(self.probe("login", "alice")["status"], 200)
        identity = self.probe("access", "alice")
        self.assertEqual(identity["status"], 200)
        self.assertEqual(identity["value"]["principal"]["kind"], "user")
        self.assertEqual(self.probe("session", "alice")["status"], 200)
        self.assertEqual(
            self.probe("turn", "alice", {"query": "氣候", "version": 0})["status"],
            200,
        )
        self.assertEqual(self.calls(), 2)

        # APISIX successfully starts with hostname-only trust for the entry.
        # This configuration is generated by APISIX itself, not a stub proxy.
        configured = self.compose(
            "exec",
            "--no-TTY",
            "public-entry",
            "/bin/sh",
            "-c",
            "grep -F public-edge /usr/local/apisix/conf/nginx.conf",
        ).stdout
        self.assertRegex(configured, r"set_real_ip_from\s+['\"]?public-edge")

        # Missing challenge proof on encoded/alternate paths never reaches AI.
        for path in (
            "/assets/../api/turn",
            "/assets/%2e%2e/api/turn",
            "/api/%74urn",
            "/api//turn",
        ):
            blocked = self.probe("path", "unverified", {"path": path})
            self.assertIn(blocked["status"], (400, 401, 403, 404, 405))
        self.assertEqual(self.calls(), 2)

        time.sleep(1)
        self.compose("stop", "--timeout", "2", "antibot", timeout=15)
        unavailable = self.probe("turn", "alice", {"query": "氣候", "version": 1})
        self.assertEqual(unavailable["status"], 503)
        self.assertEqual(unavailable["value"]["error"]["code"], "service_unavailable")
        self.assertEqual(self.calls(), 2)
        self.compose("up", "--detach", "--wait", "antibot", timeout=45)

        self.compose("stop", "--timeout", "2", "quota-redis", timeout=15)
        quota_down = self.probe("turn", "alice", {"query": "氣候", "version": 1})
        self.assertIn(quota_down["status"], (500, 503))
        self.assertEqual(self.calls(), 2)
        self.compose("start", "quota-redis", timeout=15)
        time.sleep(1)
        self.assertEqual(
            self.probe("turn", "alice", {"query": "氣候", "version": 1})["status"],
            200,
        )
        self.assertEqual(self.calls(), 3)


if __name__ == "__main__":
    unittest.main()
