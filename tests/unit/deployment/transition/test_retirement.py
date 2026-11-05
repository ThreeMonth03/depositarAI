"""Exercise irreversible cleanup with real topology proofs and fake Docker."""

from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.catalog import execution
from search.operations.deployment.provider import authority
from search.operations.deployment.transition import (
    contract,
    journal,
    retirement,
)
from search.operations.shared import private_io
from tests.unit.deployment.provider.fixtures import dual_snapshot
from tests.unit.deployment.provider.test_contract import records
from .fixtures import (
    ObservingOwner,
    frontend_projection,
    operation_record,
    reference,
    tls_projection,
)


class CleanupDocker:
    """Only exact owned deletion and read-only inventory commands exist."""

    def __init__(self, groups, networks):
        self.groups, self.networks = groups, networks
        self.calls = []
        self.crash_after_remove = False

    def call(self, args, *, timeout=None):
        self.calls.append(list(args))
        if args[0] == "ps":
            return b"\n".join(
                json.dumps({"Id": row["Id"], "Project": name}).encode()
                for name, rows in self.groups.items()
                for row in rows
            )
        if args[:2] == ["network", "ls"]:
            return b"\n".join(
                json.dumps({"Name": name}).encode() for name in self.networks
            )
        if args[0] == "inspect":
            return json.dumps(
                [
                    row
                    for rows in self.groups.values()
                    for row in rows
                    if row["Id"] in args[1:]
                ]
            ).encode()
        if args[:2] == ["network", "inspect"]:
            return json.dumps([self.networks[n] for n in args[2:]]).encode()
        if args[:2] == ["rm", "--force"]:
            self.remove_containers(args[2:])
            return b""
        if args[:2] == ["network", "rm"]:
            self.remove_network(args[2])
            return b""
        raise AssertionError("cleanup cannot create, start or alter images")

    def remove_containers(self, identifiers):
        for identifier in identifiers:
            for project in self.groups:
                self.groups[project] = [
                    row
                    for row in self.groups[project]
                    if row["Id"] != identifier
                ]
            for network in self.networks.values():
                network.get("Containers", {}).pop(identifier, None)
            if self.crash_after_remove:
                self.crash_after_remove = False
                raise OSError("owned crash after one container removal")

    def remove_network(self, identifier):
        name = next(
            n for n, row in self.networks.items() if row["Id"] == identifier
        )
        if self.networks[name].get("Containers"):
            raise AssertionError("cannot remove an attached network")
        del self.networks[name]

    def mutations(self):
        return [
            args
            for args in self.calls
            if args[0] == "rm" or args[:2] == ["network", "rm"]
        ]


class RetirementCleanupTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="transition-cleanup-")
        )
        self.root = Path(directory)
        self.root.chmod(0o700)
        self.stack.enter_context(
            patch.object(execution, "require_storage", return_value=object())
        )
        self.stack.enter_context(
            patch.object(execution, "validate_storage_mounts")
        )

    def fixture(self, kind="backend-code-update"):
        data = dual_snapshot()
        config = self.root / "control.json"
        config.write_bytes(
            private_io.canonical(
                {"schema_version": 1, "state_root": str(self.root / "control")}
            )
        )
        config.chmod(0o600)
        control = self.stack.enter_context(
            authority.open_control(config, create=True)
        )
        value = operation_record(control, kind=kind)
        self.publish_hosts(control, data)
        before = self.rebind_intent(control, value, data, kind)
        owner = SimpleNamespace(
            provider=data.provider,
            members=lambda: data.hosts,
            catalogs=lambda: data.catalogs,
        )
        disposal = retirement.disposal(
            owner, data.hosts[1], {"networks": data.networks}
        )
        if kind == "backend-code-retire":
            value["retirement"]["dispose"] = disposal
        operation = journal.create(control, value)
        witness = ObservingOwner()
        operation.begin(witness, witness)
        # Native provider source/image/model authorities are outside this
        # domain unit; use the genuine private Operation as its refresh seam.
        data.provider._refresh_authorities = operation.refresh
        data.provider.docker = CleanupDocker(data.groups, data.networks)
        baseline = retirement.seal(owner, operation, data.hosts[1])
        return SimpleNamespace(
            owner=owner,
            operation=operation,
            data=data,
            baseline=baseline,
            docker=data.provider.docker,
            before=before,
        )

    def publish_hosts(self, control, data):
        for index, host in enumerate(data.hosts):
            record = copy.deepcopy(records()[1])
            retained = self.root / ("retained-" + str(index))
            retained.mkdir(mode=0o700)
            (retained / "sentinel").write_bytes(b"preserved source and build")
            record.update(
                source={
                    "root": str(host.source_root),
                    "revision": host.source_revision,
                },
                compose=host.compose,
                lease=host.lease,
                runtime=host.expected_runtime,
                image_proof=host.image_proof,
                catalog_binding=host.catalog.identity(),
                namespace_sha256=host.namespace_sha256,
                owned_paths=[str(retained)],
            )
            host.backend_sha256 = control.publish("backend", record)
            host.record = record

    def rebind_intent(self, control, value, data, kind):
        a, b = data.hosts
        old = control.roles
        roles = {
            **old,
            "epoch": old["epoch"] + 1,
            "active_backend_sha256": a.backend_sha256,
            "rollback_backend_sha256": None
            if kind == "backend-code-update"
            else b.backend_sha256,
        }
        control._compare_and_swap_roles(
            roles,
            expected_epoch=old["epoch"],
            expected_sha256=contract.digest(old),
        )
        value["predecessor_roles"] = roles
        value["predecessor_roles_sha256"] = contract.digest(roles)
        before = self.host_projection(control, a, roles["tls_plan_ref"])
        target = self.host_projection(control, b, reference("/private/B.json"))
        value["sides"] = {"predecessor": before, "target": target}
        if kind == "backend-code-update":
            value["candidate"].update(
                original_epoch=roles["epoch"],
                original_roles_sha256=contract.digest(roles),
                backend=target["backend"],
            )
        if kind == "backend-code-retire":
            value["sides"]["target"] = copy.deepcopy(before)
            value["retirement"]["backend"] = target["backend"]
        return copy.deepcopy(data.groups)

    def host_projection(self, control, host, plan):
        return {
            "backend": reference(
                control.root / ("backend-" + host.backend_sha256 + ".json"),
                host.backend_sha256,
            ),
            "tls": tls_projection(host.record, plan),
            "frontend": frontend_projection(host.source_revision),
        }

    def authorize(self, fixture):
        operation = fixture.operation
        if operation.intent["kind"] == "backend-code-update":
            operation.lock_predecessor(
                fixture.baseline["resources"], ObservingOwner()
            )
        else:
            operation.dispatch_retirement()

    def assert_protected(self, fixture):
        removed = fixture.data.hosts[1].compose["name"]
        for project, rows in fixture.before.items():
            if project != removed:
                self.assertEqual(fixture.data.groups[project], rows)
        for host in fixture.data.hosts:
            for root in host.record["owned_paths"]:
                self.assertEqual(
                    (Path(root) / "sentinel").read_bytes(),
                    b"preserved source and build",
                )

    def test_missing_discard_never_dispatches_cleanup(self):
        fixture = self.fixture()
        with self.assertRaisesRegex(
            contract.TransitionError, "cleanup_not_authorized"
        ):
            retirement.Cleanup(fixture.owner, fixture.operation)
        self.assertEqual(fixture.docker.mutations(), [])
        self.assert_protected(fixture)

    def test_removal_crash_continues_same_operation_without_recreating_b(self):
        fixture = self.fixture()
        self.authorize(fixture)
        fixture.docker.crash_after_remove = True
        cleanup = retirement.Cleanup(fixture.owner, fixture.operation)
        with self.assertRaisesRegex(OSError, "after one container removal"):
            cleanup.finish()
        remaining = fixture.data.groups[fixture.data.hosts[1].compose["name"]]
        self.assertEqual(len(remaining), 2)
        self.assert_protected(fixture)
        recovered = retirement.Cleanup(fixture.owner, fixture.operation)
        proof = recovered.finish()
        self.assertTrue(proof["runtime_lease_closed"])
        self.assertEqual(proof["remaining_container_ids"], [])
        self.assertTrue(proof["exclusive_network_absent"])
        self.assertEqual(recovered.finish(), proof)
        self.assert_protected(fixture)
        removed = {
            identifier
            for args in fixture.docker.mutations()
            if args[0] == "rm"
            for identifier in args[2:]
        }
        self.assertEqual(
            removed,
            set(fixture.baseline["resources"]["cpu_container_ids"].values()),
        )

    def test_foreign_replacement_container_refuses_before_any_removal(self):
        fixture = self.fixture()
        self.authorize(fixture)
        group = fixture.data.groups[fixture.data.hosts[1].compose["name"]]
        group[0]["Id"] = "0" * 64
        with self.assertRaisesRegex(
            contract.TransitionError, "cleanup_changed"
        ):
            retirement.Cleanup(fixture.owner, fixture.operation).finish()
        self.assertEqual(fixture.docker.mutations(), [])
        self.assert_protected(fixture)

    def test_retire_requires_dispatch_then_cleans_only_the_rollback_backend(
        self,
    ):
        fixture = self.fixture("backend-code-retire")
        with self.assertRaisesRegex(
            contract.TransitionError, "cleanup_not_authorized"
        ):
            retirement.Cleanup(fixture.owner, fixture.operation)
        self.assertEqual(fixture.docker.mutations(), [])
        fixture.operation.dispatch_retirement()
        with self.assertRaisesRegex(contract.TransitionError, "outcome_fixed"):
            fixture.operation.resolve(
                ObservingOwner(),
                ObservingOwner(),
                "predecessor",
                reason="verified_return",
            )
        proof = retirement.Cleanup(fixture.owner, fixture.operation).finish()
        self.assertTrue(proof["runtime_lease_closed"])
        self.assertEqual(
            proof["backend_sha256"],
            fixture.operation.intent["predecessor_roles"][
                "rollback_backend_sha256"
            ],
        )
        self.assert_protected(fixture)

    def test_rollback_kind_has_no_cleanup_authority(self):
        fixture = self.fixture("backend-code-rollback")
        with self.assertRaisesRegex(contract.TransitionError, "kind_invalid"):
            retirement.Cleanup(fixture.owner, fixture.operation)
        self.assertEqual(fixture.docker.mutations(), [])
        self.assert_protected(fixture)

    def test_foreign_peer_on_owned_network_blocks_all_removal(self):
        fixture = self.fixture()
        self.authorize(fixture)
        name = fixture.baseline["network"]["Name"]
        fixture.data.networks[name]["Containers"]["0" * 64] = {}
        with self.assertRaisesRegex(
            contract.TransitionError, "runtime_changed"
        ):
            retirement.Cleanup(fixture.owner, fixture.operation).finish()
        self.assertEqual(fixture.docker.mutations(), [])
        self.assert_protected(fixture)
