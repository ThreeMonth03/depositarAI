"""Private remote binding and version-only admission, without model services."""

import copy
from dataclasses import FrozenInstanceError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.deployment.public_entry import remote


REVISION = "a" * 40
MANIFEST = "b" * 64
API_KEY = "c" * 64


class RemoteFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certificates = tempfile.TemporaryDirectory(prefix="remote-backend-tls-")
        cls.cert_root = Path(cls.certificates.name)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(cls.cert_root / "server.key"),
                "-out",
                str(cls.cert_root / "ca.pem"),
                "-days",
                "2",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
            ],
            capture_output=True,
            check=True,
            timeout=15,
            umask=0o077,
        )

    @classmethod
    def tearDownClass(cls):
        cls.certificates.cleanup()

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="remote-backend-unit-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.key = self.root / "api-key"
        self.key.write_text(API_KEY + "\n")
        self.key.chmod(0o600)
        self.ca = self.root / "ca.pem"
        self.ca.write_bytes((self.cert_root / "ca.pem").read_bytes())
        self.ca.chmod(0o600)
        self.config_path = self.root / "backend.json"
        self.value = {
            "schema_version": 1,
            "origin": "https://backend.example.test",
            "api_revision": REVISION,
            "catalog_manifest_sha256": MANIFEST,
            "api_key_file": str(self.key),
            "ca_file": str(self.ca),
        }
        self.write_config()

    def write_config(self, value=None):
        self.config_path.write_text(json.dumps(self.value if value is None else value))
        self.config_path.chmod(0o600)

    def load(self):
        return remote.load_remote_backend(self.config_path)

    def profile(self):
        backend = self.load()
        return {
            "x-depositar-plan": {"private_backend": backend.to_plan()},
            "networks": {
                "website": {"internal": True},
                "outbound": {"internal": False},
            },
            "services": {
                "public-web": {
                    "runtime": "runc",
                    "networks": {"website": {"aliases": ["web"]}, "outbound": {}},
                    "environment": {
                        "WEB_UPSTREAM": backend.origin,
                        "WEB_UPSTREAM_CA_FILE": remote.CA_TARGET,
                        "NVIDIA_VISIBLE_DEVICES": "void",
                    },
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(path),
                            "target": target,
                            "read_only": True,
                        }
                        for path, target in (
                            (self.key, remote.API_KEY_TARGET),
                            (self.ca, remote.CA_TARGET),
                        )
                    ],
                }
            },
        }


