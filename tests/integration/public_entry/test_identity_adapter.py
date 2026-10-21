"""Opt-in pinned OpenResty adapter checks without networks, Redis or a GPU."""

import os
from pathlib import Path
import subprocess
import unittest

from search.operations.deployment.public_entry.profile import APISIX_IMAGE


ROOT = Path(__file__).resolve().parents[3]


@unittest.skipUnless(
    os.environ.get("DEPOSITAR_TEST_PUBLIC_ENTRY_RUNTIME") == "1",
    "explicit isolated Docker opt-in required",
)
class IdentityAdapterRuntimeTests(unittest.TestCase):
    def test_real_lua_identity_and_bounded_quota_response(self):
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--runtime",
                "runc",
                "--network",
                "none",
                "--cpus",
                "0.5",
                "--memory",
                "256m",
                "--memory-swap",
                "256m",
                "--pids-limit",
                "32",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16m",
                "--env",
                "NVIDIA_VISIBLE_DEVICES=void",
                "--env",
                "DEPOSITAR_EDGE_KEY=" + "d" * 64,
                "--mount",
                f"type=bind,src={ROOT},dst=/source,readonly",
                "--entrypoint",
                "/usr/local/openresty/bin/resty",
                APISIX_IMAGE,
                "-I",
                "/usr/local/apisix",
                "-I",
                "/usr/local/apisix/deps/share/lua/5.1",
                "/source/tests/integration/public_entry/identity_adapter_contract.lua",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        self.assertIn("Lua contracts passed", result.stdout)
