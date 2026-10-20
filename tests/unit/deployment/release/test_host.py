"""Real three-plane policy integration under the release host, without Docker IO."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
import unittest

from search.operations.catalog import client_lease, policy
from search.operations.deployment import composition, inference_runtime
from search.operations.deployment.release import controller as release
from tests.unit.catalog.test_client_lease import (
    REVISION,
    leased_runtime_evidence as catalog_evidence,
)
from tests.unit.deployment.test_composition import product
from tests.unit.deployment.catalog_fixtures import sealed_catalog_storage
from tests.unit.deployment.test_inference_runtime import images, snapshot
from tests.unit.deployment.release.test_runtime import evidence as release_evidence


def evidence(test):
    """Align existing catalog, GPU and website fixtures to one actual topology.

    These are Docker inspection fixtures, not self-authenticating host receipts.
    The callback below represents the separately sealed inference receipt while
    every actual container/network profile is checked by the real validators.
    """
    cpu, catalog = release_evidence(), catalog_evidence()
    catalog_project = catalog["core_plan"]["project"]
    cpu_compose = cpu["compose"]
    gpu_compose = composition._inference(
        product(), REVISION, 21599, 564, inference_source_sha256="6" * 64
    )
    gpu_images = images()
    # All Ollama roles share one immutable image/config in a real daemon.
    ollama = copy.deepcopy(cpu["images"]["filtered-embedding"])
    gpu_images.update({"scope-model": ollama, "answer-model": ollama})
    gpu_images["reranker"]["Id"] = "sha256:" + "c" * 64
    for role, image in gpu_images.items():
        gpu_compose["services"][role]["image"] = image["Id"]
    gpu_containers, gpu_networks = snapshot(gpu_compose, gpu_images)
    replaced_ids = {}
    for row, character in zip(gpu_containers, "abc"):
        replaced_ids[row["Id"]] = character * 64
        row["Id"] = character * 64
    network_ids = cpu["lease"]["network_ids"]
    for network in gpu_networks:
        network["Id"] = network_ids[network["Name"]]
        network["Containers"] = {
            replaced_ids[identifier]: member
            for identifier, member in network["Containers"].items()
        }
    for row in gpu_containers:
        for name, attachment in row["NetworkSettings"]["Networks"].items():
            attachment["NetworkID"] = network_ids[name]
    catalog_containers = catalog["core_containers"]
    for row in catalog_containers:
        if row["Config"]["Labels"]["com.docker.compose.service"] == "ollama-embedding":
            row["Id"] = "6" * 64
    catalog_network = catalog["catalog_network"]
    website_network = cpu["networks"][0]
    leased = (
        {
            catalog_project: catalog_containers,
            gpu_compose["name"]: gpu_containers,
            cpu_compose["name"]: cpu["containers"],
        },
        {
            network["Name"]: network
            for network in [catalog_network, *gpu_networks, website_network]
        },
    )
    api_id = cpu["lease"]["api_container_id"]
    for network in gpu_networks:
        network["Containers"][api_id] = {"Name": "not-authority"}
    baseline = copy.deepcopy(leased)
    baseline[0].pop(cpu_compose["name"])
    baseline[1].pop(website_network["Name"])
    for network in baseline[1].values():
        network["Containers"].pop(api_id, None)
    context = {
        "plan": catalog["core_plan"],
        "compose": catalog["core_compose"],
        "bundle": SimpleNamespace(bundle_id=cpu["lease"]["bundle_id"]),
    }
    sealed_catalog_storage(test, context)
    catalog_proof = policy.validate_runtime(
        context["plan"],
        context["compose"],
        catalog["core_images"],
        baseline[0][catalog_project],
        baseline[1][catalog_network["Name"]],
    )
    gpu_proof = inference_runtime.validate_runtime(
        gpu_compose,
        gpu_images,
        baseline[0][gpu_compose["name"]],
        [baseline[1][network["Name"]] for network in gpu_networks],
    )
    authority = {"catalog_identity": cpu["catalog_identity"], "runtime": catalog_proof}
    verified = []

    def verify_inference(proof):
        release._same(
            {key: proof[key] for key in release.PROOF_FIELDS},
            gpu_proof,
            "release_inference_receipt_changed",
        )
        verified.append(copy.deepcopy(proof))

    host = release._Host(
        None,
        context,
        authority,
        catalog["core_images"],
        catalog["core_image_inspections"],
        gpu_compose,
        gpu_images,
        verify_inference,
        cpu_compose,
        cpu["images"],
        cpu["image_proof"],
    )
    return host, baseline, leased, cpu["lease"], verified


class SnapshotDocker:
    """Exercise the host collector's real unfiltered inspect/ls argument flow."""

    def __init__(self, values):
        self.groups, self.networks = copy.deepcopy(values)
        self.calls = []
        self.extra_ids = []
        self.extra_networks = []
        self.reverse = True

    def call(self, arguments, *, timeout=30):
        self.calls.append((list(arguments), timeout))
        if arguments[0] == "ps":
            project = arguments[-1].split("=", 2)[-1]
            ids = [row["Id"] for row in self.groups.get(project, [])]
            ids.extend(self.extra_ids)
            return "".join(identifier + "\n" for identifier in ids).encode()
        if arguments[:2] == ["network", "ls"]:
            prefix = arguments[-1].removeprefix("name=")
            names = [name for name in self.networks if name.startswith(prefix)]
            names.extend(self.extra_networks)
            return "".join(
                json.dumps({"ID": "f" * 64, "Name": name}) + "\n" for name in names
            ).encode()
        if arguments[0] == "inspect":
            rows = {row["Id"]: row for group in self.groups.values() for row in group}
            result = [rows[identifier] for identifier in arguments[1:]]
        elif arguments[:2] == ["network", "inspect"]:
            result = [self.networks[name] for name in arguments[2:]]
        else:
            raise AssertionError("unexpected Docker operation")
        if self.reverse:
            result.reverse()
        return json.dumps(result).encode()


