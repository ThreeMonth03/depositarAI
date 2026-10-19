"""Offline CPU release drift regressions; no Docker, models or HTTP requests."""

from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from search.operations.catalog import client_lease
from search.operations.deployment.release import runtime as release_runtime
from tests.unit.catalog.test_client_lease import evidence as client_evidence


def evidence():
    values = client_evidence()
    compose = values.pop("release_compose")
    api = values.pop("api_container")
    web = compose["services"]["web"]
    web.update(
        image="sha256:" + "a" * 64,
        labels={
            "org.opencontainers.image.revision": values["lease"]["revision"],
            "io.depositar.ai.source.revision": values["lease"]["revision"],
            "io.depositar.ai.source.service": "web",
            "io.depositar.ai.source.sha256": "f" * 64,
        },
    )
    compose["services"]["filtered-embedding"]["image"] = "sha256:" + "b" * 64
    values["lease"]["release_compose_sha256"] = client_lease.lease_digest(compose)
    images = {
        "conversation-api": {
            "Id": values["lease"]["api_image_id"],
            "Config": {
                "Cmd": list(client_lease.API_COMMAND),
                "Entrypoint": None,
                "WorkingDir": "/app",
                "Env": list(values["image_environment"]),
                "Labels": copy.deepcopy(
                    compose["services"]["conversation-api"]["labels"]
                ),
            },
        },
        "web": {
            "Id": web["image"],
            "Config": {
                "Cmd": list(release_runtime.WEB_COMMAND),
                "Entrypoint": None,
                "WorkingDir": "/app",
                "Env": [
                    "PATH=/usr/bin",
                    "SOURCE_REVISION=" + values["lease"]["revision"],
                ],
                "Labels": copy.deepcopy(web["labels"]),
            },
        },
        "filtered-embedding": {
            "Id": compose["services"]["filtered-embedding"]["image"],
            "Config": {
                "Cmd": ["serve"],
                "Entrypoint": ["/bin/ollama"],
                "WorkingDir": "",
                "Env": ["PATH=/usr/bin", "NVIDIA_VISIBLE_DEVICES=all"],
            },
        },
    }
    api["Config"]["StdinOnce"] = False
    api["RestartCount"] = 0
    api["State"].update(
        Restarting=False,
        Dead=False,
        Pid=1001,
        StartedAt="2026-10-12T01:02:03.000000000Z",
    )
    containers = [api]
    website = compose["networks"]["website"]["name"]
    website_id = values["lease"]["network_ids"][website]
    for index, role in enumerate(("web", "filtered-embedding"), 2):
        service, image = compose["services"][role], images[role]
        row = copy.deepcopy(api)
        row.update(Id=str(index + 2) * 64, Image=image["Id"])
        row["State"]["Pid"] = 1000 + index
        row["Config"].update(
            Image=image["Id"],
            User=service["user"],
            Cmd=service.get("command", image["Config"]["Cmd"]),
            Entrypoint=image["Config"]["Entrypoint"],
            WorkingDir=image["Config"]["WorkingDir"],
            Env=[
                key + "=" + value
                for key, value in (
                    dict(entry.split("=", 1) for entry in image["Config"]["Env"])
                    | service["environment"]
                ).items()
            ],
            Labels={
                **service.get("labels", {}),
                "com.docker.compose.project": compose["name"],
                "com.docker.compose.service": role,
                "com.docker.compose.oneoff": "False",
            },
            Healthcheck={
                "Test": list(service["healthcheck"]["test"]),
                "Retries": 5,
                "Interval": 20 * 10**9,
                "Timeout": 5 * 10**9,
                "StartPeriod": 0,
                "StartInterval": 0,
            },
        )
        row["HostConfig"].update(
            NanoCpus=service["cpus"] * 10**9,
            Memory=service["mem_limit"],
            MemorySwap=service["memswap_limit"],
            PidsLimit=service["pids_limit"],
            NetworkMode=website,
            Tmpfs={
                entry.split(":", 1)[0]: entry.split(":", 1)[1]
                for entry in service["tmpfs"]
            },
        )
        row["Mounts"] = [
            {
                "Type": mount["type"],
                "Source": mount["source"],
                "Destination": mount["target"],
                "RW": False,
                "Propagation": "rprivate",
            }
            for mount in service["volumes"]
        ]
        row["NetworkSettings"] = {
            "Ports": {},
            "Networks": {
                website: {
                    "NetworkID": website_id,
                    "Aliases": [role, compose["name"] + "-web"],
                }
            },
        }
        containers.append(row)
    network = {
        "Id": website_id,
        "Name": website,
        "Driver": "bridge",
        "Scope": "local",
        "Internal": True,
        "Attachable": False,
        "Ingress": False,
        "Labels": {
            "com.docker.compose.project": compose["name"],
            "com.docker.compose.network": "website",
        },
        "Containers": {row["Id"]: {"Name": "not-authority"} for row in containers},
    }
    return {
        **values,
        "compose": compose,
        "images": images,
        "containers": containers,
        "networks": [network],
    }


