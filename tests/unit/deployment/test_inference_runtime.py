"""Pure runtime-policy tests; no Docker, GPU, model loading or network access."""

from __future__ import annotations

import copy
import unittest
from unittest import mock

from search.operations.catalog import client_lease, policy
from search.operations.deployment import composition, inference_runtime
from tests.unit.catalog.test_client_lease import evidence as client_evidence
from tests.unit.deployment.test_composition import REVISION, product


def snapshot(compose, images):
    containers, networks = [], []
    network_ids = {
        key: str(index) * 64 for index, key in enumerate(compose["networks"], 7)
    }
    for index, (name, expected) in enumerate(sorted(compose["services"].items()), 1):
        network_key = expected["networks"][0]
        network_name = compose["networks"][network_key]["name"]
        environment = inference_runtime._environment(
            images[name]["Config"].get("Env") or []
        )
        environment.update(expected["environment"])
        row = {
            "Id": str(index) * 64,
            "Image": images[name]["Id"],
            "RestartCount": 0,
            "Config": {
                "User": expected["user"],
                "Image": images[name]["Id"],
                "Tty": False,
                "OpenStdin": False,
                "StdinOnce": False,
                "Env": [key + "=" + value for key, value in environment.items()],
                "Cmd": expected.get("command", images[name]["Config"].get("Cmd")),
                "Entrypoint": images[name]["Config"].get("Entrypoint"),
                "WorkingDir": images[name]["Config"].get("WorkingDir", ""),
                "StopSignal": images[name]["Config"].get("StopSignal", ""),
                "Labels": {
                    **(images[name]["Config"].get("Labels") or {}),
                    "com.docker.compose.service": name,
                    "com.docker.compose.project": compose["name"],
                    "com.docker.compose.oneoff": "False",
                },
                "Healthcheck": {
                    "Test": expected["healthcheck"]["test"],
                    "Retries": expected["healthcheck"]["retries"],
                    **{
                        actual: policy._duration(
                            expected["healthcheck"].get(setting, "0s")
                        )
                        for actual, setting in (
                            ("Interval", "interval"),
                            ("Timeout", "timeout"),
                            ("StartPeriod", "start_period"),
                            ("StartInterval", "start_interval"),
                        )
                    },
                },
            },
            "HostConfig": {
                "ReadonlyRootfs": True,
                "Privileged": False,
                "Init": True,
                "AutoRemove": False,
                "CapDrop": ["ALL"],
                "CapAdd": [],
                "Devices": [],
                "DeviceCgroupRules": [],
                "VolumesFrom": [],
                "SecurityOpt": ["no-new-privileges:true"],
                "Runtime": "nvidia",
                "PidMode": "",
                "IpcMode": "private",
                "UTSMode": "",
                "UsernsMode": "",
                "NanoCpus": expected["cpus"] * 10**9,
                "Memory": expected["mem_limit"],
                "MemorySwap": expected["memswap_limit"],
                "PidsLimit": expected["pids_limit"],
                "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                "LogConfig": {
                    "Type": expected["logging"]["driver"],
                    "Config": expected["logging"]["options"],
                },
                "Tmpfs": {
                    entry.split(":", 1)[0]: entry.split(":", 1)[1]
                    for entry in expected["tmpfs"]
                },
                "Ulimits": [],
                "PortBindings": {},
                "DeviceRequests": [
                    {
                        "Driver": "nvidia",
                        "Count": 0,
                        "DeviceIDs": expected["deploy"]["resources"]["reservations"][
                            "devices"
                        ][0]["device_ids"],
                        "Capabilities": [["gpu"]],
                        "Options": None,
                    }
                ],
                "NetworkMode": network_name,
            },
            "State": {
                "Running": True,
                "Paused": False,
                "Restarting": False,
                "Dead": False,
                "OOMKilled": False,
                "Pid": 100 + index,
                "StartedAt": "2026-10-08T08:00:00Z",
                "Health": {"Status": "healthy"},
            },
            "NetworkSettings": {
                "Ports": {"11434/tcp": None},
                "Networks": {network_name: {"NetworkID": network_ids[network_key]}},
            },
            "Mounts": [
                {
                    "Type": entry["type"],
                    "Source": entry["source"],
                    "Destination": entry["target"],
                    "RW": False,
                    "Propagation": "rprivate",
                }
                for entry in expected["volumes"]
            ],
        }
        containers.append(row)
    for key, expected in compose["networks"].items():
        networks.append(
            {
                "Id": network_ids[key],
                "Name": expected["name"],
                "Internal": True,
                "Attachable": False,
                "Ingress": False,
                "Driver": "bridge",
                "Scope": "local",
                "Labels": {
                    "com.docker.compose.project": compose["name"],
                    "com.docker.compose.network": key,
                },
                "Containers": {
                    row["Id"]: {}
                    for row in containers
                    if key
                    in compose["services"][
                        row["Config"]["Labels"]["com.docker.compose.service"]
                    ]["networks"]
                },
            }
        )
    return containers, networks


