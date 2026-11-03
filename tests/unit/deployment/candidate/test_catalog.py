"""Independent catalog authority, locks and cross-catalog snapshot
regressions.
"""

from contextlib import ExitStack
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
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
                row["NetworkSettings"]["Networks"][network["Name"]][
                    "NetworkID"
                ] = network["Id"]
            else:
                row["Id"] = ("4" if index else "c") * 64
                row["HostConfig"]["NetworkMode"] = (
                    "container:" + cores["fess-search"]
                )
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
                        r["Config"]["Labels"]["com.docker.compose.service"]: r[
                            "Id"
                        ]
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


class CatalogClientIdentityTests(unittest.TestCase):
    def test_producer_and_consumer_relationship_is_independent_of_provider(
        self,
    ):
        for index, consumer, provider in (
            (0, REVISION, "b" * 40),
            (1, "7" * 40, REVISION),
            (1, REVISION, REVISION),
        ):
            for existing_optional in (False, True):
                with self.subTest(
                    index=index,
                    consumer=consumer,
                    provider=provider,
                    existing_optional=existing_optional,
                ):
                    binding = two_catalogs()[0][index]
                    producer = binding.context["plan"]["revision"]
                    raw = {
                        key: binding.authority["catalog_identity"][key]
                        for key in client_lease.CATALOG_FIELDS
                    }
                    if existing_optional:
                        raw["catalog_revision"] = producer
                    binding.authority["catalog_identity"] = raw
                    binding.context["source_revision"] = provider
                    original = copy.deepcopy(
                        (binding.context, binding.authority)
                    )
                    expected = {
                        key: raw[key] for key in client_lease.CATALOG_FIELDS
                    }
                    if producer != consumer:
                        expected["catalog_revision"] = producer
                    result = catalog.client_identity(binding, consumer)
                    self.assertEqual(result, expected)
                    self.assertIsNot(result, raw)
                    self.assertEqual(
                        (binding.context, binding.authority), original
                    )

    def test_returned_nested_identity_cannot_change_authenticated_authority(
        self,
    ):
        binding = two_catalogs()[0][0]
        original = copy.deepcopy(binding.authority)
        result = catalog.client_identity(binding, REVISION)
        result["core_container_ids"]["fess-search"] = "f" * 64
        self.assertEqual(binding.authority, original)

    def test_missing_or_invalid_producer_revision_is_rejected(self):
        for producer in (None, True, 1, "", "a" * 39, "a" * 41, "A" * 40):
            with self.subTest(producer=producer):
                binding = two_catalogs()[0][0]
                binding.context["plan"]["revision"] = producer
                with self.assertRaisesRegex(
                    catalog.SharedCatalogError, "catalog_identity_invalid$"
                ):
                    catalog.client_identity(binding, REVISION)
        binding = two_catalogs()[0][0]
        del binding.context["plan"]["revision"]
        with self.assertRaisesRegex(
            catalog.SharedCatalogError, "catalog_identity_invalid$"
        ):
            catalog.client_identity(binding, REVISION)

    def test_invalid_verified_consumer_revision_is_rejected(self):
        binding = two_catalogs()[0][0]
        original = copy.deepcopy((binding.context, binding.authority))
        for consumer in (None, True, 1, "", "a" * 39, "a" * 41, "g" * 40):
            with (
                self.subTest(consumer=consumer),
                self.assertRaisesRegex(
                    catalog.SharedCatalogError, "catalog_identity_invalid$"
                ),
            ):
                catalog.client_identity(binding, consumer)
        self.assertEqual((binding.context, binding.authority), original)

    def test_missing_plan_or_authority_is_not_rebuilt_from_a_lease(self):
        for field, value in (
            ("context", None),
            ("context", {}),
            ("context", {"plan": None}),
            ("authority", None),
            ("authority", {}),
            ("authority", {"catalog_identity": None}),
            ("authority", {"catalog_identity": []}),
        ):
            with self.subTest(field=field, value=value):
                binding = two_catalogs()[0][0]
                setattr(binding, field, value)
                binding.lease = shared_evidence()[0]["lease"]
                original = copy.deepcopy(binding.__dict__)
                with self.assertRaisesRegex(
                    catalog.SharedCatalogError, "catalog_identity_invalid$"
                ):
                    catalog.client_identity(binding, REVISION)
                self.assertEqual(binding.__dict__, original)

    def test_identity_requires_the_closed_native_fields(self):
        raw = two_catalogs()[0][0].authority["catalog_identity"]
        variants = [raw | {"extra": "untrusted"}] + [
            {key: value for key, value in raw.items() if key != missing}
            for missing in client_lease.CATALOG_FIELDS
        ]
        for identity in variants:
            with self.subTest(identity=identity):
                binding = two_catalogs()[0][0]
                binding.authority["catalog_identity"] = identity
                original = copy.deepcopy(binding.authority)
                with self.assertRaisesRegex(
                    catalog.SharedCatalogError, "catalog_authority_changed$"
                ):
                    catalog.client_identity(binding, REVISION)
                self.assertEqual(binding.authority, original)

    def test_existing_optional_revision_cannot_forge_the_producer(self):
        for claimed in (None, True, 1, "", "b" * 40, "a" * 39, "A" * 40):
            with self.subTest(claimed=claimed):
                binding = two_catalogs()[0][0]
                binding.authority["catalog_identity"]["catalog_revision"] = (
                    claimed
                )
                original = copy.deepcopy(binding.authority)
                with self.assertRaisesRegex(
                    catalog.SharedCatalogError, "catalog_authority_changed$"
                ):
                    catalog.client_identity(binding, REVISION)
                self.assertEqual(binding.authority, original)

    def test_identity_cannot_authorize_a_different_project(self):
        for project in (None, 1, "another-project"):
            with self.subTest(project=project):
                binding = two_catalogs()[0][0]
                binding.context["plan"]["project"] = project
                with self.assertRaisesRegex(
                    catalog.SharedCatalogError, "catalog_authority_changed$"
                ):
                    catalog.client_identity(binding, REVISION)


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

    def test_each_catalog_authenticates_its_own_real_core_and_complete_client(
        self,
    ):
        bindings, groups, networks, clients = two_catalogs()
        expected = copy.deepcopy((groups, networks, clients))
        for binding in bindings:
            snapshot = catalog.observe(binding, groups, networks, clients)
            self.assertEqual(snapshot[0], binding.authority["runtime"])
        self.assertEqual((groups, networks, clients), expected)
        self.assertEqual(self.mounts.call_count, 2)

    def test_other_catalog_lease_or_runtime_cannot_authorize_the_selected_core(
        self,
    ):
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
                    networks[
                        target.context["plan"]["project"] + "_search_backend"
                    ]["Containers"]["f" * 64] = {}
                else:
                    clients.append(copy.deepcopy(clients[0]))
                with self.assertRaises((ValueError, RuntimeError)):
                    catalog.observe(target, groups, networks, clients)

    def test_precreation_selected_catalog_keeps_exact_core_contract(
        self,
    ):
        bindings, groups, networks, clients = two_catalogs()
        target = bindings[1]
        network = networks[
            target.context["plan"]["project"] + "_search_backend"
        ]
        del network["Containers"][clients[1]["lease"]["api_container_id"]]
        snapshot = catalog.observe(target, groups, networks, clients[:1])
        self.assertEqual(snapshot[0], target.authority["runtime"])
        network["Containers"]["f" * 64] = {}
        with self.assertRaises(ValueError):
            catalog.observe(target, groups, networks, clients[:1])

    def test_network_type_is_checked_before_and_after_selected_cpu_creation(
        self,
    ):
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

    def test_wrong_image_startup_or_storage_never_becomes_query_authority(
        self,
    ):
        bindings, groups, networks, clients = two_catalogs()
        bindings[1].inspections["fess-search"]["Config"]["Cmd"] = [
            "unreviewed"
        ]
        with self.assertRaises(client_lease.LeaseError):
            catalog.observe(bindings[1], groups, networks, clients)
        self.mounts.side_effect = ValueError("catalog_storage_changed")
        with self.assertRaisesRegex(ValueError, "catalog_storage_changed"):
            catalog.observe(bindings[0], groups, networks, clients)


