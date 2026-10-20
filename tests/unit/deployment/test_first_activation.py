"""First-activation barriers with real private receipts and fake Docker IO."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from search.operations import state
from search.operations.deployment import composition, credentials, first_activation
from tests.unit.deployment.test_composition import product


PROJECT = composition.GATEWAY_PROJECT
CONTAINERS = [
    "ps",
    "--all",
    "--quiet",
    "--no-trunc",
    "--filter",
    "label=com.docker.compose.project=" + PROJECT,
]
NETWORKS = [
    "network",
    "ls",
    "--no-trunc",
    "--format",
    "{{json .}}",
    "--filter",
    "name=" + PROJECT + "_",
]


class FirstActivationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="depositar-first-activation-test-"
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = replace(product(), state_root=self.root / "product")
        with state.locked_state(self.config, create=True):
            pass
        self.calls, self.clients, self.timeouts = [], [], []
        self.containers = self.networks = b""
        self.fail_command = None
        self.on_command = None

    def runner(self, argv, *, timeout, environment):
        self.assertEqual(
            ["docker", "--host", "unix:///var/run/docker.sock", "--config"], argv[:4]
        )
        client = Path(argv[4])
        self.assertEqual(0o700, stat.S_IMODE(client.stat().st_mode))
        self.assertEqual(os.geteuid(), client.stat().st_uid)
        self.assertEqual([], list(client.iterdir()))
        self.assertEqual(
            {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
            environment,
        )
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 30)
        arguments = argv[5:]
        self.assertIn(arguments, (CONTAINERS, NETWORKS))
        self.calls.append(arguments)
        self.clients.append(client)
        self.timeouts.append(timeout)
        if self.on_command:
            self.on_command(client)
        raw = self.containers if arguments == CONTAINERS else self.networks
        return subprocess.CompletedProcess(
            argv,
            1 if arguments == self.fail_command else 0,
            raw,
            b"PRIVATE_SENTINEL" if arguments == self.fail_command else b"",
        )

    def invoke(self):
        with state.locked_state(self.config) as store:
            return first_activation.require_first_activation(
                store, docker_runner=self.runner
            )

    def receipt(self, index, *, operation="sync", status="staged"):
        with state.locked_state(self.config) as store:
            store.write(
                f"op-{index:032x}.json",
                {
                    "schema_version": 1,
                    "operation": operation,
                    "source_revision": "a" * 40,
                    "status": status,
                    "active_changed": operation == "activate-gateway"
                    and status == "staged",
                },
            )

    def snapshot(self):
        return {
            path.name: (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
            for path in self.config.state_root.iterdir()
            if path.is_file() and not path.is_symlink()
        }

    def assert_rejected(self, code):
        before = self.snapshot()
        with self.assertRaises(first_activation.FirstActivationError) as caught:
            self.invoke()
        self.assertEqual(code, str(caught.exception))
        self.assertIn(code, first_activation.ERROR_CODES)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(all(not path.exists() for path in self.clients))

    def test_empty_gateway_inventory_uses_only_two_isolated_readonly_commands(self):
        before = self.snapshot()
        self.assertIsNone(self.invoke())
        self.assertEqual([CONTAINERS, NETWORKS], self.calls)
        self.assertEqual(1, len(set(self.clients)))
        self.assertFalse(self.clients[0].exists())
        self.assertEqual(before, self.snapshot())

    def test_catalog_checkpoint_receipts_remain_eligible_without_new_operations(self):
        for index, operation in enumerate(
            (
                "sync",
                "prepare-index",
                "build-catalog-images",
                "start-index",
                "import-index",
                "reconcile-index",
                "build-vectors",
            )
        ):
            self.receipt(index, operation=operation)
        before = self.snapshot()
        self.invoke()
        self.assertEqual(before, self.snapshot())

    def test_started_or_failed_receipts_reject_before_docker(self):
        for index, status in enumerate(("started", "failed")):
            self.receipt(index, status=status)
            self.assert_rejected("product_private_recovery_required")
        self.assertFalse(self.calls)

    def test_activation_history_cannot_be_hidden_by_removing_the_active_pointer(self):
        self.receipt(0, operation="activate-gateway")
        self.assert_rejected("product_private_existing_activation")
        self.assertFalse(self.calls)

    def test_other_completed_operations_are_not_a_first_catalog_checkpoint(self):
        allowed = {
            "sync",
            "prepare-index",
            "build-catalog-images",
            "start-index",
            "import-index",
            "reconcile-index",
            "build-vectors",
            "activate-gateway",
        }
        for index, operation in enumerate(sorted(state.OPERATIONS - allowed)):
            self.receipt(index, operation=operation)
            self.assert_rejected("product_private_recovery_required")
            (self.config.state_root / f"op-{index:032x}.json").unlink()
        self.assertFalse(self.calls)

    def test_activation_names_reject_every_file_type_including_broken_links(self):
        for name in ("active.json", "gateway"):
            for kind in ("file", "directory", "broken-link", "fifo"):
                with self.subTest(name=name, kind=kind):
                    path = self.config.state_root / name
                    if kind == "directory":
                        path.mkdir(mode=0o700)
                    elif kind == "broken-link":
                        path.symlink_to(self.root / "missing")
                    elif kind == "fifo":
                        os.mkfifo(path, 0o600)
                    else:
                        path.touch(mode=0o600)
                    self.assert_rejected("product_private_existing_activation")
                    if kind == "directory":
                        path.rmdir()
                    else:
                        path.unlink()
        self.assertFalse(self.calls)

    def test_startup_artifacts_reject_even_without_operation_receipts(self):
        for name in (
            "image-builds",
            "image-candidate.json",
            "secrets",
            credentials.ANCHOR,
            "inference",
            "inference-candidate.json",
            "releases",
            "release-candidate.json",
            "query-candidate.json",
        ):
            with self.subTest(name=name):
                path = self.config.state_root / name
                path.symlink_to(self.root / "missing")
                self.assert_rejected("product_private_recovery_required")
                path.unlink()
        self.assertFalse(self.calls)

    def test_malformed_or_unsafe_receipts_reject_before_docker(self):
        path = self.config.state_root / ("op-" + "a" * 32 + ".json")
        for raw, mode in ((b"[]", 0o600), (b"{}", 0o600), (b"{}", 0o644)):
            path.write_bytes(raw)
            path.chmod(mode)
            self.assert_rejected("product_private_recovery_required")
        path.unlink()
        (self.config.state_root / "op-malformed.json").touch(mode=0o600)
        self.assert_rejected("product_private_recovery_required")
        self.assertFalse(self.calls)

    def test_invalid_or_replaced_state_root_rejects_before_docker(self):
        with self.assertRaisesRegex(
            first_activation.FirstActivationError, "^product_private_recovery_required$"
        ):
            first_activation.require_first_activation(None, docker_runner=self.runner)
        with state.locked_state(self.config) as store:
            self.config.state_root.rename(self.root / "retained")
            self.config.state_root.mkdir(mode=0o700)
            with self.assertRaisesRegex(
                first_activation.FirstActivationError,
                "^product_private_recovery_required$",
            ):
                first_activation.require_first_activation(
                    store, docker_runner=self.runner
                )
        self.assertFalse(self.calls)

    def test_receipt_capacity_reserves_all_thirteen_steps_before_docker(self):
        for index in range(state.MAX_OPERATIONS - 13):
            self.receipt(index)
        self.invoke()
        self.calls.clear()
        self.receipt(state.MAX_OPERATIONS - 13)
        self.assert_rejected("product_private_receipt_capacity")
        self.assertFalse(self.calls)

    def test_existing_containers_or_networks_are_never_adopted(self):
        for containers, networks in (
            (b"a" * 64 + b"\n", b""),
            (b"", json.dumps({"Name": PROJECT + "_ingress"}).encode()),
        ):
            self.containers, self.networks = containers, networks
            self.assert_rejected("product_private_existing_activation")

    def test_malformed_inventory_fails_closed_without_raw_diagnostics(self):
        for containers, networks in (
            (b"PRIVATE_SENTINEL", b""),
            (b"\xff", b""),
            ((b"a" * 64 + b"\n") * 2, b""),
            (b"", b"PRIVATE_SENTINEL"),
            (b"", b"[]"),
            (b"", b"{}"),
            (b"", b'{"Name":"same"}\n{"Name":"same"}'),
        ):
            self.containers, self.networks = containers, networks
            self.assert_rejected("product_private_gateway_inventory_unverified")

    def test_nonzero_inventory_query_does_not_retry_and_cleans_private_config(self):
        for command in (CONTAINERS, NETWORKS):
            self.calls.clear()
            self.fail_command = command
            self.assert_rejected("product_private_gateway_inventory_unverified")
            self.assertEqual(1 if command == CONTAINERS else 2, len(self.calls))

    def test_private_config_mutation_is_rejected_and_removed(self):
        self.on_command = lambda client: (client / "config.json").write_text("{}")
        self.assert_rejected("product_private_gateway_inventory_unverified")
        self.assertEqual(1, len(self.calls))

    def test_two_commands_share_one_thirty_second_budget(self):
        clock = [100.0]

        def advance(_client):
            clock[0] = 105.0

        self.on_command = advance
        with mock.patch.object(
            first_activation.time, "monotonic", side_effect=lambda: clock[0]
        ):
            self.invoke()
        self.assertEqual([30, 25], self.timeouts)

    def test_inventory_that_exceeds_the_deadline_is_not_a_success(self):
        clock = [100.0]

        def expire(_client):
            clock[0] = 131.0

        self.on_command = expire
        with mock.patch.object(
            first_activation.time, "monotonic", side_effect=lambda: clock[0]
        ):
            self.assert_rejected("product_private_gateway_inventory_unverified")
        self.assertEqual(1, len(self.calls))

    def test_activation_added_during_inventory_is_rejected_before_return(self):
        self.on_command = lambda _client: (
            self.config.state_root / "active.json"
        ).touch(mode=0o600, exist_ok=True)
        with self.assertRaisesRegex(
            first_activation.FirstActivationError,
            "^product_private_existing_activation$",
        ):
            self.invoke()
        self.assertTrue(all(not path.exists() for path in self.clients))


if __name__ == "__main__":
    unittest.main()
