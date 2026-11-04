"""GPU ownership volumes fixtures and regressions."""

from __future__ import annotations
import copy
from dataclasses import replace
import json
import unittest
from unittest import mock
from search.operations.deployment.gpu import contract as gpu_contract
from search.operations.deployment.gpu import snapshot as gpu_snapshot
from search.operations.deployment.gpu import collect as gpu_collect
from tests.unit.deployment.gpu.fixtures import (
    CONFIG,
    GPUS,
    INVENTORY,
    LocalVolumeRunner,
    local_volume_proof,
    volume_container,
)


class LocalVolumeOwnershipTests(unittest.TestCase):
    """Fixture hardware only; these tests never establish host GPU
    ownership.
    """

    def validate(self, rows, proofs=()):
        return gpu_snapshot.validate_gpu_ownership(
            GPUS,
            inventory=INVENTORY,
            containers=rows,
            compute_apps="",
            container_pids={},
            verified_local_volumes=proofs,
        )

    def test_volume_requires_separate_daemon_evidence(self):
        row = volume_container()
        with self.assertRaisesRegex(
            gpu_contract.GPUOwnershipError, "volume_evidence_invalid"
        ):
            self.validate([row])
        self.assertEqual(
            self.validate([row], (local_volume_proof(),))["status"], "ok"
        )
        self.assertEqual(gpu_snapshot._container(row)[3], ())

    def test_shared_local_volume_is_deduplicated_and_host_binds_match_name(
        self,
    ):
        proof = local_volume_proof()
        row = volume_container()
        row["HostConfig"]["Binds"] = [
            proof.name + ":/usr/share/opensearch/data:rw,nocopy"
        ]
        other = copy.deepcopy(row)
        other["Id"] = "5" * 64
        self.assertEqual(self.validate([row, other], (proof,))["status"], "ok")
        row["HostConfig"]["Binds"] = ["other:/usr/share/opensearch/data:rw"]
        with self.assertRaisesRegex(
            gpu_contract.GPUOwnershipError, "mount_inventory_mismatch"
        ):
            self.validate([row], (proof,))
        row["HostConfig"]["Binds"] = [
            proof.mountpoint + ":/usr/share/opensearch/data:rw"
        ]
        with self.assertRaisesRegex(
            gpu_contract.GPUOwnershipError, "mount_inventory_mismatch"
        ):
            self.validate([row], (proof,))

    def test_invalid_volume_metadata_and_aliases_are_rejected(self):
        class Nonliteral(str):
            pass

        proof = local_volume_proof()
        for field, value in (
            ("driver", "plugin"),
            ("driver", Nonliteral("local")),
            ("scope", "global"),
            ("scope", Nonliteral("local")),
            ("options_empty", False),
            ("options_empty", 1),
            ("created_at", "private-path"),
            ("name", "../dev"),
            ("name", "-option"),
            ("name", "x" * 256),
            ("mountpoint", proof.mountpoint + "/"),
            ("mountpoint", "/var/lib/docker/volumes/other/_data"),
            (
                "mountpoint",
                "/var/lib/docker-other/volumes/catalog-index/_data",
            ),
            ("docker_root_dir", "/var/lib/docker/../dev"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(gpu_contract.GPUOwnershipError):
                    self.validate(
                        [volume_container()],
                        (replace(proof, **{field: value}),),
                    )
        for proofs in (
            (proof, proof),
            (proof, local_volume_proof("extra")),
            [proof],
        ):
            with self.assertRaisesRegex(
                gpu_contract.GPUOwnershipError, "volume_evidence_invalid"
            ):
                self.validate([volume_container()], proofs)

    def test_volume_mount_cannot_escape_proof_or_host_target_rules(self):
        proof = local_volume_proof()
        for field, value in (
            ("Source", proof.mountpoint + "-alias"),
            ("Source", "/dev/nvidia0"),
            ("Driver", "plugin"),
            ("Driver", 1),
            ("Name", "other"),
            ("Destination", "/run/tls/key"),
            ("Destination", "/run/secrets/api_key"),
            ("Destination", "/run/index/binding.json"),
            ("Destination", "/proc"),
            ("Destination", "/sys"),
            ("Destination", "/dev"),
            ("Destination", "/private/docker.sock"),
            ("Propagation", "rshared"),
            ("RW", None),
        ):
            with self.subTest(field=field, value=value):
                row = volume_container()
                row["Mounts"][0][field] = value
                with self.assertRaises(gpu_contract.GPUOwnershipError):
                    self.validate([row], (proof,))

    def test_collector_rechecks_root_and_volumes_with_bounded_projection(
        self,
    ):
        runner = LocalVolumeRunner([volume_container()])
        result = gpu_collect.run_gpu_ownership(CONFIG, runner=runner)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(runner.volume_calls, 2)
        self.assertEqual(runner.info_calls, 2)
        probes = [a for a, _t, _m in runner.calls if a[0] == "python3"]
        self.assertEqual([a[-1] for a in probes], [runner.root, runner.root])
        self.assertNotIn("json .Options", gpu_collect.VOLUME_INSPECT_FORMAT)
        self.assertNotIn("Labels", gpu_collect.VOLUME_INSPECT_FORMAT)
        self.assertIn("if .Options", gpu_collect.VOLUME_INSPECT_FORMAT)
        self.assertTrue(
            all(
                0 < t <= 15 and 0 < m <= gpu_contract.MAX_OUTPUT_BYTES
                for _a, t, m in runner.calls
            )
        )

    def test_metadata_and_root_drift_are_blocked(self):
        for key, value in (
            ("created_at", "2026-10-22T00:00:00Z"),
            ("driver", "plugin"),
            ("scope", "global"),
            ("options_empty", False),
            ("name", "other"),
            ("mountpoint", "/dev/private"),
        ):
            with self.subTest(field=key):
                runner = LocalVolumeRunner([volume_container()])
                runner.changed_metadata = (
                    lambda row, count: row.update({key: value})
                    if count > 1
                    else None
                )
                report = gpu_collect.run_gpu_ownership(CONFIG, runner=runner)
                self.assertEqual(report["status"], "blocked")
                self.assertIn(report["code"], gpu_contract.ERROR_CODES)
                self.assertNotIn("/dev/private", json.dumps(report))
        runner = LocalVolumeRunner([volume_container()])
        runner.changed_root = "/other/docker"
        self.assertEqual(
            gpu_collect.run_gpu_ownership(CONFIG, runner=runner)["code"],
            "gpu_ownership_snapshot_changed",
        )
        runner = LocalVolumeRunner([volume_container()])
        runner.changed_inspect = lambda row: row["Mounts"][0].update(RW=False)
        self.assertEqual(
            gpu_collect.run_gpu_ownership(CONFIG, runner=runner)["code"],
            "gpu_ownership_snapshot_changed",
        )

    def test_driver_options_missing_metadata_and_volume_bounds_fail_closed(
        self,
    ):
        for options in (False, None, 1):
            runner = LocalVolumeRunner([volume_container()])
            runner.changed_metadata = lambda row, _count: row.update(
                options_empty=options
            )
            report = gpu_collect.run_gpu_ownership(CONFIG, runner=runner)
            self.assertEqual(
                report["code"], "gpu_ownership_volume_metadata_invalid"
            )
        runner = LocalVolumeRunner([volume_container()])
        runner.changed_metadata = lambda row, _count: row.pop("scope")
        self.assertEqual(
            gpu_collect.run_gpu_ownership(CONFIG, runner=runner)["code"],
            "gpu_ownership_volume_metadata_invalid",
        )
        other = volume_container(local_volume_proof("other"))
        other["Id"] = "5" * 64
        with mock.patch.object(gpu_contract, "MAX_LOCAL_VOLUMES", 1):
            runner = LocalVolumeRunner([volume_container(), other])
            self.assertEqual(
                gpu_collect.run_gpu_ownership(CONFIG, runner=runner)["code"],
                "gpu_ownership_volume_limit",
            )
            self.assertEqual(runner.volume_calls, 0)


if __name__ == "__main__":
    unittest.main()