class CatalogAuditTests(unittest.TestCase):
    def test_refresh_rejects_drift_in_config_source_plan_and_api_key_authority(
        self,
    ):
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
                        catalog,
                        "require_clean_revision",
                        return_value=REVISION,
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
                        binding.store.read.return_value = {
                            "other_catalog": True
                        }
                    else:
                        key.return_value = {"credential_sha256": "2" * 64}
                    with self.assertRaises(ValueError):
                        binding.refresh()

    def test_chunk_profile_rejects_missing_duplicate_or_overridden_fields(
        self,
    ):
        context = {
            "compose": {
                "services": {
                    "fess-search": {
                        "environment": {
                            "FESS_JAVA_OPTS": " ".join(
                                catalog.chunks.JAVA_OPTIONS
                            )
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

    def test_query_profile_reads_fixed_literals_without_executing_module_text(
        self,
    ):
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
                "MODEL = __import__('pathlib').Path("
                f"{str(marker)!r}).touch()\n"
                "MODEL_DIGEST = 'unsealed'\nDIMENSIONS = 768\n"
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
            self.assertEqual(
                json.loads(Path(argv[9]).read_bytes()), list(snapshot)
            )
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(value).encode()
            )

        with (
            patch.object(
                catalog, "require_clean_revision", return_value=REVISION
            ),
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

    def test_source_drift_rejects_before_execution_and_child_failure_closes(
        self,
    ):
        with (
            patch.object(
                catalog, "require_clean_revision", return_value="b" * 40
            ),
            patch.object(catalog.image_build, "_run") as run,
        ):
            with self.assertRaisesRegex(
                catalog.SharedCatalogError, "source_changed"
            ):
                catalog.source_record(
                    Path("/selected-source"), Path("/config"), REVISION
                )
            run.assert_not_called()
        with (
            patch.object(
                catalog, "require_clean_revision", return_value=REVISION
            ),
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
                    patch.object(
                        catalog, "source_record", return_value=changed
                    ),
                ):
                    with self.assertRaisesRegex(
                        ValueError, "catalog_authority_changed"
                    ):
                        binding.audit(({}, [], {}), timeout=50)


class CatalogSelectionTests(unittest.TestCase):
    def test_existing_selection_reuses_exact_authority_without_new_state(self):
        from tests.unit.deployment.test_composition import product

        config = product()
        existing = SimpleNamespace(
            config=config,
            context={"repo": Path("/native-source")},
            authority={"native": True},
        )
        profile = {
            "catalog_config": Path("/native.json"),
            "catalog_source": Path("/native-source"),
        }
        with (
            ExitStack() as stack,
            patch.object(
                catalog.product_config, "load_config", return_value=config
            ),
            patch.object(catalog, "locked_state") as lock,
        ):
            selected = catalog.select(
                profile, stack, config, [existing], object()
            )
            self.assertIs(existing, selected)
            self.assertIs(selected.authority, existing.authority)
            lock.assert_not_called()

    def test_nested_provider_state_is_rejected_before_any_new_lock(self):
        from tests.unit.deployment.test_composition import product

        config = product()
        profile = {
            "catalog_config": Path("/nested.json"),
            "catalog_source": Path("/source"),
        }
        for target in (config.state_root / "nested", config.state_root.parent):
            with (
                self.subTest(target=target),
                ExitStack() as stack,
                patch.object(
                    catalog.product_config,
                    "load_config",
                    return_value=replace(config, state_root=target),
                ),
                patch.object(catalog, "locked_state") as lock,
            ):
                with self.assertRaisesRegex(
                    catalog.SharedCatalogError, "state_overlaps_provider"
                ):
                    catalog.select(profile, stack, config, [], object())
                lock.assert_not_called()

    def test_matching_state_with_other_source_cannot_reuse_authority(self):
        from tests.unit.deployment.test_composition import product

        config = product()
        existing = SimpleNamespace(
            config=config, context={"repo": Path("/native-source")}
        )
        with (
            ExitStack() as stack,
            patch.object(
                catalog.product_config, "load_config", return_value=config
            ),
            patch.object(catalog, "locked_state") as lock,
        ):
            with self.assertRaisesRegex(
                catalog.SharedCatalogError, "selection_invalid"
            ):
                catalog.select(
                    {
                        "catalog_config": Path("/config.json"),
                        "catalog_source": Path("/other-source"),
                    },
                    stack,
                    config,
                    [existing],
                    object(),
                )
            lock.assert_not_called()

    def test_product_and_catalog_locks_remain_until_session_exit(
        self,
    ):
        from tests.unit.catalog.test_client_lease import evidence
        from search.operations.config import load_config

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            fixture = evidence()
            # Obtain the canonical product shape without needing any real
            # models.
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
            baseline = SimpleNamespace(
                config=config,
                context={
                    "plan": {"project": "native"},
                    "repo": root / "native-source",
                },
            )
            docker = SimpleNamespace(inspect=lambda _: {})
            profile = {
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
                                "FESS_JAVA_OPTS": " ".join(
                                    catalog.chunks.JAVA_OPTIONS
                                )
                            }
                        }
                    }
                },
            }
            value = {"images": {}, "authority": fixture["catalog_identity"]}
            with (
                patch.object(
                    catalog, "load_context", return_value=(context, value)
                ),
                patch.object(catalog.Catalog, "refresh"),
                patch.object(
                    catalog.credentials,
                    "read_api_key_authority",
                    return_value={"credential_sha256": "1" * 64},
                ),
            ):
                with ExitStack() as stack:
                    chosen = catalog.select(
                        profile, stack, config, [baseline], docker
                    )
                    self.assertIsNot(baseline, chosen)
                    with self.assertRaisesRegex(
                        ValueError, "product_operation_busy"
                    ):
                        with locked_state(selected):
                            self.fail("selected product lock was released")
                    with self.assertRaises(BlockingIOError):
                        with execution.operation_lock({"data": data}):
                            self.fail("selected catalog lock was released")
                with (
                    locked_state(selected),
                    execution.operation_lock({"data": data}),
                ):
                    pass


class CatalogCollectionTests(unittest.TestCase):
    @staticmethod
    def fixture(count=5):
        projects = [
            (f"project-{n}", {f"project-{n}_network"}) for n in range(count)
        ]
        rows = [
            {
                "Id": f"{index * 3 + member + 1:064x}",
                "Config": {"Labels": {"com.docker.compose.project": project}},
            }
            for index, (project, _) in enumerate(projects)
            for member in range(3)
        ]
        networks = [
            {
                "Name": f"project-{n}_network",
                "Containers": {"unknown-peer": {}},
            }
            for n in range(count)
        ]
        return projects, rows, networks

    @staticmethod
    def inventory(rows):
        return [
            {
                "Id": row["Id"],
                "Project": row["Config"]["Labels"][
                    "com.docker.compose.project"
                ],
            }
            for row in rows
        ]

    def client(self, rows, networks, *, inventory=None, names=None):
        inventory = self.inventory(rows) if inventory is None else inventory
        names = [row["Name"] for row in networks] if names is None else names

        def run(arguments, **_):
            args = arguments[5:]
            if args[0] == "ps":
                values = inventory
            elif args[:2] == ["network", "ls"]:
                values = [{"Name": name} for name in names]
            else:
                values = None
            if values is not None:
                raw = b"\n".join(json.dumps(v).encode() for v in values)
            else:
                self.assertIn(args[:1], (["inspect"], ["network"]))
                raw = json.dumps(
                    rows if args[0] == "inspect" else networks
                ).encode()
            return subprocess.CompletedProcess(arguments, 0, raw, b"")

        runner = Mock(side_effect=run)
        return (
            catalog.runtime_docker.Docker(
                "/private-client", runner, max_commands=128
            ),
            runner,
        )

    def test_five_projects_use_four_commands_and_keep_unknown_peers(self):
        projects, rows, networks = self.fixture()
        inventory = self.inventory(rows) + [
            {"Id": "f" * 64, "Project": "foreign"}
        ]
        docker, runner = self.client(
            rows,
            networks,
            inventory=inventory,
            names=[row["Name"] for row in networks] + ["foreign_network"],
        )
        expected = copy.deepcopy((rows, networks))
        groups, actual = catalog.collect(docker, projects)
        self.assertEqual(docker.calls, 4)
        self.assertEqual(runner.call_count, 4)
        commands = [call.args[0][5:] for call in runner.call_args_list]
        self.assertEqual(
            commands[2], ["inspect", *sorted(row["Id"] for row in rows)]
        )
        self.assertEqual(
            commands[3],
            ["network", "inspect", *sorted(row["Name"] for row in networks)],
        )
        self.assertEqual(
            groups,
            {
                project: rows[index * 3 : index * 3 + 3]
                for index, (project, _) in enumerate(projects)
            },
        )
        self.assertEqual(actual, {row["Name"]: row for row in networks})
        self.assertEqual((rows, networks), expected)
        self.assertNotIn("f" * 64, commands[2])
        self.assertTrue(
            all(
                "unknown-peer" in network["Containers"]
                for network in actual.values()
            )
        )

    def test_missing_project_member_or_network_is_rejected_before_bulk_inspect(
        self,
    ):
        for identifiers, networks in (
            (["one", "two"], {"owned"}),
            (["one", "one", "two"], {"owned"}),
            (["one", "two", "three"], {"owned", "foreign"}),
        ):
            with (
                self.subTest(networks=networks),
                patch.object(
                    catalog.runtime_docker,
                    "project_inventories",
                    return_value={"project": (identifiers, networks)},
                ),
            ):
                docker = SimpleNamespace(call=unittest.mock.Mock())
                with self.assertRaisesRegex(
                    catalog.SharedCatalogError, "inventory_changed"
                ):
                    catalog.collect(docker, [("project", {"owned"})])
                docker.call.assert_not_called()

    def test_extra_substring_network_is_not_hidden_before_bulk_inspection(
        self,
    ):
        for extra in ("project-0_rogue", "xproject-0_rogue"):
            with self.subTest(extra=extra):
                projects, rows, networks = self.fixture(1)
                docker, runner = self.client(
                    rows, networks, names=[networks[0]["Name"], extra]
                )
                with self.assertRaisesRegex(
                    catalog.SharedCatalogError, "inventory_changed"
                ):
                    catalog.collect(docker, projects)
                self.assertEqual(runner.call_count, 2)

    def test_missing_and_rogue_members_are_not_hidden_from_inventory(self):
        projects, rows, networks = self.fixture(1)
        for members, error in (
            (rows[:2], catalog.SharedCatalogError),
            (
                rows + [rows[0] | {"Id": "d" * 64}],
                catalog.runtime_docker.InferenceError,
            ),
        ):
            with self.subTest(count=len(members)):
                docker, runner = self.client(members, networks)
                with self.assertRaises(error):
                    catalog.collect(docker, projects)
                self.assertTrue(
                    all(
                        call.args[0][5] != "inspect"
                        for call in runner.call_args_list
                    )
                )

    def test_inspect_drift_cannot_form_a_snapshot(self):
        projects, rows, networks = self.fixture(1)
        inventory = self.inventory(rows)
        for observed_rows, observed_networks in (
            (rows[:2], networks),
            (rows + [rows[0] | {"Id": "f" * 64}], networks),
            ([rows[0], rows[0], rows[2]], networks),
            ([*rows[:2], rows[2] | {"Id": "f" * 64}], networks),
            (rows, []),
            (rows, [*networks, {"Name": "foreign"}]),
            (rows, [{"Name": "renamed"}]),
        ):
            with self.subTest(rows=observed_rows, networks=observed_networks):
                docker, _ = self.client(
                    observed_rows,
                    observed_networks,
                    inventory=inventory,
                    names=[networks[0]["Name"]],
                )
                with self.assertRaisesRegex(
                    catalog.SharedCatalogError, "snapshot_invalid"
                ):
                    catalog.collect(docker, projects)

    def test_collected_unknown_peer_and_project_drift_keep_native_rejection(
        self,
    ):
        for kind in ("unknown_peer", "project_label"):
            with self.subTest(kind=kind):
                bindings, groups, network_map, clients = two_catalogs()
                rows = [row for group in groups.values() for row in group]
                networks = list(network_map.values())
                inventory = self.inventory(rows)
                if kind == "unknown_peer":
                    networks[0]["Containers"]["f" * 64] = {}
                else:
                    rows[0]["Config"]["Labels"][
                        "com.docker.compose.project"
                    ] = "foreign"
                docker, _ = self.client(rows, networks, inventory=inventory)
                projects = [
                    (project, {project + "_search_backend"})
                    for project in groups
                ]
                with (
                    patch.object(execution, "require_storage"),
                    patch.object(execution, "validate_storage_mounts"),
                    self.assertRaises((ValueError, RuntimeError)),
                ):
                    collected, observed = catalog.collect(docker, projects)
                    for binding in bindings:
                        catalog.observe(binding, collected, observed, clients)
