"""Private inference start/reuse tests with real Git/files and fake Docker/GPU."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from search.operations.config import GPUConfig
from search.operations.deployment import (
    composition,
    image_build,
    inference,
    runtime_docker,
)
from search.operations.shared import private_io
from search.operations.state import locked_state
from tests.unit.deployment.test_composition import UUIDS
from tests.unit.deployment.image_fixtures import ImageStagingFixture
from tests.unit.deployment.test_inference_runtime import snapshot


class FakeInferenceDocker:
    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []
        self.compose = None
        self.containers, self.networks = [], []
        self.on_up = None
        self.up_failed = False
        self.on_inspect = None
        self.info = "linux"
        self.ollama = {
            "Id": "sha256:" + "9" * 64,
            "Os": "linux",
            "Architecture": "amd64",
            "RepoDigests": [
                "ollama/ollama@" + composition.OLLAMA_IMAGE.split("@", 1)[1]
            ],
            "Config": {
                "Env": ["PATH=/usr/bin", "PRIVATE_SENTINEL=never-save-this-value"],
                "Cmd": None,
                "Entrypoint": ["/bin/ollama"],
            },
        }

    def __call__(self, argv, *, timeout, environment):
        self.calls.append((list(argv), timeout, dict(environment)))
        assert argv[:3] == ["docker", "--host", image_build.DOCKER_HOST]
        assert argv[3] == "--config"
        client = Path(argv[4])
        assert client.stat().st_mode & 0o777 == 0o700
        assert not list(client.iterdir())
        arguments = argv[5:]
        value = b""
        if arguments[0] == "info":
            value = json.dumps(self.info)
        elif arguments[:2] == ["image", "inspect"]:
            reference = arguments[-1]
            if reference in {composition.OLLAMA_IMAGE, self.ollama["Id"]}:
                image = self.ollama
            else:
                image = self.fixture.docker.images["reranker"]
                assert reference in {
                    image["Id"],
                    image_build.TAGS["reranker"] + self.fixture.revision,
                }
            value = json.dumps(image)
        elif arguments[0] == "ps":
            value = "".join(row["Id"] + "\n" for row in self.containers)
        elif arguments[:2] == ["network", "ls"]:
            value = "".join(
                json.dumps({"ID": row["Id"], "Name": row["Name"]}) + "\n"
                for row in self.networks
            )
        elif arguments[0] == "compose":
            path = Path(arguments[arguments.index("--file") + 1])
            self.compose = json.loads(path.read_bytes())
            slot = path.parent
            self.fixture.test.assertTrue((slot / "started.json").is_file())
            self.fixture.test.assertFalse((slot / "complete.json").exists())
            self.fixture.test.assertNotIn("ports", str(self.compose))
            if "up" in arguments:
                self.fixture.test.assertIn("--no-build", arguments)
                self.fixture.test.assertEqual(
                    "never", arguments[arguments.index("--pull") + 1]
                )
                self.fixture.test.assertEqual(
                    {"scope-model", "answer-model", "reranker"}, set(arguments[-3:])
                )
                image_records = {
                    "scope-model": self.ollama,
                    "answer-model": self.ollama,
                    "reranker": self.fixture.docker.images["reranker"],
                }
                self.containers, self.networks = snapshot(self.compose, image_records)
                if self.on_up:
                    self.on_up(slot)
                if self.up_failed:
                    return subprocess.CompletedProcess(
                        argv, 1, b"", b"PRIVATE_SENTINEL failed up"
                    )
        elif arguments[0] == "inspect":
            if self.on_inspect:
                self.on_inspect(self)
            value = json.dumps(self.containers)
        elif arguments[:2] == ["network", "inspect"]:
            value = json.dumps(self.networks)
        else:
            raise AssertionError(arguments)
        return subprocess.CompletedProcess(argv, 0, value, b"")


@unittest.skipIf(os.geteuid() == 0, "production inference operator requires non-root")
class PrivateInferenceTests(unittest.TestCase):
    def setUp(self):
        base = ImageStagingFixture(self)
        self.fixture = base
        base.config = replace(
            base.config, gpus=GPUConfig(UUIDS[0], (UUIDS[1], UUIDS[2]), UUIDS[3])
        )
        self.config = base.config
        base.invoke()
        self.repo, self.revision = base.repo, base.revision
        self.docker = FakeInferenceDocker(base)
        self.gpu_calls = []
        self.gpu_callback = None
        self.model_bytes = {}
        pins = {}
        for model in ("fixture-scope:8b", "fixture-planner:20b"):
            blob = ("fixture weights " + model).encode()
            digest = hashlib.sha256(blob).hexdigest()
            relative = "blobs/sha256-" + digest
            self.model_bytes[relative] = blob
            manifest = private_io.canonical(
                {
                    "schemaVersion": 2,
                    "config": {"digest": "sha256:" + digest, "size": len(blob)},
                    "layers": [{"digest": "sha256:" + digest, "size": len(blob)}],
                }
            )
            relative = "manifests/registry.ollama.ai/library/" + model.replace(":", "/")
            self.model_bytes[relative] = manifest
            pins[model] = hashlib.sha256(manifest).hexdigest()
        self.config.api_model_root.mkdir(mode=0o700)
        for relative, raw in self.model_bytes.items():
            path = self.config.api_model_root / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(0o400)
        self.config.reranker_model_root.mkdir(mode=0o700)
        reranker_pins = {}
        for name in ("model.safetensors", "config.json", "tokenizer.json"):
            raw = ("fixture " + name).encode()
            path = self.config.reranker_model_root / name
            path.write_bytes(raw)
            path.chmod(0o400)
            reranker_pins[name] = hashlib.sha256(raw).hexdigest()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(inference.prepare_search_models, "PINS", pins).start()
        mock.patch("search.reranker.model.MODEL_HASHES", reranker_pins).start()
        mock.patch.object(
            inference.gpu_ownership, "run_gpu_ownership", side_effect=self.gpu
        ).start()

    def gpu(self, config, *, allowed, runner=None):
        self.assertEqual(self.config, config)
        self.gpu_calls.append(allowed)
        if self.gpu_callback:
            self.gpu_callback(allowed)
        return {
            "schema_version": 1,
            "status": "ok",
            "code": "gpu_ownership_verified",
            "snapshot_only": True,
            "exclusive_reservation": False,
            "runtime_modified": False,
            "sealed_gpu_container_count": len(allowed),
            "gpu_count": 4,
        }

    def invoke(self, *, revision=None):
        with locked_state(self.config) as store:
            return inference.start_private_inference(
                self.config,
                store,
                revision or self.revision,
                repo_root=self.repo,
                docker_runner=self.docker,
            )

    def slot(self):
        children = list((self.config.state_root / "inference").iterdir())
        self.assertEqual(1, len(children))
        return children[0]

    def commands(self, name):
        return [argv[5:] for argv, _, _ in self.docker.calls if argv[5] == name]

    def test_fresh_start_seals_source_models_images_resources_and_gpu_roles(self):
        result = self.invoke()
        self.assertEqual("runtime_running", result["status"])
        self.assertFalse(result["runtime_reused"])
        self.assertTrue(result["runtime_verified"])
        self.assertTrue(result["models_verified"])
        self.assertTrue(result["gpu_ownership_snapshot_verified"])
        self.assertFalse(result["model_warmup_verified"])
        self.assertFalse(result["website_started"])
        self.assertFalse(result["active_changed"])
        self.assertEqual(self.revision, result["revision"])
        self.assertEqual([0, 0, 3], [len(allowed) for allowed in self.gpu_calls])
        self.assertEqual(
            {"scope", "planner", "reranker"},
            {proof.role for proof in self.gpu_calls[-1]},
        )
        self.assertEqual(
            4, len({uuid for proof in self.gpu_calls[-1] for uuid in proof.gpu_uuids})
        )
        self.assertEqual(1, sum("up" in call for call in self.commands("compose")))
        self.assertEqual(3, len(result["container_ids"]))
        slot = self.slot()
        self.assertEqual(
            {"compose.json", "started.json", "complete.json"},
            {path.name for path in slot.iterdir()},
        )
        for path in slot.iterdir():
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertNotIn("never-save-this-value", path.read_text())
            self.assertNotIn("PRIVATE_SENTINEL", path.read_text())
        with locked_state(self.config) as store:
            expected = dict(result)
            del expected["runtime_reused"]
            self.assertEqual(expected, store.read("inference-candidate.json"))

    def test_completed_reuse_rehashes_models_reinspects_runtime_and_never_restarts(
        self,
    ):
        first = self.invoke()
        slot = self.slot()
        before = {path.name: path.stat().st_mtime_ns for path in slot.iterdir()}
        self.docker.calls.clear()
        self.gpu_calls.clear()
        second = self.invoke()
        self.assertTrue(second["runtime_reused"])
        self.assertEqual(first["container_ids"], second["container_ids"])
        self.assertEqual([], self.commands("compose"))
        self.assertEqual([3], [len(allowed) for allowed in self.gpu_calls])
        self.assertEqual(
            before, {path.name: path.stat().st_mtime_ns for path in slot.iterdir()}
        )
        model = next(self.config.api_model_root.glob("blobs/*"))
        model.chmod(0o600)
        raw = model.read_bytes()
        model.write_bytes(b"x" * len(raw))
        model.chmod(0o400)
        with self.assertRaisesRegex(inference.InferenceError, "model_digest_changed"):
            self.invoke()
        self.assertEqual(
            before, {path.name: path.stat().st_mtime_ns for path in slot.iterdir()}
        )

    def test_ui_only_commit_reuses_original_gpu_images_ids_and_provenance(self):
        first = self.invoke()
        origin = self.revision
        slot = self.slot()
        before = {path.name: path.read_bytes() for path in slot.iterdir()}
        page = self.repo / "web/src/main.tsx"
        page.write_text("new UI only\n")
        self.fixture.git("add", ".")
        self.fixture.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "--quiet",
            "-m",
            "UI only",
        )
        self.revision = self.fixture.git("rev-parse", "HEAD").strip()
        self.assertNotEqual(origin, self.revision)
        self.docker.calls.clear()
        second = self.invoke()
        self.assertTrue(second["runtime_reused"])
        self.assertEqual(origin, second["revision"])
        self.assertEqual(first["project"], second["project"])
        self.assertEqual(first["image_ids"], second["image_ids"])
        self.assertEqual(first["container_ids"], second["container_ids"])
        self.assertEqual(
            first["source_manifest_sha256"], second["source_manifest_sha256"]
        )
        self.assertEqual(
            before, {path.name: path.read_bytes() for path in slot.iterdir()}
        )
        self.assertEqual([], self.commands("compose"))
        tag = self.commands("image")
        self.assertTrue(
            any(image_build.TAGS["reranker"] + origin in call for call in tag)
        )
        self.assertFalse(
            any(image_build.TAGS["reranker"] + self.revision in call for call in tag)
        )

    def test_reranker_code_commit_cannot_reuse_old_image_authority(self):
        first = self.invoke()
        server = self.repo / "search/reranker/server.py"
        server.write_text("changed inference implementation\n")
        self.fixture.git("add", ".")
        self.fixture.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "--quiet",
            "-m",
            "Reranker changed",
        )
        revision = self.fixture.git("rev-parse", "HEAD").strip()
        plan = inference.source_bundle.committed_source_plan(self.repo, revision)
        composed = composition._inference(
            self.config,
            revision,
            os.geteuid(),
            os.getegid(),
            inference_source_sha256=plan["services"]["reranker"]["source_sha256"],
        )
        self.assertNotEqual(first["project"], composed["name"])
        self.docker.calls.clear()
        with self.assertRaises(inference.InferenceError):
            self.invoke(revision=revision)
        self.assertEqual([], self.docker.calls)

    def test_legacy_app_sha_state_requires_explicit_transition_without_adoption(self):
        self.invoke()
        slot = self.slot()
        started = json.loads((slot / "started.json").read_bytes())
        started["schema_version"] = 1
        (slot / "started.json").write_bytes(private_io.canonical(started))
        slot.rename(slot.parent / "inference-ffffffffffff")
        self.docker.calls.clear()
        with self.assertRaisesRegex(
            inference.InferenceError, "legacy_state_requires_transition"
        ):
            self.invoke()
        self.assertEqual([], self.docker.calls)

    def test_dirty_or_wrong_source_rejected_before_docker_or_inference_state(self):
        with self.assertRaises(inference.InferenceError):
            self.invoke(revision="a" * 40)
        self.assertFalse((self.config.state_root / "inference").exists())
        (self.repo / "search/api/conversation/app.py").write_text("dirty\n")
        with self.assertRaises(inference.InferenceError):
            self.invoke()
        self.assertEqual([], self.docker.calls)

    def test_partial_up_leaves_durable_intent_and_never_blindly_retries(self):
        self.docker.up_failed = True
        with self.assertRaisesRegex(
            inference.InferenceError, "command_failed"
        ) as caught:
            self.invoke()
        self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))
        slot = self.slot()
        self.assertTrue((slot / "started.json").exists())
        self.assertFalse((slot / "complete.json").exists())
        self.docker.calls.clear()
        self.docker.up_failed = False
        with self.assertRaisesRegex(inference.InferenceError, "partial_state"):
            self.invoke()
        self.assertEqual([], self.commands("compose"))

    def test_release_model_verification_includes_cpu_embedding_bytes(self):
        raw = b"fixture embedding weights"
        digest = hashlib.sha256(raw).hexdigest()
        model = "fixture-embedding:tiny"
        manifest = private_io.canonical(
            {
                "schemaVersion": 2,
                "config": {"digest": "sha256:" + digest, "size": len(raw)},
                "layers": [{"digest": "sha256:" + digest, "size": len(raw)}],
            }
        )
        root = self.config.embedding_model_root
        root.mkdir(mode=0o700)
        blob = root / "blobs" / ("sha256-" + digest)
        entry = root / "manifests/registry.ollama.ai/library/fixture-embedding/tiny"
        for path, content in ((blob, raw), (entry, manifest)):
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o400)
        with (
            mock.patch("search.catalog.embedding.MODEL", model),
            mock.patch(
                "search.catalog.embedding.MODEL_DIGEST",
                hashlib.sha256(manifest).hexdigest(),
            ),
        ):
            with inference._verified_models(self.config, include_embedding=True) as (
                plans,
                verify,
            ):
                self.assertEqual({"api", "reranker", "embedding"}, set(plans))
                verify()
            blob.chmod(0o600)
            blob.write_bytes(b"x" * len(raw))
            blob.chmod(0o400)
            with self.assertRaisesRegex(
                inference.InferenceError, "model_digest_changed"
            ):
                with inference._verified_models(self.config, include_embedding=True):
                    self.fail("corrupted CPU embedding bytes were accepted")
        self.assertEqual([], self.docker.calls)

    def test_unsealed_existing_project_is_not_adopted_by_labels_or_names(self):
        self.docker.containers = [{"Id": "a" * 64}]
        with self.assertRaisesRegex(
            inference.InferenceError, "legacy_runtime_not_adopted"
        ):
            self.invoke()
        self.assertEqual([], self.commands("compose"))

    def test_missing_image_receipt_or_tampered_source_stage_blocks_before_docker(self):
        (self.fixture.slot / "images/reranker/complete.json").unlink()
        with self.assertRaises(inference.InferenceError):
            self.invoke()
        self.assertEqual([], self.docker.calls)

    def test_image_candidate_cannot_attest_its_own_source_or_allow_extra_proof(self):
        with locked_state(self.config) as store:
            candidate = store.read("image-candidate.json")
            candidate["source_manifest_sha256"] = "f" * 64
            store.write("image-candidate.json", candidate, replace=True)
        with self.assertRaisesRegex(
            inference.InferenceError, "image_candidate_changed"
        ):
            self.invoke()
        self.assertEqual([], self.docker.calls)

    def test_stage_context_bytes_must_match_independent_committed_sources(self):
        (self.fixture.slot / "context/search/reranker/server.py").write_text(
            "tampered\n"
        )
        with self.assertRaises(inference.InferenceError):
            self.invoke()
        self.assertEqual([], self.docker.calls)

    def test_ollama_repo_digest_platform_and_immutable_id_are_mandatory(self):
        for key, value in (
            ("RepoDigests", []),
            ("Architecture", "arm64"),
            ("Os", "windows"),
            ("Id", "mutable:tag"),
        ):
            with self.subTest(key=key):
                prior = self.docker.ollama[key]
                self.docker.ollama[key] = value
                with self.assertRaisesRegex(
                    inference.InferenceError, "registry_identity_changed"
                ):
                    self.invoke()
                self.docker.ollama[key] = prior
                self.assertEqual([], self.commands("compose"))

    def test_global_gpu_preflight_failure_prevents_start(self):
        with mock.patch.object(
            inference.gpu_ownership,
            "run_gpu_ownership",
            return_value={"status": "blocked", "code": "unknown_compute_pid"},
        ):
            with self.assertRaisesRegex(
                inference.InferenceError, "gpu_ownership_blocked"
            ):
                self.invoke()
        self.assertEqual([], self.commands("compose"))
        self.assertEqual([], list((self.config.state_root / "inference").iterdir()))

    def test_actual_start_pid_or_container_change_on_reuse_fails_before_gpu_allowance(
        self,
    ):
        self.invoke()
        self.gpu_calls.clear()
        self.docker.containers[0]["State"]["Pid"] += 10
        with self.assertRaisesRegex(
            inference.InferenceError, "completed_runtime_changed"
        ):
            self.invoke()
        self.assertEqual([], self.gpu_calls)

    def test_extra_release_peer_is_not_silently_accepted_without_lease(self):
        self.invoke()
        self.docker.networks[0]["Containers"]["f" * 64] = {}
        with self.assertRaises(inference.inference_runtime.InferenceRuntimeError):
            self.invoke()

    def test_mutating_runtime_after_gpu_snapshot_prevents_completion(self):
        def change(allowed):
            if allowed:
                self.docker.containers[0]["State"]["Pid"] += 100

        self.gpu_callback = change
        with self.assertRaisesRegex(
            inference.InferenceError, "runtime_changed_during_verification"
        ):
            self.invoke()
        self.assertFalse((self.slot() / "complete.json").exists())

    def test_model_file_replacement_during_up_is_detected_and_quarantined(self):
        def change(_slot):
            path = next(self.config.api_model_root.glob("blobs/*"))
            old = path.with_suffix(".saved")
            path.rename(old)
            path.write_bytes(old.read_bytes())
            path.chmod(0o400)

        self.docker.on_up = change
        with self.assertRaisesRegex(inference.InferenceError, "model_file_changed"):
            self.invoke()
        self.assertFalse((self.slot() / "complete.json").exists())

    def test_final_model_context_exit_failure_precedes_success_publication(self):
        original = inference._verified_models

        @contextmanager
        def fail_on_exit(config, **options):
            with original(config, **options) as value:
                yield value
            raise inference.InferenceError("inference_model_late_verification_failed")

        with mock.patch.object(inference, "_verified_models", side_effect=fail_on_exit):
            with self.assertRaisesRegex(
                inference.InferenceError, "late_verification_failed"
            ):
                self.invoke()
        self.assertFalse((self.slot() / "complete.json").exists())
        with locked_state(self.config) as store:
            with self.assertRaises(FileNotFoundError):
                store.read("inference-candidate.json")

    def test_final_compose_or_started_replacement_does_not_get_completed_receipt(self):
        def change(slot):
            path = slot / "started.json"
            value = json.loads(path.read_bytes())
            value["revision"] = "f" * 40
            path.write_bytes(private_io.canonical(value))

        self.docker.on_up = change
        with self.assertRaisesRegex(inference.InferenceError, "start_identity_changed"):
            self.invoke()
        self.assertFalse((self.slot() / "complete.json").exists())

    def test_unknown_private_slot_or_symlink_is_never_adopted(self):
        parent = self.config.state_root / "inference"
        parent.mkdir(mode=0o700)
        (parent / "unexpected").write_text("operator file")
        with self.assertRaisesRegex(inference.InferenceError, "unknown_state"):
            self.invoke()
        self.assertEqual([], self.commands("compose"))

    def test_root_and_mismatched_store_are_rejected(self):
        with locked_state(self.config) as store:
            with mock.patch.object(inference.os, "geteuid", return_value=0):
                with self.assertRaisesRegex(
                    inference.InferenceError, "product_state_invalid"
                ):
                    inference.start_private_inference(
                        self.config,
                        store,
                        self.revision,
                        repo_root=self.repo,
                        docker_runner=self.docker,
                    )
        self.assertEqual([], self.docker.calls)

    def test_runtime_child_environment_never_inherits_docker_context_or_tokens(self):
        with mock.patch.dict(
            os.environ,
            DOCKER_HOST="tcp://remote",
            DOCKER_CONTEXT="remote",
            HF_TOKEN="PRIVATE_SENTINEL_TOKEN",
        ):
            self.invoke()
        for argv, timeout, environment in self.docker.calls:
            self.assertEqual({"PATH", "LANG", "LC_ALL"}, set(environment))
            self.assertEqual("unix:///var/run/docker.sock", argv[2])
            self.assertNotIn("PRIVATE_SENTINEL_TOKEN", str(environment))
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 660)
        self.assertFalse(
            any(
                call[5] in {"pull", "build", "rm", "stop", "run"}
                for call, _, _ in self.docker.calls
            )
        )


class InferenceDockerBoundsTests(unittest.TestCase):
    def client(self, runner):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-inference-bounds-")
        self.addCleanup(temporary.cleanup)
        return runtime_docker.Docker(Path(temporary.name), runner)

    def test_nonzero_timeout_output_and_command_budget_are_fixed_errors(self):
        fixtures = (
            (
                lambda *args, **kwargs: subprocess.CompletedProcess(
                    args[0], 1, b"", b"PRIVATE_SENTINEL"
                ),
                "command_failed",
            ),
            (
                lambda *args, **kwargs: subprocess.CompletedProcess(
                    args[0], 0, b"x" * (image_build.MAX_OUTPUT_BYTES + 1), b""
                ),
                "output_limit",
            ),
            (
                lambda *args, **kwargs: subprocess.CompletedProcess(
                    args[0], True, b"", b""
                ),
                "command_invalid",
            ),
        )
        for runner, code in fixtures:
            with self.subTest(code=code):
                with self.assertRaisesRegex(inference.InferenceError, code) as caught:
                    self.client(runner).call(["info"])
                self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))
        client = self.client(
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, b"", b"")
        )
        client.calls = runtime_docker.MAX_COMMANDS
        with self.assertRaisesRegex(inference.InferenceError, "command_budget"):
            client.call(["info"])
        client.calls = 0
        client.deadline = 0
        with self.assertRaisesRegex(inference.InferenceError, "command_budget"):
            client.call(["info"])

    def test_default_capture_is_shared_and_timeout_does_not_reveal_output(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("PRIVATE_SENTINEL", 1)

        with mock.patch.object(image_build, "_run", side_effect=timeout) as runner:
            with self.assertRaisesRegex(
                inference.InferenceError, "command_timeout"
            ) as caught:
                self.client(None).call(["info"])
            self.assertEqual(1, runner.call_count)
            self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))

    def test_client_guard_rejects_configuration_drift_before_and_after_command(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-client-guard-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        descriptor = private_io.open_directory(root)
        self.addCleanup(os.close, descriptor)

        def verify():
            private_io.verify_directory(descriptor, root)
            if os.listdir(descriptor):
                raise inference.InferenceError("inference_client_configuration_changed")

        def inject(argv, **options):
            (root / "config.json").write_text("{}")
            return subprocess.CompletedProcess(argv, 0, b"PRIVATE_SENTINEL", b"")

        runner = mock.Mock(side_effect=inject)
        client = runtime_docker.Docker(root, runner, verify_client=verify)
        with self.assertRaisesRegex(
            inference.InferenceError, "client_configuration_changed"
        ) as caught:
            client.call(["info"])
        self.assertEqual(1, runner.call_count)
        self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))
        with self.assertRaisesRegex(
            inference.InferenceError, "client_configuration_changed"
        ):
            client.call(["info"])
        self.assertEqual(1, runner.call_count)


class PrivateInferenceTLSOwnershipTests(unittest.TestCase):
    def test_gpu_gate_keeps_tls_in_the_real_ownership_collector(self):
        from tests.unit.deployment.test_gpu_ownership import (
            CONFIG,
            FakeRunner,
            tls_container,
            tls_proof,
        )

        row = tls_container("outer")
        proof = tls_proof(row, "outer")
        runner = FakeRunner([row])
        inference._gpu(CONFIG, (), runner, cpu_proofs=(proof,))
        self.assertEqual(runner.inspect_count[row["Id"]], 2)
        with self.assertRaisesRegex(inference.InferenceError, "gpu_ownership_blocked"):
            inference._gpu(CONFIG, (), FakeRunner([row]))

    def test_gpu_gate_requires_cpu_count_in_the_verified_result(self):
        from tests.unit.deployment.test_gpu_ownership import (
            CONFIG,
            tls_container,
            tls_proof,
        )

        proof = tls_proof(tls_container())
        response = {
            "schema_version": 1,
            "status": "ok",
            "code": "gpu_ownership_verified",
            "snapshot_only": True,
            "exclusive_reservation": False,
            "runtime_modified": False,
            "sealed_gpu_container_count": 0,
            "gpu_count": 4,
        }
        with mock.patch.object(
            inference.gpu_ownership, "run_gpu_ownership", return_value=response
        ):
            with self.assertRaisesRegex(
                inference.InferenceError, "gpu_ownership_blocked"
            ):
                inference._gpu(CONFIG, (), None, cpu_proofs=(proof,))
