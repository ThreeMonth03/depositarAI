"""Keep provider proofs independent of a retired API-A instance."""

import hashlib
import copy
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

from search.operations.catalog import execution
from search.operations.deployment import inference_runtime
from search.operations.deployment.provider import runtime, contract
from search.operations.shared import private_io
from search.operations.state import locked_state
from tests.unit.deployment import test_inference_runtime as gpu_fixtures
from tests.unit.deployment.candidate import test_probe as probe_fixtures
from .test_contract import records


from .fixtures import (
    provider_fixture,
    model_checkpoint_fixture,
    image_binding_fixture,
    checkpoint_evidence,
    shared_checkpoint_evidence,
    dual_snapshot,
)


class ProviderRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(execution, "require_storage", return_value=object())
        )
        self.stack.enter_context(
            patch.object(execution, "validate_storage_mounts")
        )

    def native_provider_slot(self, provider):
        from search.operations.deployment import inference
        from search.operations.deployment.release import controller

        directory = self.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="provider-native-seal-")
        )
        config = replace(
            gpu_fixtures.product(), state_root=Path(directory) / "state"
        )
        store = self.stack.enter_context(locked_state(config, create=True))
        proof = provider.observe()["gpu"]
        identity = inference._runtime_identity(
            provider.inference_compose,
            {"api": {}, "reranker": {}},
            {"revision": "a" * 40, "source_manifest_sha256": "b" * 64},
            provider.inference_images,
        )
        complete = {
            **identity,
            "status": "runtime_running",
            **{key: proof[key] for key in controller.PROOF_FIELDS},
            "runtime_verified": True,
            "models_verified": True,
            "gpu_ownership_snapshot_verified": True,
            "model_warmup_verified": False,
            "website_started": False,
        }
        parent = config.state_root / "inference"
        parent.mkdir(mode=0o700)
        slot = parent / provider.inference_compose["name"].removeprefix(
            "depositar-ai-"
        )
        slot.mkdir(mode=0o700)
        for name, value in (
            ("started.json", identity),
            ("compose.json", provider.inference_compose),
            ("complete.json", complete),
        ):
            path = slot / name
            path.write_bytes(private_io.canonical(value))
            path.chmod(0o600)
        store.write("inference-candidate.json", complete)
        provider.verify_inference = controller._sealed_inference(
            self.stack, config, store, provider.inference_compose, identity
        )
        return config

    def test_retired_a_is_not_required_when_complete_b_is_active(self):
        provider, backend, _groups, _networks = provider_fixture()
        with patch("subprocess.run", side_effect=AssertionError("no host IO")):
            result = provider.observe()
        provider.verify_inference.assert_called_once()
        self.assertEqual(
            result["backend_proofs"][backend.compose["name"]],
            backend.expected_runtime,
        )
        self.assertEqual(
            result["gpu"]["api_container_ids"],
            [backend.lease["api_container_id"]],
        )

    def test_same_and_dual_catalog_clients_keep_native_producer_authority(
        self,
    ):
        for separate in (False, True):
            for active_first in (False, True):
                with self.subTest(
                    separate=separate, active_first=active_first
                ):
                    evidence = shared_checkpoint_evidence(
                        self,
                        separate_catalogs=separate,
                        active_first=active_first,
                    )
                    before = [
                        copy.deepcopy(binding.authority)
                        for binding in evidence.catalogs
                    ]
                    evidence.provider.refresh()
                    evidence.session.verify(
                        evidence.policy, evidence.record["compute_sha256"]
                    )
                    evidence.session.check_models(
                        evidence.policy, evidence.record["compute_sha256"]
                    )
                    self.assertEqual(
                        before,
                        [binding.authority for binding in evidence.catalogs],
                    )
                    self.assertTrue(
                        all(
                            "catalog_revision"
                            not in binding.authority["catalog_identity"]
                            for binding in evidence.catalogs
                        )
                    )
                    self.assertEqual(
                        evidence.provider.source_revision, "a" * 40
                    )
                    self.assertEqual(
                        evidence.provider.backends[0].source_revision,
                        "a" * 40 if active_first else "6" * 40,
                    )

    def test_native_gpu_image_owner_performs_four_bounded_inspections(self):
        from search.operations.deployment import (
            composition,
            inference,
            source_bundle,
        )
        from search.operations.deployment.images import build

        images = gpu_fixtures.images()
        ollama, reranker = images["scope-model"], images["reranker"]
        ollama.update(
            Os="linux",
            Architecture="amd64",
            RepoDigests=[
                "ollama/ollama@" + composition.OLLAMA_IMAGE.split("@", 1)[1]
            ],
        )
        proof = {"image_id": reranker["Id"]}
        references = {
            reranker["Id"]: reranker,
            build.TAGS["reranker"] + "a" * 40: reranker,
            composition.OLLAMA_IMAGE: ollama,
            ollama["Id"]: ollama,
        }
        docker = Mock()
        docker.inspect.side_effect = lambda reference: copy.deepcopy(
            references[reference]
        )
        with patch.object(
            source_bundle,
            "validate_compatible_built_image",
            return_value=proof,
        ):
            observed = inference._images(
                docker,
                {"revision": "a" * 40},
                {"revision": "a" * 40, "images": {"reranker": proof}},
            )
        self.assertEqual(docker.inspect.call_count, 4)
        self.assertEqual(observed["scope-model"]["Id"], ollama["Id"])

    def test_native_catalog_selection_inspects_three_existing_core_images(
        self,
    ):
        from search.operations.deployment.candidate import catalog

        snapshot = dual_snapshot(separate_catalogs=True)
        selected = snapshot.catalogs[1]
        config = replace(
            gpu_fixtures.product(), state_root=Path("/private/shadow-state")
        )
        store = SimpleNamespace(read=lambda _name: selected.context["plan"])
        docker = Mock()
        inspections = {row["Id"]: row for row in selected.inspections.values()}
        docker.inspect.side_effect = lambda identifier: copy.deepcopy(
            inspections[identifier]
        )
        with (
            patch.object(
                catalog.product_config, "load_config", return_value=config
            ),
            patch.object(
                catalog, "locked_state", return_value=nullcontext(store)
            ),
            patch.object(
                catalog.execution, "operation_lock", return_value=nullcontext()
            ),
            patch.object(
                catalog,
                "load_context",
                return_value=(
                    selected.context,
                    {
                        "authority": selected.authority,
                        "images": selected.images,
                    },
                ),
            ),
            patch.object(
                catalog,
                "require_clean_revision",
                return_value=selected.context["source_revision"],
            ),
            patch.object(
                catalog.credentials, "read_api_key_authority", return_value={}
            ),
            ExitStack() as resources,
        ):
            observed = catalog.select(
                {
                    "catalog_source": selected.context["repo"],
                    "catalog_config": selected.config_file,
                },
                resources,
                gpu_fixtures.product(),
                [],
                docker,
            )
        self.assertEqual(docker.inspect.call_count, 3)
        self.assertEqual(observed.images, selected.images)

    def test_image_binding_reuses_only_file_authority_inside_one_session(self):
        provider, host, _groups, _networks = provider_fixture()
        evidence = image_binding_fixture(self, provider, host)
        self.assertEqual(provider.docker.inspect.call_count, 4)
        evidence.binding.refresh(provider)
        evidence.binding.require_host(provider, host)
        self.assertEqual(provider.docker.inspect.call_count, 4)
        evidence.binding.inspect(provider)
        self.assertEqual(provider.docker.inspect.call_count, 8)

    def test_unknown_or_cross_session_binding_is_never_checkpoint_authority(
        self,
    ):
        provider, host, _groups, _networks = provider_fixture()
        provider.observe()
        other, other_host, _groups, _networks = provider_fixture()
        evidence = image_binding_fixture(self, other, other_host)
        for unknown in (
            None,
            SimpleNamespace(images=host.images),
            evidence.binding,
        ):
            host.image_binding = unknown
            with (
                self.subTest(binding=type(unknown).__name__),
                self.assertRaisesRegex(
                    contract.ProviderError, "authority_changed"
                ),
            ):
                provider._checkpoint_authorities()

    def test_binding_source_plan_and_proof_drift_refuse_without_inspect(
        self,
    ):
        from search.operations.deployment import source_bundle

        provider, host, _groups, _networks = provider_fixture()
        evidence = image_binding_fixture(self, provider, host)
        with patch.object(
            source_bundle,
            "committed_source_plan",
            return_value={"changed": True},
        ):
            with self.assertRaisesRegex(
                contract.ProviderError, "authority_changed"
            ):
                evidence.binding.refresh(provider)
        evidence.binding.proof["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            contract.ProviderError, "authority_changed"
        ):
            evidence.binding.require_owner(provider)
        self.assertEqual(provider.docker.inspect.call_count, 4)

    def test_final_image_inspection_rejects_tag_drift_kept_out_of_checkpoint(
        self,
    ):
        from search.operations.deployment.images import build
        from search.operations.deployment.release.controller import (
            ReleaseError,
        )

        provider, host, _groups, _networks = provider_fixture()
        evidence = image_binding_fixture(self, provider, host)
        tag = build.TAGS["web"] + host.source_revision
        changed = copy.deepcopy(evidence.inspections[tag])
        changed["Id"] = "sha256:" + "0" * 64
        evidence.inspections[tag] = changed
        evidence.binding.refresh(provider)
        with self.assertRaises(ReleaseError):
            evidence.binding.inspect(provider)

    def test_real_checkpoint_has_fresh_full_runtime_and_no_tag_inspections(
        self,
    ):
        evidence = checkpoint_evidence(self)
        provider = evidence.provider
        before = provider.docker.calls
        provider.refresh()
        self.assertEqual(provider.docker.calls - before, 4)
        for _ in range(8):
            before = provider.docker.calls
            provider.check_models(evidence.policy)
            self.assertEqual(provider.docker.calls - before, 5)
        before = provider.docker.calls
        provider.refresh()
        self.assertEqual(provider.docker.calls - before, 4)

    def test_checkpoint_rechecks_roles_records_query_key_and_model_authorities(
        self,
    ):
        from search.operations.deployment import inference

        for drift in ("roles", "record", "query", "key", "models"):
            with self.subTest(drift=drift):
                evidence = checkpoint_evidence(self)
                provider = evidence.provider
                if drift == "roles":
                    provider.control.roles_sha256 = "0" * 64
                elif drift == "record":
                    evidence.current["record"]["owned_paths"] = []
                elif drift == "query":
                    evidence.current["catalog"]["authority"]["changed"] = True
                elif drift == "key":
                    evidence.current["key"]["credential_sha256"] = "0" * 64
                else:
                    provider.verify_models.side_effect = (
                        inference.InferenceError(
                            "inference_model_file_changed"
                        )
                    )
                before = provider.docker.calls
                with self.assertRaises(ValueError):
                    provider.check_models(evidence.policy)
                self.assertEqual(provider.docker.calls, before)

    def test_checkpoint_rechecks_image_namespace_and_unfiltered_peers(self):
        for drift in ("image", "namespace", "peer"):
            with self.subTest(drift=drift):
                evidence = checkpoint_evidence(self)
                row = evidence.groups[evidence.host.compose["name"]][0]
                if drift == "image":
                    row["Image"] = "sha256:" + "0" * 64
                elif drift == "namespace":
                    row["NetworkSettings"]["SandboxID"] = "f" * 64
                else:
                    name = evidence.host.compose["networks"][
                        "existing-inference"
                    ]["name"]
                    evidence.networks[name]["Containers"]["e" * 64] = {}
                with self.assertRaises(ValueError):
                    evidence.provider.check_models(evidence.policy)

    def test_late_tag_change_is_rejected_by_mandatory_full_final_refresh(self):
        from search.operations.deployment.images import build
        from search.operations.deployment.release.controller import (
            ReleaseError,
        )

        evidence = checkpoint_evidence(self)
        provider = evidence.provider
        provider.refresh()
        tag = build.TAGS["web"] + evidence.host.source_revision
        changed = copy.deepcopy(evidence.image.inspections[tag])
        changed["Id"] = "sha256:" + "0" * 64
        evidence.image.inspections[tag] = changed
        provider.check_models(evidence.policy)
        with self.assertRaises(ReleaseError):
            provider.refresh()

    def test_full_checkpoint_rejects_new_off_network_project_container(self):
        evidence = checkpoint_evidence(self)
        evidence.groups[evidence.host.compose["name"]].append({"Id": "e" * 64})
        with self.assertRaisesRegex(ValueError, "container_inventory_invalid"):
            evidence.provider.check_models(evidence.policy)

    def test_fresh_collector_shares_checkpoint_deadline_and_original_counter(
        self,
    ):
        evidence = checkpoint_evidence(self)
        provider, clock = evidence.provider, {"now": 100.0}
        provider.deadline = 400.0
        original, observed = provider.docker.runner, []

        def runner(argv, *, timeout, environment):
            observed.append(timeout)
            clock["now"] += 0.1
            return original(argv, timeout=timeout, environment=environment)

        before = provider.docker.calls
        with (
            patch.object(
                runtime.time, "monotonic", side_effect=lambda: clock["now"]
            ),
            patch.object(provider.docker, "runner", runner),
        ):
            provider._checkpoint_runtime(deadline=100.5)
            for timeout, expected in zip(observed, (0.5, 0.4, 0.3, 0.2)):
                self.assertAlmostEqual(timeout, expected)
            self.assertEqual(provider.docker.calls - before, 4)
            clock["now"] = 100.5
            with self.assertRaisesRegex(contract.ProviderError, "deadline"):
                provider._checkpoint_runtime(deadline=100.5)
        self.assertEqual(provider.docker.calls - before, 4)
        self.assertEqual(provider.deadline, 400.0)

    def test_actual_native_provider_seal_never_opens_a_live_release(self):
        provider, backend, _groups, _networks = provider_fixture()
        config = self.native_provider_slot(provider)
        result = provider.observe()
        self.assertEqual(
            result["backend_proofs"][backend.compose["name"]],
            backend.expected_runtime,
        )
        self.assertFalse((config.state_root / "releases").exists())

    def test_actual_native_provider_seal_rejects_changed_gpu_process(self):
        from search.operations.deployment.release.controller import (
            ReleaseError,
        )

        provider, _backend, groups, _networks = provider_fixture()
        self.native_provider_slot(provider)
        groups[provider.inference_compose["name"]][0]["State"]["StartedAt"] = (
            "2026-11-01T00:00:00Z"
        )
        with self.assertRaisesRegex(ReleaseError, "inference_receipt_changed"):
            provider.observe()

    def test_unknown_or_retired_api_peer_is_never_filtered(self):
        provider, _backend, _groups, networks = provider_fixture()
        network = next(
            row
            for row in networks.values()
            if row["Name"].endswith("_inference")
        )
        network["Containers"]["e" * 64] = {"Name": "retired-A"}
        with self.assertRaises(inference_runtime.InferenceRuntimeError):
            provider.observe()

    def test_missing_active_api_refuses_before_any_residency_probe(self):
        from search.operations.deployment.candidate import probe

        for kind in ("api", "group", "duplicate"):
            provider, backend, groups, _networks = provider_fixture()
            name = backend.compose["name"]
            if kind == "group":
                del groups[name]
            elif kind == "api":
                groups[name] = groups[name][1:]
            else:
                groups[name].append(groups[name][0])
            with (
                self.subTest(kind=kind),
                patch.object(probe, "verify_residency") as resident,
                self.assertRaisesRegex(
                    contract.ProviderError, "runtime_changed"
                ),
            ):
                provider.observe()
            resident.assert_not_called()

    def test_same_container_with_changed_provider_namespace_is_rejected(self):
        provider, _backend, groups, _networks = provider_fixture()
        groups[provider.inference_compose["name"]][0]["NetworkSettings"][
            "SandboxID"
        ] = "f" * 64
        with self.assertRaisesRegex(contract.ProviderError, "runtime_changed"):
            provider.observe()

    def test_backend_image_mount_and_namespace_drift_are_not_authorized(self):
        for drift in ("image", "mount", "namespace"):
            provider, backend, groups, _networks = provider_fixture()
            row = groups[backend.compose["name"]][0]
            if drift == "image":
                row["Image"] = "sha256:" + "1" * 64
            elif drift == "mount":
                row["Mounts"][0]["RW"] = True
            else:
                row["NetworkSettings"]["SandboxID"] = "f" * 64
            with self.subTest(drift=drift), self.assertRaises(ValueError):
                provider.observe()

    def test_check_models_refuses_unverified_or_former_api_namespace(self):
        provider, backend, _groups, _networks = provider_fixture()
        provider.last_backend_proofs = {}
        with self.assertRaisesRegex(contract.ProviderError, "runtime_changed"):
            provider.check_models({}, api_container_id="a" * 64)
        self.assertNotEqual(backend.lease["api_container_id"], "a" * 64)

    def test_configuration_parses_the_same_bytes_that_passed_the_digest(self):
        from search.operations import config

        raw = b'{"approved":true}\n'
        reference = {
            "path": "/private/product.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        with (
            patch.object(runtime.authority, "read_private", return_value=raw),
            patch.object(
                config,
                "load_config",
                side_effect=AssertionError("second read"),
            ),
            patch.object(
                config, "validate_config", return_value="approved"
            ) as validate,
        ):
            self.assertEqual(
                runtime.configuration(reference, Path("/source")), "approved"
            )
        validate.assert_called_once_with(
            {"approved": True}, repo_root=Path("/source")
        )

    def test_native_state_and_catalog_locks_are_deduplicated_and_sorted(self):
        from search.operations import state

        provider = runtime.Provider.__new__(runtime.Provider)
        provider.control = object()
        provider.config = replace(
            gpu_fixtures.product(), state_root=Path("/private/z-provider")
        )
        provider.stack, provider.stores = self.stack, {}
        provider.roles = {
            "active_backend_sha256": "1" * 64,
            "rollback_backend_sha256": "2" * 64,
        }
        configs, backends, events = {}, [], []
        for index in ("a", "b"):
            backend = records()[1]
            backend["configuration"] = {
                "path": f"/private/{index}.json",
                "sha256": "1" * 64,
            }
            backend["build_state_root"] = f"/private/{index}-build"
            configs[backend["configuration"]["path"]] = replace(
                provider.config, state_root=Path(f"/private/{index}-catalog")
            )
            backends.append(backend)

        @contextmanager
        def locked(config, *, create):
            self.assertFalse(create)
            events.append(("state", config.state_root))
            yield SimpleNamespace(
                read=lambda _name: {
                    "data_root": str(config.state_root / "data")
                }
            )

        @contextmanager
        def operation(context):
            events.append(("catalog", context["data"]))
            yield

        with (
            patch.object(
                runtime.authority, "read_backend", side_effect=backends
            ),
            patch.object(runtime, "_source", return_value=Path("/source")),
            patch.object(
                runtime,
                "configuration",
                side_effect=lambda ref, _: configs[ref["path"]],
            ),
            patch.object(state, "locked_state", locked),
            patch.object(execution, "operation_lock", operation),
        ):
            provider._lock_states()
        roots = {
            provider.config.state_root,
            *(entry.state_root for entry in configs.values()),
            *(Path(backend["build_state_root"]) for backend in backends),
        }
        expected = [("state", root) for root in sorted(roots, key=str)]
        expected += [
            ("catalog", root / "data")
            for root in sorted(
                (entry.state_root for entry in configs.values()), key=str
            )
        ]
        self.assertEqual(events, expected)

    def test_residency_success_uses_verified_active_b_api_after_a_retirement(
        self,
    ):
        from search.operations.deployment import inference, private_api_tls
        from search.operations.deployment.images import build
        from search.operations.deployment.candidate import probe

        provider, backend, raw, compute = model_checkpoint_fixture()
        policy = probe_fixtures.POLICY
        report = {
            "schema_version": 1,
            "status": "resident",
            "full_gpu_residency_verified": True,
            "models": [
                {**item, "size_bytes": 1234, "vram_bytes": 1234}
                for item in probe._expected(policy)
            ],
        }
        provider.docker = probe_fixtures.FakeDocker(report)
        gpu = {
            "schema_version": 1,
            "status": "ok",
            "code": "gpu_ownership_verified",
            "snapshot_only": True,
            "exclusive_reservation": False,
            "runtime_modified": False,
            "sealed_gpu_container_count": 3,
            "gpu_count": 4,
        }
        with (
            patch.object(runtime.authority, "read_private", return_value=raw),
            patch.object(
                private_api_tls, "gpu_ownership_proofs", return_value=()
            ),
            patch.object(
                inference.gpu_collect, "run_gpu_ownership", return_value=gpu
            ),
            patch.object(
                build,
                "_run",
                return_value=SimpleNamespace(returncode=0, stdout=compute),
            ),
        ):
            self.assertEqual(provider.check_models(policy), report)
        self.assertEqual(
            provider.docker.calls[0][0][:3],
            ["exec", "-i", backend.lease["api_container_id"]],
        )
        self.assertEqual(len(provider.docker.calls), 1)
        self.assertEqual(provider._checkpoint_authorities.call_count, 2)

    def checkpoint_trace(self):
        from search.operations.deployment import inference, private_api_tls
        from search.operations.deployment.candidate import probe
        from search.operations.deployment.candidate.batch import execution
        from search.operations.deployment.images import build

        provider, backend, raw, compute = model_checkpoint_fixture()
        clock, observed = {"now": 100.0}, []
        provider.deadline = clock["now"] + execution.FINAL_AUDIT_RESERVE + 0.5
        provider.docker = object()

        def record(stage, timeout):
            observed.append((stage, timeout))
            clock["now"] += 0.1

        def execute(argv, *, timeout, environment):
            self.assertEqual(set(environment), {"PATH", "LANG"})
            if argv[0] == "fixture-tls":
                record("tls", timeout)
                return SimpleNamespace(returncode=0, stdout=b"{}")
            self.assertEqual(argv[0], "nvidia-smi")
            record("compute", timeout)
            return SimpleNamespace(returncode=0, stdout=compute)

        def bounded(argv, *, timeout, max_bytes):
            self.assertEqual(argv, ["fixture-gpu"])
            self.assertEqual(max_bytes, 4096)
            record("gpu", timeout)
            return SimpleNamespace(returncode=0, stdout=b"{}")

        def tls(_plan, *, runner):
            runner(["fixture-tls"], timeout=60)
            return ()

        def gpu(_config, _proof, runner, *, cpu_proofs):
            self.assertEqual(cpu_proofs, ())
            runner(["fixture-gpu"], timeout=30, max_bytes=4096)

        def resident(_docker, identifier, _policy, *, timeout):
            self.assertEqual(identifier, backend.lease["api_container_id"])
            record("resident", timeout)
            return {"fixture": "resident"}

        for owner, name, replacement in (
            (runtime.time, "monotonic", lambda: clock["now"]),
            (runtime.authority, "read_private", lambda _path: raw),
            (private_api_tls, "gpu_ownership_proofs", tls),
            (inference, "_gpu", gpu),
            (inference.gpu_collect, "bounded_runner", bounded),
            (build, "_run", execute),
            (probe, "verify_residency", resident),
        ):
            self.stack.enter_context(patch.object(owner, name, replacement))
        return provider, clock, observed

    def test_checkpoint_clips_all_tools_without_spending_final_audit_reserve(
        self,
    ):
        from search.operations.deployment.candidate.batch import execution

        provider, clock, observed = self.checkpoint_trace()
        original = provider.deadline
        checkpoint = original - execution.FINAL_AUDIT_RESERVE
        self.assertEqual(
            provider.check_models({}, deadline=checkpoint),
            {"fixture": "resident"},
        )
        self.assertEqual(
            [stage for stage, _ in observed],
            ["tls", "gpu", "resident", "compute"],
        )
        for (_, timeout), expected in zip(observed, (0.5, 0.4, 0.3, 0.2)):
            self.assertAlmostEqual(timeout, expected)
        self.assertEqual(provider.deadline, original)
        clock["now"] = checkpoint + 1
        observed.clear()
        provider.check_models({})
        self.assertEqual(
            observed,
            [("tls", 60), ("gpu", 30), ("resident", 20), ("compute", 15)],
        )
        self.assertEqual(provider.deadline, original)

    def test_expired_checkpoint_refuses_before_tls_gpu_or_residency_tool(
        self,
    ):
        from search.operations.deployment.candidate.batch import execution

        provider, clock, observed = self.checkpoint_trace()
        original = provider.deadline
        checkpoint = original - execution.FINAL_AUDIT_RESERVE
        clock["now"] = checkpoint
        with self.assertRaisesRegex(contract.ProviderError, "deadline"):
            provider.check_models({}, deadline=checkpoint)
        self.assertEqual(observed, [])
        self.assertEqual(provider.deadline, original)

    def test_real_cpu_runner_timeouts_obey_the_checkpoint_deadline(self):
        from search.operations.deployment.gpu import contract as gpu_contract
        from search.operations.deployment.images import build

        provider = runtime.Provider.__new__(runtime.Provider)
        provider.deadline = time.monotonic() + 300
        original = provider.deadline
        command = [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            "import time; time.sleep(2)",
        ]
        for runner, options, error in (
            (provider._tls_runner, {"timeout": 60}, build.ImageBuildError),
            (
                provider._gpu_runner,
                {"timeout": 30, "max_bytes": 128},
                gpu_contract.GPUOwnershipError,
            ),
        ):
            with (
                self.subTest(runner=runner.__name__),
                self.assertRaisesRegex(error, "command_timeout"),
            ):
                runner(command, deadline=time.monotonic() + 0.05, **options)
            self.assertEqual(provider.deadline, original)


if __name__ == "__main__":
    unittest.main()
