"""Candidate preparation with real private IO and offline authority boundaries."""

from __future__ import annotations

from dataclasses import replace
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from search.operations.deployment import composition
from search.operations.deployment.gateway import preparation as gateway
from search.operations.deployment.release import runtime as release_runtime
from search.operations.shared import private_io
from search.operations.state import locked_state
from tests.unit.deployment.test_composition import catalog, product
from tests.unit.deployment.gateway.test_runtime import evidence
from tests.unit.deployment.release.test_host import (
    SnapshotDocker,
    evidence as host_evidence,
)


@unittest.skipIf(os.geteuid() == 0, "gateway operator requires non-root")
class GatewayPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-gateway-prepare-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = replace(product(), state_root=self.root / "product")
        self.plan = catalog(self.config)
        self.values = evidence()
        self.proof = self.values["gateway_image_proof"]
        self.revision = self.proof["revision"]
        self.lease = copy.deepcopy(self.values["lease"])
        self.compose = composition.render_composition(
            self.config,
            self.plan,
            composition.QueryBindingPaths.for_catalog(self.config, self.plan),
            self.revision,
            uid=os.geteuid(),
            gid=os.getegid(),
        )["release"]
        for role, service in self.compose["services"].items():
            service["image"] = self.values["images"][role]["Id"]
            if role != "filtered-embedding":
                service["labels"] = release_runtime._labels(self.lease, role, "c" * 64)
        self.completed = {
            "schema_version": 1,
            "status": "private_release_running",
            "revision": self.revision,
            "project": self.compose["name"],
        }
        self.records = {
            "started.json": {"revision": self.revision},
            "compose.json": self.compose,
            "lease.json": self.lease,
            "complete.json": self.completed,
        }
        self.slot = (
            self.config.state_root
            / "releases"
            / self.compose["name"].removeprefix("depositar-ai-")
        )
        with locked_state(self.config, create=True) as store:
            parent = private_io.open_child_directory(store.fd, "releases", create=True)
            slot = private_io.open_child_directory(parent, self.slot.name, create=True)
            try:
                for name, value in self.records.items():
                    private_io.write_json(slot, name, value, max_bytes=1024**2)
            finally:
                os.close(slot)
                os.close(parent)
            store.write("release-candidate.json", self.completed)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(gateway.inference, "_source_revision").start()
        mock.patch.object(
            gateway.source_bundle,
            "committed_source_plan",
            return_value={"revision": self.revision},
        ).start()
        mock.patch.object(
            gateway.image_evidence,
            "read_completed_images",
            return_value={
                "source_manifest_sha256": "e" * 64,
                "images": {"web-nginx": self.proof},
            },
        ).start()
        self.docker = mock.Mock()
        self.docker.inspect.return_value = self.values["gateway_image"]
        mock.patch.object(
            gateway.runtime_docker, "Docker", return_value=self.docker
        ).start()
        self.real_inventory = gateway.inference._project_inventory
        self.inventory = mock.patch.object(
            gateway.inference, "_project_inventory", return_value=([], set())
        ).start()
        self.image_check = mock.patch.object(
            gateway.source_bundle, "validate_built_image", return_value=self.proof
        ).start()
        self.acceptance = mock.patch.object(
            gateway.release,
            "start_private_release",
            return_value={
                **self.completed,
                "runtime_reused": True,
            },
        ).start()

    def prepare(self):
        with locked_state(self.config) as store:
            return gateway.prepare_gateway(
                self.config, store, self.revision, self.plan, repo_root=self.root
            )

    def test_only_candidate_is_published_after_completed_release_reverification(self):
        def verify(*args, **kwargs):
            self.assertFalse((self.config.state_root / "gateway").exists())
            self.assertEqual(self.plan, args[3])
            return {**self.completed, "runtime_reused": True}

        self.acceptance.side_effect = verify
        result = self.prepare()
        self.assertEqual("gateway_prepared", result["status"])
        self.assertFalse(result["preparation_reused"])
        self.assertTrue(result["activation_required"])
        for flag in (
            "runtime_verified",
            "gateway_verified",
            "ports_published",
            "active_changed",
            "confers_runtime_authority",
            "active_route_published",
            "activation_performed",
            "loopback_verified",
            "routed_two_turn_search_verified",
            "routed_session_reset_verified",
        ):
            self.assertIs(result[flag], False)
        root = self.config.state_root / "gateway"
        self.assertEqual(gateway.FILES, set(os.listdir(root)))
        self.assertEqual({"candidate.conf"}, set(os.listdir(root / "routes")))
        compose = json.loads((root / "compose.json").read_bytes())
        self.assertEqual(
            self.proof["image_id"], compose["services"]["web-nginx"]["image"]
        )
        self.assertEqual(
            [
                {
                    "target": 8080,
                    "published": "18083",
                    "host_ip": "127.0.0.1",
                    "protocol": "tcp",
                }
            ],
            compose["services"]["web-nginx"]["ports"],
        )
        for path in (
            root / "intent.json",
            root / "compose.json",
            root / "prepared.json",
            root / "routes/candidate.conf",
        ):
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertFalse((self.config.state_root / "active.json").exists())
        self.assertFalse((root / "routes/active.conf").exists())
        self.docker.call.assert_not_called()

    def test_completed_reuse_reverifies_authority_and_changes_no_candidates(self):
        self.prepare()
        paths = tuple((self.config.state_root / "gateway").rglob("*"))
        before = {
            str(path): (path.stat().st_ino, path.read_bytes())
            for path in paths
            if path.is_file()
        }
        self.assertTrue(self.prepare()["preparation_reused"])
        after = {
            str(path): (path.stat().st_ino, path.read_bytes())
            for path in paths
            if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual(2, self.acceptance.call_count)

    def test_candidate_files_preserve_real_exact_three_release_host_boundary(self):
        host, unused_baseline, leased, lease, unused_verified = host_evidence()
        host.docker = SnapshotDocker(leased)
        host.lease = lease
        with mock.patch.object(
            gateway.inference, "_project_inventory", self.real_inventory
        ):
            before = host.observe()
            private_proof = copy.deepcopy(host.last)
        self.prepare()
        with mock.patch.object(
            gateway.inference, "_project_inventory", self.real_inventory
        ):
            self.assertEqual(before, host.observe())
            self.assertEqual(private_proof, host.last)
            self.assertEqual(3, len(host.last[1]["container_ids"]))
            website = host.compose["networks"]["website"]["name"]
            host.docker.networks[website]["Containers"]["e" * 64] = {
                "Name": "foreign-gateway"
            }
            with self.assertRaises(ValueError):
                host.observe()

    def test_reuse_expiring_while_reading_prepared_record_is_rejected(self):
        self.prepare()
        prepared = self.config.state_root / "gateway/prepared.json"
        before = (prepared.stat().st_ino, prepared.read_bytes())
        clock = [0.0]
        original = gateway.image_build._record

        def expire(fd, name):
            value = original(fd, name)
            if name == "prepared.json":
                clock[0] = gateway.runtime_docker.TOTAL_SECONDS + 601
            return value

        with (
            mock.patch.object(gateway.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(gateway.image_build, "_record", side_effect=expire),
        ):
            with self.assertRaisesRegex(gateway.GatewayError, "preparation_deadline"):
                self.prepare()
        self.assertEqual(before, (prepared.stat().st_ino, prepared.read_bytes()))
        self.docker.call.assert_not_called()

    def test_partial_release_rejected_before_facade_could_start_any_services(self):
        (self.slot / "complete.json").unlink()
        with self.assertRaisesRegex(gateway.GatewayError, "completed_release_required"):
            self.prepare()
        self.acceptance.assert_not_called()
        self.docker.inspect.assert_not_called()
        self.assertFalse((self.config.state_root / "gateway").exists())

    def test_failed_release_reverification_publishes_nothing_and_hides_private_error(
        self,
    ):
        self.acceptance.side_effect = ValueError("PRIVATE_SENTINEL")
        with self.assertRaisesRegex(
            gateway.GatewayError, "^gateway_preparation_failed$"
        ):
            self.prepare()
        self.assertFalse((self.config.state_root / "gateway").exists())

    def test_start_result_cannot_be_adopted_as_completed_reuse(self):
        self.acceptance.return_value = {**self.completed, "runtime_reused": False}
        with self.assertRaisesRegex(gateway.GatewayError, "completed_release_required"):
            self.prepare()
        self.assertFalse((self.config.state_root / "gateway").exists())

    def test_foreign_singleton_is_rejected_before_release_probe(self):
        self.inventory.return_value = (["f" * 64], {"depositar-ai-gateway_ingress"})
        with self.assertRaisesRegex(
            gateway.GatewayError, "existing_runtime_not_adopted"
        ):
            self.prepare()
        self.acceptance.assert_not_called()
        self.assertFalse((self.config.state_root / "gateway").exists())

    def test_nginx_source_image_mismatch_prevents_any_preparation(self):
        self.image_check.return_value = {**self.proof, "image_id": "sha256:" + "f" * 64}
        with self.assertRaisesRegex(gateway.GatewayError, "image_changed"):
            self.prepare()
        self.acceptance.assert_not_called()
        self.assertFalse((self.config.state_root / "gateway").exists())

    def test_interrupted_candidate_remains_quarantined_without_automatic_retry(self):
        original = gateway.private_io.write_json

        def fail(fd, name, value, **options):
            if name == "prepared.json":
                raise OSError("PRIVATE_SENTINEL")
            return original(fd, name, value, **options)

        with mock.patch.object(gateway.private_io, "write_json", side_effect=fail):
            with self.assertRaises(gateway.GatewayError):
                self.prepare()
        root = self.config.state_root / "gateway"
        self.assertTrue((root / "intent.json").exists())
        self.assertFalse((root / "prepared.json").exists())
        self.acceptance.reset_mock()
        with self.assertRaisesRegex(
            gateway.GatewayError, "partial_state_recovery_required"
        ):
            self.prepare()
        self.acceptance.assert_not_called()

    def test_tampered_route_is_never_repaired_or_overwritten(self):
        self.prepare()
        path = self.config.state_root / "gateway/routes/candidate.conf"
        path.write_bytes(b"untrusted\n")
        with self.assertRaisesRegex(gateway.GatewayError, "route_changed"):
            self.prepare()
        self.assertEqual(b"untrusted\n", path.read_bytes())

    def test_route_drift_during_creation_never_publishes_prepared_receipt(self):
        original = gateway.private_io.write_json

        def drift(fd, name, value, **options):
            result = original(fd, name, value, **options)
            if name == "compose.json":
                (self.config.state_root / "gateway/routes/candidate.conf").write_bytes(
                    b"untrusted\n"
                )
            return result

        with mock.patch.object(gateway.private_io, "write_json", side_effect=drift):
            with self.assertRaisesRegex(gateway.GatewayError, "route_changed"):
                self.prepare()
        self.assertFalse((self.config.state_root / "gateway/prepared.json").exists())

    def test_release_receipt_drift_after_probe_blocks_publication(self):
        def drift(*args, **kwargs):
            (self.slot / "lease.json").write_text('{"changed":true}\n')
            return {**self.completed, "runtime_reused": True}

        self.acceptance.side_effect = drift
        with self.assertRaises(gateway.GatewayError):
            self.prepare()
        self.assertFalse((self.config.state_root / "gateway").exists())

    def test_route_drift_after_prepared_write_cannot_return_success(self):
        original = gateway.private_io.write_json

        def drift(fd, name, value, **options):
            result = original(fd, name, value, **options)
            if name == "prepared.json":
                (self.config.state_root / "gateway/routes/candidate.conf").write_bytes(
                    b"untrusted\n"
                )
            return result

        with mock.patch.object(gateway.private_io, "write_json", side_effect=drift):
            with self.assertRaisesRegex(gateway.GatewayError, "route_changed"):
                self.prepare()
        self.assertTrue((self.config.state_root / "gateway/prepared.json").exists())
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_route_drift_during_each_prepared_read_cannot_return_reuse(self):
        self.prepare()
        prepared = self.config.state_root / "gateway/prepared.json"
        before = (prepared.stat().st_ino, prepared.read_bytes())
        original = gateway.image_build._record

        def drift(fd, name):
            value = original(fd, name)
            if name == "prepared.json":
                (self.config.state_root / "gateway/routes/candidate.conf").write_bytes(
                    b"untrusted\n"
                )
            return value

        with mock.patch.object(gateway.image_build, "_record", side_effect=drift):
            with self.assertRaisesRegex(gateway.GatewayError, "route_changed"):
                self.prepare()
        self.assertEqual(before, (prepared.stat().st_ino, prepared.read_bytes()))
        self.docker.call.assert_not_called()