def images():
    ollama = {
        "Id": "sha256:" + "a" * 64,
        "Config": {
            "Env": ["PATH=/usr/bin", "PRIVATE_SENTINEL=never-save-this-value"],
            "Cmd": None,
            "Entrypoint": ["/bin/ollama"],
        },
    }
    reranker = {
        "Id": "sha256:" + "b" * 64,
        "Config": {
            "Env": ["PATH=/opt/conda/bin", "MODEL_ROOT=/model"],
            "Labels": {"io.depositar.ai.source.sha256": "6" * 64},
            "Cmd": ["/opt/conda/bin/python", "-m", "search.reranker.server"],
            "Entrypoint": None,
        },
    }
    return {"scope-model": ollama, "answer-model": ollama, "reranker": reranker}


class InferenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.compose = composition._inference(
            product(), REVISION, 1000, 1000, inference_source_sha256="6" * 64
        )
        self.images = images()
        for name, image in self.images.items():
            self.compose["services"][name]["image"] = image["Id"]
        self.containers, self.networks = snapshot(self.compose, self.images)

    def invoke(self):
        return inference_runtime.validate_runtime(
            self.compose, self.images, self.containers, self.networks
        )

    def test_exact_three_private_gpu_roles_are_proved_without_returning_env(self):
        result = self.invoke()
        self.assertEqual(inference_runtime.SERVICES, set(result["container_ids"]))
        self.assertEqual({"inference", "ranking"}, set(result["network_ids"]))
        self.assertEqual(64, len(result["start_identity_sha256"]))
        self.assertNotIn("never-save-this-value", str(result))
        proofs = inference_runtime.ownership_proofs(self.compose, result, self.images)
        self.assertEqual(
            {"scope", "planner", "reranker"}, {proof.role for proof in proofs}
        )
        self.assertEqual([2, 1, 1], [len(proof.gpu_uuids) for proof in proofs])
        self.assertEqual(4, len({uuid for proof in proofs for uuid in proof.gpu_uuids}))

    def test_service_inventory_identity_and_extra_container_are_rejected(self):
        for mutation in (
            lambda: self.containers.append(copy.deepcopy(self.containers[0])),
            lambda: self.containers[0]["Config"]["Labels"].update(
                {"com.docker.compose.project": "legacy"}
            ),
            lambda: self.containers[0].update({"Id": "short"}),
            lambda: self.containers[0].update({"Image": "sha256:" + "f" * 64}),
            lambda: self.containers[0]["Config"]["Labels"].update(
                {"com.docker.compose.oneoff": "True"}
            ),
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                mutation()
                with self.assertRaises(inference_runtime.InferenceRuntimeError):
                    self.invoke()

    def test_gpu_requests_require_exact_full_uuid_role_and_no_options(self):
        for key, value in (
            ("Count", -1),
            ("Count", True),
            ("DeviceIDs", ["0"]),
            ("DeviceIDs", product().gpus.all_uuids()),
            ("Capabilities", [["gpu", "utility"]]),
            ("Options", {"key": "value"}),
            ("Driver", "other"),
        ):
            with self.subTest(key=key, value=value):
                self.setUp()
                self.containers[0]["HostConfig"]["DeviceRequests"][0][key] = value
                with self.assertRaises(inference_runtime.InferenceRuntimeError):
                    self.invoke()

    def test_resource_boundaries_and_host_namespaces_fail_closed(self):
        for key, value in (
            ("Privileged", True),
            ("Init", False),
            ("ReadonlyRootfs", False),
            ("AutoRemove", True),
            ("PidMode", "host"),
            ("IpcMode", "host"),
            ("UTSMode", "host"),
            ("UsernsMode", "host"),
            ("Runtime", "custom"),
            ("Devices", [{"PathOnHost": "/dev/nvidia0"}]),
            ("DeviceCgroupRules", ["c *:* rwm"]),
            ("CapAdd", ["SYS_ADMIN"]),
            ("SecurityOpt", []),
            ("Memory", True),
            ("MemorySwap", -1),
            ("NanoCpus", 1),
            ("PidsLimit", 0),
            ("Annotations", {"x": "y"}),
        ):
            with self.subTest(key=key):
                self.setUp()
                self.containers[0]["HostConfig"][key] = value
                with self.assertRaises(inference_runtime.InferenceRuntimeError):
                    self.invoke()

    def test_not_running_restart_oom_and_nonhealthy_are_rejected(self):
        for key, value in (
            ("Running", False),
            ("Paused", True),
            ("Restarting", True),
            ("Dead", True),
            ("OOMKilled", True),
            ("Pid", 0),
            ("Health", {"Status": "starting"}),
            ("StartedAt", ""),
        ):
            with self.subTest(key=key):
                self.setUp()
                self.containers[0]["State"][key] = value
                with self.assertRaises(inference_runtime.InferenceRuntimeError):
                    self.invoke()
        self.setUp()
        self.containers[0]["RestartCount"] = 1
        with self.assertRaises(inference_runtime.InferenceRuntimeError):
            self.invoke()

    def test_ports_commands_extra_environment_and_healthcheck_are_rejected(self):
        for mutation in (
            lambda: self.containers[0]["HostConfig"].update(
                {
                    "PortBindings": {
                        "11434/tcp": [{"HostIp": "0.0.0.0", "HostPort": "11434"}]
                    }
                }
            ),
            lambda: self.containers[0]["NetworkSettings"]["Ports"].update(
                {"11434/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18000"}]}
            ),
            lambda: self.containers[0]["Config"].update({"Cmd": ["shell"]}),
            lambda: self.containers[0]["Config"].update({"Entrypoint": ["shell"]}),
            lambda: self.containers[0]["Config"]["Env"].append("OLLAMA_NUM_PARALLEL=9"),
            lambda: self.containers[0]["Config"]["Env"].append("EXTRA=value"),
            lambda: self.containers[0]["Config"]["Healthcheck"].update(
                {"Retries": True}
            ),
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                mutation()
                with self.assertRaises(inference_runtime.InferenceRuntimeError):
                    self.invoke()

    def test_exact_readonly_model_mount_and_private_network_peers_required(self):
        for mutation in (
            lambda: self.containers[0]["Mounts"][0].update({"RW": True}),
            lambda: self.containers[0]["Mounts"][0].update({"Source": "/srv/old"}),
            lambda: self.containers[0]["Mounts"][0].update({"Propagation": "rshared"}),
            lambda: self.containers[0]["Mounts"].append(
                {"Type": "bind", "Source": "/", "Destination": "/host", "RW": False}
            ),
            lambda: self.containers[0]["NetworkSettings"]["Networks"].update(
                {"extra": {}}
            ),
            lambda: self.networks[0]["Containers"].update({"f" * 64: {}}),
            lambda: self.networks[0].update({"Internal": False}),
            lambda: self.networks[0].update({"Attachable": True}),
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                mutation()
                with self.assertRaises(inference_runtime.InferenceRuntimeError):
                    self.invoke()

    def test_network_identity_and_service_namespace_must_match_same_snapshot(self):
        self.networks[0]["Id"] = "f" * 64
        with self.assertRaises(inference_runtime.InferenceRuntimeError):
            self.invoke()

    def test_noninteractive_input_closed_dns_and_immutable_config_image_are_required(
        self,
    ):
        for section, key, value in (
            ("Config", "Tty", True),
            ("Config", "OpenStdin", True),
            ("Config", "StdinOnce", True),
            ("Config", "Image", "mutable:tag"),
            ("HostConfig", "Links", ["foreign"]),
            ("HostConfig", "ExtraHosts", ["scope-model:203.0.113.1"]),
            ("HostConfig", "Dns", ["203.0.113.2"]),
            ("HostConfig", "DnsOptions", ["ndots:9"]),
            ("HostConfig", "DnsSearch", ["evil.example"]),
            ("HostConfig", "CgroupnsMode", "host"),
        ):
            with self.subTest(key=key):
                self.setUp()
                self.containers[0][section][key] = value
                with self.assertRaises(inference_runtime.InferenceRuntimeError):
                    self.invoke()

    def test_inherited_source_labels_cannot_be_overridden_at_container_creation(self):
        self.images["reranker"]["Config"]["Labels"] = {
            "io.depositar.ai.source.revision": REVISION,
            "io.depositar.ai.source.sha256": "6" * 64,
        }
        self.containers, self.networks = snapshot(self.compose, self.images)
        self.invoke()
        reranker = next(
            row
            for row in self.containers
            if row["Config"]["Labels"]["com.docker.compose.service"] == "reranker"
        )
        reranker["Config"]["Labels"]["io.depositar.ai.source.revision"] = "f" * 40
        with self.assertRaises(inference_runtime.InferenceRuntimeError):
            self.invoke()
        self.setUp()
        self.containers[0]["HostConfig"]["NetworkMode"] = "bridge"
        with self.assertRaises(inference_runtime.InferenceRuntimeError):
            self.invoke()

    def test_malformed_snapshots_fail_with_fixed_code_not_raw_values(self):
        for value in (None, [], {}, {"Id": "PRIVATE_SENTINEL"}, 1):
            with self.subTest(value=value):
                self.containers = [value, value, value]
                with self.assertRaises(
                    inference_runtime.InferenceRuntimeError
                ) as caught:
                    self.invoke()
                self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))


class LeasedInferenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.client = client_evidence()
        self.compose = composition._inference(
            product(),
            self.client["lease"]["revision"],
            21599,
            564,
            inference_source_sha256="6" * 64,
        )
        self.images = images()
        for name, image in self.images.items():
            self.compose["services"][name]["image"] = image["Id"]
        self.containers, self.networks = snapshot(self.compose, self.images)
        # Docker IDs are daemon-wide; use different IDs from API/catalog cores.
        replacements = {
            row["Id"]: "abc"[index] * 64 for index, row in enumerate(self.containers)
        }
        for row in self.containers:
            row["Id"] = replacements[row["Id"]]
            for name, attachment in row["NetworkSettings"]["Networks"].items():
                attachment["NetworkID"] = self.client["lease"]["network_ids"][name]
        for row in self.networks:
            row["Id"] = self.client["lease"]["network_ids"][row["Name"]]
            row["Containers"] = {
                replacements[identifier]: value
                for identifier, value in row["Containers"].items()
            }
            row["Containers"][self.client["lease"]["api_container_id"]] = {}

    def invoke(self):
        return inference_runtime.validate_leased_runtime(
            self.compose,
            self.images,
            self.containers,
            self.networks,
            **self.client,
        )

    def reject(self, code=None):
        with self.assertRaises(inference_runtime.InferenceRuntimeError) as caught:
            self.invoke()
        self.assertRegex(str(caught.exception), r"^inference_[a-z_]+$")
        if code is not None:
            self.assertEqual(code, str(caught.exception))

    def test_exact_gpu_roles_and_sealed_api_are_proved_without_returning_inputs(self):
        before = copy.deepcopy(
            (self.compose, self.images, self.containers, self.networks, self.client)
        )
        result = self.invoke()
        self.assertEqual(
            {
                "container_ids",
                "network_ids",
                "start_identity_sha256",
                "client_lease_sha256",
                "api_container_id",
                "api_image_id",
            },
            set(result),
        )
        self.assertEqual(inference_runtime.SERVICES, set(result["container_ids"]))
        self.assertEqual(
            client_lease.lease_digest(self.client["lease"]),
            result["client_lease_sha256"],
        )
        self.assertEqual(
            self.client["lease"]["api_container_id"], result["api_container_id"]
        )
        self.assertEqual(self.client["lease"]["api_image_id"], result["api_image_id"])
        self.assertEqual(result, self.invoke())
        self.assertEqual(
            before,
            (self.compose, self.images, self.containers, self.networks, self.client),
        )
        self.assertNotIn("never-save-this-value", str(result))
        self.assertNotIn("/srv/", str(result))
        proofs = inference_runtime.ownership_proofs(self.compose, result, self.images)
        self.assertEqual(3, len(proofs))
        self.assertEqual({"scope", "planner", "reranker"}, {p.role for p in proofs})

    def test_baseline_keeps_exact_peers_and_leased_checker_uses_original_snapshots(
        self,
    ):
        with self.assertRaisesRegex(
            inference_runtime.InferenceRuntimeError, "network_peers_changed"
        ):
            inference_runtime.validate_runtime(
                self.compose, self.images, self.containers, self.networks
            )
        original_client = client_lease.validate_client_lease
        original_gpu = inference_runtime._validate_runtime
        with (
            mock.patch.object(
                inference_runtime,
                "validate_runtime",
                side_effect=AssertionError("no fabricated baseline"),
            ),
            mock.patch.object(
                client_lease, "validate_client_lease", wraps=original_client
            ) as lease_check,
            mock.patch.object(
                inference_runtime, "_validate_runtime", wraps=original_gpu
            ) as gpu_check,
        ):
            self.invoke()
        self.assertIs(
            self.client["catalog_network"],
            lease_check.call_args.kwargs["catalog_network"],
        )
        self.assertIs(self.networks, gpu_check.call_args.args[3])
        self.assertIs(
            self.client["api_container"],
            lease_check.call_args.kwargs["api_container"],
        )

    def test_both_inference_networks_require_exact_gpu_peers_and_the_same_api(self):
        for index in range(2):
            for mutation in ("extra", "missing_api", "missing_gpu", "list"):
                with self.subTest(network=index, mutation=mutation):
                    self.setUp()
                    row = self.networks[index]
                    api_id = self.client["lease"]["api_container_id"]
                    if mutation == "extra":
                        row["Containers"]["f" * 64] = {}
                    elif mutation == "missing_api":
                        del row["Containers"][api_id]
                    elif mutation == "missing_gpu":
                        del row["Containers"][next(iter(row["Containers"]))]
                    else:
                        row["Containers"] = list(row["Containers"])
                    self.reject("inference_runtime_network_peers_changed")

    def test_network_ids_must_match_lease_even_with_consistent_gpu_attachments(self):
        for index in range(2):
            with self.subTest(network=index):
                self.setUp()
                row = self.networks[index]
                row["Id"] = "f" * 64
                for container in self.containers:
                    attachments = container["NetworkSettings"]["Networks"]
                    if row["Name"] in attachments:
                        attachments[row["Name"]]["NetworkID"] = row["Id"]
                self.reject("inference_client_network_id_changed")

    def test_recipe_and_network_names_bind_inference_to_release(self):
        for mutation in (
            lambda: self.compose.update({"name": "depositar-ai-inference-" + "f" * 12}),
            lambda: self.compose["networks"]["ranking"].update(
                {"name": "depositar-ai-inference-" + "f" * 12 + "_ranking"}
            ),
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                mutation()
                self.reject("inference_client_source_or_network_changed")

    def test_source_fingerprint_cannot_be_changed_without_the_sealed_image(self):
        self.compose["x-depositar-plan"]["inference_source_sha256"] = "f" * 64
        self.reject("inference_runtime_source_changed")

    def test_original_gpu_image_revision_can_differ_from_application_revision(self):
        origin = "b" * 40
        self.images["reranker"]["Config"]["Labels"].update(
            {
                "org.opencontainers.image.revision": origin,
                "io.depositar.ai.source.revision": origin,
            }
        )
        reranker = next(
            row
            for row in self.containers
            if row["Config"]["Labels"]["com.docker.compose.service"] == "reranker"
        )
        reranker["Config"]["Labels"].update(self.images["reranker"]["Config"]["Labels"])
        self.assertNotEqual(origin, self.client["lease"]["revision"])
        result = self.invoke()
        self.assertEqual(
            self.client["lease"]["api_container_id"], result["api_container_id"]
        )

    def test_api_image_source_capabilities_mounts_and_attachments_are_reverified(self):
        for mutation in (
            lambda row: row.update({"Image": "sha256:" + "f" * 64}),
            lambda row: row["Config"].update({"Image": "mutable:tag"}),
            lambda row: row["Config"]["Labels"].update(
                {"io.depositar.ai.source.sha256": "f" * 64}
            ),
            lambda row: row["Config"].update({"Cmd": ["shell"]}),
            lambda row: row["Config"]["Env"].append("EXTRA=PRIVATE_SENTINEL"),
            lambda row: row["HostConfig"].update({"Privileged": True}),
            lambda row: row["HostConfig"].update({"Runtime": "nvidia"}),
            lambda row: row["HostConfig"].update({"DeviceRequests": [{"Count": -1}]}),
            lambda row: row["HostConfig"].update({"MemorySwap": -1}),
            lambda row: row["HostConfig"].update(
                {"PortBindings": {"8000/tcp": [{"HostPort": "8000"}]}}
            ),
            lambda row: row["Mounts"][0].update({"RW": True}),
            lambda row: row["NetworkSettings"]["Networks"].update({"extra": {}}),
            lambda row: next(iter(row["NetworkSettings"]["Networks"].values())).update(
                {"NetworkID": "f" * 64}
            ),
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                mutation(self.client["api_container"])
                self.reject("inference_client_lease_invalid")

    def test_unsealed_image_or_catalog_receipts_and_extra_catalog_peer_are_rejected(
        self,
    ):
        for mutation in (
            lambda: self.client["lease"].update({"unknown": "PRIVATE_SENTINEL"}),
            lambda: self.client["image_proof"].update({"source_sha256": "f" * 64}),
            lambda: self.client["image_environment"].append("EXTRA=value"),
            lambda: self.client["catalog_identity"].update(
                {"projection_sha256": "f" * 64}
            ),
            lambda: self.client["catalog_network"]["Containers"].update({"f" * 64: {}}),
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                mutation()
                self.reject("inference_client_lease_invalid")

    def test_leased_profile_keeps_gpu_resource_and_command_policy(self):
        for mutation in (
            lambda row: row["HostConfig"]["DeviceRequests"][0].update({"Count": -1}),
            lambda row: row["HostConfig"].update({"Privileged": True}),
            lambda row: row["Config"]["Env"].append("EXTRA=value"),
            lambda row: row["Config"].update({"Cmd": ["shell"]}),
            lambda row: row["Mounts"][0].update({"RW": True}),
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                mutation(self.containers[0])
                self.reject()

    def test_gpu_ids_cannot_alias_the_api_or_catalog_core(self):
        for identifier in (
            self.client["lease"]["api_container_id"],
            *self.client["lease"]["core_container_ids"].values(),
        ):
            with self.subTest(identifier=identifier):
                self.setUp()
                row = self.containers[0]
                previous = row["Id"]
                row["Id"] = identifier
                for network in self.networks:
                    if previous in network["Containers"]:
                        del network["Containers"][previous]
                        network["Containers"][identifier] = {}
                self.reject("inference_client_container_identity_collision")

    def test_malformed_input_has_fixed_errors_and_exact_inventory(self):
        for mutation in (
            lambda: self.containers.append(self.client["api_container"]),
            lambda: self.networks.append(self.client["catalog_network"]),
            lambda: self.networks.append(copy.deepcopy(self.networks[0])),
            lambda: self.client.update({"api_container": "PRIVATE_SENTINEL"}),
            lambda: self.client.update({"lease": {"revision": "PRIVATE_SENTINEL"}}),
            lambda: self.compose.update({"networks": None}),
        ):
            with self.subTest(mutation=mutation):
                self.setUp()
                mutation()
                self.reject()
