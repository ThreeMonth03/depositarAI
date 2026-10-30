"""Independent catalog authority, locks and cross-catalog snapshot regressions."""

from contextlib import ExitStack
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from search.operations.catalog import client_lease, execution
from search.operations.deployment.candidate import catalog
from search.operations.state import locked_state
from tests.unit.catalog.test_client_lease import (
    leased_runtime_evidence,
    shared_evidence,
)


REVISION = "a" * 40


def two_catalogs():
    clients = shared_evidence(separate_catalogs=True)
    bindings, groups, networks = [], {}, {}
    for index, member in enumerate(clients):
        values = leased_runtime_evidence(
            catalog_revision=REVISION if index == 0 else "7" * 40
        )
        project = values["core_plan"]["project"]
        network = member["catalog_network"]
        network.update(Driver="bridge", Scope="local", Ingress=False)
        cores = member["catalog_identity"]["core_container_ids"]
        for row in values["core_containers"]:
            role = row["Config"]["Labels"]["com.docker.compose.service"]
            if role in cores:
                row["Id"] = cores[role]
                row["NetworkSettings"]["Networks"][network["Name"]]["NetworkID"] = (
                    network["Id"]
                )
            else:
                row["Id"] = ("4" if index else "c") * 64
                row["HostConfig"]["NetworkMode"] = "container:" + cores["fess-search"]
        context = {
            "plan": values["core_plan"],
            "compose": values["core_compose"],
            "repo": Path("/source") / str(index),
            "source_revision": REVISION,
        }
        binding = catalog.Catalog(
            Path("/config") / str(index),
            None,
            None,
            context,
            {
                "catalog_identity": member["catalog_identity"],
                "runtime": {
                    "container_ids": {
                        r["Config"]["Labels"]["com.docker.compose.service"]: r["Id"]
                        for r in values["core_containers"]
                    },
                    "network_id": network["Id"],
                },
            },
            values["core_images"],
            values["core_image_inspections"],
            {},
        )
        bindings.append(binding)
        groups[project] = values["core_containers"]
        networks[network["Name"]] = network
    return bindings, groups, networks, clients


class CatalogSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.storage = self.stack.enter_context(
            patch.object(execution, "require_storage", return_value=object())
        )
        self.mounts = self.stack.enter_context(
            patch.object(execution, "validate_storage_mounts")
        )

    def test_each_catalog_authenticates_its_own_real_core_and_complete_client(self):
        bindings, groups, networks, clients = two_catalogs()
        expected = copy.deepcopy((groups, networks, clients))
        for binding in bindings:
            snapshot = catalog.observe(binding, groups, networks, clients)
            self.assertEqual(snapshot[0], binding.authority["runtime"])
        self.assertEqual((groups, networks, clients), expected)
        self.assertEqual(self.mounts.call_count, 2)

    def test_other_catalog_lease_or_runtime_cannot_authorize_the_selected_core(self):
        for kind in ("authority", "core", "unknown_peer", "third_client"):
            with self.subTest(kind=kind):
                bindings, groups, networks, clients = two_catalogs()
                target = bindings[1]
                if kind == "authority":
                    target.authority = bindings[0].authority
                elif kind == "core":
                    groups[target.context["plan"]["project"]] = groups[
                        bindings[0].context["plan"]["project"]
                    ]
                elif kind == "unknown_peer":
                    networks[target.context["plan"]["project"] + "_search_backend"][
                        "Containers"
                    ]["f" * 64] = {}
                else:
                    clients.append(copy.deepcopy(clients[0]))
                with self.assertRaises((ValueError, RuntimeError)):
                    catalog.observe(target, groups, networks, clients)

    def test_selected_catalog_before_cpu_creation_keeps_exact_core_only_contract(self):
        bindings, groups, networks, clients = two_catalogs()
        target = bindings[1]
        network = networks[target.context["plan"]["project"] + "_search_backend"]
        del network["Containers"][clients[1]["lease"]["api_container_id"]]
        snapshot = catalog.observe(target, groups, networks, clients[:1])
        self.assertEqual(snapshot[0], target.authority["runtime"])
        network["Containers"]["f" * 64] = {}
        with self.assertRaises(ValueError):
            catalog.observe(target, groups, networks, clients[:1])

    def test_network_type_is_checked_before_and_after_selected_cpu_creation(self):
        for attached in (False, True):
            for field, invalid in (
                ("Driver", "overlay"),
                ("Scope", "swarm"),
                ("Ingress", True),
            ):
                with self.subTest(attached=attached, field=field):
                    bindings, groups, networks, clients = two_catalogs()
                    target = bindings[1]
                    network = networks[
                        target.context["plan"]["project"] + "_search_backend"
                    ]
                    if not attached:
                        del network["Containers"][
                            clients[1]["lease"]["api_container_id"]
                        ]
                        clients = clients[:1]
                    network[field] = invalid
                    with self.assertRaisesRegex(
                        catalog.SharedCatalogError, "network_changed"
                    ):
                        catalog.observe(target, groups, networks, clients)

    def test_wrong_image_startup_or_storage_never_becomes_query_authority(self):
        bindings, groups, networks, clients = two_catalogs()
        bindings[1].inspections["fess-search"]["Config"]["Cmd"] = ["unreviewed"]
        with self.assertRaises(client_lease.LeaseError):
            catalog.observe(bindings[1], groups, networks, clients)
        self.mounts.side_effect = ValueError("catalog_storage_changed")
        with self.assertRaisesRegex(ValueError, "catalog_storage_changed"):
            catalog.observe(bindings[0], groups, networks, clients)


