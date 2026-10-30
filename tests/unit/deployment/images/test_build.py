"""Image builder transaction tests with fake Docker; real harmless pipe probes."""

from __future__ import annotations

import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from search.operations.deployment import source_bundle
from search.operations.deployment.images import build
from search.operations.shared import private_io
from tests.unit.deployment.images.fixtures import (
    FakeDocker as FakeDocker,
    fixture_sources as fixture_sources,
    fixture_tree as fixture_tree,
)


REVISION = "a" * 40


class ProductImageBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-image-build-test-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "staged"
        self.root.mkdir(mode=0o700)
        self.context = self.root / "context"
        self.context.mkdir(mode=0o700)
        sources = fixture_sources()
        tree = fixture_tree(sources)
        tree["search/catalog/query.py"]["mode"] = "100755"
        self.plan = source_bundle.plan_source_bundle(REVISION, tree, sources)
        for path, raw in sources.items():
            destination = self.context / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            destination.chmod(
                0o755 if self.plan["files"][path]["mode"] == "100755" else 0o644
            )
        self.write_manifest(self.plan)
        self.docker = FakeDocker(self.root, self.plan)

    def write_manifest(self, value):
        path = self.root / "manifest.json"
        path.write_bytes(private_io.canonical(value))
        path.chmod(0o600)

    def invoke(self, **options):
        return build.build_product_images(
            self.root, REVISION, runner=self.docker, **options
        )

    def commands(self, operation):
        return [argv[5:] for argv, _, _ in self.docker.calls if argv[5] == operation]

    def test_four_fixed_builds_intent_then_iid_inspection_then_private_receipts(self):
        result = self.invoke()
        self.assertEqual(set(build.SERVICES), set(result["images"]))
        self.assertFalse(result["services_started"])
        self.assertFalse(result["registry_published"])
        self.assertEqual(4, len(self.commands("build")))
        self.assertEqual(8, len(self.commands("image")))
        for service, command in zip(build.SERVICES, self.commands("build")):
            recipe = self.plan["services"][service]
            self.assertEqual(
                str(self.context / recipe["dockerfile"]),
                command[command.index("--file") + 1],
            )
            self.assertEqual(str(self.context), command[-1])
            self.assertEqual("linux/amd64", command[command.index("--platform") + 1])
            self.assertIn("--pull", command)
            self.assertIn("SOURCE_REVISION=" + REVISION, command)
            for name, value in recipe["required_labels"].items():
                self.assertIn(name + "=" + value, command)
            slot = self.root / "images" / service
            self.assertEqual(
                {"started.json", "image.iid", "complete.json"},
                {path.name for path in slot.iterdir()},
            )
            for name in ("started.json", "complete.json"):
                self.assertEqual(0o600, (slot / name).stat().st_mode & 0o777)
                self.assertNotIn("never-save-this-value", (slot / name).read_text())
                self.assertNotIn("PRIVATE_SENTINEL", (slot / name).read_text())
            self.assertEqual(
                self.docker.images[service]["Id"], result["images"][service]["image_id"]
            )

    def test_same_source_reuse_reinspects_every_image_without_build_or_receipt_rewrite(
        self,
    ):
        first = self.invoke()
        before = {
            service: (self.root / "images" / service / "complete.json")
            .stat()
            .st_mtime_ns
            for service in build.SERVICES
        }
        self.docker.calls.clear()
        second = self.invoke()
        self.assertEqual(first, second)
        self.assertEqual([], self.commands("build"))
        self.assertEqual(8, len(self.commands("image")))
        self.assertEqual(
            before,
            {
                service: (self.root / "images" / service / "complete.json")
                .stat()
                .st_mtime_ns
                for service in build.SERVICES
            },
        )

    def test_source_bytes_tamper_on_reuse_is_rejected_before_any_docker_call(self):
        self.invoke()
        self.docker.calls.clear()
        (self.context / "web/src/styles.css").write_bytes(b"changed source\n")
        with self.assertRaises(build.ImageBuildError):
            self.invoke()
        self.assertEqual([], self.docker.calls)

    def test_independent_expected_manifest_digest_binds_source_before_docker(self):
        expected = build._digest(self.plan)
        first = self.invoke(expected_manifest_sha256=expected)
        self.docker.calls.clear()
        self.assertEqual(first, self.invoke(expected_manifest_sha256=expected))
        self.assertEqual([], self.commands("build"))

    def test_wrong_expected_manifest_rejected_before_lock_or_docker_mutation(self):
        with self.assertRaisesRegex(build.ImageBuildError, "expected_manifest_changed"):
            self.invoke(expected_manifest_sha256="f" * 64)
        self.assertEqual([], self.docker.calls)
        self.assertFalse((self.root / "images").exists())
        self.assertFalse((self.root / "image-build.lock").exists())

    def test_invalid_expected_manifest_digest_is_not_accepted(self):
        for value in (True, "a" * 40, "F" * 64, [], "f" * 64 + "\n"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    build.ImageBuildError, "expected_manifest_invalid"
                ):
                    self.invoke(expected_manifest_sha256=value)
        self.assertEqual([], self.docker.calls)

    def test_completed_image_labels_and_environment_are_reverified_on_reuse(self):
        self.invoke()
        self.docker.calls.clear()
        self.docker.images["conversation-api"]["Config"]["Env"].append(
            "ADDED=unexpected"
        )
        with self.assertRaisesRegex(build.ImageBuildError, "completed_image_changed"):
            self.invoke()
        self.assertEqual([], self.commands("build"))

    def test_environment_and_local_daemon_cannot_be_overridden_by_parent_environment(
        self,
    ):
        with mock.patch.dict(
            os.environ,
            DOCKER_HOST="tcp://remote:2375",
            DOCKER_CONTEXT="remote",
            DOCKER_CONFIG="/private",
            HF_TOKEN="must-not-inherit",
        ):
            self.invoke()
        for argv, _, environment in self.docker.calls:
            self.assertEqual(build.DOCKER_HOST, argv[2])
            self.assertEqual({"PATH", "LANG", "LC_ALL"}, set(environment))
            self.assertNotIn("HOME", environment)
            self.assertFalse(any(key.startswith("DOCKER_") for key in environment))
            self.assertNotIn("must-not-inherit", str(environment))
            self.assertNotIn("--context", argv)
            self.assertNotIn("push", argv)
            self.assertNotIn("run", argv)
            self.assertNotIn("create", argv)
            self.assertNotIn("--publish", argv)
            self.assertNotIn("--gpus", argv)

    def test_non_linux_daemon_rejected_before_any_started_receipt(self):
        self.docker.info = "windows"
        with self.assertRaisesRegex(build.ImageBuildError, "daemon_unsupported"):
            self.invoke()
        self.assertEqual([], self.commands("build"))
        self.assertEqual([], list((self.root / "images").iterdir()))

    def test_build_failure_preserves_started_and_never_blindly_retries(self):
        self.docker.return_failure = True
        with self.assertRaisesRegex(build.ImageBuildError, "command_failed") as caught:
            self.invoke()
        self.assertNotIn("private", str(caught.exception))
        slot = self.root / "images/conversation-api"
        self.assertEqual({"started.json"}, {path.name for path in slot.iterdir()})
        self.docker.return_failure = False
        self.docker.calls.clear()
        with self.assertRaisesRegex(build.ImageBuildError, "partial_state"):
            self.invoke()
        self.assertEqual([], self.docker.calls)
        self.assertTrue((slot / "started.json").exists())

    def test_unknown_or_empty_partial_state_is_rejected_without_cleanup(self):
        images = self.root / "images"
        images.mkdir(mode=0o700)
        partial = images / "web"
        partial.mkdir(mode=0o700)
        with self.assertRaisesRegex(build.ImageBuildError, "partial_state"):
            self.invoke()
        self.assertTrue(partial.exists())
        self.assertEqual([], self.docker.calls)
        partial.rmdir()
        marker = images / "unknown"
        marker.write_bytes(b"preserve evidence")
        with self.assertRaisesRegex(build.ImageBuildError, "unknown_state"):
            self.invoke()
        self.assertEqual(b"preserve evidence", marker.read_bytes())

    def test_invalid_manifest_revision_recipe_flags_and_extra_root_files_fail_preflight(
        self,
    ):
        for change in (
            lambda p: p.update(revision="b" * 40),
            lambda p: p.update(schema_version=True),
            lambda p: p["services"]["web"].update(base_images=["python:latest"]),
        ):
            plan = copy.deepcopy(self.plan)
            change(plan)
            self.write_manifest(plan)
            with self.assertRaises(build.ImageBuildError):
                self.invoke()
            self.assertEqual([], self.docker.calls)
        self.write_manifest(self.plan)
        (self.root / "private-unexpected").write_bytes(b"preserve")
        with self.assertRaisesRegex(build.ImageBuildError, "unknown_root_state"):
            self.invoke()

    def test_manifest_json_duplicate_nonfinite_and_noncanonical_forms_rejected(self):
        path = self.root / "manifest.json"
        for raw in (
            b'{"schema_version":1,"schema_version":2}',
            b'{"x":NaN}',
            b'{"x":1}',
            b"[]",
        ):
            path.write_bytes(raw)
            with self.assertRaises(build.ImageBuildError):
                self.invoke()
            self.assertEqual([], self.docker.calls)

    def test_private_root_manifest_context_and_receipt_modes_required(self):
        manifest = self.root / "manifest.json"
        manifest.chmod(0o644)
        with self.assertRaises(build.ImageBuildError):
            self.invoke()
        manifest.chmod(0o600)
        self.context.chmod(0o755)
        with self.assertRaises(build.ImageBuildError):
            self.invoke()
        self.context.chmod(0o700)
        self.root.chmod(0o755)
        try:
            with self.assertRaises(build.ImageBuildError):
                self.invoke()
        finally:
            self.root.chmod(0o700)
        self.assertEqual([], self.docker.calls)

    def test_context_or_lock_tampering_during_build_never_gets_complete_receipt(self):
        def tamper(service, iid):
            (self.context / "search/api/conversation/app.py").write_bytes(
                b"changed after durable started"
            )

        self.docker.on_build = tamper
        with self.assertRaises(build.ImageBuildError):
            self.invoke()
        slot = self.root / "images/conversation-api"
        self.assertTrue((slot / "started.json").exists())
        self.assertTrue((slot / "image.iid").exists())
        self.assertFalse((slot / "complete.json").exists())

    def test_manifest_boolean_alias_change_after_build_is_rejected(self):
        def tamper(service, iid):
            changed = copy.deepcopy(self.plan)
            changed["runtime_verified"] = 0
            self.write_manifest(changed)

        self.docker.on_build = tamper
        with self.assertRaisesRegex(build.ImageBuildError, "manifest_changed"):
            self.invoke()
        self.assertFalse((self.root / "images/conversation-api/complete.json").exists())

    def test_replaced_lock_cannot_complete_a_build_under_a_different_mutex(self):
        def replace_lock(service, iid):
            path = self.root / "image-build.lock"
            path.rename(self.root / "old-image-build.lock")
            path.touch(mode=0o600)

        self.docker.on_build = replace_lock
        with self.assertRaisesRegex(build.ImageBuildError, "lock_unsafe"):
            self.invoke()
        self.assertFalse((self.root / "images/conversation-api/complete.json").exists())

    def test_later_build_cannot_silently_change_a_previously_verified_receipt(self):
        def tamper_previous_receipt(service, iid):
            if service == "web":
                path = self.root / "images/conversation-api/complete.json"
                complete = json.loads(path.read_text())
                complete["image"]["image_environment_sha256"] = "f" * 64
                path.write_bytes(private_io.canonical(complete))

        self.docker.on_build = tamper_previous_receipt
        with self.assertRaisesRegex(build.ImageBuildError, "final_receipt_changed"):
            self.invoke()
        self.assertTrue((self.root / "images/conversation-api/complete.json").exists())

    def test_held_lock_rejects_concurrent_builder_before_docker(self):
        descriptor = os.open(
            self.root / "image-build.lock", os.O_RDWR | os.O_CREAT, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(build.ImageBuildError, "lock_busy"):
                self.invoke()
        finally:
            os.close(descriptor)
        self.assertEqual([], self.docker.calls)

    def test_malformed_iid_and_nonprivate_iid_modes_preserve_uncertainty(self):
        def invalid_iid(service, iid):
            iid.write_bytes(b"sha256:" + b"A" * 64)
            iid.chmod(0o600)

        self.docker.on_build = invalid_iid
        with self.assertRaisesRegex(build.ImageBuildError, "iid_invalid"):
            self.invoke()
        self.assertFalse((self.root / "images/conversation-api/complete.json").exists())

    def test_builder_644_iid_is_accepted_inside_private_role_directory(self):
        self.docker.iid_mode = 0o644
        self.docker.iid_suffix = b""
        self.invoke()
        self.assertEqual(
            0o700, (self.root / "images/conversation-api").stat().st_mode & 0o777
        )

    def test_symlink_iid_never_read_or_followed(self):
        def linked_iid(service, iid):
            target = self.parent / "private-value"
            target.write_bytes(("sha256:" + "1" * 64).encode())
            target.chmod(0o600)
            iid.symlink_to(target)

        self.docker.on_build = linked_iid
        with self.assertRaisesRegex(build.ImageBuildError, "iid_unsafe"):
            self.invoke()
        self.assertEqual([], self.commands("image"))

    def test_missing_source_labels_or_wrong_revision_environment_rejected(self):
        self.docker.images["conversation-api"]["Config"]["Labels"].pop(
            "org.opencontainers.image.revision"
        )
        with self.assertRaises(build.ImageBuildError):
            self.invoke()
        self.assertFalse((self.root / "images/conversation-api/complete.json").exists())

    def test_wrong_source_revision_environment_rejected(self):
        environment = self.docker.images["conversation-api"]["Config"]["Env"]
        environment[environment.index("SOURCE_REVISION=" + REVISION)] = (
            "SOURCE_REVISION=" + "b" * 40
        )
        with self.assertRaises(build.ImageBuildError):
            self.invoke()
        self.assertFalse((self.root / "images/conversation-api/complete.json").exists())

    def test_iid_fifo_is_rejected_before_image_inspection(self):
        def bad_iid(service, iid):
            os.mkfifo(iid, 0o600)

        self.docker.on_build = bad_iid
        with self.assertRaisesRegex(build.ImageBuildError, "iid_unsafe"):
            self.invoke()
        self.assertEqual([], self.commands("image"))

    def test_world_writable_iid_is_rejected_before_image_inspection(self):
        self.docker.iid_mode = 0o666
        with self.assertRaisesRegex(build.ImageBuildError, "iid_unsafe"):
            self.invoke()
        self.assertEqual([], self.commands("image"))

    def test_duplicate_image_environment_is_rejected_without_saving_values(self):
        self.docker.images["conversation-api"]["Config"]["Env"].append(
            "SOURCE_REVISION=" + REVISION
        )
        with self.assertRaises(build.ImageBuildError):
            self.invoke()
        self.assertFalse((self.root / "images/conversation-api/complete.json").exists())

    def test_completed_receipt_and_iid_modes_are_checked_before_reuse(self):
        self.invoke()
        self.docker.calls.clear()
        (self.root / "images/conversation-api/complete.json").chmod(0o644)
        with self.assertRaises(build.ImageBuildError):
            self.invoke()
        self.assertEqual([], self.docker.calls)


class ImageBuildRunnerTests(unittest.TestCase):
    def test_binary_output_and_nonzero_exit_keep_completed_process_contract(self):
        argv = [
            sys.executable,
            "-c",
            "import os; os.write(1, bytes([0,255,10])); "
            "os.write(2, bytes([128,0])); raise SystemExit(7)",
        ]
        result = build._run(argv, timeout=2, environment={})
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertIs(result.args, argv)
        self.assertEqual(7, result.returncode)
        self.assertEqual(b"\x00\xff\n", result.stdout)
        self.assertEqual(b"\x80\x00", result.stderr)

    def test_pipe_cap_counts_stdout_and_stderr_together(self):
        argv = [
            sys.executable,
            "-c",
            "import os; os.write(1,b'abcd'); os.write(2,b'efgh')",
        ]
        with mock.patch.object(build, "MAX_OUTPUT_BYTES", 8):
            result = build._run(argv, timeout=2, environment={})
        self.assertEqual((b"abcd", b"efgh"), (result.stdout, result.stderr))
        with (
            mock.patch.object(build, "MAX_OUTPUT_BYTES", 7),
            self.assertRaisesRegex(build.ImageBuildError, "^image_build_output_limit$"),
        ):
            build._run(argv, timeout=2, environment={})

    def test_eof_before_leader_exit_maps_final_wait_timeout(self):
        original_popen = subprocess.Popen
        created = []

        def spawn(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            process.wait = mock.Mock(wraps=process.wait)
            created.append(process)
            return process

        with (
            mock.patch.object(build.subprocess, "Popen", side_effect=spawn),
            self.assertRaisesRegex(
                build.ImageBuildError, "^image_build_command_timeout$"
            ),
        ):
            build._run(
                [
                    sys.executable,
                    "-c",
                    "import os,time; os.close(1); os.close(2); time.sleep(10)",
                ],
                timeout=1,
                environment={},
            )
        self.assertEqual(1, len(created))
        process = created[0]
        # A capture-loop timeout would reach only the five-second cleanup wait.
        self.assertLessEqual(process.wait.call_args_list[0].kwargs["timeout"], 1)
        self.assertIsNotNone(process.returncode)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_actual_harmless_subprocess_has_private_umask_and_no_global_env_mutation(
        self,
    ):
        before = dict(os.environ)
        result = build._run(
            [sys.executable, "-c", "import os; print(oct(os.umask(0)))"],
            timeout=2,
            environment={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(b"0o77\n", result.stdout)
        self.assertEqual(before, dict(os.environ))

    def test_live_output_limit_and_timeout_terminate_process_group(self):
        with mock.patch.object(build, "MAX_OUTPUT_BYTES", 4096):
            with self.assertRaisesRegex(build.ImageBuildError, "output_limit"):
                build._run(
                    [sys.executable, "-c", "import os; os.write(2,b'x'*65536)"],
                    timeout=2,
                    environment={},
                )
        with self.assertRaisesRegex(build.ImageBuildError, "command_timeout"):
            build._run(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout=0.1,
                environment={},
            )

    def test_exited_parent_with_child_retaining_pipe_is_killed_as_a_group(self):
        program = "import os,time; child=os.fork(); time.sleep(10) if child==0 else os._exit(0)"
        with mock.patch.object(build.os, "killpg", wraps=os.killpg) as kill:
            with self.assertRaisesRegex(build.ImageBuildError, "command_timeout"):
                build._run([sys.executable, "-c", program], timeout=0.1, environment={})
        self.assertEqual(1, kill.call_count)

    def test_injected_command_output_and_count_budgets_are_checked(self):
        def fake(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, b"x" * 20, b"")

        docker = build._Docker(Path("/private-client"), fake)
        with mock.patch.object(build, "MAX_OUTPUT_BYTES", 10):
            with self.assertRaisesRegex(build.ImageBuildError, "output_limit"):
                docker.call(["info"], timeout=1)
        docker.calls = build.MAX_COMMANDS
        with self.assertRaisesRegex(build.ImageBuildError, "command_budget"):
            docker.call(["info"], timeout=1)

    def test_total_output_and_expired_wall_budgets_do_not_accept_success(self):
        def fake(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, b"123456", b"")

        docker = build._Docker(Path("/private-client"), fake)
        with mock.patch.object(build, "MAX_TOTAL_OUTPUT_BYTES", 10):
            self.assertEqual(b"123456", docker.call(["info"], timeout=1))
            with self.assertRaisesRegex(build.ImageBuildError, "output_limit"):
                docker.call(["info"], timeout=1)
        docker.deadline = 0
        with self.assertRaisesRegex(build.ImageBuildError, "command_budget"):
            docker.call(["info"], timeout=1)

    def test_invalid_or_nonfinite_inspection_json_is_not_accepted(self):
        for value in (b'{"Id":1,"Id":2}', b'{"Id":NaN}', b"[]", b"not json"):
            with self.subTest(value=value[:10]):

                def fake(argv, **kwargs):
                    return subprocess.CompletedProcess(argv, 0, value, b"")

                docker = build._Docker(Path("/private-client"), fake)
                with self.assertRaises(build.ImageBuildError):
                    docker.inspect("sha256:" + "1" * 64)


if __name__ == "__main__":
    unittest.main()
