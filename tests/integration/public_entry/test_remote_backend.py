"""Optional cross-host HTTPS binding in disposable namespaces, without a GPU.

The upstream is a fake API on its own private bridge behind a separate TLS
proxy. This models network separation; it is not a VM/GPU deployment receipt.
The inherited fixture supplies only the IdP, HTTPS browser probe and allocator.
"""

import copy
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest

from search.operations.deployment.public_entry import host, remote
from tests.integration.public_entry import test_runtime as runtime_fixture


BACKEND_ORIGIN = "https://backend.test:9443"
EXPECTED_VERSION = {
    "revision": "a" * 40,
    "catalog_manifest_sha256": "b" * 64,
    "conversation_contract_version": 2,
}


@unittest.skipUnless(
    os.environ.get("DEPOSITAR_TEST_REMOTE_BACKEND_RUNTIME") == "1",
    "cross-host HTTPS acceptance requires explicit isolated CPU test opt-in",
)
class RemoteBackendTests(runtime_fixture.PublicEntryRuntimeFixture):
    mixed = True
    remote_prepared = False

    @classmethod
    def networks(cls):
        # Reuse the collision-aware allocator's four-network mode. No Anubis
        # service is required for this independent backend-binding scenario.
        allocator = SimpleNamespace(antibot=True, command=cls.command)
        result = runtime_fixture.PublicEntryRuntimeFixture.networks.__func__(allocator)
        result["backend-private"] = result.pop("antibot-gateway")
        result["outbound"]["internal"] = False
        return result

    @classmethod
    def compose(cls, *arguments, timeout=30):
        if arguments and arguments[0] == "up" and not cls.remote_prepared:
            cls.configure_remote()
            cls.remote_prepared = True
        return super().compose(*arguments, timeout=timeout)

    @classmethod
    def configure_remote(cls):
        """Replace only this fixture's pending CPU plan before first startup."""
        root = cls.compose_file.parent
        cls.directory = root
        initial = json.loads(cls.compose_file.read_text())
        services = initial["services"]
        images = {
            role: services[role]["image"]
            for role in ("public-entry", "public-web", "public-frontend", "quota-redis")
        }
        cls.command(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(root / "backend.key"),
                "-out",
                str(root / "backend.crt"),
                "-subj",
                "/CN=remote-backend-fixture",
                "-addext",
                "subjectAltName=DNS:backend.test",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
            ]
        )
        cls.ca = root / "backend-ca.pem"
        cls.ca.write_bytes((root / "backend.crt").read_bytes())
        state = root / "remote-state"
        state.mkdir(mode=0o700)
        cls.entry_config = root / "remote-entry.json"
        cls.entry_config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "state_root": str(state),
                    "public_origin": runtime_fixture.ORIGIN,
                    "oidc_issuer": runtime_fixture.ISSUER,
                    "oidc_discovery": runtime_fixture.ISSUER
                    + "/.well-known/openid-configuration",
                    "oidc_client_id": "public-entry-test",
                    "oidc_client_secret_file": str(root / "client-secret"),
                    "oidc_session_secret_file": str(root / "session-secret"),
                    "edge_secret_file": str(root / "edge-key"),
                    "redis_password_file": str(root / "redis-password"),
                    "oidc_ca_file": str(root / "tls.crt"),
                    "access_mode": "mixed",
                    "guest_session_secret_file": str(root / "guest-secret"),
                    "quota_per_account": 3,
                    "quota_per_guest": 1,
                    "quota_global": 6,
                    "quota_guest_pool": 2,
                    "quota_user_pool": 4,
                    "quota_window_seconds": cls.quota_window_seconds,
                }
            )
        )
        cls.backend_config = root / "backend.json"
        cls.backend_config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "origin": BACKEND_ORIGIN,
                    "api_revision": EXPECTED_VERSION["revision"],
                    "catalog_manifest_sha256": EXPECTED_VERSION[
                        "catalog_manifest_sha256"
                    ],
                    "api_key_file": str(root / "api-key"),
                    "ca_file": str(cls.ca),
                }
            )
        )
        for filename in (
            "backend.key",
            "backend.crt",
            "backend-ca.pem",
            "remote-entry.json",
            "backend.json",
        ):
            (root / filename).chmod(0o600)
        receipt = host.prepare_public_entry(
            cls.entry_config,
            None,
            images["public-web"],
            images["public-frontend"],
            backend_config=cls.backend_config,
        )
        cls.profile = json.loads(Path(receipt["compose_file"]).read_text())
        for role in images:
            services[role] = copy.deepcopy(cls.profile["services"][role])
            services[role]["image"] = images[role]
            services[role].pop("ports", None)
        # Keep the fixture's ephemeral Compose project and explicit subnets;
        # the production plan above remains sealed and unmodified.
        initial["x-depositar-plan"] = cls.profile["x-depositar-plan"]
        for role in ("quota-redis", "public-web", "public-entry", "public-frontend"):
            services[role]["healthcheck"].update(
                start_period="20s", start_interval="1s"
            )
        services["probe"]["volumes"].append(
            {
                "type": "bind",
                "source": str(root),
                "target": str(root),
                "read_only": True,
            }
        )
        services["remote-api"] = {
            **copy.deepcopy(services["fixture"]),
            "networks": ["backend-private"],
        }
        services["remote-api"]["healthcheck"] = {
            "test": [
                "CMD",
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/__calls',timeout=2).read()",
            ],
            "interval": "2s",
            "timeout": "3s",
            "retries": 10,
        }
        nginx = """worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr crit;
events { worker_connections 64; }
http {
 access_log off;
 resolver 127.0.0.11 valid=1s;
 client_body_temp_path /tmp/client_body;
 proxy_temp_path /tmp/proxy;
 fastcgi_temp_path /tmp/fastcgi;
 uwsgi_temp_path /tmp/uwsgi;
 scgi_temp_path /tmp/scgi;
 server {
  listen 9443 ssl;
  server_name backend.test;
  ssl_certificate /fixture/backend.crt;
  ssl_certificate_key /fixture/backend.key;
  location / {
   set $api remote-api:8000;
   proxy_pass http://$api;
   proxy_set_header Host $http_host;
  }
 }
}
"""
        (root / "backend-nginx.conf").write_text(nginx)
        (root / "backend-nginx.conf").chmod(0o600)
        services["remote-tls"] = {
            **copy.deepcopy(services["tls-front"]),
            "entrypoint": [
                "nginx",
                "-c",
                "/fixture/backend-nginx.conf",
                "-g",
                "daemon off;",
            ],
            "networks": {
                "outbound": {"aliases": ["backend.test"]},
                "backend-private": {},
            },
            "depends_on": {"remote-api": {"condition": "service_healthy"}},
        }
        # The inherited public TLS probe's health command targets public.test;
        # this different proxy is validated by the actual authenticated probe.
        services["remote-tls"].pop("healthcheck", None)
        services["public-web"]["depends_on"] = {
            "remote-tls": {"condition": "service_started"}
        }
        cls.compose_file.write_text(json.dumps(initial))

    def backend_json(self, path):
        source = (
            "import json,ssl,urllib.request; "
            "ctx=ssl.create_default_context(cafile='/fixture/backend.crt'); "
            "print(urllib.request.urlopen("
            + repr(BACKEND_ORIGIN + path)
            + ",context=ctx,timeout=5).read().decode())"
        )
        return json.loads(
            self.compose("exec", "-T", "probe", "python", "-c", source).stdout
        )

    def calls(self):
        return self.backend_json("/__calls")["calls"]

    def host_probe_namespace(self, argv, **kwargs):
        """Run the exact host probe in a disposable namespace with Docker DNS.

        The paths are mounted unchanged. The later post-start check below calls
        the real remote verifier's docker-compose-exec path without this adapter.
        """
        environment = kwargs.get("env", {})
        command = ["docker", "compose", "-f", str(self.compose_file), "exec", "-T"]
        for key in ("WEB_UPSTREAM", "WEB_UPSTREAM_CA_FILE"):
            command.extend(["-e", key + "=" + environment[key]])
        command.extend(["probe", "python", *argv[1:]])
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=kwargs.get("timeout", 15),
            check=False,
        )

    def test_version_only_preflight_tls_identity_and_real_quota_routes(self):
        web = self.profile["services"]["public-web"]
        self.assertEqual(set(web["networks"]), {"website", "outbound"})
        self.assertNotIn("existing-release", self.profile["networks"])
        self.assertFalse(
            any(
                network.get("external") for network in self.profile["networks"].values()
            )
        )
        self.assertEqual(web["runtime"], "runc")
        self.assertEqual(web["environment"]["NVIDIA_VISIBLE_DEVICES"], "void")
        self.assertFalse(web.get("devices"))
        self.assertFalse(web.get("ports"))
        container = self.compose("ps", "--quiet", "public-web").stdout.strip()
        actual = json.loads(
            self.command(
                [
                    "docker",
                    "inspect",
                    "--format",
                    '{"runtime":{{json .HostConfig.Runtime}},"devices":{{json .HostConfig.Devices}},'
                    '"requests":{{json .HostConfig.DeviceRequests}},"networks":{{json .NetworkSettings.Networks}}}',
                    container,
                ]
            ).stdout
        )
        self.assertEqual(actual["runtime"], "runc")
        self.assertFalse(actual["devices"])
        self.assertFalse(actual["requests"])
        self.assertEqual(
            set(actual["networks"]),
            {self.project + "_website", self.project + "_outbound"},
        )
        self.assertEqual(self.calls(), 0)

        self.assertEqual(
            remote.verify_remote_backend(
                self.profile, runner=self.host_probe_namespace
            ),
            remote.PROOF,
        )
        self.assertEqual(
            remote.verify_remote_backend(self.profile, compose_file=self.compose_file),
            remote.PROOF,
        )
        requests = self.backend_json("/__version_calls")["requests"]
        self.assertEqual(
            requests,
            [
                {"method": "GET", "path": "/v1/version", "status": status}
                for status in (401, 200, 401, 200)
            ],
        )
        self.assertEqual(self.calls(), 0)

        wrong_manifest = copy.deepcopy(self.profile)
        wrong_manifest["x-depositar-plan"]["private_backend"][
            "catalog_manifest_sha256"
        ] = "e" * 64
        with self.assertRaisesRegex(ValueError, "remote_probe_failed"):
            remote.verify_remote_backend(wrong_manifest, compose_file=self.compose_file)
        good_ca = self.ca.read_bytes()
        self.ca.write_bytes((self.directory / "tls.crt").read_bytes())
        try:
            with self.assertRaisesRegex(ValueError, "remote_probe_failed"):
                remote.verify_remote_backend(
                    self.profile, compose_file=self.compose_file
                )
        finally:
            self.ca.write_bytes(good_ca)
        wrong_wire = self.directory / "backend-version.json"
        wrong_wire.write_text(
            json.dumps({**EXPECTED_VERSION, "conversation_contract_version": 1})
        )
        wrong_wire.chmod(0o600)
        try:
            with self.assertRaisesRegex(ValueError, "remote_probe_failed"):
                remote.verify_remote_backend(
                    self.profile, compose_file=self.compose_file
                )
        finally:
            wrong_wire.unlink()
        self.assertEqual(self.calls(), 0)
        self.assertTrue(
            all(
                request["method"] == "GET" and request["path"] == "/v1/version"
                for request in self.backend_json("/__version_calls")["requests"]
            )
        )

        self.assertEqual(self.probe("session", "guest")["status"], 200)
        self.assertEqual(
            self.probe("turn", "guest", {"query": "氣候", "version": 0})["status"], 200
        )
        self.assertEqual(self.calls(), 1)
        self.assertEqual(
            self.probe("turn", "guest", {"query": "氣候", "version": 1})["status"], 429
        )
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.probe("login", "alice")["status"], 200)
        self.assertEqual(self.probe("session", "alice")["status"], 200)
        self.assertEqual(
            self.probe("turn", "alice", {"query": "氣候", "version": 0})["status"], 200
        )
        self.assertEqual(self.calls(), 2)


if __name__ == "__main__":
    unittest.main()