class CatalogAuditTests(unittest.TestCase):
    def test_refresh_rejects_drift_in_config_source_plan_and_api_key_authority(self):
        for kind in ("config", "source", "plan", "key"):
            with self.subTest(kind=kind):
                binding = two_catalogs()[0][1]
                binding.config = object()
                binding.key = {"credential_sha256": "1" * 64}
                binding.store = SimpleNamespace(
                    read=Mock(return_value=binding.context["plan"])
                )
                with (
                    patch.object(
                        catalog.product_config,
                        "load_config",
                        return_value=binding.config,
                    ) as config,
                    patch.object(
                        catalog, "require_clean_revision", return_value=REVISION
                    ) as source,
                    patch.object(
                        catalog.credentials,
                        "read_api_key_authority",
                        return_value=binding.key,
                    ) as key,
                ):
                    if kind == "config":
                        config.return_value = object()
                    elif kind == "source":
                        source.return_value = "b" * 40
                    elif kind == "plan":
                        binding.store.read.return_value = {"other_catalog": True}
                    else:
                        key.return_value = {"credential_sha256": "2" * 64}
                    with self.assertRaises(ValueError):
                        binding.refresh()

    def test_selected_chunk_profile_rejects_missing_duplicate_and_overriding_properties(
        self,
    ):
        context = {
            "compose": {
                "services": {
                    "fess-search": {
                        "environment": {
                            "FESS_JAVA_OPTS": " ".join(catalog.chunks.JAVA_OPTIONS)
                        }
                    }
                }
            }
        }
        catalog.validate_chunk_profile(context)
        for value in (
            " ".join(catalog.chunks.JAVA_OPTIONS[1:]),
            " ".join(catalog.chunks.JAVA_OPTIONS)
            + " "
            + catalog.chunks.JAVA_OPTIONS[0],
            " ".join(catalog.chunks.JAVA_OPTIONS)
            + " "
            + catalog.chunks.JAVA_OPTIONS[1].split("=")[0]
            + "=42",
        ):
            with self.subTest(value=value):
                context["compose"]["services"]["fess-search"]["environment"][
                    "FESS_JAVA_OPTS"
                ] = value
                with self.assertRaisesRegex(
                    catalog.SharedCatalogError, "chunk_profile_changed"
                ):
                    catalog.validate_chunk_profile(context)

    def test_query_profile_reads_fixed_literals_without_executing_module_text(self):
        source = Path(__file__).resolve().parents[4]
        expected = catalog.query_profile(source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (
                "search/catalog/embedding.py",
                "search/planning/index_filters.py",
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((source / relative).read_bytes())
            self.assertEqual(catalog.query_profile(root), expected)
            embedding = root / "search/catalog/embedding.py"
            marker = root / "must-not-exist"
            embedding.write_text(
                f"MODEL = __import__('pathlib').Path({str(marker)!r}).touch()\nMODEL_DIGEST = 'unsealed'\nDIMENSIONS = 768\n"
            )
            with self.assertRaisesRegex(
                catalog.SharedCatalogError, "query_profile_invalid"
            ):
                catalog.query_profile(root)
            self.assertFalse(marker.exists())

    def test_source_audit_uses_selected_source_config_and_full_snapshot(self):
        snapshot = (
            {"network_id": "b" * 64},
            [{"Id": "c" * 64}],
            {"Containers": {"foreign": {}}},
        )
        value = {
            "source_revision": REVISION,
            "plan": {},
            "compose": {},
            "authority": {},
            "images": {},
        }

        def run(argv, **kwargs):
            self.assertEqual(
                argv[6:9], ["/selected-source", "/selected-config", REVISION]
            )
            self.assertEqual(kwargs["timeout"], 37)
            self.assertEqual(json.loads(Path(argv[9]).read_bytes()), list(snapshot))
            return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())

        with (
            patch.object(catalog, "require_clean_revision", return_value=REVISION),
            patch.object(catalog.image_build, "_run", side_effect=run),
        ):
            actual = catalog.source_record(
                Path("/selected-source"),
                Path("/selected-config"),
                REVISION,
                snapshot=snapshot,
                timeout=37,
            )
        self.assertEqual(actual, value)

    def test_changed_source_is_rejected_before_execution_and_child_failure_is_closed(
        self,
    ):
        with (
            patch.object(catalog, "require_clean_revision", return_value="b" * 40),
            patch.object(catalog.image_build, "_run") as run,
        ):
            with self.assertRaisesRegex(catalog.SharedCatalogError, "source_changed"):
                catalog.source_record(
                    Path("/selected-source"), Path("/config"), REVISION
                )
            run.assert_not_called()
        with (
            patch.object(catalog, "require_clean_revision", return_value=REVISION),
            patch.object(
                catalog.image_build,
                "_run",
                return_value=SimpleNamespace(
                    returncode=1, stdout=b"PRIVATE_CREDENTIAL"
                ),
            ),
        ):
            with self.assertRaisesRegex(
                catalog.SharedCatalogError, "source_audit_failed"
            ) as caught:
                catalog.source_record(
                    Path("/selected-source"), Path("/config"), REVISION
                )
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_query_audit_compares_every_selected_authority_component(self):
        binding = two_catalogs()[0][1]
        record = {
            "source_revision": REVISION,
            "plan": binding.context["plan"],
            "compose": binding.context["compose"],
            "authority": binding.authority,
            "images": binding.images,
        }
        for key in ("plan", "compose", "authority", "images"):
            with self.subTest(key=key):
                changed = copy.deepcopy(record)
                changed[key] = {"other_catalog": True}
                with (
                    patch.object(binding, "refresh"),
                    patch.object(catalog, "source_record", return_value=changed),
                ):
                    with self.assertRaisesRegex(
                        ValueError, "catalog_authority_changed"
                    ):
                        binding.audit(({}, [], {}), timeout=50)


class CatalogSelectionTests(unittest.TestCase):
    def test_default_selection_reuses_exact_native_authority_without_new_state(self):
        native = SimpleNamespace(
            context={},
            authority={"native": True},
            catalog_images={},
            catalog_inspections={},
        )
        with ExitStack() as stack, patch.object(catalog, "locked_state") as lock:
            first, selected = catalog.select(
                {"production_config": Path("/native.json")},
                stack,
                object(),
                object(),
                native,
                {"native_key": True},
            )
            self.assertIs(first, selected)
            self.assertIs(selected.authority, native.authority)
            lock.assert_not_called()

    def test_selected_product_and_catalog_locks_are_retained_until_session_exit(self):
        from tests.unit.catalog.test_client_lease import evidence
        from search.operations.config import load_config

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            fixture = evidence()
            # Obtain the canonical product shape without needing any real models.
            raw = {
                "schema_version": 2,
                "cache_ttl_seconds": 0,
                "state_root": str(root / "native"),
                "api_model_root": str(root / "api"),
                "embedding_model_root": str(root / "embedding"),
                "reranker_model_root": str(root / "reranker"),
                "catalog_storage": "docker-volume",
                "gpus": {
                    "scope": "GPU-00000000-0000-4000-8000-000000000001",
                    "planner": [
                        "GPU-00000001-0000-4000-8000-000000000001",
                        "GPU-00000002-0000-4000-8000-000000000001",
                    ],
                    "reranker": "GPU-00000003-0000-4000-8000-000000000001",
                },
            }
            for name in ("api", "embedding", "reranker"):
                (root / name).mkdir(mode=0o700)
            config_file = root / "product.json"
            config_file.write_text(json.dumps(raw))
            config_file.chmod(0o600)
            config = load_config(config_file, repo_root=root / "source")
            selected = replace(config, state_root=root / "selected")
            config_file.write_text(json.dumps(selected.to_dict()))
            data = root / "selected-data"
            (data / "state").mkdir(parents=True, mode=0o700)
            os.chmod(data, 0o700)
            (data / "state/operations.lock").touch(mode=0o600)
            with locked_state(selected, create=True) as target:
                target.write("index-candidate.json", {"data_root": str(data)})
            native = SimpleNamespace(
                context={"plan": {"project": "native"}},
                authority={},
                catalog_images={},
                catalog_inspections={},
                docker=SimpleNamespace(inspect=lambda _: {}),
            )
            profile = {
                "production_config": root / "native.json",
                "production_source": root / "native-source",
                "catalog_config": config_file,
                "catalog_source": root / "source",
            }
            context = {
                "plan": {"project": "selected", "data_root": str(data)},
                "source_revision": REVISION,
                "repo": root / "source",
                "compose": {
                    "services": {
                        "fess-search": {
                            "environment": {
                                "FESS_JAVA_OPTS": " ".join(catalog.chunks.JAVA_OPTIONS)
                            }
                        }
                    }
                },
            }
            value = {"images": {}, "authority": fixture["catalog_identity"]}
            with (
                patch.object(catalog, "load_context", return_value=(context, value)),
                patch.object(catalog.Catalog, "refresh"),
                patch.object(
                    catalog.credentials,
                    "read_api_key_authority",
                    return_value={"credential_sha256": "1" * 64},
                ),
            ):
                with ExitStack() as stack:
                    original, chosen = catalog.select(
                        profile, stack, config, None, native, {}
                    )
                    self.assertIsNot(original, chosen)
                    with self.assertRaisesRegex(ValueError, "product_operation_busy"):
                        with locked_state(selected):
                            self.fail("selected product lock was released")
                    with self.assertRaises(BlockingIOError):
                        with execution.operation_lock({"data": data}):
                            self.fail("selected catalog lock was released")
                with locked_state(selected), execution.operation_lock({"data": data}):
                    pass


class CatalogCollectionTests(unittest.TestCase):
    def test_five_projects_use_twelve_commands_and_retain_unknown_network_peers(self):
        projects = [(f"project-{n}", {f"project-{n}_network"}) for n in range(5)]
        rows = [{"Id": f"{n:064x}"} for n in range(1, 16)]
        networks = [
            {"Name": f"project-{n}_network", "Containers": {"unknown-peer": {}}}
            for n in range(5)
        ]
        calls = []

        def inventory(docker, project):
            number = int(project[-1])
            calls.extend(["containers", "networks"])
            return [row["Id"] for row in rows[number * 3 : number * 3 + 3]], {
                project + "_network"
            }

        def call(arguments):
            calls.append(arguments)
            return json.dumps(rows if arguments[0] == "inspect" else networks).encode()

        with patch.object(
            catalog.runtime_docker, "project_inventory", side_effect=inventory
        ):
            groups, actual = catalog.collect(SimpleNamespace(call=call), projects)
        self.assertEqual(len(calls), 12)
        self.assertEqual(sum(map(len, groups.values())), 15)
        self.assertTrue(
            all("unknown-peer" in network["Containers"] for network in actual.values())
        )

    def test_missing_project_member_or_network_is_rejected_before_bulk_inspect(self):
        for identifiers, networks in (
            (["one", "two"], {"owned"}),
            (["one", "one", "two"], {"owned"}),
            (["one", "two", "three"], {"owned", "foreign"}),
        ):
            with (
                self.subTest(networks=networks),
                patch.object(
                    catalog.runtime_docker,
                    "project_inventory",
                    return_value=(identifiers, networks),
                ),
            ):
                docker = SimpleNamespace(call=unittest.mock.Mock())
                with self.assertRaisesRegex(
                    catalog.SharedCatalogError, "inventory_changed"
                ):
                    catalog.collect(docker, [("project", {"owned"})])
                docker.call.assert_not_called()