class RemoteConfigurationTests(RemoteFixture):
    def test_private_configuration_is_frozen_and_plan_contains_no_file_or_secret(self):
        backend = self.load()
        self.assertEqual(backend.api_key_file, self.key)
        self.assertEqual(backend.ca_file, self.ca)
        self.assertEqual(
            backend.to_plan(),
            {
                "schema_version": 2,
                "kind": "https",
                "origin": self.value["origin"],
                "api_revision": REVISION,
                "catalog_manifest_sha256": MANIFEST,
                "conversation_contract_version": 4,
            },
        )
        self.assertNotIn(API_KEY, repr(backend))
        with self.assertRaises(FrozenInstanceError):
            backend.origin = "https://other.example.test"

    def test_unknown_missing_duplicate_and_untyped_fields_are_rejected(self):
        original = dict(self.value)
        bad_values = [
            {**original, "unknown": True},
            {k: v for k, v in original.items() if k != "ca_file"},
            {**original, "schema_version": True},
            {**original, "schema_version": 2},
            {**original, "api_revision": "development"},
            {**original, "catalog_manifest_sha256": "f" * 40},
            [],
        ]
        for value in bad_values:
            with self.subTest(value=value):
                self.write_config(value)
                with self.assertRaisesRegex(
                    remote.PublicEntryRemoteError, "config_invalid$"
                ):
                    self.load()
        self.config_path.write_text(
            json.dumps(original)[:-1] + ',"origin":"https://duplicate.test"}'
        )
        with self.assertRaisesRegex(remote.PublicEntryRemoteError, "config_invalid$"):
            self.load()

    def test_only_a_canonical_https_origin_is_allowed(self):
        for origin in (
            "http://backend.example.test",
            "https://backend.example.test/",
            "https://backend.example.test/v1",
            "https://backend.example.test?next=other",
            "https://backend.example.test#fragment",
            "https://user:password@backend.example.test",
            "https://BACKEND.EXAMPLE.TEST",
            "https://backend.example.test:443",
            "https://backend.example.test:0",
            "https://backend.example.test\\@attacker.test",
            "https://backend.example.test\n",
            443,
        ):
            with self.subTest(origin=origin):
                self.value["origin"] = origin
                self.write_config()
                with self.assertRaisesRegex(
                    remote.PublicEntryRemoteError, "config_invalid$"
                ):
                    self.load()

    def test_private_inputs_reject_world_readable_parent_and_linked_files(self):
        for path in (self.config_path, self.key, self.ca):
            with self.subTest(path=path):
                path.chmod(0o644)
                with self.assertRaisesRegex(
                    remote.PublicEntryRemoteError, "config_invalid$"
                ):
                    self.load()
                path.chmod(0o600)
        self.root.chmod(0o755)
        with self.assertRaisesRegex(remote.PublicEntryRemoteError, "config_invalid$"):
            self.load()
        self.root.chmod(0o700)
        linked = self.root / "linked"
        linked.symlink_to(self.key)
        self.value["api_key_file"] = str(linked)
        self.write_config()
        with self.assertRaisesRegex(remote.PublicEntryRemoteError, "config_invalid$"):
            self.load()
        linked.unlink()
        os.link(self.key, linked)
        self.value["api_key_file"] = str(self.key)
        self.write_config()
        with self.assertRaisesRegex(remote.PublicEntryRemoteError, "config_invalid$"):
            self.load()

    def test_repository_paths_and_shared_credential_paths_are_rejected(self):
        with self.assertRaisesRegex(remote.PublicEntryRemoteError, "config_invalid$"):
            remote.load_remote_backend(self.config_path, repo_root=self.root)
        self.value["ca_file"] = str(self.key)
        self.write_config()
        with self.assertRaisesRegex(remote.PublicEntryRemoteError, "config_invalid$"):
            self.load()

    def test_bad_key_and_non_certificate_ca_are_rejected_with_closed_errors(self):
        for key in ("", "PRIVATE_CREDENTIAL", "a" * 63, "a" * 64 + "\n\n", "A" * 64):
            with self.subTest(key_length=len(key)):
                self.key.write_text(key)
                with self.assertRaisesRegex(
                    remote.PublicEntryRemoteError, "config_invalid$"
                ) as caught:
                    self.load()
                self.assertNotIn("PRIVATE_CREDENTIAL", str(caught.exception))
        self.key.write_text(API_KEY)
        for ca in (
            "",
            "not a certificate",
            "-----BEGIN CERTIFICATE-----\nbroken\n-----END CERTIFICATE-----",
        ):
            with self.subTest(ca_length=len(ca)):
                self.ca.write_text(ca)
                with self.assertRaisesRegex(
                    remote.PublicEntryRemoteError, "config_invalid$"
                ):
                    self.load()


