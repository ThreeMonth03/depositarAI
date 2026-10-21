"""Activation proofs and an explicitly opted-in local Nginx bootstrap smoke."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import builtins
import copy
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from search.operations.catalog import volume_policy
from search.operations.deployment import composition
from search.operations.deployment.gateway import activation
from search.operations.shared import private_io
from search.operations.state import locked_state
from tests.unit.deployment.gateway import test_preparation as preparation_tests
from tests.unit.deployment.gateway import test_probe_runtime as native_tests
from tests.unit.deployment.gateway.test_runtime import evidence as gateway_evidence
from tests.unit.deployment.catalog_fixtures import sealed_catalog_storage
from tests.unit.deployment.release.test_host import (
    SnapshotDocker,
    evidence as host_evidence,
)

REAL_DOCKER = activation.runtime_docker.Docker
REAL_HOST = activation.release._Host
REAL_ROUTED_HOST = activation._RoutedHost
REAL_GPU_IMAGES = activation.inference._images
REAL_RELEASE_IMAGES = activation.release._images
REAL_INVENTORY = activation.runtime_docker.project_inventory
REAL_VALIDATE_IMAGE = activation.source_bundle.validate_built_image


@unittest.skipUnless(
    os.environ.get("DEPOSITAR_TEST_GATEWAY_IMAGE"),
    "native bootstrap smoke requires an explicit existing immutable Nginx image",
)
class NativeBootstrapTests(unittest.TestCase):
    def run_bootstrap(self, config):
        """No pulls, models, bridge networks, published ports, or live services."""
        image = os.environ["DEPOSITAR_TEST_GATEWAY_IMAGE"]
        self.assertRegex(image, r"\Asha256:[0-9a-f]{64}\Z")
        uid, gid = (os.geteuid(), os.getegid()) if os.geteuid() else (65532, 65532)
        with tempfile.TemporaryDirectory(prefix="depositar-bootstrap-test-") as name:
            root = Path(name)
            fixture = root / "fixture"
            fixture.mkdir(mode=0o755)
            fixture.chmod(0o755)
            config_path, cidfile = fixture / "bootstrap.conf", root / "container.cid"
            config_path.write_bytes(config)
            config_path.chmod(0o644)
            client = root / "docker-client"
            client.mkdir(mode=0o700)
            prefix = [
                "docker",
                "--host",
                "unix:///var/run/docker.sock",
                "--config",
                str(client),
            ]
            script = r"""
set -eu
nginx -t -c /fixture/bootstrap.conf
nginx -c /fixture/bootstrap.conf -g 'daemon off;' &
nginx_pid=$!
trap 'kill "$nginx_pid" 2>/dev/null || true; wait "$nginx_pid" 2>/dev/null || true' EXIT
ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if wget -q -S -O /dev/null http://127.0.0.1:8080/healthz 2>/tmp/health.headers; then
        ready=1
        break
    fi
    sleep 1
done
test "$ready" = 1
grep -q 'HTTP/1.1 200' /tmp/health.headers
if wget -q -S -O /dev/null http://127.0.0.1:8080/ 2>/tmp/root.headers; then
    exit 1
