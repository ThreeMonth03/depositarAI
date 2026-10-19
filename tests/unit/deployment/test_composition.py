"""Pure private product plans, without filesystem, Docker or GPU mutations."""

from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest import mock

from search.operations.config import GPUConfig, ProductConfig
from search.operations.deployment.composition import (
    LOOPBACK_PORT,
    OLLAMA_IMAGE,
    QueryBindingPaths,
    render_composition,
)


REVISION = "a" * 40
INFERENCE_SOURCE = "6" * 64
BUNDLE_ID = "b" * 64
UUIDS = tuple(f"GPU-0000000{index}-0000-4000-8000-000000000001" for index in range(4))


def product():
    return ProductConfig(
        1,
        Path("/srv/private/depositar-ai"),
        Path("/srv/models/api"),
        Path("/srv/models/embedding"),
        Path("/srv/models/reranker"),
        GPUConfig(UUIDS[0], (UUIDS[1], UUIDS[2]), UUIDS[3]),
    )


def catalog(config, *, bundle_id=BUNDLE_ID, revision=REVISION):
    name = bundle_id[:12] + "-" + revision[:12]
    work = config.state_root / "catalogs" / name
    return {
        "schema_version": 1,
        "project": "depositar-ai-catalog-" + name,
        "work_root": str(work),
        "data_root": str(work / "data"),
        "tooling_root": str(work / "tooling"),
        "compose_file": str(work / "compose.json"),
        "bundle_path": str(
            config.state_root / "cache/bundles" / ("sha256-" + bundle_id)
        ),
        "manifest_sha256": "c" * 64,
        "revision": revision,
        "document_count": 4001,
        "status": "prepared",
        "source_sha256": {"search/catalog/query.py": "d" * 64},
        "compose_sha256": "e" * 64,
        "security_material_sha256": "f" * 64,
    }


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.config = product()
        self.catalog = catalog(self.config)
        self.query = QueryBindingPaths.for_catalog(self.config, self.catalog)

    def render(
        self, *, config=None, plan=None, paths=None, revision=REVISION, **options
    ):
        return render_composition(
            self.config if config is None else config,
            self.catalog if plan is None else plan,
            self.query if paths is None else paths,
            revision,
            uid=options.pop("uid", 1000),
            gid=options.pop("gid", 1000),
            inference_source_sha256=options.pop(
                "inference_source_sha256", INFERENCE_SOURCE
            ),
            **options,
        )

    def test_schema_two_local_catalog_storage_preserves_private_plane_plans(self):
        local = replace(self.config, schema_version=2, catalog_storage="docker-volume")
        with (
            mock.patch("builtins.open", side_effect=AssertionError("no file reads")),
            mock.patch("subprocess.run", side_effect=AssertionError("no processes")),
        ):
            self.assertEqual(self.render(), self.render(config=local))
        self.assertNotIn("catalog_storage", self.config.to_dict())

    def test_product_schema_and_catalog_storage_pairing_is_closed(self):
        for schema in (True, False, 1.0, 2.0, "1", "2", None, 0, -1, 3):
            with (
                self.subTest(schema=schema),
                self.assertRaisesRegex(ValueError, "composition_config_invalid"),
            ):
                self.render(config=replace(self.config, schema_version=schema))
        for schema, storage in (
            (1, "docker-volume"),
            (1, "unknown"),
            (1, False),
            (2, "bind"),
            (2, "unknown"),
            (2, True),
            (2, 1),
            (2, None),
        ):
            with (
                self.subTest(schema=schema, storage=storage),
                self.assertRaisesRegex(ValueError, "composition_config_invalid"),
            ):
                self.render(
                    config=replace(
                        self.config, schema_version=schema, catalog_storage=storage
                    )
                )

    def test_local_volume_profile_keeps_root_gpu_and_catalog_guards(self):
        local = replace(self.config, schema_version=2, catalog_storage="docker-volume")
        for changed in (
            replace(local, api_model_root=Path("/srv/../models")),
            replace(local, api_model_root=local.state_root / "models"),
            replace(local, gpus=GPUConfig(UUIDS[0], (UUIDS[0], UUIDS[2]), UUIDS[3])),
        ):
            with self.subTest(config=changed), self.assertRaises(ValueError):
                self.render(config=changed)
        with self.assertRaises(ValueError):
            self.render(config=local, plan={**self.catalog, "schema_version": 2})

    def test_returns_separate_catalog_inference_release_and_singleton_gateway(self):
        result = self.render()
        self.assertEqual(
            {"catalog_reference", "inference", "release", "gateway"}, set(result)
        )
        self.assertEqual(
            {"scope-model", "answer-model", "reranker"},
            set(result["inference"]["services"]),
        )
        self.assertEqual(
            {"web", "conversation-api", "filtered-embedding"},
            set(result["release"]["services"]),
        )
        self.assertEqual({"web-nginx"}, set(result["gateway"]["services"]))
        self.assertEqual("depositar-ai-gateway", result["gateway"]["name"])
        reference = result["catalog_reference"]
        self.assertEqual(self.catalog["project"], reference["project"])
        self.assertEqual(BUNDLE_ID, reference["bundle_id"])
        self.assertEqual("c" * 64, reference["manifest_sha256"])
        self.assertEqual(4001, reference["document_count"])
        self.assertEqual("sealed_reference_only", reference["status"])
        self.assertFalse(reference["runtime_verified"])
        self.assertTrue(reference["client_lease_required"])

    def test_old_catalog_paths_and_new_application_images_remain_separate(self):
        origin = "1" * 40
        old_catalog = catalog(self.config, revision=origin)
        before = copy.deepcopy(old_catalog)
        with (
            mock.patch("builtins.open", side_effect=AssertionError("no IO")),
            mock.patch("subprocess.run", side_effect=AssertionError("no processes")),
        ):
            result = self.render(
                plan=old_catalog,
                paths=QueryBindingPaths.for_catalog(self.config, old_catalog),
            )
        self.assertEqual(before, old_catalog)
        self.assertEqual(origin, result["catalog_reference"]["revision"])
        release = result["release"]
        self.assertEqual(REVISION, release["x-depositar-plan"]["revision"])
        self.assertEqual(origin, release["x-depositar-plan"]["catalog_revision"])
        self.assertTrue(release["name"].endswith(REVISION[:12]))
        self.assertEqual(
            old_catalog["project"] + "_search_backend",
            release["networks"]["catalog"]["name"],
        )
        self.assertEqual(
            "depositar-search-conversation:" + REVISION,
            release["services"]["conversation-api"]["image"],
        )
        mounts = release["services"]["conversation-api"]["volumes"]
        self.assertIn(
            str(Path(old_catalog["work_root"]) / "query-access/binding.json"),
            [mount["source"] for mount in mounts],
        )
        self.assertNotIn(
            "catalog_revision", self.render()["release"]["x-depositar-plan"]
        )

    def test_render_is_pure_and_deterministic_and_does_not_mutate_inputs(self):
        before = copy.deepcopy(self.catalog)
        with (
            mock.patch("builtins.open", side_effect=AssertionError("no file reads")),
            mock.patch(
                "pathlib.Path.open", side_effect=AssertionError("no file reads")
            ),
            mock.patch(
                "pathlib.Path.stat", side_effect=AssertionError("no file stats")
            ),
            mock.patch("subprocess.run", side_effect=AssertionError("no processes")),
        ):
            first, second = self.render(), self.render()
        self.assertEqual(first, second)
        self.assertEqual(before, self.catalog)
        json.dumps(first, allow_nan=False)
        first["release"]["services"]["web"]["networks"]["website"]["aliases"].append(
            "untrusted"
        )
        self.assertNotEqual(first, self.render())

    def test_all_cpu_services_have_bounded_unprivileged_runc_and_no_gpu(self):
        result = self.render()
        for plane in ("release", "gateway"):
            for service in result[plane]["services"].values():
                self.assertEqual("runc", service["runtime"])
                self.assertEqual("1000:1000", service["user"])
                self.assertIs(service["read_only"], True)
                self.assertEqual(["ALL"], service["cap_drop"])
                self.assertEqual(["no-new-privileges:true"], service["security_opt"])
                self.assertEqual(service["mem_limit"], service["memswap_limit"])
                self.assertGreater(service["mem_limit"], 0)
                self.assertGreater(service["cpus"], 0)
                self.assertGreater(service["pids_limit"], 0)
                for key in ("deploy", "gpus", "devices", "network_mode", "privileged"):
                    self.assertNotIn(key, service)

    def test_only_singleton_gateway_publishes_fixed_loopback_port(self):
        result = self.render()
        published = []
        for plane in ("inference", "release", "gateway"):
            for name, service in result[plane]["services"].items():
                if service.get("ports"):
                    published.append((plane, name, service["ports"]))
        self.assertEqual(
            [
                (
                    "gateway",
                    "web-nginx",
                    [
                        {
                            "target": 8080,
                            "published": "18083",
                            "host_ip": "127.0.0.1",
                            "protocol": "tcp",
                        }
                    ],
                )
            ],
            published,
        )
        self.assertEqual(18083, LOOPBACK_PORT)

    def test_exact_uuid_allocation_is_disjoint_one_two_one(self):
        services = self.render()["inference"]["services"]
        expected = {
            "scope-model": [UUIDS[0]],
            "answer-model": list(UUIDS[1:3]),
            "reranker": [UUIDS[3]],
        }
        for name, identifiers in expected.items():
            self.assertEqual("nvidia", services[name]["runtime"])
            self.assertEqual(
                ",".join(identifiers),
                services[name]["environment"]["NVIDIA_VISIBLE_DEVICES"],
            )
            self.assertEqual(
                "compute,utility",
                services[name]["environment"]["NVIDIA_DRIVER_CAPABILITIES"],
            )
            self.assertEqual(
                [
                    {
                        "driver": "nvidia",
                        "device_ids": identifiers,
                        "capabilities": ["gpu"],
                    }
                ],
                services[name]["deploy"]["resources"]["reservations"]["devices"],
            )
        self.assertEqual(
            4,
            len({identifier for values in expected.values() for identifier in values}),
        )

    def test_same_commit_metadata_update_reuses_inference_but_has_new_cpu_release(self):
        other = catalog(self.config, bundle_id="1" * 64)
        other["manifest_sha256"] = "2" * 64
        new = self.render(
            plan=other, paths=QueryBindingPaths.for_catalog(self.config, other)
        )
        old = self.render()
        self.assertEqual(old["inference"], new["inference"])
        self.assertNotEqual(old["release"]["name"], new["release"]["name"])
        self.assertEqual(old["gateway"]["name"], new["gateway"]["name"])
        self.assertNotEqual(
            old["gateway"]["x-depositar-plan"]["approved_upstream"],
            new["gateway"]["x-depositar-plan"]["approved_upstream"],
        )
        for service in new["release"]["services"].values():
            self.assertNotIn("ports", service)
            self.assertNotIn("deploy", service)

    def test_operator_model_path_gpu_or_source_change_requires_new_inference_identity(
        self,
    ):
        base = self.render()["inference"]["name"]
        options = (
            replace(self.config, api_model_root=Path("/srv/other-api-models")),
            replace(
                self.config, gpus=GPUConfig(UUIDS[3], (UUIDS[1], UUIDS[2]), UUIDS[0])
            ),
        )
        for config in options:
            self.assertNotEqual(base, self.render(config=config)["inference"]["name"])
        self.assertNotEqual(base, self.render(uid=2000)["inference"]["name"])
        other = catalog(self.config, revision="1" * 40)
        result = self.render(
            plan=other,
            paths=QueryBindingPaths.for_catalog(self.config, other),
            revision="1" * 40,
        )
        self.assertEqual(base, result["inference"]["name"])
        self.assertNotEqual(
            base, self.render(inference_source_sha256="7" * 64)["inference"]["name"]
        )

    def test_ui_only_revision_preserves_immutable_gpu_recipe_and_networks(self):
        before = self.render()["inference"]
        after = self.render(revision="1" * 40)["inference"]
        self.assertNotEqual(
            before["services"]["reranker"]["image"],
            after["services"]["reranker"]["image"],
        )
        for compose in (before, after):
            compose["services"]["reranker"]["image"] = "sha256:" + "2" * 64
        self.assertEqual(before, after)
        self.assertNotIn("revision", before["x-depositar-plan"])

    def test_model_pin_or_runtime_recipe_change_requires_new_gpu_identity(self):
        base = self.render()["inference"]["name"]
        with mock.patch("search.model_policy.SCOPE_MODEL_DIGEST", "9" * 64):
            self.assertNotEqual(base, self.render()["inference"]["name"])
        with mock.patch(
            "search.operations.deployment.composition.OLLAMA_IMAGE",
            "changed@sha256:" + "9" * 64,
        ):
            self.assertNotEqual(base, self.render()["inference"]["name"])
        from search.operations.deployment import composition

        runtime = composition._runtime

        def changed_runtime(*args, **kwargs):
            result = runtime(*args, **kwargs)
            if kwargs.get("gpu"):
                result["pids_limit"] += 1
            return result

        with mock.patch.object(composition, "_runtime", side_effect=changed_runtime):
            self.assertNotEqual(base, self.render()["inference"]["name"])

    def test_inference_source_fingerprint_is_required_and_bounded(self):
        for value in (None, True, 1, "", "a" * 40, "A" * 64, "a" * 65):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "inference_source_invalid"),
            ):
                self.render(inference_source_sha256=value)

    def test_registry_images_are_pinned_and_built_images_are_source_tagged_only(self):
        result = self.render()
        ollama_services = (
            result["inference"]["services"]["scope-model"],
            result["inference"]["services"]["answer-model"],
            result["release"]["services"]["filtered-embedding"],
        )
        for service in ollama_services:
            self.assertEqual(OLLAMA_IMAGE, service["image"])
            self.assertIn("@sha256:", service["image"])
        for plane, name in (
            ("inference", "reranker"),
            ("release", "web"),
            ("release", "conversation-api"),
            ("gateway", "web-nginx"),
        ):
            self.assertTrue(
                result[plane]["services"][name]["image"].endswith(":" + REVISION)
            )
            self.assertTrue(
                result[plane]["x-depositar-plan"]["immutable_built_images_required"]
            )
        self.assertNotIn("depositar-search-canary", json.dumps(result))
        self.assertNotIn("depositar-search-api_inference", json.dumps(result))

    def test_model_identity_comes_from_pinned_production_authorities(self):
        from search.catalog.embedding import MODEL, MODEL_DIGEST
        from search.model_policy import (
            PLANNER_MODEL,
            PLANNER_MODEL_DIGEST,
            SCOPE_MODEL,
            SCOPE_MODEL_DIGEST,
        )
        from search.reranker.contract import MODEL_ID, MODEL_REVISION
        from search.reranker.model import MODEL_HASHES

        result = self.render()
        pins = result["inference"]["x-depositar-plan"]["models"]
        self.assertEqual(
            {"model": SCOPE_MODEL, "manifest_sha256": SCOPE_MODEL_DIGEST}, pins["scope"]
        )
        self.assertEqual(
            {"model": PLANNER_MODEL, "manifest_sha256": PLANNER_MODEL_DIGEST},
            pins["planner"],
        )
        self.assertEqual(
            {"model": MODEL_ID, "revision": MODEL_REVISION, "files": MODEL_HASHES},
            pins["reranker"],
        )
        self.assertIsNot(MODEL_HASHES, pins["reranker"]["files"])
        self.assertEqual(
            {"model": MODEL, "manifest_sha256": MODEL_DIGEST, "dimensions": 768},
            result["release"]["x-depositar-plan"]["embedding"],
        )

    def test_query_embedding_is_cpu_and_separate_from_build_namespace(self):
        service = self.render()["release"]["services"]["filtered-embedding"]
        self.assertEqual(["website"], service["networks"])
        self.assertEqual("-1", service["environment"]["CUDA_VISIBLE_DEVICES"])
        self.assertEqual("void", service["environment"]["NVIDIA_VISIBLE_DEVICES"])
        self.assertEqual("1", service["environment"]["OLLAMA_NO_CLOUD"])
        self.assertEqual(
            str(self.config.embedding_model_root), service["volumes"][0]["source"]
        )
        self.assertNotIn("network_mode", service)

    def test_existing_dns_contract_is_preserved_and_internal_bridges_are_explicit(self):
        result = self.render()
        inference, release = result["inference"], result["release"]
        self.assertEqual(
            ["inference"], inference["services"]["scope-model"]["networks"]
        )
        self.assertEqual(
            ["inference"], inference["services"]["answer-model"]["networks"]
        )
        self.assertEqual(["ranking"], inference["services"]["reranker"]["networks"])
        for network in inference["networks"].values():
            self.assertIs(network["internal"], True)
            self.assertIs(network["attachable"], False)
        self.assertIs(release["networks"]["website"]["internal"], True)
        self.assertEqual(
            self.catalog["project"] + "_search_backend",
            release["networks"]["catalog"]["name"],
        )
        self.assertIs(release["networks"]["catalog"]["external"], True)
        self.assertEqual(
            inference["networks"]["inference"]["name"],
            release["networks"]["existing-inference"]["name"],
        )
        self.assertEqual(
            inference["networks"]["ranking"]["name"],
            release["networks"]["ranking"]["name"],
        )
        self.assertEqual(
            release["name"] + "_website", result["gateway"]["networks"]["route"]["name"]
        )

    def test_api_gets_only_canonical_query_secrets_binding_ca_and_bundle(self):
        service = self.render()["release"]["services"]["conversation-api"]
        mounts = {mount["target"]: mount for mount in service["volumes"]}
        target = "/catalog/sha256-" + BUNDLE_ID
        self.assertEqual(
            {
                target,
                "/run/secrets/api_key",
                "/run/secrets/index_query_password",
                "/run/index/binding.json",
                "/run/index/root-ca.pem",
            },
            set(mounts),
        )
        self.assertEqual(target, service["environment"]["SEARCH_API_BUNDLE_PATH"])
        self.assertEqual(self.catalog["bundle_path"], mounts[target]["source"])
        self.assertNotIn("SEARCH_API_INDEX_PREFILTER", service["environment"])
        self.assertNotIn("admin", json.dumps(mounts).lower())
        self.assertEqual(
            str(Path(self.catalog["work_root"]) / "query-access/index-query-password"),
            mounts["/run/secrets/index_query_password"]["source"],
        )

    def test_all_mounts_read_only_existing_inputs_without_docker_socket(self):
        for plane in ("inference", "release", "gateway"):
            for service in self.render()[plane]["services"].values():
                for mount in service["volumes"]:
                    self.assertEqual("bind", mount["type"])
                    self.assertIs(mount["read_only"], True)
                    self.assertIs(mount["bind"]["create_host_path"], False)
                    self.assertNotIn("docker.sock", mount["source"])

    def test_gateway_route_is_operator_owned_directory_and_unique_fixed_alias(self):
        result = self.render()
        gateway = result["gateway"]
        service = gateway["services"]["web-nginx"]
        self.assertEqual(
            [
                {
                    "type": "bind",
                    "source": str(self.config.state_root / "gateway/routes"),
                    "target": "/etc/nginx/routes",
                    "read_only": True,
                    "bind": {"create_host_path": False},
                }
            ],
            service["volumes"],
        )
        self.assertEqual(
            ["-c", "/etc/nginx/routes/active.conf", "-g", "daemon off;"],
            service["command"],
        )
        plan = gateway["x-depositar-plan"]
        alias = result["release"]["services"]["web"]["networks"]["website"]["aliases"][
            0
        ]
        self.assertEqual(alias + ":8000", plan["approved_upstream"])
        self.assertIn("server " + alias + ":8000 resolve;", plan["route_text"])
        self.assertIn("include /etc/nginx/http.conf;", plan["route_text"])
        self.assertIn("include /etc/nginx/website.conf;", plan["route_text"])
        self.assertIs(plan["route_publication_required"], True)

    def test_renderer_never_claims_provisioning_ownership_lease_or_health_success(self):
        result = self.render()
        for plane in ("inference", "release", "gateway"):
            plan = result[plane]["x-depositar-plan"]
            self.assertEqual("plan_only", plan["status"])
            self.assertIs(plan["runtime_verified"], False)
        self.assertIs(
            result["inference"]["x-depositar-plan"]["ownership_verified"], False
        )
        self.assertIs(
            result["release"]["x-depositar-plan"]["catalog_client_lease_required"], True
        )
        self.assertIs(
            result["release"]["x-depositar-plan"]["query_provisioning_required"], True
        )
        self.assertIs(result["release"]["x-depositar-plan"]["generated_answers"], False)

    def test_strict_nonroot_operator_ids_reject_boolean_and_permissive_values(self):
        for name in ("uid", "gid"):
            for value in (True, False, 0, -1, "1000", 1.0, 2**31):
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaisesRegex(ValueError, "operator"),
                ):
                    self.render(**{name: value})

    def test_catalog_schema_source_bounds_and_identity_cannot_be_relabelled(self):
        mutations = {
            "schema_version": True,
            "status": "ready",
            "revision": "1" * 40,
            "document_count": True,
            "project": "depositar-search-canary",
            "manifest_sha256": "not-a-digest",
            "source_sha256": {"../secret": "d" * 64},
            "work_root": "/srv/other-catalog",
            "data_root": self.catalog["work_root"] + "/other-data",
            "bundle_path": self.catalog["bundle_path"].replace("sha256-", "alias-"),
        }
        for name, value in mutations.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.render(plan={**self.catalog, name: value})
        for count in (0, -1, 10001):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.render(plan={**self.catalog, "document_count": count})
        with self.assertRaises(ValueError):
            self.render(plan={**self.catalog, "url": "https://example.com"})
        with self.assertRaises(ValueError):
            self.render(
                plan={
                    key: value
                    for key, value in self.catalog.items()
                    if key != "source_sha256"
                }
            )
        for value in ("development", "a" * 39, "A" * 40, True):
            with self.subTest(revision=value), self.assertRaises(ValueError):
                self.render(revision=value)

    def test_alternative_credentials_and_admin_paths_are_rejected_not_mounted(self):
        for field in vars(self.query):
            for path in (
                Path("/tmp/credential"),
                Path(self.catalog["data_root"]) / "secrets/opensearch_admin_password",
            ):
                with (
                    self.subTest(field=field, path=path),
                    self.assertRaisesRegex(ValueError, "query_paths"),
                ):
                    self.render(paths=replace(self.query, **{field: path}))
        with self.assertRaises(ValueError):
            self.render(paths=vars(self.query))

    def test_unsafe_model_roots_and_overlapping_uuid_allocations_fail(self):
        for root in (
            Path("/"),
            Path("relative"),
            Path("/srv/../models"),
            Path("/srv/model\nroot"),
        ):
            with self.subTest(root=root), self.assertRaises(ValueError):
                self.render(config=replace(self.config, api_model_root=root))
        with self.assertRaisesRegex(ValueError, "roots_overlap"):
            self.render(
                config=replace(
                    self.config, api_model_root=self.config.state_root / "models"
                )
            )
        for gpus in (
            None,
            GPUConfig(UUIDS[0], (UUIDS[0], UUIDS[2]), UUIDS[3]),
            GPUConfig(
                UUIDS[0], (UUIDS[1], UUIDS[2]), UUIDS[0].lower().replace("gpu-", "GPU-")
            ),
            GPUConfig("0", (UUIDS[1], UUIDS[2]), UUIDS[3]),
            GPUConfig(UUIDS[0], (UUIDS[1],), UUIDS[3]),
        ):
            with (
                self.subTest(gpus=gpus),
                self.assertRaisesRegex(ValueError, "gpu_allocation"),
            ):
                self.render(config=replace(self.config, gpus=gpus))


if __name__ == "__main__":
    unittest.main()
