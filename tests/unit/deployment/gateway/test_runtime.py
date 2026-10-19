"""Offline gateway capability and unfiltered peer regressions; no runtime IO."""

from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from search.operations.catalog import client_lease
from search.operations.deployment import composition
from search.operations.deployment.gateway import runtime as gateway_runtime
from search.operations.deployment.release import runtime as release_runtime
from tests.unit.deployment.test_composition import catalog, product
from tests.unit.deployment.release.test_runtime import evidence as release_evidence


GATEWAY_ID = "a" * 64
INGRESS_ID = "b" * 64


def evidence():
    values = release_evidence()
    accepted = release_runtime.validate_runtime(**values)
    config = product()
    plan = catalog(config)
    gateway = composition.render_composition(
        config,
        plan,
        composition.QueryBindingPaths.for_catalog(config, plan),
        values["lease"]["revision"],
        uid=21599,
        gid=564,
        inference_source_sha256="6" * 64,
    )["gateway"]
    service = gateway["services"]["web-nginx"]
    labels = release_runtime._labels(values["lease"], "web-nginx", "c" * 64)
    service.update(image="sha256:" + "d" * 64, labels=labels)
    environment = {"PATH": "/usr/local/sbin:/usr/bin", "NGINX_VERSION": "1.30.4"}
    image = {
        "Id": service["image"],
        "Config": {
            "Cmd": ["-g", "daemon off;"],
            "Entrypoint": ["nginx"],
            "User": "1000:1000",
            "WorkingDir": "",
            "StopSignal": "SIGQUIT",
            "Env": [key + "=" + value for key, value in environment.items()],
            "Labels": copy.deepcopy(labels),
            "ExposedPorts": {"80/tcp": {}},
        },
    }
    image_proof = {
        "schema_version": 1,
        "revision": values["lease"]["revision"],
        "service": "web-nginx",
        "source_sha256": "c" * 64,
        "image_id": image["Id"],
        "image_environment_sha256": client_lease.lease_digest(environment),
    }
    row = copy.deepcopy(values["containers"][1])
    row.update(
        Id=GATEWAY_ID, Image=image["Id"], Name="/depositar-ai-gateway-web-nginx-1"
    )
    row["State"]["Pid"] = 2001
    row["Config"].update(
        Image=image["Id"],
        Cmd=list(service["command"]),
        Entrypoint=["nginx"],
        WorkingDir="",
        StopSignal="SIGQUIT",
        Env=[
            key + "=" + value
            for key, value in (environment | service["environment"]).items()
        ],
        Labels={
            **labels,
            "com.docker.compose.project": "depositar-ai-gateway",
            "com.docker.compose.service": "web-nginx",
            "com.docker.compose.oneoff": "False",
            "com.docker.compose.container-number": "1",
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
    mount = service["volumes"][0]
    binding = [{"HostIp": "127.0.0.1", "HostPort": "18083"}]
    row["HostConfig"].update(
        NanoCpus=500000000,
        Memory=64 * 1024**2,
        MemorySwap=64 * 1024**2,
        PidsLimit=32,
        Binds=None,
        Mounts=[
            {
                "Type": "bind",
                "Source": mount["source"],
                "Target": mount["target"],
                "ReadOnly": True,
                "BindOptions": {"CreateMountpoint": False},
            }
        ],
        PortBindings={"8080/tcp": copy.deepcopy(binding)},
    )
    row["Mounts"] = [
        {
            "Type": "bind",
            "Source": mount["source"],
            "Destination": mount["target"],
            "RW": False,
            "Propagation": "rprivate",
        }
    ]
    website = values["networks"][0]
    website["EnableIPv6"] = False
    ingress_name = "depositar-ai-gateway_ingress"
    ingress = {
        "Id": INGRESS_ID,
        "Name": ingress_name,
        "Driver": "bridge",
        "Scope": "local",
        "Internal": False,
        "Attachable": False,
        "Ingress": False,
        "EnableIPv6": False,
        "Labels": {
            "com.docker.compose.project": "depositar-ai-gateway",
            "com.docker.compose.network": "ingress",
        },
        "Containers": {GATEWAY_ID: {"EndpointID": "e" * 64}},
    }
    website["Containers"][GATEWAY_ID] = {"EndpointID": "f" * 64}
    row["NetworkSettings"] = {
        "Ports": {"80/tcp": None, "8080/tcp": binding},
        "Networks": {
            website["Name"]: {"NetworkID": website["Id"], "EndpointID": "f" * 64},
            ingress_name: {"NetworkID": INGRESS_ID, "EndpointID": "e" * 64},
        },
    }
    values["containers"].append(row)
    values["networks"].append(ingress)
    return {
        **values,
        "gateway_compose": gateway,
        "gateway_image": image,
        "gateway_image_proof": image_proof,
        "gateway_container_id": GATEWAY_ID,
        "ingress_network_id": INGRESS_ID,
        "release_proof": accepted,
    }


class GatewayRuntimeTests(unittest.TestCase):
    def reject(self, values):
        before = copy.deepcopy(values)
        with self.assertRaises(gateway_runtime.GatewayRuntimeError) as raised:
            gateway_runtime.validate_runtime(**values)
        self.assertEqual(values, before)
        self.assertRegex(str(raised.exception), r"\Agateway_runtime_[a-z_]+\Z")
        self.assertNotIn("private", str(raised.exception))

    def test_renderer_pinned_image_exact_four_peers_pure_safe_result(self):
        values = evidence()
        before = copy.deepcopy(values)
        with (
            mock.patch("builtins.open", side_effect=AssertionError("no files")),
            mock.patch("pathlib.Path.stat", side_effect=AssertionError("no stats")),
            mock.patch("subprocess.run", side_effect=AssertionError("no processes")),
            mock.patch("socket.socket", side_effect=AssertionError("no network")),
        ):
            first = gateway_runtime.validate_runtime(**values)
            self.assertEqual(first, gateway_runtime.validate_runtime(**values))
        self.assertEqual(values, before)
        self.assertEqual(
            set(first),
            {
                "container_ids",
                "network_ids",
                "start_identity_sha256",
                "release_proof_sha256",
                "gateway_compose_sha256",
                "gateway_image_proof_sha256",
            },
        )
        self.assertEqual(
            set(first["container_ids"]), release_runtime.SERVICES | {"web-nginx"}
        )
        self.assertEqual(first["container_ids"]["web-nginx"], GATEWAY_ID)
        self.assertEqual(
            first["release_proof_sha256"],
            client_lease.lease_digest(values["release_proof"]),
        )
        self.assertNotIn("/srv", json.dumps(first))
        self.assertNotIn("Env", json.dumps(first))
        self.assertNotIn("active", json.dumps(first))

    def test_existing_pre_routing_profile_still_rejects_gateway_peer(self):
        values = release_evidence()
        values["networks"][0]["Containers"][GATEWAY_ID] = {}
        with self.assertRaisesRegex(
            release_runtime.ReleaseRuntimeError, "release_runtime_website_peers_changed"
        ):
            release_runtime.validate_runtime(**values)

    def test_cpu_checker_receives_actual_rows_and_unfiltered_catalog(self):
        values = evidence()
        real = client_lease.validate_client_lease
        with mock.patch.object(
            client_lease, "validate_client_lease", wraps=real
        ) as checker:
            gateway_runtime.validate_runtime(**values)
        self.assertIs(
            checker.call_args.kwargs["api_container"], values["containers"][0]
        )
        self.assertIs(
            checker.call_args.kwargs["catalog_network"], values["catalog_network"]
        )

    def test_native_primary_network_may_be_either_exact_attachment(self):
        for name in evidence()["containers"][-1]["NetworkSettings"]["Networks"]:
            with self.subTest(name=name):
                values = evidence()
                values["containers"][-1]["HostConfig"]["NetworkMode"] = name
                gateway_runtime.validate_runtime(**values)

    def test_native_readonly_bind_options_and_image_exposed_null_port(self):
        for options in (
            {},
            {"CreateMountpoint": False},
            {"Propagation": "rprivate"},
            {"CreateMountpoint": False, "Propagation": "rprivate"},
        ):
            with self.subTest(options=options):
                values = evidence()
                values["containers"][-1]["HostConfig"]["Mounts"][0]["BindOptions"] = (
                    options
                )
                gateway_runtime.validate_runtime(**values)

    def test_gateway_inventory_requires_one_pinned_full_id(self):
        for kind in (
            "extra",
            "missing",
            "duplicate",
            "replacement",
            "short",
            "collision",
        ):
            with self.subTest(kind=kind):
                values = evidence()
                if kind == "extra":
                    values["containers"].append(copy.deepcopy(values["containers"][-1]))
                elif kind == "missing":
                    values["containers"].pop()
                elif kind == "duplicate":
                    values["containers"][1] = copy.deepcopy(values["containers"][-1])
                elif kind == "replacement":
                    values["containers"][-1]["Id"] = "c" * 64
                elif kind == "short":
                    values["gateway_container_id"] = GATEWAY_ID[:12]
                else:
                    values["gateway_container_id"] = values["containers"][0]["Id"]
                self.reject(values)

    def test_exact_unfiltered_website_and_ingress_peers(self):
        for network_index in (0, 1):
            for kind in (
                "extra",
                "missing_gateway",
                "replacement",
                "missing_cpu",
                "non_dict",
            ):
                with self.subTest(network=network_index, kind=kind):
                    values = evidence()
                    peers = values["networks"][network_index]["Containers"]
                    if kind == "extra":
                        peers["c" * 64] = {}
                    elif kind == "missing_gateway":
                        peers.pop(GATEWAY_ID)
                    elif kind == "replacement":
                        peers["c" * 64] = peers.pop(GATEWAY_ID)
                    elif kind == "missing_cpu":
                        if network_index == 0:
                            peers.pop(values["containers"][0]["Id"])
                        else:
                            peers[values["containers"][0]["Id"]] = {}
                    else:
                        values["networks"][network_index]["Containers"] = []
                    self.reject(values)

    def test_cpu_replacement_start_or_capability_still_rejected(self):
        for role_index in range(3):
            for kind in (
                "replacement",
                "start",
                "pid",
                "gpu",
                "writable",
                "secret_env",
            ):
                with self.subTest(role=role_index, kind=kind):
                    values = evidence()
                    row = values["containers"][role_index]
                    if kind == "replacement":
                        old = row["Id"]
                        row["Id"] = "c" * 64
                        peers = values["networks"][0]["Containers"]
                        peers[row["Id"]] = peers.pop(old)
                    elif kind == "start":
                        row["State"]["StartedAt"] += "changed"
                    elif kind == "pid":
                        row["State"]["Pid"] += 1
                    elif kind == "gpu":
                        row["HostConfig"]["DeviceRequests"] = [{"Driver": "nvidia"}]
                    elif kind == "writable":
                        row["HostConfig"]["ReadonlyRootfs"] = False
                    else:
                        row["Config"]["Env"].append("UNAPPROVED_SECRET=private-value")
                    self.reject(values)

    def test_catalog_extra_peer_is_not_filtered_or_reauthorized(self):
        values = evidence()
        values["catalog_network"]["Containers"][GATEWAY_ID] = {}
        self.reject(values)

    def test_only_exact_ipv4_loopback_binding_accepted_in_both_native_views(self):
        for native in ("host", "effective"):
            for kind in (
                "wildcard",
                "v6",
                "empty_ip",
                "localhost",
                "duplicate",
                "extra",
                "wrong_host_port",
                "wrong_target",
                "udp",
                "extra_key",
                "missing",
                "unbound",
            ):
                with self.subTest(native=native, kind=kind):
                    values = evidence()
                    row = values["containers"][-1]
                    ports = (
                        row["HostConfig"]["PortBindings"]
                        if native == "host"
                        else row["NetworkSettings"]["Ports"]
                    )
                    bindings = ports["8080/tcp"]
                    if kind in {"wildcard", "v6", "localhost", "empty_ip"}:
                        bindings[0]["HostIp"] = {
                            "wildcard": "0.0.0.0",
                            "v6": "::1",
                            "localhost": "localhost",
                            "empty_ip": "",
                        }[kind]
                    elif kind == "duplicate":
                        bindings.append(copy.deepcopy(bindings[0]))
                    elif kind == "extra":
                        ports["9000/tcp"] = copy.deepcopy(bindings)
                    elif kind == "wrong_host_port":
                        bindings[0]["HostPort"] = "18084"
                    elif kind in {"wrong_target", "udp"}:
                        ports["8000/tcp" if kind == "wrong_target" else "8080/udp"] = (
                            ports.pop("8080/tcp")
                        )
                    elif kind == "extra_key":
                        bindings[0]["Private"] = "private-value"
                    elif kind == "missing":
                        ports.pop("8080/tcp")
                    else:
                        ports["8080/tcp"] = None
                    self.reject(values)

    def test_network_id_flags_owner_and_gateway_endpoint_are_exact(self):
        for index in (0, 1):
            for key, value in (
                ("Id", "c" * 64),
                ("Driver", "host"),
                ("Scope", "swarm"),
                ("Internal", bool(index)),
                ("Attachable", True),
                ("Ingress", True),
                ("EnableIPv6", True),
            ):
                with self.subTest(index=index, key=key):
                    values = evidence()
                    values["networks"][index][key] = value
                    self.reject(values)
            for label in ("project", "network"):
                values = evidence()
                values["networks"][index]["Labels"]["com.docker.compose." + label] = (
                    "substitute"
                )
                self.reject(values)
        for kind in (
            "extra_network",
            "missing_network",
            "duplicate",
            "endpoint",
            "attachment_id",
            "extra_attachment",
            "host_mode",
        ):
            with self.subTest(kind=kind):
                values = evidence()
                row = values["containers"][-1]
                if kind == "extra_network":
                    values["networks"].append(copy.deepcopy(values["catalog_network"]))
                elif kind == "missing_network":
                    values["networks"].pop()
                elif kind == "duplicate":
                    values["networks"][1] = copy.deepcopy(values["networks"][0])
                elif kind == "endpoint":
                    values["networks"][0]["Containers"][GATEWAY_ID]["EndpointID"] = (
                        "c" * 64
                    )
                elif kind == "attachment_id":
                    next(iter(row["NetworkSettings"]["Networks"].values()))[
                        "NetworkID"
                    ] = "c" * 64
                elif kind == "extra_attachment":
                    row["NetworkSettings"]["Networks"]["catalog-or-gpu"] = {
                        "NetworkID": "c" * 64
                    }
                else:
                    row["HostConfig"]["NetworkMode"] = "host"
                self.reject(values)

    def test_gateway_source_image_command_identity_and_environment_closed(self):
        for kind in (
            "image_id",
            "source_label",
            "image_label",
            "revision",
            "entrypoint",
            "image_cmd",
            "command",
            "user",
            "stop_signal",
            "env",
            "image_env",
            "project",
            "service",
            "oneoff",
            "number",
            "name",
        ):
            with self.subTest(kind=kind):
                values = evidence()
                row = values["containers"][-1]
                if kind == "image_id":
                    row["Image"] = "sha256:" + "f" * 64
                elif kind in {"source_label", "image_label"}:
                    config = (
                        row["Config"]
                        if kind == "source_label"
                        else values["gateway_image"]["Config"]
                    )
                    config["Labels"]["io.depositar.ai.source.sha256"] = "f" * 64
                elif kind == "revision":
                    values["gateway_image_proof"]["revision"] = "e" * 40
                elif kind in {"entrypoint", "command", "user", "stop_signal"}:
                    row["Config"][
                        {
                            "entrypoint": "Entrypoint",
                            "command": "Cmd",
                            "user": "User",
                            "stop_signal": "StopSignal",
                        }[kind]
                    ] = "changed"
                elif kind == "image_cmd":
                    values["gateway_image"]["Config"]["Cmd"] = ["sh"]
                elif kind in {"env", "image_env"}:
                    config = (
                        row["Config"]
                        if kind == "env"
                        else values["gateway_image"]["Config"]
                    )
                    config["Env"].append("PRIVATE=private-value")
                elif kind == "name":
                    row["Name"] = "/substitute"
                else:
                    row["Config"]["Labels"][
                        "com.docker.compose."
                        + {"number": "container-number"}.get(kind, kind)
                    ] = "wrong"
                self.reject(values)

    def test_gateway_resources_namespace_process_and_health_closed(self):
        for key, value in (
            ("Runtime", "nvidia"),
            ("ReadonlyRootfs", False),
            ("Privileged", True),
            ("Init", False),
            ("NanoCpus", 1000000000),
            ("Memory", 128 * 1024**2),
            ("Memory", None),
            ("MemorySwap", True),
            ("PidsLimit", 33),
            ("CapDrop", []),
            ("CapAdd", ["NET_ADMIN"]),
            ("DeviceRequests", [{"Driver": "nvidia"}]),
            ("PidMode", "host"),
            ("IpcMode", "host"),
            ("ExtraHosts", ["private:127.0.0.1"]),
            ("Tmpfs", {}),
            ("Tmpfs", None),
            ("Ulimits", "malformed-private-value"),
        ):
            with self.subTest(key=key):
                values = evidence()
                values["containers"][-1]["HostConfig"][key] = value
                self.reject(values)
        for kind in ("unhealthy", "restarting", "pid", "tty", "health_cmd"):
            values = evidence()
            row = values["containers"][-1]
            if kind == "unhealthy":
                row["State"]["Health"]["Status"] = "unhealthy"
            elif kind == "restarting":
                row["State"]["Restarting"] = True
            elif kind == "pid":
                row["State"]["Pid"] = True
            elif kind == "tty":
                row["Config"]["Tty"] = True
            else:
                row["Config"]["Healthcheck"]["Test"] = ["CMD", "true"]
            self.reject(values)

    def test_gateway_mounts_cannot_gain_secrets_write_or_new_bind_semantics(self):
        for kind in (
            "secret",
            "writable",
            "source",
            "binds",
            "request_write",
            "request_bool_spoof",
            "effective_bool_spoof",
            "create",
            "create_bool_spoof",
            "propagation",
            "extra_option",
            "extra_mount",
        ):
            with self.subTest(kind=kind):
                values = evidence()
                row = values["containers"][-1]
                effective = row["Mounts"][0]
                requested = row["HostConfig"]["Mounts"][0]
                if kind == "secret":
                    effective["Destination"] = "/run/secrets/api_key"
                elif kind == "writable":
                    effective["RW"] = True
                elif kind == "source":
                    effective["Source"] = "/srv/private/secret"
                elif kind == "binds":
                    row["HostConfig"]["Binds"] = ["/srv/private/secret:/secret:ro"]
                elif kind == "request_write":
                    requested["ReadOnly"] = False
                elif kind == "request_bool_spoof":
                    requested["ReadOnly"] = 1
                elif kind == "effective_bool_spoof":
                    effective["RW"] = 0
                elif kind == "create_bool_spoof":
                    requested["BindOptions"] = {"CreateMountpoint": 0}
                elif kind in {"create", "propagation"}:
                    requested["BindOptions"] = (
                        {"CreateMountpoint": True}
                        if kind == "create"
                        else {"Propagation": "shared"}
                    )
                elif kind == "extra_option":
                    requested["VolumeOptions"] = {"NoCopy": True}
                else:
                    row["Mounts"].append(copy.deepcopy(effective))
                self.reject(values)

    def test_malformed_snapshot_and_unapproved_image_exposed_ports_are_closed(self):
        for kind in (
            "containers",
            "networks",
            "config",
            "host",
            "state",
            "attachments",
            "labels",
            "image",
            "proof",
            "environment",
            "exposed",
            "extra_null",
        ):
            with self.subTest(kind=kind):
                values = evidence()
                row = values["containers"][-1]
                if kind in {"containers", "networks"}:
                    values[kind] = None
                elif kind in {"config", "host", "state"}:
                    row[
                        {"config": "Config", "host": "HostConfig", "state": "State"}[
                            kind
                        ]
                    ] = None
                elif kind == "attachments":
                    row["NetworkSettings"]["Networks"] = None
                elif kind == "labels":
                    row["Config"]["Labels"] = []
                elif kind in {"image", "proof"}:
                    values[
                        "gateway_image" if kind == "image" else "gateway_image_proof"
                    ] = None
                elif kind == "environment":
                    row["Config"]["Env"].append("PATH=duplicate-private-value")
                elif kind == "exposed":
                    values["gateway_image"]["Config"]["ExposedPorts"] = {
                        "private-value": {}
                    }
                else:
                    row["NetworkSettings"]["Ports"]["9000/tcp"] = None
                self.reject(values)

    def test_renderer_plan_and_previous_release_proof_are_closed(self):
        for kind in (
            "extra_service",
            "port",
            "image_tag",
            "route",
            "mount",
            "resource",
            "network",
            "proof_extra",
            "proof_image",
            "release_proof",
            "ingress_collision",
        ):
            with self.subTest(kind=kind):
                values = evidence()
                gateway = values["gateway_compose"]
                service = gateway["services"]["web-nginx"]
                if kind == "extra_service":
                    gateway["services"]["sidecar"] = {}
                elif kind == "port":
                    service["ports"][0]["host_ip"] = "0.0.0.0"
                elif kind == "image_tag":
                    service["image"] = "nginx:latest"
                elif kind == "route":
                    gateway["x-depositar-plan"]["approved_upstream"] = "foreign:8000"
                elif kind == "mount":
                    service["volumes"][0]["source"] = "/srv/private/secret"
                elif kind == "resource":
                    service["cpus"] = True
                elif kind == "network":
                    gateway["networks"]["route"]["name"] = "catalog"
                elif kind == "proof_extra":
                    values["gateway_image_proof"]["runtime_verified"] = True
                elif kind == "proof_image":
                    values["gateway_image_proof"]["image_id"] = "nginx:latest"
                elif kind == "release_proof":
                    values["release_proof"]["start_identity_sha256"] = "f" * 64
                else:
                    values["ingress_network_id"] = next(
                        iter(values["lease"]["network_ids"].values())
                    )
                self.reject(values)


if __name__ == "__main__":
    unittest.main()