fi
grep -q 'HTTP/1.1 503' /tmp/root.headers
printf '{"nginx_t":true,"healthz":200,"other":503}\n'
"""
            argv = [
                *prefix,
                "run",
                "--rm",
                "--pull",
                "never",
                "--cidfile",
                str(cidfile),
                "--name",
                "depositar-bootstrap-test-" + uuid.uuid4().hex,
                "--runtime",
                "runc",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                f"{uid}:{gid}",
                "--cpus",
                "0.5",
                "--memory",
                "64m",
                "--pids-limit",
                "32",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
                "-e",
                "NVIDIA_VISIBLE_DEVICES=void",
                "-e",
                "CUDA_VISIBLE_DEVICES=",
                "--mount",
                f"type=bind,src={fixture},dst=/fixture,readonly",
                "--entrypoint",
                "/bin/sh",
                image,
                "-ec",
                script,
            ]
            environment = {"PATH": os.defpath, "LC_ALL": "C"}
            try:
                return activation.image_build._run(
                    argv, timeout=30, environment=environment
                )
            finally:
                # A timed-out Docker client does not prove its container exited.
                # Remove only this test's exact cid; never force any live peer.
                if cidfile.is_file():
                    cid = cidfile.read_text().strip()
                    if re.fullmatch(r"[0-9a-f]{64}", cid):
                        activation.image_build._run(
                            [*prefix, "container", "rm", "--force", cid],
                            timeout=10,
                            environment=environment,
                        )

    def test_exact_bootstrap_serves_health_only_on_readonly_nonroot_rootfs(self):
        result = self.run_bootstrap(activation.BOOTSTRAP)
        self.assertEqual(result.returncode, 0, "readonly Nginx bootstrap failed")
        self.assertEqual(
            json.loads(result.stdout), {"nginx_t": True, "healthz": 200, "other": 503}
        )


def routed_fixture(test):
    host, unused_baseline, leased, lease, unused_verified = host_evidence(test)
    host.lease = lease
    host.docker = SnapshotDocker(leased)
    host.observe()
    private_proof = copy.deepcopy(host.last[1])
    values = gateway_evidence()
    gateway = copy.deepcopy(values["containers"][-1])
    gateway_id, ingress_id = "d" * 64, "0" * 64
    gateway["Id"] = gateway_id
    website = host.compose["networks"]["website"]["name"]
    ingress = composition.GATEWAY_PROJECT + "_ingress"
    network = copy.deepcopy(values["networks"][-1])
    network["Id"] = ingress_id
    network["Containers"] = {gateway_id: {"EndpointID": "e" * 64}}
    gateway["NetworkSettings"]["Networks"][ingress]["NetworkID"] = ingress_id
    host.docker.groups[composition.GATEWAY_PROJECT] = [gateway]
    host.docker.networks[website]["EnableIPv6"] = False
    host.docker.networks[website]["Containers"][gateway_id] = {"EndpointID": "f" * 64}
    host.docker.networks[ingress] = network
    args = (
        host.docker,
        host.context,
        host.authority,
        host.catalog_images,
        host.catalog_inspections,
        host.inference_compose,
        host.inference_images,
        host.verify_inference,
        host.compose,
        host.images,
        host.image_proof,
    )
    routed = activation._RoutedHost(
        *args,
        gateway_compose=values["gateway_compose"],
        gateway_image=values["gateway_image"],
        gateway_proof=values["gateway_image_proof"],
        gateway_id=gateway_id,
        ingress_id=ingress_id,
        release_proof=private_proof,
    )
    routed.lease = lease
    return host, routed


class RoutedHostTests(unittest.TestCase):
    def test_full_four_peer_route_keeps_original_catalog_gpu_and_private_strict_three(
        self,
    ):
        private, routed = routed_fixture(self)
        with self.assertRaises(ValueError):
            private.observe()
        result = routed.observe()
        self.assertEqual(private.authority["runtime"], result[0])
        self.assertEqual(4, len(routed.last[1]["container_ids"]))
        self.assertEqual(3, len(routed.last[0]["container_ids"]))
        self.assertEqual(private.lease, routed.lease)
        self.assertEqual(1, len(routed.docker.groups[composition.GATEWAY_PROJECT]))

    def test_foreign_peer_is_rejected_on_each_unfiltered_network(self):
        for kind in ("website", "ingress", "catalog", "inference", "ranking"):
            with self.subTest(kind=kind):
                unused_private, routed = routed_fixture(self)
                if kind == "website":
                    name = routed.compose["networks"][kind]["name"]
                elif kind == "ingress":
                    name = composition.GATEWAY_PROJECT + "_ingress"
                elif kind == "catalog":
                    name = routed.context["plan"]["project"] + "_search_backend"
                else:
                    name = routed.inference_compose["networks"][kind]["name"]
                routed.docker.networks[name]["Containers"]["f" * 64] = {
                    "Name": "foreign"
                }
                with self.assertRaises(ValueError):
                    routed.observe()

    def test_unknown_gateway_project_container_and_endpoint_mismatch_are_rejected(self):
        for changed in ("extra", "endpoint"):
            with self.subTest(changed=changed):
                unused_private, routed = routed_fixture(self)
                row = routed.docker.groups[composition.GATEWAY_PROJECT][0]
                if changed == "extra":
                    other = copy.deepcopy(row)
                    other["Id"] = "e" * 64
                    routed.docker.groups[composition.GATEWAY_PROJECT].append(other)
                else:
                    name = routed.compose["networks"]["website"]["name"]
                    row["NetworkSettings"]["Networks"][name]["EndpointID"] = "0" * 64
                with self.assertRaises(ValueError):
                    routed.observe()


@unittest.skipIf(os.geteuid() == 0, "gateway operator requires non-root")
class GatewayActivationTests(unittest.TestCase):
    prepare = preparation_tests.GatewayPreparationTests.prepare

    def setUp(self):
        preparation_tests.GatewayPreparationTests.setUp(self)
        self.private_proof = copy.deepcopy(self.values["release_proof"])
        self.completed.update(self.private_proof)
        self.acceptance.return_value = {**self.completed, "runtime_reused": True}
        with locked_state(self.config) as store:
            descriptor = private_io.open_directory(self.slot)
            try:
                private_io.write_json(
                    descriptor,
                    "complete.json",
                    self.completed,
                    replace=True,
                    max_bytes=1024**2,
                )
            finally:
                os.close(descriptor)
            store.write("release-candidate.json", self.completed, replace=True)
        prepared = self.prepare()
        self.prepared = {**prepared, "preparation_reused": True}
        self.gateway_root = self.config.state_root / "gateway"
        self.started = False
        self.fail_phase = None
        self.events = []
        self.routed_proof = {
            "container_ids": {
                **self.private_proof["container_ids"],
                "web-nginx": "d" * 64,
            },
            "start_identity_sha256": "a" * 64,
        }
        self.gpu_compose = {
            "name": "depositar-ai-inference-" + "1" * 12,
            "services": {"scope-model": {}, "answer-model": {}, "reranker": {}},
            "networks": {},
        }
        self.gpu_images = {
            "scope-model": self.values["images"]["filtered-embedding"],
            "answer-model": {"Id": "sha256:" + "7" * 64},
            "reranker": {"Id": "sha256:" + "8" * 64},
        }
        self.context = {
            "plan": self.plan,
            "bundle": SimpleNamespace(bundle_id=self.lease["bundle_id"]),
        }
        self.authority = {"fixture": "query-authority"}
        self.key = {"fixture": "credential-authority"}
        self.image_authority = {
            "revision": self.revision,
            "source_manifest_sha256": "e" * 64,
            "images": {
                "web-nginx": self.proof,
                "conversation-api": self.values["image_proof"],
                "web": {"image_id": self.values["images"]["web"]["Id"]},
            },
        }
        patches = (
            (activation.gateway, "prepare_gateway", self.prepared),
            (activation.execution, "load_context", self.context),
            (activation.execution, "operation_lock", nullcontext()),
            (activation.query, "read_authority", self.authority),
            (activation.credentials, "read_api_key_authority", self.key),
            (activation.image_evidence, "read_completed_images", self.image_authority),
            (activation.inference, "_authority", self.image_authority),
            (activation.inference, "_refresh_authority", self.image_authority),
            (activation.composition, "_inference", self.gpu_compose),
            (activation.inference, "_images", self.gpu_images),
            (
                activation.release,
                "_images",
                {
                    role: self.values["images"][role]
                    for role in ("web", "conversation-api")
                },
            ),
            (
                activation.execution,
                "load_images",
                {"fixture": {"image_id": "sha256:" + "9" * 64}},
            ),
            (
                activation.composition,
                "render_composition",
                {"inference": self.gpu_compose},
            ),
            (activation.inference, "_runtime_identity", {"fixture": "gpu-identity"}),
            (activation.release, "_sealed_inference", lambda proof: None),
            (activation.inference, "_gpu", None),
            (activation.inference_runtime, "ownership_proofs", []),
        )
        for owner, name, value in patches:
            mock.patch.object(owner, name, return_value=value).start()
        mock.patch.object(
            activation.release,
            "_images",
            side_effect=lambda *args: {
                role: copy.deepcopy(self.values["images"][role])
                for role in ("web", "conversation-api")
            },
        ).start()
        mock.patch.object(
            activation.inference, "_verified_models", side_effect=self.models
        ).start()
        mock.patch.object(
            activation.runtime_docker,
            "project_inventory",
            side_effect=self.runtime_inventory,
        ).start()
        mock.patch.object(
            activation.query, "verify_authority", side_effect=self.query
        ).start()
        self.docker.call.side_effect = self.command
        self.docker.calls = 43
        self.docker.max_commands = 128
        fixture = self

        class Host:
            def __init__(self, *args, **kwargs):
                self.lease = None
                self.last = None
                self.routed = bool(kwargs)

            def observe(self, *args, **kwargs):
                fixture.events.append(
                    "routed-proof" if self.routed else "private-proof"
                )
                self.last = (
                    {"fixture": "gpu-proof"},
                    copy.deepcopy(
                        fixture.routed_proof if self.routed else fixture.private_proof
                    ),
                )
                return {}, [], {}

        mock.patch.object(activation.release, "_Host", Host).start()
        mock.patch.object(activation, "_RoutedHost", Host).start()
        self.http = mock.Mock(side_effect=self.probe)

    @contextmanager
    def models(self, *args, **kwargs):
        yield ({"api": {}, "embedding": {}, "reranker": {}}, lambda: None)

    def runtime_inventory(self, docker, project):
        self.assertEqual(composition.GATEWAY_PROJECT, project)
        return (["d" * 64], {project + "_ingress"}) if self.started else ([], set())

    def query(self, context, *, observe_runtime):
        observe_runtime()
        observe_runtime()
        return self.authority

    def command(self, arguments, **kwargs):
        if arguments[0] == "info":
            return b'"linux"'
        if arguments[:2] == ["network", "inspect"]:
            return json.dumps([{"Id": "0" * 64, "Name": arguments[-1]}]).encode()
        self.assertTrue((self.gateway_root / "activation/intent.json").exists())
        self.assertFalse((self.config.state_root / "active.json").exists())
        if arguments[0] == "compose":
            phase = "up" if "up" in arguments else "config"
            if phase == "up":
                self.assertEqual(
                    activation.BOOTSTRAP,
                    (self.gateway_root / "routes/active.conf").read_bytes(),
                )
                self.assertIn("--no-build", arguments)
                self.assertEqual("never", arguments[arguments.index("--pull") + 1])
                self.started = True
        elif arguments[0] == "exec":
            self.assertEqual("d" * 64, arguments[1])
            phase = (
                "reload"
                if "reload" in arguments
                else (
                    "candidate-t"
                    if arguments[-1].endswith("candidate.conf")
                    else "active-t"
                )
            )
        else:
            raise AssertionError("unexpected native command")
        self.events.append(phase)
        if phase == self.fail_phase:
            raise ValueError("PRIVATE_SENTINEL")
        return b""

    def probe(self, docker, **options):
        self.events.append("http")
        self.assertIs(self.docker, docker)
        identity = {
            key: options[key] for key in ("revision", "bundle_id", "manifest_sha256")
        }
        self.assertEqual(self.image_authority["images"]["web"], options["image_proof"])
        self.assertEqual(self.values["images"]["web"], options["image_inspection"])
        self.assertFalse((self.config.state_root / "active.json").exists())
        self.assertTrue((self.gateway_root / "activation/routed.json").exists())
        self.assertEqual(
            (self.gateway_root / "routes/candidate.conf").read_bytes(),
            (self.gateway_root / "routes/active.conf").read_bytes(),
        )
        report = {
            "schema_version": 1,
            **identity,
            "status": "ready",
            "code": "ready",
            "gateway_http_verified": True,
            "model_warmup_verified": False,
            "readiness_verified": False,
            "private_release_acceptance_required": True,
            "gateway_runtime_validation_required": True,
            "confers_runtime_authority": False,
            "active_changed": False,
            "search_quality_verified": False,
            "probe_runtime": {
                "container_id": "f" * 64,
                "run_id": options["run_id"],
                "helper_removed_verified": True,
                "host_network_verified": True,
            },
            "website": {
                "schema_version": 1,
                **identity,
                "website_ready": True,
                "two_turn_search_verified": True,
                "session_reset_verified": True,
                "search_quality_verified": False,
                "code": "ready",
                "turns": [
                    {"version": 1, "cards": 1, "elapsed_ms": 1},
                    {"version": 2, "cards": 1, "elapsed_ms": 1},
                ],
            },
        }
        native_identity = {
            "schema_version": 1,
            **identity,
            "run_id": options["run_id"],
            "image_id": options["image_proof"]["image_id"],
            "container_name": "depositar-ai-gateway-probe-" + options["run_id"],
            "probe_sha256": activation.hashlib.sha256(
                activation.gateway_probe_runtime.SCRIPT.encode()
            ).hexdigest(),
            "confers_runtime_authority": False,
            "active_changed": False,
        }
        for filename, record in {
            "intent.json": {**native_identity, "status": "probe_started"},
            "container.json": {
                **native_identity,
                "status": "probe_created",
                "container_id": "f" * 64,
            },
            "complete.json": {
                **native_identity,
                "status": "probe_complete",
                "report": report,
            },
        }.items():
            private_io.write_json(
                options["directory_fd"], filename, record, max_bytes=16384
            )
        return report

    def activate(self):
        with locked_state(self.config) as store:
            return activation.activate_gateway(
                self.config,
                store,
                self.revision,
                self.plan,
                repo_root=self.root,
                http_probe=self.http,
            )

    def test_first_activation_publishes_pointer_only_after_native_route_http_fresh_proof(
        self,
    ):
        result = self.activate()
        self.assertTrue(result["activation_performed"])
        self.assertTrue(result["active_changed"])
        self.assertTrue(result["confers_runtime_authority"])
        self.assertEqual(
            activation.ACTIVATION_FILES,
            set(os.listdir(self.gateway_root / "activation")),
        )
        with locked_state(self.config) as store:
            pointer = store.read("active.json")
        complete = json.loads(
            (self.gateway_root / "activation/complete.json").read_bytes()
        )
        self.assertEqual(
            activation.execution.digest_record(complete),
            pointer["activation_complete_sha256"],
        )
        self.assertEqual("private-proof", self.events[0])
        self.assertLess(self.events.index("candidate-t"), self.events.index("active-t"))
        self.assertLess(self.events.index("active-t"), self.events.index("reload"))
        self.assertLess(self.events.index("reload"), self.events.index("http"))
        self.assertIn("routed-proof", self.events[self.events.index("http") + 1 :])
        self.http.assert_called_once()
        self.start_release.assert_not_called()
        self.private_probe.assert_not_called()
        native = json.loads(
            (self.gateway_root / "activation/compose.json").read_bytes()
        )
        self.assertEqual({"name", "services", "networks"}, set(native))
        self.assertNotIn(b"$", private_io.canonical(native))
        self.assertEqual(1, len(native["services"]))
        for path in (
            self.config.state_root / "active.json",
            *(self.gateway_root / "activation").iterdir(),
        ):
            self.assertEqual(
                0o700 if path.is_dir() else 0o600, path.stat().st_mode & 0o777
            )

    def test_default_native_probe_does_not_import_host_pydantic_or_wire_models(self):
        original_import = builtins.__import__

        def missing(name, *args, **kwargs):
            if name.startswith(
                (
                    "pydantic",
                    "search.api.conversation.contract",
                    "search.api.retrieval.schemas",
                )
            ):
                raise ImportError("PRIVATE_SENTINEL missing Pydantic")
            return original_import(name, *args, **kwargs)

        with (
            mock.patch.object(
                activation.gateway_probe_runtime,
                "verify_gateway_from_image",
                side_effect=self.probe,
            ),
            mock.patch.object(builtins, "__import__", side_effect=missing),
            locked_state(self.config) as store,
        ):
            result = activation.activate_gateway(
                self.config,
                store,
                self.revision,
                self.plan,
                repo_root=self.root,
            )
        self.assertIs(result["active_changed"], True)
        self.http.assert_not_called()

    def test_insufficient_shared_native_budget_refuses_before_intent_or_ports(self):
        self.docker.calls = 44
        with self.assertRaisesRegex(
            activation.GatewayActivationError, "command_budget"
        ):
            self.activate()
        self.assertFalse((self.gateway_root / "activation").exists())
        self.assertNotIn("up", self.events)
        self.http.assert_not_called()

    def test_native_collectors_and_probe_keep_128_calls_and_six_volume_observations(
        self,
    ):
        self.native_call_budget()

    def test_changed_volume_metadata_blocks_before_gateway_intent_or_ports(self):
        self.native_call_budget(bad_metadata=True)

    def native_call_budget(self, *, bad_metadata=False):
        self.context["compose"] = {
            "name": self.plan["project"],
            "services": {"opensearch-search": {"volumes": []}},
        }
        volume_name = self.plan["project"] + "-index"
        self.context["compose"]["volumes"] = {
            "catalog-index": {"name": volume_name, "external": True}
        }
        self.context["compose"]["services"]["opensearch-search"]["volumes"] = [
            {
                "type": "volume",
                "source": "catalog-index",
                "target": volume_policy.TARGET,
                "volume": {"nocopy": True},
            }
        ]
        storage = sealed_catalog_storage(self, self.context)
        metadata, catalog_calls = storage.metadata, storage.calls
        if bad_metadata:
            storage.metadata = {**metadata, "mountpoint": "/changed"}
        source_plan, web, web_proof = native_tests.materials()
        web_proof = REAL_VALIDATE_IMAGE(source_plan, "web", web["Id"], web)
        self.image_authority["images"]["web"] = web_proof
        self.image_authority["images"]["reranker"] = {"image_id": "sha256:" + "c" * 64}
        self.image_authority["images"]["conversation-api"] = {
            **self.values["image_proof"],
            "image_id": "sha256:" + "2" * 64,
        }
        self.gpu_compose["networks"] = {
            kind: {"name": self.gpu_compose["name"] + "_" + kind}
            for kind in ("inference", "ranking")
        }
        ollama = copy.deepcopy(self.gpu_images["scope-model"])
        ollama.update(
            Os="linux",
            Architecture="amd64",
            RepoDigests=[
                "ollama/ollama@" + activation.composition.OLLAMA_IMAGE.split("@", 1)[1]
            ],
        )
        images = {
            "web": web,
            "conversation-api": {
                **self.values["images"]["conversation-api"],
                "Id": "sha256:" + "2" * 64,
            },
            "reranker": {"Id": "sha256:" + "c" * 64, "Config": {}},
            "web-nginx": self.values["gateway_image"],
        }
        inspections = {image["Id"]: image for image in (*images.values(), ollama)}
        catalog_images = {
            role: {"image_id": "sha256:" + digit * 64}
            for role, digit in zip(
                ("ollama-embedding", "fess-search", "opensearch-search"), "789"
            )
        }
        activation.execution.load_images.return_value = catalog_images
        for item in catalog_images.values():
            inspections[item["image_id"]] = {"Id": item["image_id"]}
        inspections[activation.composition.OLLAMA_IMAGE] = ollama
        inspections.update(
            {
                activation.image_build.TAGS[role] + self.revision: image
                for role, image in images.items()
            }
        )
        projects = (
            self.plan["project"],
            self.gpu_compose["name"],
            self.compose["name"],
        )
        groups = {
            project: [
                {"Id": str(number) * 64}
                for number in range(index * 3 + 1, index * 3 + 4)
            ]
            for index, project in enumerate(projects)
        }
        index_row = groups[self.plan["project"]][-1]
        index_row.update(
            Config={"Labels": {"com.docker.compose.service": "opensearch-search"}},
            Mounts=[
                {
                    "Type": "volume",
                    "Name": metadata["name"],
                    "Source": metadata["mountpoint"],
                    "Driver": "local",
                    "Destination": volume_policy.TARGET,
                    "RW": True,
                }
            ],
        )
        names = [
            projects[0] + "_search_backend",
            *(entry["name"] for entry in self.gpu_compose["networks"].values()),
            self.compose["networks"]["website"]["name"],
        ]
        snapshots = SnapshotDocker(
            (
                groups,
                {
                    name: {"Id": str(index + 1) * 64, "Name": name}
                    for index, name in enumerate(names)
                },
            )
        )
        native = native_tests.FakeDocker(web)
        all_calls, transports = [], []
        fixture = self

        def observe(host, routed):
            # Preserve the production observer's storage-prefix calls while
            # this existing fixture isolates native collector/probe budgets.
            storage = activation.execution.require_storage(host.context)
            actual_groups, _networks = host.collect(clients=True)
            activation.execution.validate_storage_mounts(
                storage, actual_groups[self.plan["project"]]
            )
            fixture.events.append("routed-proof" if routed else "private-proof")
            host.last = (
                {"fixture": "gpu-proof"},
                copy.deepcopy(
                    fixture.routed_proof if routed else fixture.private_proof
                ),
            )
            return {}, [], {}

        class Host(REAL_HOST):
            def observe(self, *args, **kwargs):
                return observe(self, False)

        class Routed(REAL_ROUTED_HOST):
            def observe(self, *args, **kwargs):
                return observe(self, True)

        def runner(argv, *, timeout, environment):
            arguments = argv[5:]
            all_calls.append(arguments)
            probe_started = (
                self.gateway_root / "activation/probe/intent.json"
            ).exists()
            if probe_started and len(native.calls) < 12:
                output = native.call(arguments, timeout=timeout)
            elif arguments[:2] == ["image", "inspect"]:
                output = json.dumps(inspections[arguments[-1]]).encode()
            elif arguments[0] == "info":
                output = b'"linux"'
            elif arguments[0] in {"compose", "exec"}:
                output = self.command(arguments)
                if arguments[0] == "compose" and "up" in arguments:
                    snapshots.groups[composition.GATEWAY_PROJECT] = [{"Id": "d" * 64}]
                    name = composition.GATEWAY_PROJECT + "_ingress"
                    snapshots.networks[name] = {"Id": "0" * 64, "Name": name}
            else:
                output = snapshots.call(arguments)
            return SimpleNamespace(stdout=output, stderr=b"", returncode=0)

        def docker(*args, **kwargs):
            value = REAL_DOCKER(args[0], runner, **kwargs)
            transports.append(value)
            return value

        def validate(plan, role, image_id, inspection):
            if role == "web":
                return REAL_VALIDATE_IMAGE(plan, role, image_id, inspection)
            return self.image_authority["images"][role]

        with (
            mock.patch.object(activation.runtime_docker, "Docker", side_effect=docker),
            mock.patch.object(
                activation.inference, "_images", side_effect=REAL_GPU_IMAGES
            ),
            mock.patch.object(
                activation.release, "_images", side_effect=REAL_RELEASE_IMAGES
            ),
            mock.patch.object(
                activation.runtime_docker,
                "project_inventory",
                side_effect=REAL_INVENTORY,
            ),
            mock.patch.object(
                activation.source_bundle, "validate_built_image", side_effect=validate
            ),
            mock.patch.object(
                activation.source_bundle,
                "validate_compatible_built_image",
                side_effect=lambda plan, role, revision, image_id, inspection: validate(
                    plan, role, image_id, inspection
                ),
            ),
            mock.patch.object(
                activation.source_bundle,
                "committed_source_plan",
                return_value=source_plan,
            ),
            mock.patch.object(activation.release, "_Host", Host),
            mock.patch.object(activation, "_RoutedHost", Routed),
            mock.patch.object(
                activation.uuid,
                "uuid4",
                return_value=SimpleNamespace(hex=native_tests.RUN),
            ),
            locked_state(self.config) as store,
        ):
            if bad_metadata:
                with self.assertRaisesRegex(
                    activation.GatewayActivationError, "^gateway_activation_failed$"
                ):
                    activation.activate_gateway(
                        self.config,
                        store,
                        self.revision,
                        self.plan,
                        repo_root=self.root,
                    )
                self.assertFalse((self.gateway_root / "activation").exists())
                self.assertFalse(self.started)
                self.assertEqual(1, len(catalog_calls))
                self.assertEqual(14, len(all_calls))
                return
            result = activation.activate_gateway(
                self.config, store, self.revision, self.plan, repo_root=self.root
            )
        self.assertIs(result["active_changed"], True)
        self.assertEqual(128, len(all_calls))
        self.assertEqual(128, transports[0].calls)
        self.assertEqual(128, transports[0].max_commands)
        self.assertEqual(12, len(native.calls))
        self.assertEqual(4, self.events.count("routed-proof"))
        self.assertEqual(2, self.events.count("private-proof"))
        self.assertEqual(6, len(catalog_calls))

    def test_native_boolean_claim_cannot_be_a_truthy_integer(self):
        def forged(docker, **options):
            result = self.probe(docker, **options)
            result["probe_runtime"]["helper_removed_verified"] = 1
            return result

        self.http.side_effect = forged
        with self.assertRaisesRegex(
            activation.GatewayActivationError, "http_contract_invalid"
        ):
            self.activate()
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_probe_report_without_complete_receipt_is_not_authority(self):
        def incomplete(docker, **options):
            result = self.probe(docker, **options)
            os.unlink("complete.json", dir_fd=options["directory_fd"])
            return result

        self.http.side_effect = incomplete
        with self.assertRaisesRegex(
            activation.GatewayActivationError, "probe_receipts_invalid"
        ):
            self.activate()
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_probe_complete_plus_failure_is_not_success(self):
        def failed(docker, **options):
            result = self.probe(docker, **options)
            private_io.write_json(
                options["directory_fd"],
                "failure.json",
                {"status": "probe_failed"},
                max_bytes=16384,
            )
            return result

        self.http.side_effect = failed
        with self.assertRaisesRegex(
            activation.GatewayActivationError, "probe_receipts_invalid"
        ):
            self.activate()
        self.assertTrue((self.gateway_root / "activation/probe/complete.json").exists())
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_probe_receipt_cannot_substitute_another_image(self):
        def foreign(docker, **options):
            result = self.probe(docker, **options)
            path = options["directory_path"] / "intent.json"
            record = json.loads(path.read_bytes())
            record["image_id"] = "sha256:" + "0" * 64
            private_io.write_json(
                options["directory_fd"],
                "intent.json",
                record,
                replace=True,
                max_bytes=16384,
            )
            return result

        self.http.side_effect = foreign
        with self.assertRaisesRegex(
            activation.GatewayActivationError, "probe_receipts_invalid"
        ):
            self.activate()
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_final_activation_completion_read_cannot_hide_probe_receipt_drift(self):
        original = activation.private_io.read_file

        def drift(fd, name, **options):
            value = original(fd, name, **options)
            if (
                name == "complete.json"
                and (self.gateway_root / "activation/complete.json").exists()
            ):
                (self.gateway_root / "activation/probe/intent.json").write_bytes(
                    b"changed\n"
                )
            return value

        with mock.patch.object(activation.private_io, "read_file", side_effect=drift):
            with self.assertRaisesRegex(
                activation.GatewayActivationError, "probe_changed"
            ):
                self.activate()
        self.assertTrue((self.gateway_root / "activation/complete.json").exists())
        self.assertFalse((self.config.state_root / "active.json").exists())

    def native_failure(self, phase):
        self.fail_phase = phase
        with self.assertRaisesRegex(
            activation.GatewayActivationError, "^gateway_activation_failed$"
        ):
            self.activate()
        self.assertTrue((self.gateway_root / "activation/intent.json").exists())
        self.assertFalse((self.config.state_root / "active.json").exists())
        self.http.assert_not_called()
        calls = len(self.docker.call.call_args_list)
        with self.assertRaisesRegex(
            activation.GatewayActivationError, "partial_state_recovery_required"
        ):
            self.activate()
        self.assertEqual(calls, len(self.docker.call.call_args_list))

    def test_config_failure_retains_intent_without_retry(self):
        self.native_failure("config")

    def test_up_failure_retains_intent_without_retry(self):
        self.native_failure("up")

    def test_candidate_nginx_test_failure_retains_bootstrap_and_intent(self):
        self.native_failure("candidate-t")
        self.assertEqual(
            activation.BOOTSTRAP,
            (self.gateway_root / "routes/active.conf").read_bytes(),
        )

    def test_active_nginx_test_failure_never_reloads_or_promotes(self):
        self.native_failure("active-t")
        self.assertNotIn("reload", self.events)

    def test_reload_failure_never_probes_or_promotes(self):
        self.native_failure("reload")

    def test_false_http_claim_never_publishes_completion_or_active_pointer(self):
        self.http.side_effect = lambda docker, **options: {
            **self.probe(docker, **options),
            "gateway_http_verified": 1,
        }
        with self.assertRaisesRegex(
            activation.GatewayActivationError, "http_contract_invalid"
        ):
            self.activate()
        self.assertFalse((self.gateway_root / "activation/complete.json").exists())
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_candidate_drift_during_http_is_not_repaired_or_promoted(self):
        def drift(docker, **options):
            result = self.probe(docker, **options)
            (self.gateway_root / "routes/candidate.conf").write_bytes(b"untrusted\n")
            return result

        self.http.side_effect = drift
        with self.assertRaisesRegex(
            activation.GatewayActivationError, "candidate_changed"
        ):
            self.activate()
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_existing_active_refuses_without_any_native_action(self):
        with locked_state(self.config) as store:
            store.write("active.json", {"existing": True})
        self.docker.reset_mock()
        with self.assertRaisesRegex(
            activation.GatewayActivationError, "existing_active_not_adopted"
        ):
            self.activate()
        self.docker.call.assert_not_called()

    def test_runtime_restart_during_http_is_never_promoted(self):
        def restart(docker, **options):
            result = self.probe(docker, **options)
            self.routed_proof["start_identity_sha256"] = "b" * 64
            return result

        self.http.side_effect = restart
        with self.assertRaisesRegex(
            activation.GatewayActivationError, "runtime_changed_during_acceptance"
        ):
            self.activate()
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_final_deadline_after_complete_record_check_never_publishes_pointer(self):
        clock = [0.0]
        original = activation.private_io.read_file

        def expire(fd, name, **options):
            value = original(fd, name, **options)
            if (
                name == "active.conf"
                and (self.gateway_root / "activation/complete.json").exists()
            ):
                clock[0] = activation.TOTAL_SECONDS + 1
            return value

        with (
            mock.patch.object(
                activation.time, "monotonic", side_effect=lambda: clock[0]
            ),
            mock.patch.object(activation.private_io, "read_file", side_effect=expire),
        ):
            with self.assertRaisesRegex(activation.GatewayActivationError, "deadline"):
                self.activate()
        self.assertTrue((self.gateway_root / "activation/complete.json").exists())
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_model_handle_exit_failure_never_publishes_completion(self):
        @contextmanager
        def broken(*args, **kwargs):
            yield ({"api": {}, "embedding": {}, "reranker": {}}, lambda: None)
            raise ValueError("PRIVATE_SENTINEL retained model changed")

        with mock.patch.object(
            activation.inference, "_verified_models", side_effect=broken
        ):
            with self.assertRaisesRegex(
                activation.GatewayActivationError, "^gateway_activation_failed$"
            ):
                self.activate()
        self.assertFalse((self.gateway_root / "activation/complete.json").exists())
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_preexisting_singleton_runtime_is_not_adopted_before_intent(self):
        self.started = True
        with self.assertRaisesRegex(
            activation.GatewayActivationError, "existing_gateway_not_adopted"
        ):
            self.activate()
        self.assertFalse((self.gateway_root / "activation").exists())
        self.assertNotIn("up", self.events)