class RemoteAdmissionTests(RemoteFixture):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.result = SimpleNamespace(
            returncode=0, stdout=json.dumps(remote.PROOF).encode(), stderr=b""
        )

    def runner(self, argv, **options):
        self.calls.append((argv, options))
        return self.result

    def test_host_probe_is_version_only_and_environment_does_not_inherit_secrets(self):
        profile = self.profile()
        before = copy.deepcopy(profile)
        with patch.dict(
            os.environ, {"HTTPS_PROXY": "http://attacker", "SECRET": API_KEY}
        ):
            self.assertEqual(
                remote.verify_remote_backend(profile, runner=self.runner), remote.PROOF
            )
        self.assertEqual(profile, before)
        self.assertEqual(len(self.calls), 1)
        argv, options = self.calls[0]
        self.assertEqual(argv[1:5], ["-E", "-s", "-B", "-c"])
        self.assertEqual(argv[-2:], [REVISION, MANIFEST])
        self.assertIn('"GET", "/v1/version"', argv[5])
        self.assertNotIn("/v1/turn", argv[5])
        self.assertNotIn(API_KEY, repr((argv, options)))
        self.assertEqual(
            options["env"],
            {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "WEB_UPSTREAM": self.value["origin"],
                "WEB_UPSTREAM_CA_FILE": str(self.ca),
            },
        )
        self.assertEqual(options["timeout"], remote.COMMAND_TIMEOUT)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)

    def test_container_probe_uses_mounted_credentials_without_host_reads(self):
        profile = self.profile()
        compose = self.root / "compose.json"
        daemon_environment = {
            "DOCKER_HOST": "unix:///tmp/fixture-rootless-docker.sock",
            "DOCKER_CONTEXT": "fixture-rootless",
            "DOCKER_CONFIG": str(self.root / "docker-config"),
            "DOCKER_TLS_VERIFY": "1",
            "DOCKER_CERT_PATH": str(self.root / "docker-certs"),
        }
        with patch.dict(
            os.environ,
            {**daemon_environment, "HTTPS_PROXY": "http://attacker", "SECRET": API_KEY},
            clear=True,
        ):
            with patch.object(
                remote.public_entry,
                "_read_private",
                side_effect=AssertionError("host credential read"),
            ):
                result = remote.verify_remote_backend(
                    profile, runner=self.runner, compose_file=compose
                )
        self.assertEqual(result, remote.PROOF)
        argv, options = self.calls[0]
        self.assertEqual(
            argv[:12],
            [
                "docker",
                "compose",
                "-f",
                str(compose),
                "exec",
                "-T",
                "public-web",
                "python",
                "-E",
                "-s",
                "-B",
                "-c",
            ],
        )
        self.assertIn(repr(remote.API_KEY_TARGET), argv[12])
        self.assertNotIn(str(self.key), argv[12])
        self.assertEqual(
            options["env"],
            {"PATH": "/usr/local/bin:/usr/bin:/bin", **daemon_environment},
        )

    def test_invalid_plan_never_runs_a_probe(self):
        original = self.profile()
        for change in (
            {"schema_version": True},
            {"schema_version": 1},
            {"kind": "http"},
            {"origin": "http://backend.example.test"},
            {"api_revision": "old"},
            {"catalog_manifest_sha256": "bad"},
            {"unexpected": "value"},
            {"conversation_contract_version": 1},
            {"conversation_contract_version": 2},
            {"conversation_contract_version": 3},
            {"conversation_contract_version": True},
            {"conversation_contract_version": "4"},
            {"conversation_contract_version": 4.0},
        ):
            value = copy.deepcopy(original)
            value["x-depositar-plan"]["private_backend"].update(change)
            with self.subTest(change=change):
                with self.assertRaisesRegex(
                    remote.PublicEntryRemoteError, "plan_invalid$"
                ):
                    remote.verify_remote_backend(value, runner=self.runner)
        self.assertEqual(self.calls, [])

    def test_mutated_environment_or_network_cannot_probe_an_unsealed_backend(self):
        mutations = (
            lambda p: p["services"]["public-web"]["environment"].update(
                WEB_UPSTREAM="https://attacker.test"
            ),
            lambda p: p["services"]["public-web"]["environment"].update(
                WEB_UPSTREAM_CA_FILE="/other-ca"
            ),
            lambda p: p["services"]["public-web"]["environment"].update(
                NVIDIA_VISIBLE_DEVICES="all"
            ),
            lambda p: p["services"]["public-web"].update(runtime="nvidia"),
            lambda p: p["services"]["public-web"].update(ports=["8000:8000"]),
            lambda p: p["services"]["public-web"]["networks"].update(
                {"gpu-private": {}}
            ),
            lambda p: p["networks"].update({"existing-release": {"external": True}}),
            lambda p: p["networks"]["website"].update(internal=False),
        )
        for mutate in mutations:
            profile = self.profile()
            mutate(profile)
            with self.assertRaisesRegex(remote.PublicEntryRemoteError, "plan_invalid$"):
                remote.verify_remote_backend(profile, runner=self.runner)
        self.assertEqual(self.calls, [])

    def test_missing_duplicate_mutable_or_symlink_credential_mount_is_rejected(self):
        for target in (remote.API_KEY_TARGET, remote.CA_TARGET):
            for mutation in (
                "missing",
                "duplicate",
                "writable",
                "volume",
                "relative",
                "symlink",
            ):
                profile = self.profile()
                mounts = profile["services"]["public-web"]["volumes"]
                mount = next(m for m in mounts if m["target"] == target)
                if mutation == "missing":
                    mounts.remove(mount)
                elif mutation == "duplicate":
                    mounts.append(dict(mount))
                elif mutation == "writable":
                    mount["read_only"] = False
                elif mutation == "volume":
                    mount["type"] = "volume"
                elif mutation == "relative":
                    mount["source"] = "relative"
                else:
                    linked = self.root / "linked"
                    linked.symlink_to(mount["source"])
                    mount["source"] = str(linked)
                with self.subTest(target=target, mutation=mutation):
                    with self.assertRaisesRegex(
                        remote.PublicEntryRemoteError, "plan_invalid$"
                    ):
                        remote.verify_remote_backend(profile, runner=self.runner)
                (self.root / "linked").unlink(missing_ok=True)
        self.assertEqual(self.calls, [])

    def test_unbounded_malformed_or_untyped_proof_is_rejected(self):
        profile = self.profile()
        values = [
            b"not json",
            b"{}",
            json.dumps({**remote.PROOF, "extra": True}).encode(),
            json.dumps({**remote.PROOF, "backend_identity_verified": 1}).encode(),
            json.dumps(
                {**remote.PROOF, "authenticated_version_status": 200.0}
            ).encode(),
        ]
        for value in values:
            self.result.stdout = value
            with self.assertRaisesRegex(remote.PublicEntryRemoteError, "probe_failed$"):
                remote.verify_remote_backend(profile, runner=self.runner)
        self.result.stdout = b"SENSITIVE" * remote.MAX_OUTPUT_BYTES
        with self.assertRaisesRegex(remote.PublicEntryRemoteError, "output_limit$"):
            remote.verify_remote_backend(profile, runner=self.runner)

    def test_runner_failures_use_fixed_diagnostics(self):
        profile = self.profile()
        for error in (
            OSError("PRIVATE_CREDENTIAL"),
            subprocess.TimeoutExpired("PRIVATE_CREDENTIAL", 15),
            remote.image_build.ImageBuildError("PRIVATE_CREDENTIAL"),
        ):
            with patch.object(remote.image_build, "_run", side_effect=error):
                with self.assertRaisesRegex(
                    remote.PublicEntryRemoteError, "probe_failed$"
                ) as caught:
                    remote.verify_remote_backend(profile)
                self.assertNotIn("PRIVATE_CREDENTIAL", str(caught.exception))
        self.result.returncode = 1
        self.result.stderr = b"PRIVATE_CREDENTIAL"
        with self.assertRaisesRegex(remote.PublicEntryRemoteError, "probe_failed$"):
            remote.verify_remote_backend(profile, runner=self.runner)

    def test_real_process_uses_existing_live_bounded_capture(self):
        with patch.object(remote.image_build, "_run", return_value=self.result) as run:
            remote.verify_remote_backend(self.profile())
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["timeout"], remote.COMMAND_TIMEOUT)


