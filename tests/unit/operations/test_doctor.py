"""All doctor commands are injected; these tests never contact a daemon/GPU."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from search.operations import doctor
from search.operations.config import validate_config
from search.operations.shared.source_identity import (
    SOURCE_PATHS,
    require_clean_revision,
)
from tests.unit.operations.test_config import UUIDS


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.compose_version = "5.5.0"
        self.revision = "a" * 40
        self.dirty = ""
        self.gpus = "\n".join(
            f"{identifier},{index},NVIDIA GeForce GTX 1080 Ti,11264,580.126.09"
            for index, identifier in enumerate(UUIDS)
        )

    def __call__(self, argv, *, timeout):
        self.calls.append((argv, timeout))
        if self.fail is not None and self.fail(argv):
            return subprocess.CompletedProcess(
                argv, 1, "PRIVATE COMMAND OUTPUT", "PRIVATE PASSWORD"
            )
        if argv[0] == "git":
            output = self.revision if "rev-parse" in argv else self.dirty
        elif argv[:2] == ["docker", "version"]:
            output = json.dumps({"Version": "28.5.0"})
        elif argv[:3] == ["docker", "compose", "version"]:
            output = self.compose_version
        elif argv[:3] == ["docker", "image", "inspect"]:
            output = "sha256:" + "b" * 64
        elif argv[:2] == ["docker", "info"]:
            output = json.dumps(
                {"runc": {}, "nvidia": {"path": "nvidia-container-runtime"}}
            )
        elif argv[:2] == ["docker", "run"]:
            output = ""
        elif argv[0] == "nvidia-smi":
            output = self.gpus
        elif argv[0] == "findmnt":
            output = json.dumps(
                {"filesystems": [{"fstype": "ceph", "options": "rw,relatime"}]}
            )
        else:
            raise AssertionError(f"Unexpected command: {argv}")
        return subprocess.CompletedProcess(argv, 0, output, "")


class ProductDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        roots = {}
        for name in (
            "api_model_root",
            "embedding_model_root",
            "reranker_model_root",
        ):
            path = self.root / name
            path.mkdir(mode=0o700)
            roots[name] = str(path)
        self.config = validate_config(
            {
                "schema_version": 2,
                "catalog_storage": "docker-volume",
                "state_root": str(self.private / "state"),
                **roots,
                "gpus": {
                    "scope": UUIDS[0],
                    "planner": list(UUIDS[1:3]),
                    "reranker": UUIDS[3],
                },
                "cache_ttl_seconds": 0,
            },
            repo_root=self.repo,
        )
        self.runner = FakeRunner()
        for name in ("_api_models", "_embedding_model", "_reranker_model"):
            patcher = mock.patch.object(doctor, name)
            self.addCleanup(patcher.stop)
            setattr(self, name, patcher.start())

    def run_doctor(self, **kwargs):
        # Tests may themselves run as root; ownership is still checked against
        # the real fixture owner. Only the separate operator assertion is
        # mocked.
        with mock.patch.object(
            doctor, "validate_config", return_value=self.config
        ):
            return doctor.run_doctor(
                self.config,
                repo_root=self.repo,
                runner=self.runner,
                uid=1234,
                **kwargs,
            )

    def codes(self, report):
        return {item["name"]: item["code"] for item in report["checks"]}

    def test_light_check_reports_roles_resources_revision_without_paths(
        self,
    ):
        result = self.run_doctor()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["verification"], "light")
        self.assertEqual(result["source_revision"], "a" * 40)
        self.assertEqual(
            [gpu["role"] for gpu in result["hardware"]["gpus"]],
            ["scope", "planner", "planner", "reranker"],
        )
        self.assertEqual(result["hardware"]["gpus"][0]["memory_mib"], 11264)
        self.assertGreater(result["hardware"]["cpu_count"], 0)
        self.assertNotIn(str(self.root), json.dumps(result))
        self._api_models.assert_called_once_with(
            self.config.api_model_root, False
        )
        self._embedding_model.assert_called_once_with(
            self.config.embedding_model_root, False
        )
        self._reranker_model.assert_called_once_with(
            self.config.reranker_model_root, False
        )

    def test_root_is_rejected_before_any_command(self):
        result = doctor.run_doctor(self.config, runner=self.runner, uid=0)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.codes(result), {"operator": "non_root_required"})
        self.assertEqual(self.runner.calls, [])

    def test_invalid_configuration_is_rejected_before_any_command(self):
        with mock.patch.object(
            doctor, "validate_config", side_effect=ValueError("PRIVATE PATH")
        ):
            result = doctor.run_doctor(
                self.config, runner=self.runner, uid=1234
            )
        self.assertEqual(
            self.codes(result)["configuration"], "configuration_failed"
        )
        self.assertEqual(self.runner.calls, [])
        self.assertNotIn("PRIVATE PATH", json.dumps(result))

    def test_docker_probe_is_readonly_cpu_and_uses_only_approved_roots(
        self,
    ):
        self.run_doctor()
        calls = [
            argv
            for argv, _timeout in self.runner.calls
            if argv[:2] == ["docker", "run"]
        ]
        self.assertEqual(len(calls), 3)
        mounts = set()
        for argv in calls:
            for flag in (
                "--pull=never",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
            ):
                self.assertIn(flag, argv)
            self.assertIn("NVIDIA_VISIBLE_DEVICES=void", argv)
            self.assertNotIn("--gpus", argv)
            self.assertIn(doctor.PROBE_IMAGE, argv)
            mount = argv[argv.index("--mount") + 1]
            self.assertTrue(mount.endswith(",target=/probe,readonly"))
            mounts.add(mount.split(",")[1].removeprefix("source="))
        self.assertEqual(
            mounts,
            {
                str(self.config.api_model_root),
                str(self.config.embedding_model_root),
                str(self.config.reranker_model_root),
            },
        )
        self.assertNotIn(str(self.private), mounts)
        self.assertFalse(self.config.state_root.exists())

    def test_existing_state_is_also_probed_without_creating_or_changing_files(
        self,
    ):
        self.config.state_root.mkdir(mode=0o700)
        marker = self.config.state_root / "marker"
        marker.write_text("kept")
        result = self.run_doctor()
        self.assertEqual(result["status"], "ok")
        calls = [
            argv
            for argv, _timeout in self.runner.calls
            if argv[:2] == ["docker", "run"]
        ]
        self.assertEqual(len(calls), 4)
        self.assertEqual(marker.read_text(), "kept")

    def test_missing_probe_image_never_pulls_or_runs_and_blocks(self):
        self.runner.fail = lambda argv: argv[:3] == [
            "docker",
            "image",
            "inspect",
        ]
        result = self.run_doctor()
        self.assertEqual(
            self.codes(result)["storage_probe_image"],
            "storage_probe_image_failed",
        )
        self.assertFalse(
            any(argv[:2] == ["docker", "run"] for argv, _ in self.runner.calls)
        )
        self.assertFalse(any("pull" in argv for argv, _ in self.runner.calls))

    def test_unavailable_daemon_skips_probe_and_sanitizes_errors(self):
        self.runner.fail = lambda argv: argv[:2] == ["docker", "version"]
        result = self.run_doctor()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.codes(result)["docker"], "docker_failed")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertFalse(
            any(argv[:2] == ["docker", "run"] for argv, _ in self.runner.calls)
        )

    def test_missing_nvidia_runtime_blocks_without_allocating_a_gpu(self):
        self.runner.fail = lambda argv: argv[:2] == ["docker", "info"]
        result = self.run_doctor()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(
            self.codes(result)["nvidia_runtime"], "nvidia_runtime_failed"
        )
        self.assertFalse(
            any("--gpus" in argv for argv, _ in self.runner.calls)
        )

    def test_old_and_malformed_compose_versions_block(self):
        for version in ("2.20.0", "PRIVATE INVALID", "", "5.5.0-malicious"):
            self.runner.compose_version = version
            with self.subTest(version=version):
                result = self.run_doctor()
                self.assertEqual(
                    self.codes(result)["compose"], "compose_failed"
                )

    def test_source_revision_must_be_exact_and_dirty_source_blocks(self):
        self.runner.revision = "PRIVATE RAW OUTPUT"
        result = self.run_doctor()
        self.assertEqual(self.codes(result)["source"], "source_failed")
        self.assertEqual(result["source_revision"], "unknown")
        self.assertNotIn("PRIVATE RAW", json.dumps(result))
        self.runner.revision = "a" * 40
        self.runner.dirty = "?? PRIVATE FILENAME"
        result = self.run_doctor()
        self.assertEqual(self.codes(result)["source"], "source_failed")
        self.assertNotIn("PRIVATE FILENAME", json.dumps(result))
        status_call = next(
            argv for argv, _ in self.runner.calls if "--porcelain" in argv
        )
        self.assertIn("scripts/depositarai.py", status_call)
        self.assertIn("web", status_call)

    def test_missing_or_invalid_gpu_records_block_without_exposing_output(
        self,
    ):
        for raw in (
            "\n".join(self.runner.gpus.splitlines()[:3]),
            "PRIVATE GPU OUTPUT",
            self.runner.gpus.replace(",3,", ",oops,"),
        ):
            self.runner.gpus = raw
            with self.subTest(raw=raw):
                result = self.run_doctor()
                self.assertEqual(self.codes(result)["gpus"], "gpus_failed")
                self.assertEqual(result["hardware"]["gpus"], [])
                self.assertNotIn("PRIVATE GPU", json.dumps(result))

    def test_model_failure_does_not_expose_error_or_claim_success(self):
        self._api_models.side_effect = ValueError("PRIVATE MODEL PATH")
        result = self.run_doctor()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.codes(result)["api_models"], "api_models_failed")
        self.assertNotIn("PRIVATE MODEL PATH", json.dumps(result))

    def test_deep_is_explicit_and_forwarded_to_each_model_verifier(self):
        result = self.run_doctor(deep_verify=True)
        self.assertEqual(result["verification"], "deep")
        self._api_models.assert_called_once_with(
            self.config.api_model_root, True
        )
        self._embedding_model.assert_called_once_with(
            self.config.embedding_model_root, True
        )
        self._reranker_model.assert_called_once_with(
            self.config.reranker_model_root, True
        )

    def test_mount_unavailable_or_daemon_cannot_see_storage_blocks(self):
        for matcher, failed_code in (
            (lambda argv: argv[0] == "findmnt", "api_model_storage_failed"),
            (
                lambda argv: argv[:2] == ["docker", "run"],
                "api_model_storage_docker_failed",
            ),
        ):
            self.runner.fail = matcher
            with self.subTest(code=failed_code):
                result = self.run_doctor()
                self.assertEqual(result["status"], "blocked")
                self.assertIn(failed_code, self.codes(result).values())

    def test_command_timeouts_and_missing_programs_are_fixed_diagnostics(self):
        for error in (
            FileNotFoundError("PRIVATE executable"),
            subprocess.TimeoutExpired(["PRIVATE command"], 15),
        ):
            runner = mock.Mock(side_effect=error)
            with mock.patch.object(
                doctor, "validate_config", return_value=self.config
            ):
                result = doctor.run_doctor(
                    self.config, runner=runner, uid=1234
                )
            self.assertEqual(result["status"], "blocked")
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_root_keeps_ram_observation_before_other_admission(self):
        with (
            mock.patch.object(doctor.os, "cpu_count", return_value=8) as cpu,
            mock.patch.object(
                doctor.os, "sysconf", side_effect=OSError("PRIVATE RAM")
            ) as memory,
            mock.patch.object(doctor.socket, "gethostname") as hostname,
            mock.patch.object(doctor, "validate_config") as validate,
        ):
            result = doctor.run_doctor(self.config, runner=self.runner, uid=0)
        cpu.assert_called_once_with()
        memory.assert_called_once_with("SC_PHYS_PAGES")
        hostname.assert_not_called()
        validate.assert_not_called()
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(
            result["hardware"],
            {"cpu_count": 8, "ram_bytes": None, "gpus": []},
        )
        self.assertEqual(
            result["checks"],
            [
                {
                    "name": "operator",
                    "status": "blocked",
                    "code": "non_root_required",
                }
            ],
        )
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_required_checks_models_and_storage_keep_their_order(self):
        events = []
        transport, storage, probe = self.runner, doctor._storage, doctor._probe

        def command(argv, *, timeout):
            name = " ".join(argv[:2]) if argv[0] == "docker" else argv[0]
            events.append(name)
            return transport(argv, timeout=timeout)

        def host_storage(runner, path, *, writable):
            events.append(("storage", path.name, writable))
            return storage(runner, path, writable=writable)

        def visibility(runner, path, uid):
            events.append(("probe", path.name, uid))
            return probe(runner, path, uid)

        self.runner = command
        for name in ("_api_models", "_embedding_model", "_reranker_model"):
            getattr(self, name).side_effect = (
                lambda directory, deep, name=name: events.append(
                    ("model", name, directory.name, deep)
                )
            )
        with (
            mock.patch.object(doctor, "_storage", side_effect=host_storage),
            mock.patch.object(doctor, "_probe", side_effect=visibility),
        ):
            result = self.run_doctor()
        self.assertEqual(
            events,
            [
                "git",
                "git",
                "docker version",
                "docker info",
                "docker compose",
                "nvidia-smi",
                ("model", "_api_models", "api_model_root", False),
                ("model", "_embedding_model", "embedding_model_root", False),
                ("model", "_reranker_model", "reranker_model_root", False),
                ("storage", "api_model_root", False),
                "findmnt",
                ("storage", "embedding_model_root", False),
                "findmnt",
                ("storage", "reranker_model_root", False),
                "findmnt",
                ("storage", "private", True),
                "findmnt",
                "docker image",
                ("probe", "api_model_root", 1234),
                "docker run",
                ("probe", "embedding_model_root", 1234),
                "docker run",
                ("probe", "reranker_model_root", 1234),
                "docker run",
            ],
        )
        self.assertEqual(
            [row["name"] for row in result["checks"]],
            [
                "operator",
                "configuration",
                "source",
                "docker",
                "nvidia_runtime",
                "compose",
                "gpus",
                "api_models",
                "embedding_model",
                "reranker_model",
                "api_model_storage",
                "embedding_model_storage",
                "reranker_model_storage",
                "state_storage",
                "storage_probe_image",
                "api_model_storage_docker",
                "embedding_model_storage_docker",
                "reranker_model_storage_docker",
            ],
        )
        self.assertEqual(result["status"], "ok")

    def test_each_known_action_error_stays_a_fixed_failed_check(self):
        for error in (
            OSError("PRIVATE model"),
            ValueError("PRIVATE model"),
            KeyError("PRIVATE model"),
            TypeError("PRIVATE model"),
            subprocess.TimeoutExpired(["PRIVATE command"], 15),
        ):
            self._api_models.side_effect = error
            with self.subTest(kind=type(error).__name__):
                result = self.run_doctor()
                self.assertEqual(
                    self.codes(result)["api_models"], "api_models_failed"
                )
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(self.codes(result)["embedding_model"], "ok")
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_unrelated_action_errors_are_not_converted_to_readiness(self):
        for error in (RuntimeError("PRIVATE"), AttributeError("PRIVATE")):
            self._api_models.side_effect = error
            with self.subTest(kind=type(error).__name__):
                with self.assertRaises(type(error)) as caught:
                    self.run_doctor()
                self.assertIs(caught.exception, error)
                self._embedding_model.assert_not_called()
                self._reranker_model.assert_not_called()

    def test_runtime_and_storage_failures_do_not_disable_visibility_probes(
        self,
    ):
        for prefix in (["docker", "info"], ["findmnt"]):
            self.runner.calls.clear()
            self.runner.fail = lambda argv: argv[: len(prefix)] == prefix
            with self.subTest(prefix=prefix):
                result = self.run_doctor()
                probes = [
                    (argv, timeout)
                    for argv, timeout in self.runner.calls
                    if argv[:2] == ["docker", "run"]
                ]
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(len(probes), 3)
                self.assertEqual([timeout for _, timeout in probes], [30] * 3)
                self.assertEqual(
                    self.codes(result)["storage_probe_image"], "ok"
                )

    def _before_image_inspection(self, action):
        transport = self.runner

        def command(argv, *, timeout):
            if argv[:3] == ["docker", "image", "inspect"]:
                action()
            return transport(argv, timeout=timeout)

        self.runner = command
        return transport

    def test_state_appearing_later_is_probed_without_mounting_its_parent(self):
        transport = self._before_image_inspection(
            lambda: self.config.state_root.mkdir(mode=0o700)
        )
        result = self.run_doctor()
        host_paths = [
            argv[3] for argv, _ in transport.calls if argv[0] == "findmnt"
        ]
        mounts = [
            argv[argv.index("--mount") + 1]
            for argv, _ in transport.calls
            if argv[:2] == ["docker", "run"]
        ]
        self.assertEqual(result["status"], "ok")
        self.assertIn(str(self.config.state_root.parent), host_paths)
        self.assertNotIn(str(self.config.state_root), host_paths)
        self.assertEqual(len(mounts), 4)
        self.assertIn(
            f"type=bind,source={self.config.state_root},target=/probe,readonly",
            mounts,
        )
        self.assertNotIn(
            "type=bind,source="
            f"{self.config.state_root.parent},target=/probe,readonly",
            mounts,
        )

    def test_state_disappearing_later_is_not_probed(self):
        self.config.state_root.mkdir(mode=0o700)
        transport = self._before_image_inspection(self.config.state_root.rmdir)
        result = self.run_doctor()
        host_paths = [
            argv[3] for argv, _ in transport.calls if argv[0] == "findmnt"
        ]
        probes = [
            argv
            for argv, _ in transport.calls
            if argv[:2] == ["docker", "run"]
        ]
        self.assertEqual(result["status"], "ok")
        self.assertIn(str(self.config.state_root), host_paths)
        self.assertEqual(len(probes), 3)
        self.assertNotIn("state_storage_docker", self.codes(result))
        self.assertFalse(self.config.state_root.exists())

    def test_command_budget_counts_utf8_bytes_before_stripping(self):
        output = " " * (doctor.MAX_COMMAND_OUTPUT - 2) + "é"
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess([], 0, output, "")
        )
        self.assertEqual(doctor._command(runner, ["fixture"]), "é")
        runner.return_value.stdout = output + " "
        with self.assertRaisesRegex(ValueError, "^command_failed$"):
            doctor._command(runner, ["fixture"])
        self.assertEqual(
            runner.call_args_list,
            [mock.call(["fixture"], timeout=15)] * 2,
        )


class ModelPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.blob = self.root / "blobs" / ("sha256-" + "b" * 64)
        self.blob.parent.mkdir()
        self.blob.write_bytes(b"weights")
        self.relative = "manifests/registry.ollama.ai/library/test/8b"
        self.manifest = self.root / self.relative
        self.manifest.parent.mkdir(parents=True)
        descriptor = {"digest": "sha256:" + "b" * 64, "size": 7}
        self.manifest.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "config": descriptor,
                    "layers": [descriptor],
                }
            )
        )
        self.digest = hashlib.sha256(self.manifest.read_bytes()).hexdigest()

    def test_light_ollama_hashes_manifest_but_only_stats_weights(self):
        from scripts.prepare_search_models import checked_file

        with mock.patch(
            "scripts.prepare_search_models.checked_file", wraps=checked_file
        ) as verify:
            files = doctor._ollama_files(self.root, {"test:8b": self.digest})
        self.assertEqual(len(files), 2)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(verify.call_args.args[0], self.manifest)

    def test_manifest_digest_missing_blob_and_blob_size_mismatch_fail(self):
        with self.assertRaises(ValueError):
            doctor._ollama_files(self.root, {"test:8b": "a" * 64})
        self.blob.write_bytes(b"truncated")
        with self.assertRaises(ValueError):
            doctor._ollama_files(self.root, {"test:8b": self.digest})
        self.blob.unlink()
        with self.assertRaises(OSError):
            doctor._ollama_files(self.root, {"test:8b": self.digest})

    def test_blob_symlink_hardlink_and_writable_files_fail(self):
        self.blob.chmod(0o666)
        with self.assertRaises(ValueError):
            doctor._ollama_files(self.root, {"test:8b": self.digest})
        self.blob.chmod(0o600)
        linked = self.root / "linked"
        os.link(self.blob, linked)
        with self.assertRaises(ValueError):
            doctor._ollama_files(self.root, {"test:8b": self.digest})
        linked.unlink()
        self.blob.unlink()
        self.blob.symlink_to(self.manifest)
        with self.assertRaises(ValueError):
            doctor._ollama_files(self.root, {"test:8b": self.digest})

    def test_api_light_deep_reuses_plan_and_hashes_weights_only_when_explicit(
        self,
    ):
        files = {
            str(self.blob.relative_to(self.root)): {
                "sha256": "b" * 64,
                "bytes": 7,
            }
        }
        with mock.patch(
            "scripts.prepare_search_models.plan", return_value=files
        ) as plan:
            with mock.patch(
                "scripts.prepare_search_models.checked_file"
            ) as verify:
                doctor._api_models(self.root, False)
                verify.assert_not_called()
                doctor._api_models(self.root, True)
                verify.assert_called_once_with(self.blob, "b" * 64, 7)
            self.assertEqual(plan.call_count, 2)

    def test_reranker_light_hashes_code_config_but_not_weights(self):
        weights = self.root / "model.safetensors"
        code = self.root / "custom.py"
        weights.write_bytes(b"weights")
        code.write_bytes(b"code")
        pins = {"model.safetensors": "a" * 64, "custom.py": "b" * 64}
        with mock.patch("search.reranker.model.MODEL_HASHES", pins):
            with mock.patch(
                "scripts.prepare_search_models.checked_file"
            ) as verify:
                doctor._reranker_model(self.root, False)
                verify.assert_called_once_with(code, "b" * 64, 4)
                verify.reset_mock()
                doctor._reranker_model(self.root, True)
                self.assertEqual(verify.call_count, 2)

    def test_large_or_bad_schema_manifests_fail_before_loading_model(self):
        for value in (
            {"schemaVersion": True, "config": {}, "layers": [{}]},
            {"schemaVersion": 2, "config": {}, "layers": []},
        ):
            self.manifest.write_text(json.dumps(value))
            digest = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
            with self.subTest(value=value), self.assertRaises(ValueError):
                doctor._ollama_files(self.root, {"test:8b": digest})


class SourceIdentityTests(unittest.TestCase):
    def test_clean_revision_includes_shared_helpers_and_untracked_sources(
        self,
    ):
        runner = FakeRunner()
        self.assertEqual(
            require_clean_revision(Path("/not-read"), runner=runner), "a" * 40
        )
        argv = runner.calls[-1][0]
        for path in (
            "Makefile",
            "scripts/depositarai.py",
            "scripts/read_env.py",
            "scripts/search-lib.sh",
            "search",
            "web",
            "deploy",
        ):
            self.assertIn(path, SOURCE_PATHS)
            self.assertIn(path, argv)
        self.assertIn("--untracked-files=all", argv)

    def test_missing_git_invalid_revision_dirty_sources_are_sanitized(self):
        runner = FakeRunner()
        for revision, dirty, code in (
            ("PRIVATE REVISION", "", "revision_invalid"),
            ("a" * 40, "?? PRIVATE FILENAME", "source_dirty"),
        ):
            runner.revision, runner.dirty = revision, dirty
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(ValueError, code) as caught,
            ):
                require_clean_revision(Path("/not-read"), runner=runner)
            self.assertNotIn("PRIVATE", str(caught.exception))
        runner.fail = lambda argv: True
        with self.assertRaisesRegex(ValueError, "source_unavailable"):
            require_clean_revision(Path("/not-read"), runner=runner)

    def test_output_limit_and_timeouts_fail_closed(self):
        runner = FakeRunner()
        runner.revision = "a" * (64 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "source_unavailable"):
            require_clean_revision(Path("/not-read"), runner=runner)
        with self.assertRaisesRegex(ValueError, "source_unavailable"):
            require_clean_revision(
                Path("/not-read"), runner=MockRunnerTimeout()
            )


class MockRunnerTimeout:
    def __call__(self, argv, *, timeout):
        raise subprocess.TimeoutExpired(["PRIVATE COMMAND"], timeout)


if __name__ == "__main__":
    unittest.main()
