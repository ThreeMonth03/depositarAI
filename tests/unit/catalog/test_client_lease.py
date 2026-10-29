"""Offline client capabilities, without granting authority to supplied JSON."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from search.operations.catalog import client_lease, policy
from search.operations.config import GPUConfig, ProductConfig
from search.operations.deployment.composition import (
    QueryBindingPaths,
    render_composition,
)
from search.operations.shared import private_io
from tests.unit.catalog import test_backend as backend_fixture
from tests.unit.catalog import test_runtime as runtime_fixture


REVISION = "a" * 40
BUNDLE = "b" * 64
MANIFEST = "c" * 64
SOURCE = "d" * 64
IMAGE = "sha256:" + "e" * 64
API_ID = "1" * 64
CORE_IDS = {"fess-search": "2" * 64, "opensearch-search": "3" * 64}


def evidence(
    *, catalog_revision=REVISION, revision=REVISION, api_id=API_ID, image_id=IMAGE
):
    """Use the actual renderer; only a future trusted host seal adds ID/labels."""
    config = ProductConfig(
        2,
        Path("/srv/private/depositar-ai"),
        Path("/srv/models/api"),
        Path("/srv/models/embedding"),
        Path("/srv/models/reranker"),
        GPUConfig(
            "GPU-00000000-0000-4000-8000-000000000001",
            (
                "GPU-00000001-0000-4000-8000-000000000001",
                "GPU-00000002-0000-4000-8000-000000000001",
            ),
            "GPU-00000003-0000-4000-8000-000000000001",
        ),
    )
    suffix = BUNDLE[:12] + "-" + catalog_revision[:12]
    work = config.state_root / "catalogs" / suffix
    plan = {
        "schema_version": 1,
        "project": "depositar-ai-catalog-" + suffix,
        "work_root": str(work),
        "data_root": str(work / "data"),
        "tooling_root": str(work / "tooling"),
        "compose_file": str(work / "compose.json"),
        "bundle_path": str(config.state_root / "cache/bundles" / ("sha256-" + BUNDLE)),
        "manifest_sha256": MANIFEST,
        "revision": catalog_revision,
        "document_count": 3618,
        "status": "prepared",
        "source_sha256": {"search/catalog/query.py": "f" * 64},
        "compose_sha256": "4" * 64,
        "security_material_sha256": "5" * 64,
    }
    compose = render_composition(
        config,
        plan,
        QueryBindingPaths.for_catalog(config, plan),
        revision,
        uid=21599,
        gid=564,
        inference_source_sha256="6" * 64,
    )["release"]
    inherited_env = {
        "PATH": "/usr/local/bin:/usr/bin",
        "SOURCE_REVISION": revision,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "HAYSTACK_TELEMETRY_ENABLED": "false",
        "HAYSTACK_CONTENT_TRACING_ENABLED": "false",
    }
    image = {
        "schema_version": 1,
        "revision": revision,
        "service": "conversation-api",
        "source_sha256": SOURCE,
        "image_id": image_id,
        "image_environment_sha256": client_lease.lease_digest(inherited_env),
    }
    labels = {
        "org.opencontainers.image.revision": revision,
        "io.depositar.ai.source.revision": revision,
        "io.depositar.ai.source.service": "conversation-api",
        "io.depositar.ai.source.sha256": SOURCE,
    }
    service = compose["services"]["conversation-api"]
    service.update(image=image_id, labels=labels)
    network_ids = {
        entry["name"]: str(index + 6) * 64
        for index, entry in enumerate(compose["networks"].values())
    }
    catalog_id = network_ids[plan["project"] + "_search_backend"]
    identity = {
        "catalog_project": plan["project"],
        "catalog_network_id": catalog_id,
        "core_container_ids": dict(CORE_IDS),
        "bundle_id": BUNDLE,
        "manifest_sha256": MANIFEST,
        "physical_index": "fess.20261008050000",
        "projection_sha256": "4" * 64,
        "access_sha256": "5" * 64,
    }
    if catalog_revision != revision:
        identity["catalog_revision"] = catalog_revision
    lease = {
        **identity,
        "lease_schema": 1,
        "phase": "private_clients",
        "release_project": compose["name"],
        "release_compose_sha256": client_lease.lease_digest(compose),
        "revision": revision,
        "source_sha256": SOURCE,
        "api_container_id": api_id,
        "api_image_id": image_id,
        "network_ids": network_ids,
    }
    api = {
        "Id": api_id,
        "Image": image_id,
        "Config": {
            "Image": image_id,
            "User": service["user"],
            "Cmd": list(client_lease.API_COMMAND),
            "Entrypoint": None,
            "WorkingDir": "/app",
            "Tty": False,
            "OpenStdin": False,
            "Labels": {
                **labels,
                "com.docker.compose.project": compose["name"],
                "com.docker.compose.service": "conversation-api",
                "com.docker.compose.oneoff": "False",
            },
            "Env": [
                f"{name}={value}"
                for name, value in (inherited_env | service["environment"]).items()
            ],
            "Healthcheck": {
                "Test": list(service["healthcheck"]["test"]),
                "Interval": 20 * 10**9,
                "Timeout": 5 * 10**9,
                "Retries": 5,
                "StartPeriod": 0,
                "StartInterval": 0,
            },
        },
        "HostConfig": {
            "Runtime": "runc",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "AutoRemove": False,
            "Init": True,
            "DeviceRequests": None,
            "Devices": [],
            "DeviceCgroupRules": None,
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "NanoCpus": 2 * 10**9,
            "Memory": 2 * 1024**3,
            "MemorySwap": 2 * 1024**3,
            "PidsLimit": 96,
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "LogConfig": {
                "Type": "json-file",
                "Config": {"max-size": "5m", "max-file": "3"},
            },
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777"},
            "Ulimits": None,
            "PortBindings": {},
            "NetworkMode": next(iter(network_ids)),
        },
        "State": {
            "Running": True,
            "Paused": False,
            "OOMKilled": False,
            "Health": {"Status": "healthy"},
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": item["source"],
                "Destination": item["target"],
                "RW": False,
            }
            for item in service["volumes"]
        ],
        "NetworkSettings": {
            "Ports": {"8000/tcp": None},
            "Networks": {
                name: {"NetworkID": value} for name, value in network_ids.items()
            },
        },
    }
    network = {
        "Id": catalog_id,
        "Name": plan["project"] + "_search_backend",
        "Internal": True,
        "Attachable": False,
        "Labels": {"com.docker.compose.project": plan["project"]},
        "Containers": {
            identifier: {"Name": "not-an-authority"}
            for identifier in (*CORE_IDS.values(), api_id)
        },
    }
    return {
        "lease": lease,
        "catalog_identity": identity,
        "release_compose": compose,
        "image_proof": image,
        "image_environment": [f"{key}={value}" for key, value in inherited_env.items()],
        "api_container": api,
        "catalog_network": network,
    }


def shared_evidence(*, separate_catalogs=False):
    """Two sealed CPU APIs, with real renderer plans and unfiltered snapshots."""
    first = evidence()
    second = evidence(
        catalog_revision="7" * 40 if separate_catalogs else REVISION,
        revision="6" * 40,
        api_id="d" * 64,
        image_id="sha256:" + "f" * 64,
    )
    # Each release has its own website; inference/ranking IDs remain shared.
    website = second["release_compose"]["networks"]["website"]["name"]
    second["lease"]["network_ids"][website] = "a" * 64
    second["api_container"]["NetworkSettings"]["Networks"][website]["NetworkID"] = (
        "a" * 64
    )
    if separate_catalogs:
        cores = {"fess-search": "c" * 64, "opensearch-search": "e" * 64}
        for value in (second["catalog_identity"], second["lease"]):
            value["core_container_ids"] = dict(cores)
            value["catalog_network_id"] = "b" * 64
        network = second["catalog_network"]
        network["Id"] = "b" * 64
        network["Containers"] = {
            identifier: {}
            for identifier in (*cores.values(), second["lease"]["api_container_id"])
        }
        second["lease"]["network_ids"][network["Name"]] = "b" * 64
        second["api_container"]["NetworkSettings"]["Networks"][network["Name"]][
            "NetworkID"
        ] = "b" * 64
    else:
        network = first["catalog_network"]
        network["Containers"][second["lease"]["api_container_id"]] = {
            "Name": "candidate"
        }
        second["catalog_network"] = copy.deepcopy(network)
    return [first, second]


def leased_runtime_evidence(*, catalog_revision=REVISION):
    """Share the real core fixture; preserve the full three-peer network input."""
    values = evidence(catalog_revision=catalog_revision)
    values["catalog_network"].update(Driver="bridge", Scope="local", Ingress=False)
    project = values["catalog_identity"]["catalog_project"]
    work = Path("/srv/private/depositar-ai/catalogs") / project.removeprefix(
        "depositar-ai-catalog-"
    )
    plan = {
        "schema_version": 1,
        "project": project,
        "revision": catalog_revision,
        "manifest_sha256": MANIFEST,
        "work_root": str(work),
        "data_root": str(work / "data"),
        "bundle_path": str(work.parent.parent / "cache/bundles" / ("sha256-" + BUNDLE)),
    }
    compose = backend_fixture.adapted_config(work / "data", project)
    for role, service in compose["services"].items():
        service["healthcheck"].update(
            interval="20s",
            timeout="10s" if role == "ollama-embedding" else "5s",
            start_period={
                "fess-search": "2m0s",
                "opensearch-search": "1m30s",
                "ollama-embedding": "30s",
            }[role],
        )
    fixture = SimpleNamespace(
        plan=plan,
        compose=compose,
        volume=SimpleNamespace(mountpoint="/opaque/docker/volume"),
    )
    runtime_fixture.CatalogRuntimeTests.create_services(fixture)
    images, inspections = {}, {}
    for row in fixture.containers:
        role = row["Config"]["Labels"]["com.docker.compose.service"]
        if role in CORE_IDS:
            row["Id"] = CORE_IDS[role]
            row["NetworkSettings"]["Networks"][project + "_search_backend"] = {
                "NetworkID": values["catalog_network"]["Id"]
            }
        else:
            row["HostConfig"]["NetworkMode"] = "container:" + CORE_IDS["fess-search"]
        images[role] = {"image_id": row["Image"]}
        inspections[role] = {
            "Id": row["Image"],
            "Config": {"Cmd": None, "Entrypoint": None},
        }
    return {
        **values,
        "core_plan": plan,
        "core_compose": compose,
        "core_images": images,
        "core_containers": fixture.containers,
        "core_image_inspections": inspections,
    }


def shared_leased_runtime_evidence():
    values = leased_runtime_evidence()
    second = shared_evidence()[1]
    values["catalog_network"]["Containers"][second["lease"]["api_container_id"]] = {}
    second["catalog_network"] = copy.deepcopy(values["catalog_network"])
    first = {key: values[key] for key in client_lease.CLIENT_FIELDS}
    values["shared_clients"] = [copy.deepcopy(first), second]
    return values


class LeasedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.values = leased_runtime_evidence()

    def verify(self):
        return client_lease.validate_leased_runtime(**self.values)

    def reject(self, error=None):
        with self.assertRaises(client_lease.LeaseError) as caught:
            self.verify()
        self.assertRegex(str(caught.exception), r"^catalog_client_[a-z_]+$")
        if error is not None:
            self.assertEqual(error, str(caught.exception))

    def test_combines_actual_core_profiles_with_unfiltered_three_peer_network(self):
        before = copy.deepcopy(self.values)
        result = self.verify()
        self.assertEqual("verified", result["status"])
        self.assertIs(result["core_runtime_verified"], True)
        self.assertEqual(
            {**CORE_IDS, "ollama-embedding": "c" * 64},
            result["catalog_container_ids"],
        )
        self.assertEqual(before, self.values)
        self.assertEqual(result, self.verify())
        for key in (
            "core_compose",
            "core_images",
            "core_image_inspections",
            "environment",
            "volumes",
            "work_root",
            "data_root",
            "bundle_path",
        ):
            self.assertNotIn(key, result)

    def test_new_api_can_use_the_same_origin_catalog_with_an_explicit_revision(self):
        self.values = leased_runtime_evidence(catalog_revision="f" * 40)
        before = copy.deepcopy(self.values)
        result = self.verify()
        self.assertEqual("f" * 40, result["catalog_revision"])
        self.assertEqual(REVISION, result["revision"])
        self.assertIs(result["core_runtime_verified"], True)
        self.assertEqual(before, self.values)
        self.values["core_plan"]["revision"] = REVISION
        self.reject("catalog_client_core_catalog_binding_changed")

    def test_does_not_weaken_baseline_exact_two_peer_contract(self):
        with self.assertRaisesRegex(policy.RuntimeError, "network_peers_changed"):
            policy.validate_runtime(
                self.values["core_plan"],
                self.values["core_compose"],
                self.values["core_images"],
                self.values["core_containers"],
                self.values["catalog_network"],
            )
        self.verify()

    def test_passes_the_original_network_object_without_fabricating_a_baseline(self):
        original = client_lease.validate_client_lease
        network = self.values["catalog_network"]
        with (
            mock.patch.object(
                policy, "validate_runtime", side_effect=AssertionError("no baseline")
            ),
            mock.patch.object(
                client_lease, "validate_client_lease", wraps=original
            ) as check,
        ):
            self.verify()
        self.assertIs(check.call_args.kwargs["catalog_network"], network)
        self.assertEqual(3, len(network["Containers"]))

    def test_unknown_fourth_peer_or_missing_core_is_rejected(self):
        for case in ("extra", "missing", "filtered_baseline"):
            with self.subTest(case=case):
                self.values = leased_runtime_evidence()
                peers = self.values["catalog_network"]["Containers"]
                if case == "extra":
                    peers["f" * 64] = {}
                elif case == "missing":
                    peers.pop(CORE_IDS["fess-search"])
                else:
                    peers.pop(API_ID)
                self.reject("catalog_client_network_peers_changed")

    def test_actual_core_network_ids_must_match_the_same_catalog_snapshot(self):
        for role in client_lease.CORES:
            for value in ("f" * 64, None, False, "missing"):
                with self.subTest(role=role, value=value):
                    self.values = leased_runtime_evidence()
                    row = next(
                        row
                        for row in self.values["core_containers"]
                        if row["Config"]["Labels"]["com.docker.compose.service"] == role
                    )
                    name = self.values["lease"]["catalog_project"] + "_search_backend"
                    attachment = row["NetworkSettings"]["Networks"][name]
                    if value == "missing":
                        attachment.pop("NetworkID")
                    else:
                        attachment["NetworkID"] = value
                    self.reject("catalog_client_core_network_identity_changed")

    def test_catalog_network_is_only_a_non_ingress_local_bridge(self):
        for field, changed in (
            ("Driver", "overlay"),
            ("Driver", "macvlan"),
            ("Scope", "swarm"),
            ("Ingress", True),
            ("Ingress", 0),
        ):
            with self.subTest(field=field, changed=changed):
                self.values = leased_runtime_evidence()
                self.values["catalog_network"][field] = changed
                self.reject("catalog_client_network_driver_changed")
        for field in ("Driver", "Scope", "Ingress"):
            with self.subTest(missing=field):
                self.values = leased_runtime_evidence()
                self.values["catalog_network"].pop(field)
                self.reject("catalog_client_network_driver_changed")

    def test_every_core_retains_resource_mount_health_and_gpu_restrictions(self):
        mutations = (
            lambda row: row["HostConfig"].update(Memory=1),
            lambda row: row["HostConfig"].update(DeviceRequests=[{"Driver": "nvidia"}]),
            lambda row: row["HostConfig"].update(Runtime="nvidia"),
            lambda row: row["HostConfig"].update(
                PortBindings={"8080/tcp": [{"HostPort": "8080"}]}
            ),
            lambda row: row["Mounts"][0].update(RW=not row["Mounts"][0]["RW"]),
            lambda row: row["State"]["Health"].update(Status="unhealthy"),
        )
        for number in range(3):
            for mutate in mutations:
                with self.subTest(core=number, mutation=mutate):
                    self.values = leased_runtime_evidence()
                    mutate(self.values["core_containers"][number])
                    self.reject("catalog_client_core_runtime_changed")

    def test_core_image_identity_command_and_entrypoint_cannot_drift(self):
        for role in policy.SERVICES:
            for case in ("image", "inspection", "command", "entrypoint"):
                with self.subTest(role=role, case=case):
                    self.values = leased_runtime_evidence()
                    row = next(
                        row
                        for row in self.values["core_containers"]
                        if row["Config"]["Labels"]["com.docker.compose.service"] == role
                    )
                    if case == "image":
                        row["Image"] = "sha256:" + "f" * 64
                        self.reject("catalog_client_core_runtime_changed")
                    else:
                        if case == "inspection":
                            self.values["core_image_inspections"][role]["Id"] = (
                                "sha256:" + "f" * 64
                            )
                        else:
                            field = "Cmd" if case == "command" else "Entrypoint"
                            row["Config"][field] = ["sh"]
                        self.reject("catalog_client_core_image_startup_changed")

    def test_native_image_omitted_command_matches_container_null(self):
        role = "fess-search"
        row = next(
            row
            for row in self.values["core_containers"]
            if row["Config"]["Labels"]["com.docker.compose.service"] == role
        )
        image = self.values["core_image_inspections"][role]
        entrypoint = ["/usr/local/bin/depositar-fess-entrypoint"]
        image["Config"].pop("Cmd")
        image["Config"]["Entrypoint"] = list(entrypoint)
        row["Config"]["Entrypoint"] = list(entrypoint)
        self.assertIsNone(row["Config"]["Cmd"])
        before = copy.deepcopy(self.values)
        self.assertEqual("verified", self.verify()["status"])
        self.assertEqual(before, self.values)

    def test_optional_null_startup_fields_can_be_omitted_without_mutation(self):
        for role in policy.SERVICES:
            for field in ("Cmd", "Entrypoint"):
                for omitted in ("image", "container", "both"):
                    with self.subTest(role=role, field=field, omitted=omitted):
                        self.values = leased_runtime_evidence()
                        row = next(
                            row
                            for row in self.values["core_containers"]
                            if row["Config"]["Labels"]["com.docker.compose.service"]
                            == role
                        )
                        image = self.values["core_image_inspections"][role]
                        other = "Entrypoint" if field == "Cmd" else "Cmd"
                        for config in (row["Config"], image["Config"]):
                            config[other] = ["/immutable-image-command"]
                        if omitted in ("image", "both"):
                            image["Config"].pop(field)
                        if omitted in ("container", "both"):
                            row["Config"].pop(field)
                        before = copy.deepcopy(self.values)
                        self.assertEqual("verified", self.verify()["status"])
                        self.assertEqual(before, self.values)

    def test_nonempty_startup_fields_cannot_be_missing_or_changed_on_either_side(self):
        for role in policy.SERVICES:
            for field in ("Cmd", "Entrypoint"):
                for changed_side in ("image", "container"):
                    for change in ("missing", "different"):
                        with self.subTest(
                            role=role,
                            field=field,
                            side=changed_side,
                            change=change,
                        ):
                            self.values = leased_runtime_evidence()
                            row = next(
                                row
                                for row in self.values["core_containers"]
                                if row["Config"]["Labels"]["com.docker.compose.service"]
                                == role
                            )
                            image = self.values["core_image_inspections"][role]
                            for config in (row["Config"], image["Config"]):
                                config[field] = ["/immutable-image-command"]
                            self.verify()
                            config = (
                                image["Config"]
                                if changed_side == "image"
                                else row["Config"]
                            )
                            if change == "missing":
                                config.pop(field)
                            else:
                                config[field] = ["/changed-command"]
                            before = copy.deepcopy(self.values)
                            self.reject("catalog_client_core_image_startup_changed")
                            self.assertEqual(before, self.values)

    def test_empty_startup_lists_are_not_normalized_to_null(self):
        for field in ("Cmd", "Entrypoint"):
            for changed_side in ("image", "container"):
                with self.subTest(field=field, side=changed_side):
                    self.values = leased_runtime_evidence()
                    row = self.values["core_containers"][0]
                    role = row["Config"]["Labels"]["com.docker.compose.service"]
                    image = self.values["core_image_inspections"][role]
                    config = (
                        image["Config"] if changed_side == "image" else row["Config"]
                    )
                    config[field] = []
                    before = copy.deepcopy(self.values)
                    self.reject("catalog_client_core_image_startup_changed")
                    self.assertEqual(before, self.values)

    def test_inspections_require_every_and_only_the_three_core_roles(self):
        for case in ("missing", "extra", "list", "bad_config"):
            with self.subTest(case=case):
                self.values = leased_runtime_evidence()
                inspections = self.values["core_image_inspections"]
                if case == "missing":
                    inspections.pop("fess-search")
                elif case == "extra":
                    inspections["conversation-api"] = {}
                elif case == "list":
                    self.values["core_image_inspections"] = []
                elif case == "bad_config":
                    inspections["fess-search"]["Config"] = None
                self.reject()

    def test_core_container_id_must_match_the_sealed_lease(self):
        row = next(
            row
            for row in self.values["core_containers"]
            if row["Config"]["Labels"]["com.docker.compose.service"]
            == "opensearch-search"
        )
        row["Id"] = "f" * 64
        self.reject("catalog_client_core_catalog_binding_changed")

    def test_all_four_container_identities_must_be_distinct(self):
        embedding = next(
            row
            for row in self.values["core_containers"]
            if row["Config"]["Labels"]["com.docker.compose.service"]
            == "ollama-embedding"
        )
        embedding["Id"] = API_ID
        self.reject("catalog_client_core_catalog_binding_changed")

    def test_catalog_manifest_source_and_project_bindings_are_not_interchangeable(self):
        for field, changed in (
            ("schema_version", True),
            ("manifest_sha256", "f" * 64),
            ("revision", "f" * 40),
        ):
            with self.subTest(field=field):
                self.values = leased_runtime_evidence()
                self.values["core_plan"][field] = changed
                self.reject("catalog_client_core_catalog_binding_changed")
        self.values = leased_runtime_evidence()
        self.values["core_plan"]["project"] = "another-catalog"
        self.reject("catalog_client_core_runtime_changed")

    def test_core_paths_and_api_mounts_must_name_the_same_prepared_catalog(self):
        for field in ("work_root", "data_root", "bundle_path"):
            with self.subTest(field=field):
                self.values = leased_runtime_evidence()
                self.values["core_plan"][field] = "/srv/another-catalog"
                self.reject("catalog_client_core_catalog_paths_changed")

    def test_other_api_container_or_wrong_lease_retains_existing_rejections(self):
        for case in ("container", "lease"):
            with self.subTest(case=case):
                self.values = leased_runtime_evidence()
                if case == "container":
                    self.values["api_container"]["Id"] = "f" * 64
                else:
                    self.values["lease"]["revision"] = "f" * 40
                self.reject()


class ClientLeaseTests(unittest.TestCase):
    def setUp(self):
        self.values = evidence()

    def verify(self):
        values = dict(self.values)
        lease = values.pop("lease")
        return client_lease.validate_client_lease(lease, **values)

    def reseal_compose(self):
        self.values["lease"]["release_compose_sha256"] = client_lease.lease_digest(
            self.values["release_compose"]
        )

    def reject(self, error=None):
        with self.assertRaises(client_lease.LeaseError) as caught:
            self.verify()
        self.assertRegex(str(caught.exception), r"^catalog_client_[a-z_]+$")
        if error is not None:
            self.assertEqual(error, str(caught.exception))

    def test_exact_three_peers_and_immutable_api_returns_sanitized_proof(self):
        result = self.verify()
        self.assertEqual("verified", result["status"])
        self.assertEqual(
            sorted([API_ID, *CORE_IDS.values()]), result["expected_peer_ids"]
        )
        self.assertEqual(
            client_lease.lease_digest(self.values["lease"]), result["lease_sha256"]
        )
        self.assertEqual(SOURCE, result["source_sha256"])
        encoded = private_io.canonical(result).decode()
        for private in ("/srv/", "SEARCH_API_", "not-an-authority", "Env", "Mounts"):
            self.assertNotIn(private, encoded)

    def test_origin_revision_is_bound_separately_from_current_api_source(self):
        self.values = evidence(catalog_revision="f" * 40)
        result = self.verify()
        self.assertEqual("f" * 40, result["catalog_revision"])
        self.assertEqual(REVISION, result["revision"])
        self.assertEqual(IMAGE, result["api_image_id"])
        self.assertTrue(result["catalog_project"].endswith("f" * 12))
        self.assertTrue(result["release_project"].endswith(REVISION[:12]))

    def test_cross_revision_records_cannot_omit_change_or_invent_provenance(self):
        for record in ("lease", "catalog_identity"):
            for value in (None, True, "f" * 12, "e" * 40, REVISION):
                with self.subTest(record=record, value=value):
                    self.values = evidence(catalog_revision="f" * 40)
                    if value is None:
                        self.values[record].pop("catalog_revision")
                    else:
                        self.values[record]["catalog_revision"] = value
                    self.reject()
        self.values = evidence(catalog_revision="f" * 40)
        self.values["release_compose"]["x-depositar-plan"].pop("catalog_revision")
        self.reseal_compose()
        self.reject("catalog_client_compose_binding_changed")

    def test_reused_catalog_does_not_relax_api_images_or_actual_network_peers(self):
        self.values = evidence(catalog_revision="f" * 40)
        self.values["image_proof"]["revision"] = "f" * 40
        self.reject("catalog_client_release_identity_changed")
        self.values = evidence(catalog_revision="f" * 40)
        self.values["catalog_network"]["Containers"]["9" * 64] = {}
        self.reject("catalog_client_network_peers_changed")

    def test_legacy_same_revision_records_keep_the_existing_output_schema(self):
        self.assertNotIn("catalog_revision", self.values["lease"])
        self.assertNotIn("catalog_revision", self.values["catalog_identity"])
        self.assertNotIn("catalog_revision", self.verify())

    def test_is_pure_deterministic_and_does_not_alias_inputs(self):
        original = copy.deepcopy(self.values)
        with (
            mock.patch("builtins.open", side_effect=AssertionError("no IO")),
            mock.patch("pathlib.Path.stat", side_effect=AssertionError("no IO")),
            mock.patch("subprocess.run", side_effect=AssertionError("no IO")),
            mock.patch("subprocess.Popen", side_effect=AssertionError("no IO")),
        ):
            result = self.verify()
            self.assertEqual(result, self.verify())
        self.assertEqual(original, self.values)
        result["core_container_ids"]["fess-search"] = "changed"
        result["network_ids"].clear()
        self.assertEqual(original, self.values)

    def test_canonical_digest_matches_shared_receipt_bytes(self):
        value = {"z": 1, "a": "資料"}
        self.assertEqual(
            hashlib.sha256(private_io.canonical(value)).hexdigest(),
            client_lease.lease_digest(value),
        )
        self.assertEqual(
            client_lease.lease_digest(value),
            client_lease.lease_digest({"a": "資料", "z": 1}),
        )
        for value in (float("nan"), {"bad": object()}):
            with (
                self.subTest(value=type(value)),
                self.assertRaisesRegex(
                    client_lease.LeaseError, "catalog_client_record_invalid"
                ),
            ):
                client_lease.lease_digest(value)
        with self.assertRaisesRegex(
            client_lease.LeaseError, "catalog_client_record_too_large"
        ):
            client_lease.lease_digest("x" * (2 * 1024**2))

    def test_closed_startup_profile_matches_reviewed_dockerfile(self):
        root = Path(__file__).resolve().parents[3]
        source = (root / "deploy/docker/api/Dockerfile").read_text()
        commands = [line[4:] for line in source.splitlines() if line.startswith("CMD ")]
        self.assertEqual(
            [list(client_lease.API_COMMAND)], [json.loads(value) for value in commands]
        )
        self.assertIn("WORKDIR /app", source.splitlines())
        self.assertFalse(
            any(line.startswith("ENTRYPOINT ") for line in source.splitlines())
        )

    def test_closed_lease_catalog_image_and_composition_records(self):
        for key in ("lease", "catalog_identity", "image_proof", "release_compose"):
            with self.subTest(record=key):
                self.values = evidence()
                self.values[key]["extra"] = "untrusted"
                self.reject()
                self.values = evidence()
                self.values[key].pop(next(iter(self.values[key])))
                self.reject()

    def test_tags_and_legacy_source_label_namespace_never_grant_access(self):
        service = self.values["release_compose"]["services"]["conversation-api"]
        service["image"] = "depositar-search-conversation:" + REVISION
        self.reseal_compose()
        self.reject()
        self.values = evidence()
        labels = self.values["api_container"]["Config"]["Labels"]
        labels["io.depositar.ai.release.source-sha256"] = labels.pop(
            "io.depositar.ai.source.sha256"
        )
        self.reject()

    def test_strict_schema_numbers_do_not_accept_boolean_or_float(self):
        for record, key in (
            ("lease", "lease_schema"),
            ("image_proof", "schema_version"),
        ):
            for value in (True, 1.0, "1"):
                with self.subTest(record=record, value=value):
                    self.values = evidence()
                    self.values[record][key] = value
                    self.reject()

    def test_all_catalog_summary_fields_must_match_host_verified_identity(self):
        for key in client_lease.CATALOG_FIELDS:
            with self.subTest(field=key):
                self.values = evidence()
                self.values["catalog_identity"][key] = "changed"
                self.reject("catalog_client_catalog_identity_changed")

    def test_source_revision_service_or_image_proof_drift(self):
        for key, value in (
            ("revision", "f" * 40),
            ("service", "web"),
            ("source_sha256", "f" * 64),
            ("image_id", "sha256:" + "f" * 64),
        ):
            with self.subTest(field=key):
                self.values = evidence()
                self.values["image_proof"][key] = value
                self.reject("catalog_client_release_identity_changed")

    def test_identity_format_duplicate_ids_and_project_names_are_rejected(self):
        for key, value in (
            ("revision", "a" * 12),
            ("api_container_id", "1" * 12),
            ("api_image_id", "tag:latest"),
            ("physical_index", "fess.*"),
            ("catalog_network_id", "8" * 12),
            ("phase", "baseline"),
            ("release_project", "attacker"),
        ):
            with self.subTest(field=key):
                self.values = evidence()
                self.values["lease"][key] = value
                self.reject()
        self.values = evidence()
        self.values["lease"]["core_container_ids"]["fess-search"] = API_ID
        self.reject("catalog_client_core_identity_invalid")

    def test_unknown_missing_or_copied_name_network_peers_are_not_filtered(self):
        for change in ("extra", "missing", "replacement"):
            with self.subTest(change=change):
                self.values = evidence()
                peers = self.values["catalog_network"]["Containers"]
                if change != "extra":
                    peers.pop(API_ID)
                if change != "missing":
                    peers["f" * 64] = {"Name": "conversation-api"}
                self.reject("catalog_client_network_peers_changed")

    def test_catalog_network_identity_internal_and_attachment_policy(self):
        for key, value in (
            ("Id", "f" * 64),
            ("Name", "wrong"),
            ("Internal", False),
            ("Attachable", True),
            ("Containers", []),
            ("Labels", {}),
        ):
            with self.subTest(field=key):
                self.values = evidence()
                self.values["catalog_network"][key] = value
                self.reject("catalog_client_network_peers_changed")

    def test_baseline_policy_remains_exact_two_peers(self):
        with mock.patch("builtins.open", side_effect=AssertionError("no IO")):
            self.verify()
        self.assertEqual(
            frozenset({"fess-search", "opensearch-search", "ollama-embedding"}),
            policy.SERVICES,
        )
        self.assertNotIn("conversation-api", policy.SERVICES)

    def test_network_plan_names_ids_and_external_flags_are_bound(self):
        cases = ("name", "internal", "external", "duplicate_id", "missing_id")
        for case in cases:
            with self.subTest(case=case):
                self.values = evidence()
                networks = self.values["release_compose"]["networks"]
                ids = self.values["lease"]["network_ids"]
                if case == "name":
                    networks["catalog"]["name"] = "wrong"
                elif case == "internal":
                    networks["website"]["internal"] = False
                elif case == "external":
                    networks["catalog"]["external"] = False
                elif case == "duplicate_id":
                    ids[networks["ranking"]["name"]] = ids[networks["website"]["name"]]
                else:
                    ids.pop(networks["website"]["name"])
                self.reseal_compose()
                self.reject()

    def test_composition_bytes_and_required_binding_flags_are_verified(self):
        self.values["release_compose"]["services"]["web"]["environment"]["extra"] = "x"
        self.reject("catalog_client_composition_changed")
        for key in (
            "catalog_client_lease_required",
            "query_provisioning_required",
            "immutable_built_images_required",
            "generated_answers",
        ):
            with self.subTest(flag=key):
                self.values = evidence()
                plan = self.values["release_compose"]["x-depositar-plan"]
                plan[key] = not plan[key]
                self.reseal_compose()
                self.reject("catalog_client_compose_binding_changed")

    def test_renderer_api_fields_are_closed_and_required_source_labels_exact(self):
        for field, value in (
            ("ports", ["0.0.0.0:8000:8000"]),
            ("privileged", True),
            ("deploy", {}),
            ("labels", {}),
            ("user", "0:0"),
            ("user", "1000"),
            ("cpus", 2.0),
            ("pids_limit", True),
        ):
            with self.subTest(field=field):
                self.values = evidence()
                self.values["release_compose"]["services"]["conversation-api"][
                    field
                ] = value
                self.reseal_compose()
                self.reject("catalog_client_api_plan_invalid")

    def test_fixed_health_probe_cannot_be_replaced_by_arbitrary_command(self):
        service = self.values["release_compose"]["services"]["conversation-api"]
        service["healthcheck"]["test"][-1] = "print('healthy')"
        self.reseal_compose()
        self.reject("catalog_client_health_plan_invalid")

    def test_five_ro_query_mounts_are_exact_canonical_and_exclude_admin_data(self):
        for case in (
            "missing",
            "duplicate",
            "rw",
            "create",
            "admin",
            "relative",
            "dotdot",
            "double_slash",
        ):
            with self.subTest(case=case):
                self.values = evidence()
                mounts = self.values["release_compose"]["services"]["conversation-api"][
                    "volumes"
                ]
                if case == "missing":
                    mounts.pop()
                elif case == "duplicate":
                    mounts[-1] = copy.deepcopy(mounts[0])
                elif case == "rw":
                    mounts[0]["read_only"] = False
                elif case == "create":
                    mounts[0]["bind"]["create_host_path"] = True
                else:
                    mounts[0]["source"] = {
                        "admin": "/srv/private/admin-password",
                        "relative": "relative",
                        "dotdot": "/srv/private/../admin",
                        "double_slash": "/srv//private",
                    }[case]
                self.reseal_compose()
                self.reject("catalog_client_mount_plan_invalid")

    def test_retired_fess_query_mount_and_prefilter_flag_are_not_lease_inputs(self):
        for case in ("fess_mount", "prefilter_flag"):
            with self.subTest(case=case):
                self.values = evidence()
                service = self.values["release_compose"]["services"]["conversation-api"]
                if case == "fess_mount":
                    mount = copy.deepcopy(service["volumes"][1])
                    mount["target"] = "/run/secrets/fess_query_password"
                    mount["source"] = "/srv/private/fess-query-password"
                    service["volumes"].append(mount)
                else:
                    service["environment"]["SEARCH_API_INDEX_PREFILTER"] = "1"
                self.reseal_compose()
                self.reject(
                    "catalog_client_mount_plan_invalid"
                    if case == "fess_mount"
                    else "catalog_client_environment_plan_invalid"
                )

    def test_actual_id_image_project_service_and_source_labels_are_bound(self):
        for case in (
            "Id",
            "Image",
            "ConfigImage",
            "project",
            "service",
            "source",
            "revision",
            "oneoff",
        ):
            with self.subTest(case=case):
                self.values = evidence()
                row = self.values["api_container"]
                if case in ("Id", "Image"):
                    row[case] = "f" * 64
                elif case == "ConfigImage":
                    row["Config"]["Image"] = "latest"
                else:
                    key = {
                        "project": "com.docker.compose.project",
                        "service": "com.docker.compose.service",
                        "source": "io.depositar.ai.source.sha256",
                        "revision": "org.opencontainers.image.revision",
                        "oneoff": "com.docker.compose.oneoff",
                    }[case]
                    row["Config"]["Labels"][key] = "changed"
                self.reject("catalog_client_container_identity_changed")

    def test_actual_unprivileged_resources_and_private_namespaces(self):
        cases = {
            "Runtime": "nvidia",
            "Privileged": True,
            "ReadonlyRootfs": False,
            "AutoRemove": True,
            "Init": False,
            "DeviceRequests": [{"Driver": "nvidia"}],
            "Devices": [{"PathOnHost": "/dev/nvidia0"}],
            "DeviceCgroupRules": ["c 195:* rwm"],
            "CapAdd": ["SYS_ADMIN"],
            "CapDrop": [],
            "SecurityOpt": [],
            "NanoCpus": 0,
            "Memory": 0,
            "MemorySwap": -1,
            "PidsLimit": -1,
            "PidMode": "host",
            "IpcMode": "host",
            "UTSMode": "host",
            "UsernsMode": "host",
            "CgroupnsMode": "host",
            "VolumesFrom": ["admin"],
            "Links": ["attacker"],
            "Ulimits": [{"Name": "nofile", "Soft": 99999, "Hard": 99999}],
            "Tmpfs": {"/tmp": "rw,exec,size=64m"},
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                self.values = evidence()
                self.values["api_container"]["HostConfig"][field] = value
                self.reject()

    def test_actual_root_user_tty_or_stdin_is_rejected(self):
        for field, value in (("User", "0:0"), ("Tty", True), ("OpenStdin", True)):
            with self.subTest(field=field):
                self.values = evidence()
                self.values["api_container"]["Config"][field] = value
                self.reject("catalog_client_resource_policy_changed")

    def test_unsealed_extra_hosts_and_dns_cannot_redirect_query_credentials(self):
        for field, value in (
            ("ExtraHosts", ["opensearch-search:192.168.1.1"]),
            ("Dns", ["192.168.1.1"]),
            ("DnsOptions", ["ndots:0"]),
            ("DnsSearch", ["attacker.invalid"]),
        ):
            with self.subTest(field=field):
                self.values = evidence()
                self.values["api_container"]["HostConfig"][field] = value
                self.reject("catalog_client_resource_policy_changed")

    def test_runtime_annotations_cannot_add_unsealed_cdi_or_device_access(self):
        self.values["api_container"]["HostConfig"]["Annotations"] = {
            "cdi.k8s.io/devices": "nvidia.com/gpu=all"
        }
        self.reject("catalog_client_resource_policy_changed")

    def test_same_image_cannot_override_its_command_entrypoint_or_workdir(self):
        for field, value in (
            ("Cmd", ["python", "-m", "http.server"]),
            ("Entrypoint", ["sh"]),
            ("WorkingDir", "/tmp"),
        ):
            with self.subTest(field=field):
                self.values = evidence()
                self.values["api_container"]["Config"][field] = value
                self.reject("catalog_client_resource_policy_changed")

    def test_actual_health_and_configured_healthcheck_both_must_match(self):
        for field, value in (
            ("Running", False),
            ("Paused", True),
            ("OOMKilled", True),
            ("Health", {"Status": "starting"}),
        ):
            with self.subTest(field=field):
                self.values = evidence()
                self.values["api_container"]["State"][field] = value
                self.reject("catalog_client_not_healthy")
        for field, value in (
            ("Retries", 5.0),
            ("Interval", 5 * 10**9),
            ("StartPeriod", True),
            ("Test", ["CMD", "true"]),
        ):
            with self.subTest(field=field):
                self.values = evidence()
                self.values["api_container"]["Config"]["Healthcheck"][field] = value
                self.reject("catalog_client_healthcheck_changed")

    def test_exposed_unpublished_ports_are_ok_but_all_host_bindings_rejected(self):
        self.verify()
        for section, field in (
            ("HostConfig", "PortBindings"),
            ("NetworkSettings", "Ports"),
        ):
            with self.subTest(section=section):
                self.values = evidence()
                self.values["api_container"][section][field] = {
                    "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18081"}]
                }
                self.reject("catalog_client_publishes_a_port")

    def test_image_inherited_environment_is_allowed_but_extra_or_override_env_fail(
        self,
    ):
        self.verify()
        for addition in (
            "NVIDIA_VISIBLE_DEVICES=all",
            "PYTHONPATH=/tmp",
            "HAYSTACK_TELEMETRY_ENABLED=true",
            "HAYSTACK_CONTENT_TRACING_ENABLED=true",
            "HTTP_PROXY=http://untrusted.invalid",
            "invalid",
            1,
        ):
            with self.subTest(addition=addition):
                self.values = evidence()
                self.values["api_container"]["Config"]["Env"].append(addition)
                self.reject("catalog_client_environment_changed")
        self.values = evidence()
        env = self.values["api_container"]["Config"]["Env"]
        env.remove("SOURCE_REVISION=" + REVISION)
        env.append("SOURCE_REVISION=development")
        self.reject("catalog_client_environment_changed")

    def test_environment_overrides_cannot_hide_as_nonduplicate_inherited_keys(self):
        for key, value in (
            ("HAYSTACK_TELEMETRY_ENABLED", "true"),
            ("HAYSTACK_CONTENT_TRACING_ENABLED", "true"),
            ("PATH", "/tmp"),
        ):
            with self.subTest(key=key):
                self.values = evidence()
                env = self.values["api_container"]["Config"]["Env"]
                env[:] = [entry for entry in env if not entry.startswith(key + "=")]
                env.append(key + "=" + value)
                self.reject("catalog_client_environment_changed")

    def test_image_environment_map_digest_is_independent_of_container_env(self):
        inherited = self.values["image_environment"]
        inherited.reverse()
        self.verify()
        inherited.append("NEW_VARIABLE=unsealed")
        self.reject("catalog_client_image_environment_changed")
        self.values = evidence()
        self.values["image_environment"] = self.values["api_container"]["Config"]["Env"]
        self.reject("catalog_client_image_environment_changed")

    def test_image_environment_parser_bounds_duplicate_and_malformed_entries(self):
        for invalid in (
            None,
            {},
            ["PATH=a", "PATH=b"],
            ["NUL=\x00"],
            ["NEWLINE=\n"],
            ["-INVALID=value"],
            [1],
            ["A=" + "x" * 4096],
            [f"V{index}=x" for index in range(129)],
            [f"V{index}=" + "x" * 4000 for index in range(17)],
        ):
            with self.subTest(type=type(invalid)):
                self.values = evidence()
                self.values["image_environment"] = invalid
                self.reject("catalog_client_image_environment_invalid")

    def test_actual_mounts_cannot_add_admin_named_volume_rw_or_unknown_tmpfs(self):
        for case in ("extra", "rw", "volume", "replacement", "tmpfs", "duplicate"):
            with self.subTest(case=case):
                self.values = evidence()
                mounts = self.values["api_container"]["Mounts"]
                if case == "rw":
                    mounts[0]["RW"] = True
                elif case == "volume":
                    mounts[0]["Type"] = "volume"
                elif case == "replacement":
                    mounts[0]["Source"] = "/srv/private/admin-password"
                elif case == "tmpfs":
                    mounts.append(
                        {"Type": "tmpfs", "Destination": "/admin", "RW": True}
                    )
                elif case == "duplicate":
                    mounts.append(copy.deepcopy(mounts[0]))
                else:
                    mounts.append(
                        {
                            "Type": "bind",
                            "Source": "/srv/private/admin-password",
                            "Destination": "/admin",
                            "RW": False,
                        }
                    )
                self.reject("catalog_client_mounts_changed")
        self.values = evidence()
        self.values["api_container"]["Mounts"].append(
            {"Type": "tmpfs", "Destination": "/tmp", "RW": True}
        )
        self.verify()

    def test_api_network_attachments_must_be_exact_full_ids(self):
        for case in ("extra", "missing", "id", "host"):
            with self.subTest(case=case):
                self.values = evidence()
                row = self.values["api_container"]
                networks = row["NetworkSettings"]["Networks"]
                name = next(iter(networks))
                if case == "extra":
                    networks["unknown"] = {"NetworkID": "f" * 64}
                elif case == "missing":
                    networks.pop(name)
                elif case == "id":
                    networks[name]["NetworkID"] = "f" * 64
                else:
                    row["HostConfig"]["NetworkMode"] = "host"
                self.reject("catalog_client_network_attachments_changed")

    def test_malformed_evidence_raises_fixed_code_without_private_text(self):
        for record in self.values:
            for malformed in (None, [], "private-password-do-not-echo"):
                with self.subTest(record=record, type=type(malformed)):
                    self.values = evidence()
                    self.values[record] = malformed
                    self.reject()


class SharedLeasedRuntimeTests(unittest.TestCase):
    def reject(self, values, error):
        with self.assertRaises(client_lease.LeaseError) as caught:
            client_lease.validate_leased_runtime(**values)
        self.assertEqual(str(caught.exception), error)

    def test_full_catalog_core_and_shared_clients_are_proved_without_mutation(self):
        values = shared_leased_runtime_evidence()
        original = copy.deepcopy(values)
        proof = client_lease.validate_leased_runtime(**values)
        self.assertTrue(proof["core_runtime_verified"])
        self.assertEqual(
            proof["catalog_container_ids"], {**CORE_IDS, "ollama-embedding": "c" * 64}
        )
        self.assertEqual(
            set(proof["expected_peer_ids"]),
            set(values["catalog_network"]["Containers"]),
        )
        self.assertEqual(values, original)
        values.pop("shared_clients")
        self.reject(values, "catalog_client_network_peers_changed")

    def test_shared_collection_is_verified_once_and_old_single_path_is_not_used(self):
        values = shared_leased_runtime_evidence()
        original = client_lease.validate_clients
        with (
            mock.patch.object(
                client_lease, "validate_clients", wraps=original
            ) as checker,
            mock.patch.object(
                client_lease,
                "validate_client_lease",
                side_effect=AssertionError("no single-peer bypass"),
            ),
        ):
            client_lease.validate_leased_runtime(**values)
        self.assertEqual(checker.call_count, 1)
        self.assertIs(checker.call_args.args[0], values["shared_clients"])

    def test_selected_member_must_match_actual_snapshot_environment_and_plan(self):
        for kind in ("snapshot", "environment", "plan", "network"):
            with self.subTest(kind=kind):
                values = shared_leased_runtime_evidence()
                member = values["shared_clients"][0]
                if kind == "snapshot":
                    member["api_container"]["State"]["StartedAt"] = (
                        "2026-10-29T00:00:00.000000000Z"
                    )
                elif kind == "environment":
                    member["image_environment"].reverse()
                elif kind == "plan":
                    member["release_compose"]["services"]["web"]["healthcheck"][
                        "interval"
                    ] = "30s"
                    member["lease"]["release_compose_sha256"] = (
                        client_lease.lease_digest(member["release_compose"])
                    )
                else:
                    for registered in values["shared_clients"]:
                        registered["catalog_network"]["Containers"]["2" * 64][
                            "Name"
                        ] = "another-snapshot"
                self.reject(values, "catalog_client_shared_member_changed")

    def test_original_core_command_mount_and_network_guards_remain_required(self):
        for kind in ("command", "mount", "driver"):
            with self.subTest(kind=kind):
                values = shared_leased_runtime_evidence()
                if kind == "command":
                    values["core_containers"][0]["Config"]["Cmd"] = ["unreviewed"]
                    error = "catalog_client_core_image_startup_changed"
                elif kind == "mount":
                    values["core_containers"][0]["Mounts"][0]["RW"] = True
                    error = "catalog_client_core_runtime_changed"
                else:
                    values["catalog_network"]["Driver"] = "overlay"
                    for registered in values["shared_clients"]:
                        registered["catalog_network"]["Driver"] = "overlay"
                    error = "catalog_client_network_driver_changed"
                self.reject(values, error)

    def test_no_registered_api_may_collide_with_the_cpu_embedding_container(self):
        values = shared_leased_runtime_evidence()
        second = values["shared_clients"][1]
        old, new = second["lease"]["api_container_id"], "c" * 64
        second["lease"]["api_container_id"] = new
        second["api_container"]["Id"] = new
        for member in values["shared_clients"]:
            peers = member["catalog_network"]["Containers"]
            peers[new] = peers.pop(old)
        peers = values["catalog_network"]["Containers"]
        peers[new] = peers.pop(old)
        self.reject(values, "catalog_client_core_catalog_binding_changed")


class RegisteredClientsTests(unittest.TestCase):
    def reject(self, clients, code=None):
        with self.assertRaises(client_lease.LeaseError) as caught:
            client_lease.validate_clients(clients)
        self.assertRegex(str(caught.exception), r"\Acatalog_client_[a-z_]+\Z")
        if code:
            self.assertEqual(str(caught.exception), code)

    def test_single_client_has_the_unchanged_proof(self):
        values = evidence()
        expected = client_lease.validate_client_lease(**values)
        self.assertEqual(client_lease.validate_clients([values]), [expected])

    def test_two_clients_keep_input_order_and_full_unfiltered_catalog_peers(self):
        for separate in (False, True):
            with self.subTest(separate_catalogs=separate):
                clients = shared_evidence(separate_catalogs=separate)
                original = copy.deepcopy(clients)
                proofs = client_lease.validate_clients(clients)
                self.assertEqual(clients, original)
                self.assertEqual(
                    [p["api_container_id"] for p in proofs],
                    [c["lease"]["api_container_id"] for c in clients],
                )
                for member, proof in zip(clients, proofs):
                    self.assertEqual(
                        set(proof["expected_peer_ids"]),
                        set(member["catalog_network"]["Containers"]),
                    )
                    self.assertEqual(
                        proof["lease_sha256"],
                        client_lease.lease_digest(member["lease"]),
                    )
                self.assertNotIn("Env", json.dumps(proofs))
                self.assertNotIn("/srv", json.dumps(proofs))

    def test_shared_admission_does_not_weaken_the_single_client_default(self):
        for member in shared_evidence():
            with self.assertRaisesRegex(
                client_lease.LeaseError, "network_peers_changed"
            ):
                client_lease.validate_client_lease(**member)

    def test_no_empty_third_client_tuple_bare_proof_or_extra_fields(self):
        for invalid in (
            None,
            [],
            (),
            [evidence()] * 3,
            [None],
            [client_lease.validate_client_lease(**evidence())],
        ):
            with self.subTest(type=type(invalid)):
                self.reject(invalid, "catalog_client_set_invalid")
        values = evidence()
        values["allow_peer_ids"] = ["f" * 64]
        self.reject([values], "catalog_client_set_invalid")

    def test_duplicate_api_or_release_project_is_not_registration(self):
        for duplicate in ("api", "project"):
            with self.subTest(duplicate=duplicate):
                clients = shared_evidence()
                if duplicate == "api":
                    clients[1]["lease"]["api_container_id"] = clients[0]["lease"][
                        "api_container_id"
                    ]
                    clients[1]["api_container"]["Id"] = clients[0]["api_container"][
                        "Id"
                    ]
                else:
                    clients[1] = copy.deepcopy(clients[0])
                    clients[1]["lease"]["api_container_id"] = "d" * 64
                    clients[1]["api_container"]["Id"] = "d" * 64
                self.reject(clients, "catalog_client_identity_duplicate")

    def test_foreign_missing_and_inconsistent_network_peers_are_rejected(self):
        for kind in ("foreign", "missing", "inconsistent"):
            with self.subTest(kind=kind):
                clients = shared_evidence()
                if kind == "inconsistent":
                    clients[1]["catalog_network"]["Containers"]["f" * 64] = {}
                else:
                    for member in clients:
                        peers = member["catalog_network"]["Containers"]
                        if kind == "foreign":
                            peers["f" * 64] = {}
                        else:
                            peers.pop(clients[1]["lease"]["api_container_id"])
                self.reject(
                    clients,
                    "catalog_client_network_snapshot_changed"
                    if kind == "inconsistent"
                    else "catalog_client_network_peers_changed",
                )

    def test_every_member_requires_full_source_capability_and_catalog_proof(self):
        for kind in ("source", "gpu", "environment", "role", "catalog"):
            with self.subTest(kind=kind):
                clients = shared_evidence()
                second = clients[1]
                if kind == "source":
                    second["image_proof"]["source_sha256"] = "f" * 64
                elif kind == "gpu":
                    second["api_container"]["HostConfig"]["DeviceRequests"] = [
                        {"Driver": "nvidia"}
                    ]
                elif kind == "environment":
                    second["api_container"]["Config"]["Env"].append(
                        "UNAPPROVED=private"
                    )
                elif kind == "role":
                    second["api_container"]["Config"]["Labels"][
                        "com.docker.compose.service"
                    ] = "web"
                else:
                    for key in ("lease", "catalog_identity"):
                        second[key]["access_sha256"] = "f" * 64
                self.reject(clients)

    def test_api_cannot_collide_with_another_catalog_core_identity(self):
        clients = shared_evidence(separate_catalogs=True)
        second = clients[1]
        old = second["lease"]["api_container_id"]
        second["lease"]["api_container_id"] = CORE_IDS["fess-search"]
        second["api_container"]["Id"] = CORE_IDS["fess-search"]
        peers = second["catalog_network"]["Containers"]
        peers[CORE_IDS["fess-search"]] = peers.pop(old)
        self.reject(clients, "catalog_client_identity_duplicate")


if __name__ == "__main__":
    unittest.main()