class RemoteTLSProbeTests(RemoteFixture):
    def setUp(self):
        super().setUp()
        self.requests = []
        self.mode = "valid"
        case = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                authenticated = self.headers.get("Authorization") == "Bearer " + API_KEY
                case.requests.append((self.command, self.path, authenticated))
                if case.mode == "redirect":
                    status, raw = 302, b"PRIVATE_RESPONSE"
                elif case.mode == "anonymous_allowed" and not authenticated:
                    status, raw = 200, b"{}"
                elif not authenticated:
                    status, raw = 401, b"{}"
                else:
                    status = 200
                    value = {
                        "revision": REVISION if case.mode != "revision" else "f" * 40,
                        "catalog_manifest_sha256": MANIFEST
                        if case.mode != "manifest"
                        else "f" * 64,
                        "conversation_contract_version": 4
                        if case.mode != "contract"
                        else 1,
                    }
                    if case.mode == "legacy_contract":
                        value.pop("conversation_contract_version")
                    elif case.mode == "old_contract":
                        value["conversation_contract_version"] = 2
                    elif case.mode == "untyped_contract":
                        value["conversation_contract_version"] = 4.0
                    raw = json.dumps(value).encode()
                    if case.mode == "oversize":
                        raw = b"PRIVATE_RESPONSE" * 2000
                    elif case.mode == "invalid_json":
                        raw = b"PRIVATE_RESPONSE"
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            str(self.cert_root / "ca.pem"), str(self.cert_root / "server.key")
        )
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.value["origin"] = "https://localhost:" + str(self.server.server_port)
        self.write_config()

    def test_real_tls_authentication_and_identity_probe_never_submits_a_turn(self):
        self.assertEqual(remote.verify_remote_backend(self.profile()), remote.PROOF)
        self.assertEqual(
            self.requests,
            [
                ("GET", "/v1/version", False),
                ("GET", "/v1/version", True),
            ],
        )

    def test_real_tls_probe_rejects_redirect_anonymous_access_and_identity_changes(
        self,
    ):
        profile = self.profile()
        for mode in (
            "redirect",
            "anonymous_allowed",
            "revision",
            "manifest",
            "contract",
            "old_contract",
            "legacy_contract",
            "untyped_contract",
            "oversize",
            "invalid_json",
        ):
            self.mode = mode
            self.requests.clear()
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(
                    remote.PublicEntryRemoteError, "probe_failed$"
                ) as caught:
                    remote.verify_remote_backend(profile)
                self.assertNotIn("PRIVATE_RESPONSE", str(caught.exception))
                self.assertTrue(self.requests)
                self.assertTrue(
                    all(
                        method == "GET" and path == "/v1/version"
                        for method, path, _ in self.requests
                    )
                )

    def test_wrong_hostname_fails_before_credentials_reach_the_server(self):
        profile = self.profile()
        origin = self.value["origin"].replace("localhost", "127.0.0.1")
        profile["x-depositar-plan"]["private_backend"]["origin"] = origin
        profile["services"]["public-web"]["environment"]["WEB_UPSTREAM"] = origin
        with self.assertRaisesRegex(remote.PublicEntryRemoteError, "probe_failed$"):
            remote.verify_remote_backend(profile)
        self.assertEqual(self.requests, [])

    def test_untrusted_certificate_fails_before_credentials_reach_the_server(self):
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(self.root / "untrusted.key"),
                "-out",
                str(self.ca),
                "-days",
                "2",
                "-subj",
                "/CN=Unrelated disposable CA",
            ],
            capture_output=True,
            check=True,
            timeout=15,
            umask=0o077,
        )
        with self.assertRaisesRegex(remote.PublicEntryRemoteError, "probe_failed$"):
            remote.verify_remote_backend(self.profile())
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
