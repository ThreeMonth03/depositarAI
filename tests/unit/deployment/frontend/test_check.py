"""Read-only runtime admission and real TLS version-only probe regressions."""

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.deployment.frontend import check
from search.operations.deployment.frontend import policy
from search.operations.deployment.frontend.access import ACCESS_TARGET, prepare_access


FRONT = "1" * 40
API = "2" * 40
CATALOG = "3" * 64
WEB_IMAGE = "sha256:" + "4" * 64
NGINX_IMAGE = "sha256:" + "5" * 64
IDS = {"web": "6" * 64, "web-nginx": "7" * 64}
PROOF = {
    "authenticated_version_status": 200,
    "backend_identity_verified": True,
    "unauthenticated_version_status": 401,
    "upstream_tls_verified": True,
}


class Runner:
    def __init__(self, root, *, mode="private"):
        self.mode = mode
        self.project = policy.PROJECTS[mode]
        self.mounted_acl = None
        address, port = (
            ("10.20.30.40", "18082")
            if mode == "intranet-http"
            else ("127.0.0.1", "18081")
        )
        self.calls = []
        self.environments = []
        self.proof = PROOF
        self.config = {
            "name": self.project,
            "services": {},
            "networks": {
                name: {
                    "name": self.project + "_" + name,
                    "internal": name == "website",
                }
                for name in ("website", "ingress", "upstream-route")
            },
        }
        self.rows, self.images, self.networks = {}, {}, {}
        for role, image_id in (("web", WEB_IMAGE), ("web-nginx", NGINX_IMAGE)):
            web = role == "web"
            environment = (
                {
                    "WEB_ORIGIN": f"http://{address}:{port}",
                    "WEB_UPSTREAM": "https://backend.test",
                    "WEB_UPSTREAM_CA_FILE": "",
                    "NVIDIA_VISIBLE_DEVICES": "void",
                }
                if web
                else {}
            )
            if mode == "intranet-http":
                environment.update(
                    {"WEB_ACCESS_MODE": mode}
                    if web
                    else {"WEB_ALLOWED_CIDRS": "10.20.30.0/24"}
                )
            mounts = (
                [
                    {
                        "type": "bind",
                        "source": str(root / "api_key"),
                        "target": "/run/secrets/api_key",
                        "read_only": True,
                        "bind": {"create_host_path": False},
                    }
                ]
                if web
                else []
            )
            if mode == "intranet-http" and not web:
                mounts = [
                    {
                        "type": "bind",
                        "source": str(root / "intranet-allow.conf"),
                        "target": ACCESS_TARGET,
                        "read_only": True,
                        "bind": {"create_host_path": False},
                    }
                ]
            network_names = ["website", "upstream-route" if web else "ingress"]
            service = {
                "image": (
                    "depositar-search-web:" if web else "depositar-search-web-nginx:"
                )
                + FRONT,
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "ports": []
                if web
                else [
                    {
                        "host_ip": address,
                        "published": port,
                        "target": 8080,
                        "protocol": "tcp",
                    }
                ],
                "environment": environment,
                "user": f"{os.geteuid()}:{os.getegid()}",
                "mem_limit": 512 * 1024**2 if web else 64 * 1024**2,
                "memswap_limit": 512 * 1024**2 if web else 64 * 1024**2,
                "cpus": 1 if web else 0.5,
                "pids_limit": 64,
                "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777"],
                "volumes": mounts,
                "networks": {name: {} for name in network_names},
            }
            self.config["services"][role] = service
            image_config = {
                "Env": ["PATH=/usr/bin"]
                + (["SOURCE_REVISION=" + FRONT] if web else []),
                "Labels": {
                    "org.opencontainers.image.revision": FRONT,
                    "io.depositar.ai.public-entry.version": "2",
                    **(
                        {"io.depositar.ai.conversation.version": "4"}
                        if web
                        else {"io.depositar.ai.frontend.version": "1"}
                    ),
                },
                "Cmd": ["python", "-m", "uvicorn"] if web else ["-g", "daemon off;"],
                "Entrypoint": None if web else ["nginx"],
                "WorkingDir": "/app" if web else "",
            }
            descriptor = {
                "digest": image_id,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "platform": {"architecture": "amd64", "os": "linux"},
            }
            self.images[image_id] = {
                "Id": image_id,
                "Config": image_config,
                "Os": "linux",
                "Architecture": "amd64",
                "Descriptor": descriptor,
            }
            container_config = copy.deepcopy(image_config)
            container_config["Env"] += [k + "=" + v for k, v in environment.items()]
            container_config["User"] = service["user"]
            container_config["Labels"].update(
                {
                    "com.docker.compose.project": self.project,
                    "com.docker.compose.service": role,
                    "com.docker.compose.project.config_files": "/older/frontend/checkout/deploy/compose/compose.search-web.yaml",
                }
            )
            ports = {} if web else {"8080/tcp": [{"HostIp": address, "HostPort": port}]}
            self.rows[IDS[role]] = {
                "Id": IDS[role],
                "Image": "sha256:" + ("8" if web else "9") * 64,
                "ImageManifestDescriptor": copy.deepcopy(descriptor),
                "Config": container_config,
                "State": {
                    "Running": True,
                    "Paused": False,
                    "Restarting": False,
                    "Dead": False,
                    "Health": {"Status": "healthy"},
                },
                "HostConfig": {
                    "ReadonlyRootfs": True,
                    "Privileged": False,
                    "CapDrop": ["ALL"],
                    "CapAdd": None,
                    "SecurityOpt": ["no-new-privileges:true"],
                    "PidMode": "",
                    "IpcMode": "private",
                    "Devices": [],
                    "DeviceRequests": None,
                    "Runtime": "runc",
                    "Memory": service["mem_limit"],
                    "MemorySwap": service["memswap_limit"],
                    "NanoCpus": int(service["cpus"] * 1_000_000_000),
                    "PidsLimit": 64,
                    "Tmpfs": {"/tmp": service["tmpfs"][0].split(":", 1)[1]},
                    "NetworkMode": self.project + "_website",
                    "PortBindings": ports,
                },
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": m["source"],
                        "Destination": m["target"],
                        "RW": False,
                    }
                    for m in mounts
                ],
                "NetworkSettings": {
                    "Ports": ports,
                    "Networks": {
                        self.project + "_" + name: {"NetworkID": name}
                        for name in network_names
                    },
                },
            }
        if mode == "intranet-http":
            prepare_access(self.config)
            self.mounted_acl = (root / "intranet-allow.conf").read_bytes()
        self.locators = {
            service["image"]: manifest
            for service, manifest in (
                (self.config["services"]["web"], WEB_IMAGE),
                (self.config["services"]["web-nginx"], NGINX_IMAGE),
            )
        }
        for name, value in self.config["networks"].items():
            self.networks[value["name"]] = {
                "Id": name,
                "Name": value["name"],
                "Driver": "bridge",
                "Internal": name == "website",
                "Labels": {
                    "com.docker.compose.project": self.project,
                    "com.docker.compose.network": name,
                },
                "Containers": {
                    IDS[role]: {}
                    for role in IDS
                    if name in self.config["services"][role]["networks"]
                },
            }

    def __call__(self, argv, *, environment, timeout):
        self.calls.append(list(argv))
        self.environments.append(dict(environment))
        self.assert_local(argv, timeout)
        args = argv[5:]
        if args[0] == "compose":
            if args[-3:] != ["config", "--format", "json"]:
                raise AssertionError("mutating compose operation")
            value = self.config
        elif args[0] == "ps":
            return SimpleNamespace(
                returncode=0, stdout=("\n".join(self.rows) + "\n").encode(), stderr=b""
            )
        elif args[0] == "inspect":
            value = self.rows[args[-1]]
        elif args[:2] == ["image", "inspect"]:
            if args[2:4] != ["--platform", "linux/amd64"]:
                raise AssertionError("platform identity was not selected explicitly")
            value = self.images[self.locators[args[-1]]]
        elif args[:2] == ["network", "inspect"]:
            value = self.networks[args[-1]]
        elif args[0] == "exec":
            if args[1:] == [IDS["web-nginx"], "cat", ACCESS_TARGET]:
                return SimpleNamespace(
                    returncode=0, stdout=self.mounted_acl, stderr=b""
                )
            if args[2:7] != ["python", "-E", "-s", "-B", "-c"] or args[-2:] != [
                API,
                CATALOG,
            ]:
                raise AssertionError("unexpected container command")
            if "/v1/turn" in args[7] or "FastAPI" in args[7]:
                raise AssertionError("inference or app import")
            value = self.proof
        else:
            raise AssertionError("unapproved docker operation")
        return SimpleNamespace(
            returncode=0, stdout=json.dumps(value).encode(), stderr=b""
        )

    @staticmethod
    def assert_local(argv, timeout):
        if (
            argv[:3] != ["docker", "--host", "unix:///var/run/docker.sock"]
            or argv[3] != "--config"
            or not 0 < timeout <= 30
        ):
            raise AssertionError("unbounded or remote client")


class FrontendCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="frontend-check-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = self.root / "frontend.env"
        self.env.write_text("WEB_UID=1003\nWEB_GID=1003\n")
        self.env.chmod(0o600)
        self.config = {
            "schema_version": 3,
            "deployment_mode": "private",
            "conversation_contract_version": 4,
            "frontend_env_file": str(self.env),
            "frontend_revision": FRONT,
            "web_platform_manifest": WEB_IMAGE,
            "web_nginx_platform_manifest": NGINX_IMAGE,
            "api_revision": API,
            "catalog_manifest_sha256": CATALOG,
        }
        self.path = self.root / "check.json"
        self.save()
        self.runner = Runner(self.root)

    def save(self):
        self.path.write_text(json.dumps(self.config))
        self.path.chmod(0o600)

    def run_check(self):
        with patch.object(check, "_homepage", return_value=2):
            return check.check(self.path, runner=self.runner)

    def test_current_schema_checks_platform_identity_and_exact_contract(self):
        result = self.run_check()
        self.assertEqual(result["schema_version"], 3)
        self.assertEqual(result["conversation_contract_version"], 4)
        self.assertEqual(result["deployment_mode"], "private")
        self.assertEqual(result["runtime"]["web"]["platform_manifest"], WEB_IMAGE)
        self.assertNotEqual(
            result["runtime"]["web"]["container_image_index"], WEB_IMAGE
        )
        source = next(c[c.index("-c") + 1] for c in self.runner.calls if "exec" in c)
        self.assertIn("EXPECTED_CONTRACT_VERSION=4\n", source)

    def test_profile_rejects_legacy_schema_protocols_and_unknown_fields(self):
        for key, value in (
            ("schema_version", 1),
            ("schema_version", 2),
            ("schema_version", True),
            ("deployment_mode", "public"),
            ("deployment_mode", None),
            ("conversation_contract_version", 3),
            ("conversation_contract_version", True),
            ("conversation_contract_version", 4.0),
            ("conversation_contract_version", "4"),
            ("extra", "refuse"),
        ):
            original = dict(self.config)
            self.config[key] = value
            self.save()
            with (
                self.subTest(key=key, value=value),
                self.assertRaises(check.FrontendCheckError),
            ):
                check.load_config(self.path)
            self.config = original
        for key in tuple(self.config):
            value = self.config.pop(key)
            self.save()
            with self.subTest(missing=key), self.assertRaises(check.FrontendCheckError):
                check.load_config(self.path)
            self.config[key] = value
        self.save()

    def test_immutable_role_and_contract_labels_are_required_before_probe(self):
        for role, label, value in (
            ("web", "io.depositar.ai.conversation.version", "3"),
            ("web", "io.depositar.ai.conversation.version", None),
            ("web", "io.depositar.ai.conversation.version", 4),
            ("web", "io.depositar.ai.public-entry.version", None),
            ("web-nginx", "io.depositar.ai.frontend.version", None),
            ("web-nginx", "org.opencontainers.image.revision", "f" * 40),
        ):
            self.runner = Runner(self.root)
            image = self.runner.images[WEB_IMAGE if role == "web" else NGINX_IMAGE]
            image["Config"]["Labels"][label] = value
            with (
                self.subTest(role=role, label=label),
                self.assertRaises(check.FrontendCheckError),
            ):
                self.run_check()
            self.assertFalse(any("exec" in c for c in self.runner.calls))

    def test_container_and_locator_platform_descriptors_must_match_pins(self):
        for target in ("container", "image"):
            for mutation in ("digest", "platform", "mediaType", "missing"):
                self.runner = Runner(self.root)
                obj, key = (
                    (self.runner.rows[IDS["web"]], "ImageManifestDescriptor")
                    if target == "container"
                    else (self.runner.images[WEB_IMAGE], "Descriptor")
                )
                if mutation == "missing":
                    obj.pop(key)
                else:
                    obj[key][mutation] = (
                        {"architecture": "arm64", "os": "linux"}
                        if mutation == "platform"
                        else "sha256:" + "f" * 64
                    )
                with (
                    self.subTest(target=target, mutation=mutation),
                    self.assertRaises(check.FrontendCheckError),
                ):
                    self.run_check()
                self.assertFalse(any("exec" in c for c in self.runner.calls))

    def test_loaded_image_descriptor_may_omit_platform_after_image_verification(self):
        for image in self.runner.images.values():
            image["Descriptor"].pop("platform")
        result = self.run_check()
        self.assertEqual(result["status"], "frontend_checked")
        self.assertEqual(result["runtime"]["web"]["platform_manifest"], WEB_IMAGE)
        self.assertEqual(
            result["runtime"]["web-nginx"]["platform_manifest"], NGINX_IMAGE
        )
        self.assertTrue(
            all(
                "platform" not in image["Descriptor"]
                for image in self.runner.images.values()
            )
        )

    def test_loaded_image_without_platform_still_requires_body_and_manifest_identity(
        self,
    ):
        mutations = (
            ("Os", "windows"),
            ("Architecture", "arm64"),
            ("Id", "sha256:" + "f" * 64),
            ("digest", "sha256:" + "f" * 64),
            ("mediaType", "application/vnd.oci.image.index.v1+json"),
        )
        for field, value in mutations:
            self.runner = Runner(self.root)
            image = self.runner.images[WEB_IMAGE]
            descriptor = image["Descriptor"]
            descriptor.pop("platform")
            (descriptor if field in ("digest", "mediaType") else image)[field] = value
            with self.subTest(field=field), self.assertRaises(check.FrontendCheckError):
                self.run_check()
            self.assertFalse(any("exec" in c for c in self.runner.calls))

    def test_container_platform_is_required_and_explicit_image_platform_is_strict(self):
        for target, platform in (
            ("container", "absent"),
            ("image", None),
            ("image", {}),
            ("image", {"architecture": "arm64", "os": "linux"}),
            ("image", {"architecture": "amd64", "os": "linux", "variant": "v3"}),
        ):
            self.runner = Runner(self.root)
            descriptor = (
                self.runner.rows[IDS["web"]]["ImageManifestDescriptor"]
                if target == "container"
                else self.runner.images[WEB_IMAGE]["Descriptor"]
            )
            if platform == "absent":
                descriptor.pop("platform")
            else:
                descriptor["platform"] = platform
            with (
                self.subTest(target=target, platform=platform),
                self.assertRaises(check.FrontendCheckError),
            ):
                self.run_check()
            self.assertFalse(any("exec" in c for c in self.runner.calls))

    def test_intranet_uses_shared_policy_mount_and_explicit_project(self):
        self.runner = Runner(self.root, mode="intranet-http")
        self.config["deployment_mode"] = "intranet-http"
        self.save()
        result = self.run_check()
        self.assertEqual(result["project"], "depositar-search-intranet")
        self.assertEqual(result["origin"], "http://10.20.30.40:18082")
        self.assertEqual(result["deployment_mode"], "intranet-http")
        commands = [c[5:] for c in self.runner.calls]
        self.assertTrue(
            all("depositar-search-intranet" in c for c in commands if c[0] == "compose")
        )
        self.assertEqual(2, sum(c[0] == "exec" and "cat" in c for c in commands))
        self.assertIn(
            str(check.ROOT / "deploy/compose/compose.search-web.intranet.yaml"),
            commands[0],
        )

    def test_intranet_rejects_origin_bind_and_host_or_mounted_acl_drift(self):
        for mutation in ("origin", "bind", "host-acl", "mounted-acl", "writable-acl"):
            self.runner = Runner(self.root, mode="intranet-http")
            self.config["deployment_mode"] = "intranet-http"
            self.save()
            services = self.runner.config["services"]
            if mutation == "origin":
                services["web"]["environment"]["WEB_ORIGIN"] = "http://8.8.8.8:18082"
            elif mutation == "bind":
                services["web-nginx"]["ports"][0]["host_ip"] = "0.0.0.0"
            elif mutation == "host-acl":
                path = self.root / "intranet-allow.conf"
                path.chmod(0o600)
                path.write_text("allow all;\n")
            elif mutation == "mounted-acl":
                self.runner.mounted_acl = b"allow all;\n"
            else:
                self.runner.rows[IDS["web-nginx"]]["Mounts"][0]["RW"] = True
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(check.FrontendCheckError),
            ):
                self.run_check()
            self.assertFalse(any("-c" in c for c in self.runner.calls))

    def test_postflight_locator_contract_and_acl_changes_reject_success(self):
        for mutation in ("contract", "manifest", "acl"):
            self.runner = Runner(self.root, mode="intranet-http")
            self.config["deployment_mode"] = "intranet-http"
            self.save()
            original = self.runner.__call__

            def runner(argv, **kwargs):
                result = original(argv, **kwargs)
                if argv[5] == "exec" and "-c" in argv:
                    if mutation == "contract":
                        self.runner.images[WEB_IMAGE]["Config"]["Labels"][
                            "io.depositar.ai.conversation.version"
                        ] = "3"
                    elif mutation == "manifest":
                        self.runner.images[WEB_IMAGE]["Descriptor"]["digest"] = (
                            "sha256:" + "f" * 64
                        )
                    else:
                        self.runner.mounted_acl = b"allow all;\n"
                return result

            with (
                patch.object(check, "_homepage", return_value=2),
                self.subTest(mutation=mutation),
                self.assertRaises(check.FrontendCheckError),
            ):
                check.check(self.path, runner=runner)

    def test_exact_deployment_without_mutation_or_host_key_read(
        self,
    ):
        # The key does not exist on this host; only the existing BFF can read it.
        with patch.dict(
            os.environ,
            {
                "SOURCE_REVISION": "f" * 40,
                "DOCKER_HOST": "tcp://attacker:2375",
                "COMPOSE_FILE": "/poison",
                "WEB_UPSTREAM": "https://attacker",
                "HTTPS_PROXY": "http://attacker",
                "PYTHONPATH": "/poison",
            },
        ):
            result = self.run_check()
        self.assertEqual(result["frontend_revision"], FRONT)
        self.assertEqual(result["runtime"]["web"]["platform_manifest"], WEB_IMAGE)
        self.assertFalse(result["deployment_performed"])
        self.assertFalse(result["public_readiness_verified"])
        self.assertFalse(result["model_inference_verified"])
        self.assertTrue(
            all(
                set(e) == {"PATH", "LANG", "LC_ALL", "SOURCE_REVISION"}
                and e["SOURCE_REVISION"] == FRONT
                for e in self.runner.environments
            )
        )
        self.assertEqual(sum("exec" in c for c in self.runner.calls), 1)

    def test_compose_decimal_resource_strings_match_exact_inspect_integers(self):
        for service in self.runner.config["services"].values():
            for key in ("mem_limit", "memswap_limit", "pids_limit"):
                service[key] = str(service[key])
        self.assertEqual(self.run_check()["status"], "frontend_checked")
        self.runner.config["services"]["web"]["mem_limit"] = "536870913"
        with self.assertRaises(check.FrontendCheckError):
            self.run_check()

    def test_compose_invalid_resource_representations_are_rejected(self):
        for value in (
            "-1",
            "0",
            "1e3",
            "512M",
            " 536870912",
            "+536870912",
            "0536870912",
            "9223372036854775808",
            536870912.0,
            True,
            None,
        ):
            with self.subTest(value=value):
                self.runner.config["services"]["web"]["mem_limit"] = value
                with self.assertRaises(check.FrontendCheckError):
                    self.run_check()

    def test_runtime_changes_and_poisoned_container_environment_reject_before_probe(
        self,
    ):
        changes = [
            lambda r: r["ImageManifestDescriptor"].update(digest="sha256:" + "e" * 64),
            lambda r: r["Config"]["Env"].append("SSL_CERT_FILE=/poison"),
            lambda r: r["Config"].update(User="0:0"),
            lambda r: r["HostConfig"].update(ReadonlyRootfs=False),
            lambda r: r["HostConfig"].update(CapAdd=["SYS_ADMIN"]),
            lambda r: r["HostConfig"].update(
                PortBindings={"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8000"}]}
            ),
        ]
        for change in changes:
            with self.subTest(change=change):
                self.runner = Runner(self.root)
                change(self.runner.rows[IDS["web"]])
                with self.assertRaises(check.FrontendCheckError):
                    self.run_check()
                self.assertFalse(any("exec" in c for c in self.runner.calls))

    def test_foreign_peer_or_missing_frontend_refuses(self):
        self.runner.networks[self.runner.project + "_website"]["Containers"][
            "f" * 64
        ] = {}
        with self.assertRaises(check.FrontendCheckError):
            self.run_check()
        self.runner.rows.pop(IDS["web-nginx"])
        with self.assertRaises(check.FrontendCheckError):
            self.run_check()

    def test_private_config_strict_fields_and_permissions(self):
        self.config["schema_version"] = True
        self.save()
        with self.assertRaises(ValueError):
            check.load_config(self.path)
        self.config["schema_version"] = 3
        self.save()
        self.env.chmod(0o644)
        with self.assertRaises(ValueError):
            check.load_config(self.path)

    def test_postflight_image_drift_refuses_success_receipt(self):
        original = self.runner.__call__

        def runner(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[5] == "exec":
                self.runner.rows[IDS["web"]]["Image"] = "sha256:" + "f" * 64
            return result

        with patch.object(check, "_homepage", return_value=2):
            with self.assertRaises(check.FrontendCheckError):
                check.check(self.path, runner=runner)

    def test_failed_probe_or_cli_error_never_reflects_diagnostics(self):
        self.runner.proof = {"error": "DO_NOT_REFLECT_SECRET"}
        with self.assertRaises(check.FrontendCheckError):
            self.run_check()
        output = io.StringIO()
        with (
            patch.object(
                check, "check", side_effect=ValueError("DO_NOT_REFLECT_SECRET")
            ),
            patch("sys.stdout", output),
        ):
            self.assertEqual(check.main(["--config", str(self.path)]), 1)
        self.assertEqual(
            json.loads(output.getvalue()), {"error": "frontend_check_failed"}
        )


class HomepageFixtureTests(unittest.TestCase):
    def test_real_home_and_assets_gets_only_and_reject_redirect(self):
        paths = []
        redirect = False

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                body = (
                    b'<html><script src="/assets/index-fixture.js"></script><link rel="stylesheet" href="/assets/index-fixture.css"></html>'
                    if self.path == "/"
                    else b"asset"
                )
                self.send_response(302 if redirect else 200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        class Endpoint(check._Endpoint):
            def connect(self):
                self._publish_channel(
                    socket.create_connection(server.server_address, timeout=3)
                )

        composition = {
            "services": {
                "web": {"environment": {"WEB_ORIGIN": "http://10.20.30.40:18082"}},
                "web-nginx": {
                    "ports": [{"host_ip": "10.20.30.40", "published": "18082"}]
                },
            }
        }
        with patch.object(check, "_Endpoint", Endpoint):
            self.assertEqual(check._homepage(composition), 2)
            self.assertEqual(
                paths, ["/", "/assets/index-fixture.css", "/assets/index-fixture.js"]
            )
            redirect = True
            with self.assertRaises(check.FrontendCheckError):
                check._homepage(composition)


class VersionTLSFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="frontend-version-tls-")
        cls.root = Path(cls.temporary.name)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(cls.root / "server.key"),
                "-out",
                str(cls.root / "ca.pem"),
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
        )
        cls.key = cls.root / "api_key"
        cls.key.write_text("a" * 64)
        cls.key.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.calls = []
        self.mode = "valid"
        case = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                authorization = self.headers.get("Authorization")
                case.calls.append((self.command, self.path, bool(authorization)))
                if case.mode == "redirect":
                    status, body = 302, b"SENSITIVE_BODY"
                elif authorization != "Bearer " + "a" * 64:
                    status, body = 401, b"{}"
                else:
                    status = 200
                    value = {
                        "revision": API if case.mode != "revision" else "f" * 40,
                        "catalog_manifest_sha256": CATALOG
                        if case.mode != "catalog"
                        else "f" * 64,
                    }
                    if case.mode != "legacy-contract":
                        value["conversation_contract_version"] = {
                            "invalid-contract": True,
                            "unsupported-contract": 1,
                            "old-contract": 2,
                            "float-contract": 4.0,
                            "string-contract": "4",
                            "prior-contract": 3,
                        }.get(case.mode, 4)
                    body = (
                        b"x" * 16385
                        if case.mode == "oversize"
                        else json.dumps(value).encode()
                    )
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            str(self.root / "ca.pem"), str(self.root / "server.key")
        )
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def probe(self, *, hostname="localhost", ca=True, contract_version=4):
        environment = dict(
            os.environ,
            WEB_UPSTREAM=f"https://{hostname}:{self.server.server_port}",
            WEB_UPSTREAM_CA_FILE=str(self.root / "ca.pem") if ca else "",
            HTTPS_PROXY="http://127.0.0.1:1",
            SSL_CERT_FILE="/nonexistent/poison",
            SSL_CERT_DIR="/nonexistent/poison",
            PYTHONPATH="/nonexistent/poison",
        )
        return subprocess.run(
            [
                sys.executable,
                "-E",
                "-s",
                "-B",
                "-c",
                check._probe_source(
                    key_file=str(self.key), contract_version=contract_version
                ),
                API,
                CATALOG,
            ],
            env=environment,
            cwd=check.ROOT,
            capture_output=True,
            timeout=15,
        )

    def test_real_tls_hostname_and_key_with_poison_environment_only_gets_version(self):
        result = self.probe()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout), PROOF)
        self.assertEqual(
            self.calls, [("GET", "/v1/version", False), ("GET", "/v1/version", True)]
        )
        self.assertNotIn(b"a" * 64, result.stdout + result.stderr)

    def test_remote_contract_is_checked_without_requiring_matching_website_revision(
        self,
    ):
        result = self.probe(contract_version=4)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout), PROOF)
        for mode in (
            "legacy-contract",
            "unsupported-contract",
            "old-contract",
            "prior-contract",
            "invalid-contract",
            "float-contract",
            "string-contract",
        ):
            with self.subTest(mode=mode):
                self.mode = mode
                result = self.probe(contract_version=4)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(
                    json.loads(result.stdout),
                    {"error": "frontend_check_upstream_failed"},
                )

    def test_probe_generator_refuses_old_or_ambiguous_contract_selection(self):
        for value in (None, True, 1, 2, 3, 4.0, "4"):
            with self.subTest(value=value), self.assertRaises(check.FrontendCheckError):
                check._probe_source(contract_version=value)

    def test_wrong_identity_redirect_and_oversized_body_fail_without_reflection(self):
        for mode in ("revision", "catalog", "redirect", "oversize"):
            with self.subTest(mode=mode):
                self.mode = mode
                self.calls.clear()
                result = self.probe()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(
                    json.loads(result.stdout),
                    {"error": "frontend_check_upstream_failed"},
                )
                self.assertNotIn(b"SENSITIVE_BODY", result.stdout + result.stderr)
                self.assertTrue(
                    all(c[:2] == ("GET", "/v1/version") for c in self.calls)
                )
                if mode == "redirect":
                    self.assertEqual(len(self.calls), 1)

    def test_untrusted_ca_or_wrong_hostname_cannot_send_http_or_key(self):
        for options in ({"ca": False}, {"hostname": "127.0.0.1"}):
            with self.subTest(options=options):
                self.calls.clear()
                result = self.probe(**options)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(
                    json.loads(result.stdout),
                    {"error": "frontend_check_upstream_failed"},
                )
                self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