class ReleaseHostIntegrationTests(unittest.TestCase):
    def test_baseline_to_actual_lease_proves_all_three_planes_without_mutation(self):
        host, baseline, leased, expected_lease, verified = evidence(self)
        original = copy.deepcopy((baseline, leased))
        baseline_result = host.observe(snapshot=baseline)
        self.assertEqual(baseline_result[0], host.authority["runtime"])
        self.assertIsNone(host.last[1])
        host.lease = host.make_lease(leased)
        self.assertEqual(host.lease, expected_lease)
        result = host.observe(snapshot=leased)
        self.assertEqual(result[0], host.authority["runtime"])
        gpu, cpu = host.last
        self.assertEqual(gpu["api_container_id"], host.lease["api_container_id"])
        self.assertEqual(gpu["client_lease_sha256"], cpu["lease_sha256"])
        self.assertEqual(cpu["lease_sha256"], client_lease.lease_digest(host.lease))
        self.assertEqual(
            set(cpu["container_ids"]), {"web", "conversation-api", "filtered-embedding"}
        )
        self.assertEqual(len(verified), 2)
        self.assertEqual((baseline, leased), original)
        self.assertNotIn("Env", json.dumps(host.last))
        self.assertNotIn("/srv", json.dumps(host.last))
        self.assertNotIn("never-save-this-value", json.dumps(host.last))

    def test_changed_volume_receipts_cannot_authorize_runtime(self):
        for filename in ("admin-image.json", "volume-started.json", "volume.json"):
            with self.subTest(filename=filename):
                host, baseline, _leased, _lease, _verified = evidence(self)
                record = release.execution.read_record(host.context["root"], filename)
                field = (
                    "image_id" if filename == "admin-image.json" else "helper_image_id"
                )
                record[field] = "sha256:" + "f" * 64
                release.execution.write_record(
                    host.context["root"], filename, record, replace=True
                )
                with self.assertRaisesRegex(
                    ValueError, "catalog_volume_receipt_changed"
                ):
                    host.observe(snapshot=baseline)
                self.assertIsNone(host.last)

    def test_actual_index_volume_mount_must_match_sealed_storage(self):
        for field, changed in (
            ("Name", "unknown-index"),
            ("Source", "/different/docker/volume"),
            ("Driver", "unknown"),
            ("Destination", "/different/index"),
            ("RW", False),
            ("Type", "bind"),
        ):
            with self.subTest(field=field):
                host, baseline, _leased, _lease, _verified = evidence(self)
                index = next(
                    row
                    for row in baseline[0][host.context["plan"]["project"]]
                    if row["Config"]["Labels"]["com.docker.compose.service"]
                    == "opensearch-search"
                )
                mount = next(
                    mount
                    for mount in index["Mounts"]
                    if mount["Destination"] == "/usr/share/opensearch/data"
                )
                mount[field] = changed
                with self.assertRaisesRegex(
                    ValueError, "catalog_runtime_volume_binding_changed"
                ):
                    host.observe(snapshot=baseline)
                self.assertIsNone(host.last)

    def test_unleased_api_on_catalog_or_gpu_network_is_not_adopted(self):
        for network_kind in ("catalog", "inference", "ranking"):
            with self.subTest(network_kind=network_kind):
                host, baseline, leased, expected, _verified = evidence(self)
                name = next(
                    name
                    for name in leased[1]
                    if name.endswith(
                        {
                            "catalog": "_search_backend",
                            "inference": "_inference",
                            "ranking": "_ranking",
                        }[network_kind]
                    )
                )
                baseline[1][name]["Containers"][expected["api_container_id"]] = {}
                with self.assertRaises(ValueError):
                    host.observe(snapshot=baseline)

    def test_every_plane_rejects_unknown_unfiltered_network_peer(self):
        for network_kind in ("catalog", "inference", "ranking", "website"):
            with self.subTest(network_kind=network_kind):
                host, _baseline, leased, _expected, _verified = evidence(self)
                host.lease = host.make_lease(leased)
                suffix = {
                    "catalog": "_search_backend",
                    "inference": "_inference",
                    "ranking": "_ranking",
                    "website": "_website",
                }[network_kind]
                name = next(name for name in leased[1] if name.endswith(suffix))
                leased[1][name]["Containers"]["f" * 64] = {"Name": "unknown-helper"}
                with self.assertRaises(ValueError):
                    host.observe(snapshot=leased)

    def test_api_replacement_capability_and_catalog_identity_drift_reject(self):
        for kind in ("replacement", "gpu", "environment", "catalog_id", "invalid_pid"):
            with self.subTest(kind=kind):
                host, _baseline, leased, _expected, _verified = evidence(self)
                host.lease = host.make_lease(leased)
                api = next(
                    row
                    for row in leased[0][host.compose["name"]]
                    if row["Id"] == host.lease["api_container_id"]
                )
                if kind == "replacement":
                    api["Id"] = "f" * 64
                elif kind == "gpu":
                    api["HostConfig"]["DeviceRequests"] = [{"Driver": "nvidia"}]
                elif kind == "environment":
                    api["Config"]["Env"].append("UNKNOWN_SECRET=never-save-this-value")
                elif kind == "catalog_id":
                    host.lease["catalog_network_id"] = "f" * 64
                else:
                    api["State"]["Pid"] = 0
                with self.assertRaises(ValueError):
                    host.observe(snapshot=leased)

    def test_gpu_process_restart_cannot_match_sealed_baseline_receipt(self):
        for field, value in (("Pid", 9999), ("StartedAt", "2026-10-12T12:00:00Z")):
            with self.subTest(field=field):
                host, _baseline, leased, _expected, _verified = evidence(self)
                host.lease = host.make_lease(leased)
                row = leased[0][host.inference_compose["name"]][0]
                row["State"][field] = value
                with self.assertRaisesRegex(
                    release.ReleaseError, "release_inference_receipt_changed"
                ):
                    host.observe(snapshot=leased)

    def test_cpu_pid_restart_changes_proof_for_post_acceptance_fence(self):
        for service in ("conversation-api", "web", "filtered-embedding"):
            with self.subTest(service=service):
                host, _baseline, leased, _expected, _verified = evidence(self)
                host.lease = host.make_lease(leased)
                host.observe(snapshot=leased)
                before = copy.deepcopy(host.last[1])
                row = next(
                    row
                    for row in leased[0][host.compose["name"]]
                    if row["Config"]["Labels"]["com.docker.compose.service"] == service
                )
                row["State"]["Pid"] += 1
                host.observe(snapshot=leased)
                self.assertNotEqual(
                    host.last[1]["start_identity_sha256"],
                    before["start_identity_sha256"],
                )
                with self.assertRaisesRegex(
                    release.ReleaseError, "changed_during_acceptance"
                ):
                    release._same(
                        host.last[1],
                        before,
                        "release_runtime_changed_during_acceptance",
                    )

    def test_collect_handles_shuffled_actual_rows_and_observe_uses_real_validators(
        self,
    ):
        host, baseline, leased, expected, _verified = evidence(self)
        host.docker = SnapshotDocker(baseline)
        collected = host.collect(clients=False)
        host.observe(snapshot=collected)
        self.assertEqual(
            host.last[0]["container_ids"],
            {
                row["Config"]["Labels"]["com.docker.compose.service"]: row["Id"]
                for row in baseline[0][host.inference_compose["name"]]
            },
        )
        host.docker = SnapshotDocker(leased)
        collected = host.collect(clients=True)
        host.lease = host.make_lease(collected)
        self.assertEqual(host.lease, expected)
        host.observe()
        self.assertIsNotNone(host.last[1])
        self.assertEqual(len(collected[0]), 3)
        self.assertEqual(len(collected[1]), 4)
        # Actual peer maps are never filtered before any pure plane validator.
        for name, network in collected[1].items():
            self.assertEqual(network["Containers"], leased[1][name]["Containers"])

    def test_collector_rejects_extra_project_container_or_network(self):
        for kind in ("container", "network"):
            with self.subTest(kind=kind):
                host, baseline, _leased, _expected, _verified = evidence(self)
                host.docker = SnapshotDocker(baseline)
                if kind == "container":
                    host.docker.extra_ids = ["f" * 64]
                else:
                    host.docker.extra_networks = [
                        host.context["plan"]["project"] + "_unknown"
                    ]
                with self.assertRaises(ValueError):
                    host.collect(clients=False)

    def test_make_lease_does_not_adopt_absent_or_duplicate_api(self):
        for kind in ("absent", "duplicate"):
            with self.subTest(kind=kind):
                host, _baseline, leased, _expected, _verified = evidence(self)
                rows = leased[0][host.compose["name"]]
                api = next(
                    row
                    for row in rows
                    if row["Config"]["Labels"]["com.docker.compose.service"]
                    == "conversation-api"
                )
                if kind == "absent":
                    rows.remove(api)
                else:
                    rows.append(copy.deepcopy(api))
                with self.assertRaisesRegex(
                    release.ReleaseError, "release_api_identity_changed"
                ):
                    host.make_lease(leased)


if __name__ == "__main__":
    unittest.main()
