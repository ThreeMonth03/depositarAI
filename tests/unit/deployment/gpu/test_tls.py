"""GPU ownership tls fixtures and regressions."""

from __future__ import annotations
import copy
from dataclasses import replace
import unittest
from search.operations.deployment.gpu import contract as gpu_contract
from search.operations.deployment.gpu import snapshot as gpu_snapshot
from search.operations.deployment.gpu import collect as gpu_collect
from tests.unit.deployment.gpu.fixtures import (
    CONFIG,
    FakeRunner,
    GPUS,
    INVENTORY,
    SCOPE,
    SCOPE_ID,
    UUIDS,
    container,
    scope_container,
    tls_container,
    tls_proof,
    tls_sources,
)


class TLSCPUOwnershipTests(unittest.TestCase):
    def validate(self, rows, proofs, **changes):
        values = dict(
            inventory=INVENTORY,
            containers=rows,
            compute_apps="",
            container_pids={},
            cpu_proofs=proofs,
            verified_bind_sources=tls_sources(*rows),
        )
        values.update(changes)
        return gpu_snapshot.validate_gpu_ownership(GPUS, **values)

    def test_exact_tls_pair_shares_the_unfiltered_gpu_snapshot(self):
        inner, outer = tls_container(), tls_container("outer")
        proofs = (tls_proof(inner), tls_proof(outer, "outer"))
        rows = [inner, container(), scope_container(), outer]
        result = self.validate(
            rows,
            proofs,
            allowed=(SCOPE,),
            container_pids={SCOPE_ID: "PID\n101"},
            compute_apps=f"{UUIDS[0]},101",
        )
        self.assertEqual(result["running_container_count"], 4)
        self.assertEqual(result["sealed_cpu_container_count"], 2)
        self.assertEqual(result["sealed_gpu_container_count"], 1)
        self.assertNotIn("/private", str(result))
        with self.assertRaises(gpu_contract.GPUOwnershipError):
            self.validate([inner, outer], ())

    def test_every_projected_field_is_bound_and_mount_order_is_not_identity(
        self,
    ):
        row = tls_container()
        proof = tls_proof(row)
        for mutate in (
            lambda r: r.update(Image="sha256:" + "e" * 64),
            lambda r: r.update(RestartCount=1),
            lambda r: r["State"].update(Pid=999),
            lambda r: r["State"].update(StartedAt="2026-10-29T00:00:00Z"),
            lambda r: r["HostConfig"].update(CapAdd=["SYS_ADMIN"]),
            lambda r: r["Mounts"][0].update(Source="/other/material"),
        ):
            with self.subTest(mutate=mutate):
                changed = copy.deepcopy(row)
                mutate(changed)
                with self.assertRaisesRegex(
                    gpu_contract.GPUOwnershipError, "cpu_identity_changed"
                ):
                    self.validate([changed], (proof,))
        row["Mounts"].reverse()
        self.assertEqual(
            gpu_snapshot.cpu_projection_sha256(row), proof.projection_sha256
        )
        self.assertEqual(self.validate([row], (proof,))["status"], "ok")

    def test_resealed_cpu_proof_cannot_grant_gpu_or_host_device_access(self):
        for mutate in (
            lambda r: r["HostConfig"].update(Privileged=True),
            lambda r: r["HostConfig"].update(PidMode="host"),
            lambda r: r["HostConfig"].update(IpcMode="host"),
            lambda r: r["HostConfig"].update(Runtime="nvidia"),
            lambda r: r["HostConfig"].update(
                Devices=[{"PathOnHost": "/dev/nvidia0"}]
            ),
            lambda r: r["HostConfig"].update(
                DeviceCgroupRules=["c 195:* rwm"]
            ),
            lambda r: r["HostConfig"].update(VolumesFrom=["other"]),
            lambda r: r["HostConfig"].update(AnnotationsPresent=True),
            lambda r: r["HostConfig"].update(
                DeviceRequests=scope_container()["HostConfig"][
                    "DeviceRequests"
                ]
            ),
            lambda r: r.update(NVIDIAEnv=["NVIDIA_VISIBLE_DEVICES=all"]),
            lambda r: r.update(
                NVIDIAEnv=[
                    "NVIDIA_VISIBLE_DEVICES=void",
                    "NVIDIA_MIG_CONFIG_DEVICES=all",
                ]
            ),
        ):
            with self.subTest(mutate=mutate):
                row = tls_container()
                mutate(row)
                with self.assertRaises(gpu_contract.GPUOwnershipError):
                    self.validate([row], (tls_proof(row),))

    def test_capability_exception_is_exactly_the_fixed_outer_profile(self):
        for role, caps, accepted in (
            ("inner", [], True),
            ("inner", ["NET_BIND_SERVICE"], False),
            ("outer", sorted(gpu_contract.TLS_OUTER_CAPS), True),
            (
                "outer",
                sorted(gpu_contract.TLS_OUTER_CAPS | {"NET_BIND_SERVICE"}),
                True,
            ),
            ("outer", ["NET_BIND_SERVICE"], False),
            (
                "outer",
                sorted(gpu_contract.TLS_OUTER_CAPS | {"SYS_ADMIN"}),
                False,
            ),
        ):
            with self.subTest(role=role, caps=caps):
                row = tls_container(role)
                row["HostConfig"]["CapAdd"] = caps
                if accepted:
                    self.assertEqual(
                        self.validate([row], (tls_proof(row, role),))[
                            "status"
                        ],
                        "ok",
                    )
                else:
                    with self.assertRaises(gpu_contract.GPUOwnershipError):
                        self.validate([row], (tls_proof(row, role),))

    def test_resealed_proof_cannot_expand_tls_mount_exception(self):
        for mutate in (
            lambda r: r["Mounts"][0].update(RW=True),
            lambda r: r["Mounts"][0].update(Destination="/run/tls/other.key"),
            lambda r: r["Mounts"][0].update(Source="/var/run/docker.sock"),
            lambda r: r["Mounts"][0].update(
                Type="volume", Name="unexpected", Driver="local"
            ),
            lambda r: r["Mounts"][0].update(Propagation="rshared"),
            lambda r: r["Mounts"].append(
                {
                    "Type": "bind",
                    "Source": "/safe/extra",
                    "Destination": "/extra",
                    "RW": False,
                }
            ),
            lambda r: r["Mounts"].append(
                {"Type": "tmpfs", "Destination": "/dev", "RW": True}
            ),
        ):
            with self.subTest(mutate=mutate):
                row = tls_container()
                mutate(row)
                with self.assertRaises(gpu_contract.GPUOwnershipError):
                    self.validate([row], (tls_proof(row),))

    def test_bind_sources_still_require_independent_metadata(self):
        row = tls_container()
        with self.assertRaisesRegex(
            gpu_contract.GPUOwnershipError, "mount_source_unverified"
        ):
            self.validate(
                [row], (tls_proof(row),), verified_bind_sources=frozenset()
            )

    def test_cpu_proofs_cannot_authorize_compute_or_replace_gpu_proofs(self):
        row = tls_container()
        proof = tls_proof(row)
        with self.assertRaisesRegex(
            gpu_contract.GPUOwnershipError, "unknown_compute_pid"
        ):
            self.validate([row], (proof,), compute_apps=f"{UUIDS[0]},100")
        with self.assertRaisesRegex(
            gpu_contract.GPUOwnershipError, "proof_invalid"
        ):
            self.validate([row], (), allowed=(proof,))
        with self.assertRaisesRegex(
            gpu_contract.GPUOwnershipError, "cpu_proof_invalid"
        ):
            self.validate(
                [row],
                (replace(proof, container_id=SCOPE_ID),),
                allowed=(SCOPE,),
            )

    def test_cpu_proof_schema_identity_and_missing_peers_are_closed(self):
        row = tls_container()
        proof = tls_proof(row)
        for values in (
            [proof],
            (proof, proof),
            (SCOPE,),
            (replace(proof, role="arbitrary-cpu"),),
            (replace(proof, container_id="inner"),),
            (replace(proof, image_id="nginx:latest"),),
            (replace(proof, projection_sha256="invalid"),),
        ):
            with (
                self.subTest(values=values),
                self.assertRaisesRegex(
                    gpu_contract.GPUOwnershipError, "cpu_proof_invalid"
                ),
            ):
                self.validate([row], values)
        with self.assertRaisesRegex(
            gpu_contract.GPUOwnershipError, "sealed_cpu_container_missing"
        ):
            self.validate([], (proof,))

    def test_gather_retains_tls_and_other_cpu_containers_and_rechecks_snapshot(
        self,
    ):
        inner, outer = tls_container(), tls_container("outer")
        runner = FakeRunner([inner, container(), outer])
        proofs = (tls_proof(inner), tls_proof(outer, "outer"))
        result = gpu_collect.run_gpu_ownership(
            CONFIG, runner=runner, cpu_proofs=proofs
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["running_container_count"], 3)
        self.assertEqual(set(runner.inspect_count.values()), {2})
        self.assertFalse(any("top" in call[0] for call in runner.calls))
        runner = FakeRunner([inner, outer])
        runner.changed_inspect = lambda row: row.update(RestartCount=1)
        result = gpu_collect.run_gpu_ownership(
            CONFIG, runner=runner, cpu_proofs=proofs
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["code"], "gpu_ownership_cpu_identity_changed")


if __name__ == "__main__":
    unittest.main()
