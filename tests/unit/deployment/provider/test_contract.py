"""Reject open records and protect complete backend resource closures."""

import copy
import unittest

from search.operations.deployment.provider import contract


def records():
    source = {"root": "/private/source", "revision": "a" * 40}
    config = {"path": "/private/product.json", "sha256": "b" * 64}
    policy = {"scope": {"model": "fixed"}}
    provider = {
        "schema_version": 1,
        "kind": "model-provider",
        "source": source,
        "configuration": config,
        "source_plan_sha256": "c" * 64,
        "inference_identity": {
            "schema_version": 2,
            "image_ids": {
                role: "sha256:" + "d" * 64 for role in contract.GPU_ROLES
            },
        },
        "inference_complete_sha256": "e" * 64,
        "model_manifest_sha256": "f" * 64,
        "model_file_identity_sha256": "1" * 64,
        "compute_sha256": "2" * 64,
        "namespace_sha256": "3" * 64,
        "policy": {
            "schema_version": 1,
            "status": "compatible",
            "request_policy_sha256": contract.digest(policy),
            "policy": policy,
        },
        "adopted_by_revision": "4" * 40,
        "bootstrap_sha256": "5" * 64,
    }
    backend = {
        "schema_version": 1,
        "kind": "backend",
        "source": dict(source),
        "configuration": dict(config),
        "catalog_source": dict(source),
        "build_state_root": "/private/build",
        "compose": {
            "name": "release-A",
            "services": {role: {} for role in contract.CPU_ROLES},
        },
        "lease": {"api_container_id": "6" * 64},
        "runtime": {
            "container_ids": {
                role: str(i) * 64
                for i, role in enumerate(sorted(contract.CPU_ROLES), 6)
            },
            "network_ids": {"website": "9" * 64},
        },
        "image_proof": {"image_id": "sha256:" + "a" * 64},
        "image_authority_sha256": "b" * 64,
        "catalog_binding": {"source_revision": source["revision"]},
        "owned_paths": ["/private/backend/A"],
        "provider_sha256": contract.digest(provider),
        "namespace_sha256": "c" * 64,
    }
    roles = {
        "schema_version": 1,
        "epoch": 1,
        "provider_sha256": contract.digest(provider),
        "active_backend_sha256": contract.digest(backend),
        "rollback_backend_sha256": None,
        "tls_plan_ref": {"path": "/private/tls/plan.json", "sha256": "d" * 64},
        "pending_operation_sha256": None,
    }
    return provider, backend, roles


class ProviderContractTests(unittest.TestCase):
    def test_closed_records_preserve_source_and_operator_independence(self):
        provider, backend, roles = records()
        self.assertIs(contract.provider(provider), provider)
        self.assertIs(contract.backend(backend), backend)
        self.assertIs(contract.roles(roles), roles)
        self.assertNotEqual(
            provider["source"]["revision"], provider["adopted_by_revision"]
        )
        for validate, value in (
            (contract.provider, provider),
            (contract.backend, backend),
            (contract.roles, roles),
        ):
            with self.subTest(validate=validate.__name__):
                with self.assertRaises(contract.ProviderError):
                    validate({**value, "credential": "PRIVATE"})

    def test_epochs_and_pending_are_exact_not_truthy(self):
        role = records()[2]
        for epoch in (True, False, 0, -1, 1.0, "1", 2**63):
            with (
                self.subTest(epoch=epoch),
                self.assertRaises(contract.ProviderError),
            ):
                contract.roles({**role, "epoch": epoch})
        with self.assertRaises(contract.ProviderError):
            contract.roles({**role, "pending_operation_sha256": "bad"})

    def test_policy_digest_and_native_kind_cannot_be_forged(self):
        provider = records()[0]
        changed = copy.deepcopy(provider)
        changed["policy"]["policy"]["scope"]["model"] = "other"
        with self.assertRaises(contract.ProviderError):
            contract.provider(changed)
        for kind in ("model", None, True):
            with self.assertRaises(contract.ProviderError):
                contract.provider({**provider, "kind": kind})

    def test_protected_ids_and_path_ancestry_reject_cleanup(self):
        role = records()[2]
        protected = {
            "container_ids": ["a" * 64],
            "network_ids": ["b" * 64],
            "owned_paths": ["/private/backends/B/source"],
        }
        candidates = [
            {
                "container_ids": ["a" * 64],
                "network_ids": [],
                "owned_paths": [],
            },
            {
                "container_ids": [],
                "network_ids": ["b" * 64],
                "owned_paths": [],
            },
            {
                "container_ids": [],
                "network_ids": [],
                "owned_paths": ["/private/backends/B"],
            },
            {
                "container_ids": [],
                "network_ids": [],
                "owned_paths": ["/private/backends/B/source/item"],
            },
        ]
        for proposed in candidates:
            with (
                self.subTest(proposed=proposed),
                self.assertRaisesRegex(
                    contract.ProviderError, "backend_protected"
                ),
            ):
                contract.require_disposable(role, proposed, [protected])
        contract.require_disposable(
            role,
            {
                "container_ids": [],
                "network_ids": [],
                "owned_paths": ["/private/backends/C/source"],
            },
            [protected],
        )

    def test_pending_intent_refuses_even_unrelated_mutation(self):
        role = {**records()[2], "pending_operation_sha256": "a" * 64}
        with self.assertRaisesRegex(
            contract.ProviderError, "recovery_required"
        ):
            contract.require_disposable(
                role,
                {
                    "container_ids": [],
                    "network_ids": [],
                    "owned_paths": ["/private/unrelated"],
                },
                [],
            )

    def test_path_aliases_cannot_escape_resource_intersection(self):
        for path in ("//private/source", "/private/./source", "/private/../x"):
            with (
                self.subTest(path=path),
                self.assertRaises(contract.ProviderError),
            ):
                contract.path(path)
        with self.assertRaises(contract.ProviderError):
            contract.paths([[]])


if __name__ == "__main__":
    unittest.main()