def reseal(values):
    values["lease"]["release_compose_sha256"] = client_lease.lease_digest(
        values["compose"]
    )


class ReleaseRuntimeTests(unittest.TestCase):
    def reject(self, values, code=None):
        with self.assertRaises(release_runtime.ReleaseRuntimeError) as raised:
            release_runtime.validate_runtime(**values)
        self.assertRegex(str(raised.exception), r"\Arelease_runtime_[a-z_]+\Z")
        if code:
            self.assertEqual(str(raised.exception), code)

    def test_three_services_exact_unfiltered_peers_and_safe_result(self):
        values = evidence()
        original = copy.deepcopy(values)
        result = release_runtime.validate_runtime(**values)
        self.assertEqual(values, original)
        self.assertEqual(
            set(result),
            {"container_ids", "network_ids", "start_identity_sha256", "lease_sha256"},
        )
        self.assertEqual(set(result["container_ids"]), release_runtime.SERVICES)
        self.assertEqual(result["network_ids"], values["lease"]["network_ids"])
        self.assertEqual(
            result["lease_sha256"], client_lease.lease_digest(values["lease"])
        )
        self.assertNotIn("Env", json.dumps(result))
        self.assertNotIn("/srv", json.dumps(result))
        self.assertRegex(result["start_identity_sha256"], r"\A[0-9a-f]{64}\Z")

    def test_api_checker_receives_actual_api_and_unfiltered_catalog(self):
        values = evidence()
        real = client_lease.validate_client_lease
        with mock.patch.object(
            client_lease, "validate_client_lease", wraps=real
        ) as checker:
            release_runtime.validate_runtime(**values)
        self.assertIs(
            checker.call_args.kwargs["api_container"], values["containers"][0]
        )
        self.assertIs(
            checker.call_args.kwargs["catalog_network"], values["catalog_network"]
        )

    def test_api_capability_or_catalog_peer_drift_is_rejected(self):
        for kind in ("port", "gpu", "mount", "environment", "catalog_peer"):
            with self.subTest(kind=kind):
                values = evidence()
                api = values["containers"][0]
                if kind == "port":
                    api["HostConfig"]["PortBindings"] = {
                        "8000/tcp": [{"HostPort": "8000"}]
                    }
                elif kind == "gpu":
                    api["HostConfig"]["DeviceRequests"] = [{"Driver": "nvidia"}]
                elif kind == "mount":
                    api["Mounts"][0]["RW"] = True
                elif kind == "environment":
                    api["Config"]["Env"].append("UNAPPROVED_SECRET=private-value")
                else:
                    values["catalog_network"]["Containers"]["f" * 64] = {}
                self.reject(values, "release_runtime_client_lease_invalid")

    def test_full_inventory_rejects_extra_missing_duplicate_wrong_project(self):
        for kind in (
            "extra",
            "missing",
            "duplicate",
            "project",
            "oneoff",
            "core_collision",
        ):
            with self.subTest(kind=kind):
                values = evidence()
                if kind == "extra":
                    values["containers"].append(copy.deepcopy(values["containers"][1]))
                elif kind == "missing":
                    values["containers"].pop()
                elif kind == "duplicate":
                    values["containers"][2] = copy.deepcopy(values["containers"][1])
                elif kind in {"project", "oneoff"}:
                    values["containers"][1]["Config"]["Labels"][
                        "com.docker.compose." + kind
                    ] = "wrong"
                else:
                    values["containers"][1]["Id"] = "2" * 64
                self.reject(values)

    def test_website_extra_missing_replaced_gateway_peer_rejected(self):
        for kind in ("extra", "missing", "replacement"):
            with self.subTest(kind=kind):
                values = evidence()
                peers = values["networks"][0]["Containers"]
                if kind != "extra":
                    peers.pop(values["containers"][1]["Id"])
                if kind != "missing":
                    peers["f" * 64] = {"Name": "gateway"}
                self.reject(values, "release_runtime_website_peers_changed")

    def test_website_driver_owner_and_flags_are_closed(self):
        for key, content in (
            ("Id", "f" * 64),
            ("Internal", False),
            ("Attachable", True),
            ("Ingress", True),
            ("Driver", "overlay"),
            ("Scope", "swarm"),
            ("Labels", {"com.docker.compose.project": "unknown"}),
        ):
            with self.subTest(key=key):
                values = evidence()
                values["networks"][0][key] = content
                self.reject(values)

    def test_website_network_required_and_duplicate_or_unknown_network_rejected(self):
        for kind in ("missing", "duplicate", "unknown"):
            with self.subTest(kind=kind):
                values = evidence()
                if kind == "missing":
                    values["networks"] = []
                else:
                    values["networks"].append(copy.deepcopy(values["networks"][0]))
                    if kind == "unknown":
                        values["networks"][1]["Name"] = "unapproved"
                self.reject(values)

    def test_optional_external_snapshot_identity_does_not_replace_peer_validation(self):
        values = evidence()
        values["networks"].append(copy.deepcopy(values["catalog_network"]))
        release_runtime.validate_runtime(**values)
        values["networks"][1]["Id"] = "f" * 64
        self.reject(values, "release_runtime_network_inventory_changed")

    def test_other_services_cannot_attach_external_network_or_wrong_id(self):
        for role_index in (1, 2):
            for kind in ("extra", "id", "mode", "alias"):
                if role_index == 2 and kind == "alias":
                    continue
                with self.subTest(role_index=role_index, kind=kind):
                    values = evidence()
                    row = values["containers"][role_index]
                    attachments = row["NetworkSettings"]["Networks"]
                    name = next(iter(attachments))
                    if kind == "extra":
                        attachments["unauthorized"] = {"NetworkID": "f" * 64}
                    elif kind == "id":
                        attachments[name]["NetworkID"] = "f" * 64
                    elif kind == "mode":
                        row["HostConfig"]["NetworkMode"] = "host"
                    else:
                        attachments[name]["Aliases"] = ["web"]
                    self.reject(values)

    def test_cpu_profiles_reject_resource_gpu_namespace_and_logging_drift(self):
        drifts = (
            ("Runtime", "nvidia"),
            ("ReadonlyRootfs", False),
            ("Privileged", True),
            ("Init", False),
            ("AutoRemove", True),
            ("CapDrop", []),
            ("CapAdd", ["SYS_ADMIN"]),
            ("DeviceRequests", [{"Driver": "nvidia"}]),
            ("Devices", [{"PathOnHost": "/dev/nvidia0"}]),
            ("DeviceCgroupRules", ["c 195:* rmw"]),
            ("VolumesFrom", ["unknown"]),
            ("SecurityOpt", []),
            ("PidMode", "host"),
            ("IpcMode", "host"),
            ("UTSMode", "host"),
            ("UsernsMode", "host"),
            ("CgroupnsMode", "host"),
            ("NanoCpus", 1),
            ("Memory", 1),
            ("MemorySwap", 1),
            ("PidsLimit", 1),
            ("RestartPolicy", {"Name": "always", "MaximumRetryCount": 0}),
            ("LogConfig", {"Type": "none", "Config": {}}),
            ("ExtraHosts", ["evil:127.0.0.1"]),
            ("Dns", ["8.8.8.8"]),
            ("Ulimits", [{"Name": "nofile", "Soft": 1, "Hard": 1}]),
        )
        for index in (1, 2):
            for key, content in drifts:
                with self.subTest(index=index, key=key):
                    values = evidence()
                    values["containers"][index]["HostConfig"][key] = content
                    self.reject(values)

    def test_user_terminal_and_port_drift(self):
        for index in (1, 2):
            for key, content in (("User", "0:0"), ("Tty", True), ("OpenStdin", True)):
                with self.subTest(index=index, key=key):
                    values = evidence()
                    values["containers"][index]["Config"][key] = content
                    self.reject(values)
            for part, key in (
                ("HostConfig", "PortBindings"),
                ("NetworkSettings", "Ports"),
            ):
                with self.subTest(index=index, key=key):
                    values = evidence()
                    values["containers"][index][part][key] = {
                        "8000/tcp": [{"HostPort": "18083"}]
                    }
                    self.reject(values, "release_runtime_publishes_port")

    def test_both_services_reject_command_entrypoint_workingdir_and_environment_drift(
        self,
    ):
        for index in (1, 2):
            for key, content in (
                ("Cmd", ["sh"]),
                ("Entrypoint", ["sh"]),
                ("WorkingDir", "/tmp"),
                ("StopSignal", "SIGKILL"),
                ("Env", ["PATH=/unapproved"]),
            ):
                with self.subTest(index=index, key=key):
                    values = evidence()
                    values["containers"][index]["Config"][key] = content
                    self.reject(values)

    def test_image_contract_and_identity_are_independently_checked(self):
        for role in release_runtime.SERVICES:
            for kind in ("id", "command", "entrypoint", "environment"):
                with self.subTest(role=role, kind=kind):
                    values = evidence()
                    image = values["images"][role]
                    if kind == "id":
                        image["Id"] = "sha256:" + "f" * 64
                    else:
                        image["Config"][
                            {
                                "command": "Cmd",
                                "entrypoint": "Entrypoint",
                                "environment": "Env",
                            }[kind]
                        ] = ["unapproved"]
                    self.reject(values)

    def test_mount_readonly_source_propagation_and_unknown_tmpfs_drift(self):
        for index in (1, 2):
            for kind in (
                "rw",
                "source",
                "propagation",
                "extra",
                "tmpfs",
                "duplicate_tmpfs",
            ):
                with self.subTest(index=index, kind=kind):
                    values = evidence()
                    mounts = values["containers"][index]["Mounts"]
                    if kind == "rw":
                        mounts[0]["RW"] = True
                    elif kind == "source":
                        mounts[0]["Source"] = "/unapproved"
                    elif kind == "propagation":
                        mounts[0]["Propagation"] = "rshared"
                    elif kind == "extra":
                        mounts.append(copy.deepcopy(mounts[0]))
                    else:
                        mounts.append(
                            {
                                "Type": "tmpfs",
                                "Destination": "/unapproved"
                                if kind == "tmpfs"
                                else "/tmp",
                                "RW": True,
                            }
                        )
                        if kind == "duplicate_tmpfs":
                            mounts.append(copy.deepcopy(mounts[-1]))
                    self.reject(values, "release_runtime_mounts_changed")

    def test_approved_tmpfs_mounts_and_exposed_unpublished_port_are_allowed(self):
        values = evidence()
        for row in values["containers"][1:]:
            for target in row["HostConfig"]["Tmpfs"]:
                row["Mounts"].append(
                    {"Type": "tmpfs", "Destination": target, "RW": True}
                )
            row["NetworkSettings"]["Ports"] = {"8000/tcp": None}
        release_runtime.validate_runtime(**values)
        values["containers"][2]["HostConfig"]["Tmpfs"]["/.ollama"] = (
            "rw,size=16m,mode=0777"
        )
        self.reject(values, "release_runtime_resource_policy_changed")

    def test_process_drift_and_changed_start_identity(self):
        for index in (0, 1, 2):
            for key, content in (
                ("Running", False),
                ("Paused", True),
                ("Restarting", True),
                ("Dead", True),
                ("OOMKilled", True),
                ("Pid", True),
                ("StartedAt", ""),
                ("Health", {"Status": "unhealthy"}),
            ):
                with self.subTest(index=index, key=key):
                    values = evidence()
                    values["containers"][index]["State"][key] = content
                    self.reject(values)
            values = evidence()
            before = release_runtime.validate_runtime(**values)["start_identity_sha256"]
            values["containers"][index]["State"]["Pid"] += 1
            after = release_runtime.validate_runtime(**values)["start_identity_sha256"]
            self.assertNotEqual(before, after)
            values["containers"][index]["RestartCount"] = 1
            self.reject(values)

    def test_healthcheck_drift_disabled_and_boolean_numbers_rejected(self):
        for index in (1, 2):
            for key, content in (
                ("Test", ["CMD", "true"]),
                ("Retries", True),
                ("Interval", 1),
                ("Disable", True),
            ):
                with self.subTest(index=index, key=key):
                    values = evidence()
                    values["containers"][index]["Config"]["Healthcheck"][key] = content
                    self.reject(values, "release_runtime_healthcheck_changed")

    def test_unsafe_resealed_service_plans_are_not_accepted(self):
        for role in ("web", "filtered-embedding"):
            for kind in (
                "resources",
                "environment",
                "mount",
                "health",
                "network",
                "extra_field",
            ):
                with self.subTest(role=role, kind=kind):
                    values = evidence()
                    service = values["compose"]["services"][role]
                    if kind == "resources":
                        service["cpus"] = 100
                    elif kind == "environment":
                        service["environment"]["UNAPPROVED"] = "private-value"
                    elif kind == "mount":
                        service["volumes"][0]["read_only"] = False
                    elif kind == "health":
                        service["healthcheck"]["test"] = ["CMD", "true"]
                    elif kind == "network":
                        service["networks"] = ["catalog"]
                    else:
                        service["privileged"] = True
                    reseal(values)
                    self.reject(values)

    def test_malformed_inputs_return_fixed_safe_codes(self):
        for key in (
            "compose",
            "images",
            "containers",
            "networks",
            "lease",
            "catalog_identity",
            "image_proof",
            "image_environment",
            "catalog_network",
        ):
            with self.subTest(key=key):
                values = evidence()
                values[key] = None
                self.reject(values)


if __name__ == "__main__":
    unittest.main()
