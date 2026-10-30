"""Exercise frozen inputs and durable failure guards without a daemon or Docker."""

from copy import deepcopy
import hashlib
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from search.operations.jobs import execution, pueue, queue
from search.operations.shared import private_io
from search.quality.contract import plan_digest
from tests.unit.quality.fixtures import capture, turn
from tests.unit.quality.test_batches import encoded, make_record, rewrite


class FakePueue:
    def __init__(self):
        self.tasks = {}
        self.calls = []
        self.group_status = "Running"
        self.captured_extras = {}

    def state(self):
        return deepcopy(
            {
                "tasks": self.tasks,
                "groups": {
                    "default": {"status": "Running", "parallel_tasks": 1},
                    "heavy": {"status": self.group_status, "parallel_tasks": 1},
                },
            }
        )

    def call(self, *arguments):
        self.calls.append(arguments)
        if arguments[0] == "add":
            number = str(len(self.tasks))
            self.tasks[number] = {
                "command": arguments[-1],
                "label": arguments[arguments.index("--label") + 1],
                "path": arguments[arguments.index("--working-directory") + 1],
                "group": "heavy",
                "envs": {**pueue.ENVIRONMENT, **self.captured_extras},
                "status": {"Stashed": {}},
            }
            return (number + "\n").encode()
        if arguments[0] == "enqueue":
            self.tasks[arguments[1]]["status"] = {"Queued": {}}
        elif arguments[0] == "remove":
            if next(iter(self.tasks[arguments[1]]["status"])) not in {
                "Queued",
                "Stashed",
            }:
                raise pueue.JobError("job_queue_command_failed")
            del self.tasks[arguments[1]]
        elif arguments[:2] == ("env", "unset"):
            self.tasks[arguments[2]]["envs"].pop(arguments[-1])
        else:
            raise AssertionError(arguments)
        return b""

    seal_environment = pueue.Pueue.seal_environment


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)
        self.root = self.directory / "queue"
        self.source = self.directory / "source"
        self.state_root = self.directory / "profiles"
        for path in (self.root, self.source, self.source / "scripts", self.state_root):
            path.mkdir(mode=0o700)
        self.plan = {"catalog_manifest_sha256": "b" * 64, "resources": []}
        queue.write(self.state_root / "plan.json", self.plan)
        self.profile = {
            "type": "resource-pass",
            "bundle": str(self.directory / "bundle"),
            "state_root": str(self.state_root),
            "plan_sha256": queue.digest(self.plan),
            "manifest_sha256": "b" * 64,
        }
        self.registry = {
            "schema_version": 1,
            "root": str(self.root),
            "runtime_root": str(self.directory),
            "pueue": str(self.directory / "pueue"),
            "operator_source": str(self.source),
            "operator_revision": "a" * 40,
            "machine_id_sha256": "c" * 64,
            "profiles": {"prepared": self.profile},
        }
        self.report = {
            "schema_version": 1,
            "status": "checkpoint",
            "plan_sha256": self.profile["plan_sha256"],
            "catalog_manifest_sha256": "b" * 64,
            "selected_count": 30,
            "pending_count": 10,
            "profile_count": 20,
            "failed_count": 0,
            "retryable_count": 0,
            "attempted_count": 20,
        }
        self.program = self.source / "scripts/depositarai.py"
        self.program.write_text(
            "import json,sys\nprint("
            + repr(json.dumps(self.report))
            + ")\nsys.exit(2)\n"
        )
        self.transport = FakePueue()
        self.git_directories = {}
        self.command = queue.command
        self.addCleanup(patch.stopall)
        patch.object(queue, "Pueue", return_value=self.transport).start()
        patch.object(queue, "ROOT", self.source).start()
        patch.object(queue, "host_identity", return_value="c" * 64).start()
        patch.object(queue, "revision", return_value="a" * 40).start()
        patch.object(
            queue, "common_directory", return_value=self.directory / "common.git"
        ).start()
        patch.object(queue, "command", side_effect=self.dispatch).start()

    def dispatch(self, argv, **options):
        if argv[0] == "git":
            if "ls-tree" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    b"100644 blob " + b"d" * 40 + b"\tscripts/depositarai.py\0",
                    b"",
                )
            self.assertIn("--lock", argv)
            frozen = Path(argv[-2])
            shutil.copytree(self.source, frozen)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return self.command(argv, **options)

    def submit(self):
        return queue.submit(
            self.registry,
            {
                "schema_version": 1,
                "issue": 37,
                "profile": "prepared",
                "source": str(self.source),
                "revision": "a" * 40,
            },
        )["job_id"]

    def start(self, identifier):
        task_id = str(
            queue.read(queue.job_root(self.registry, identifier) / "queue.json")[
                "task_id"
            ]
        )
        self.transport.tasks[task_id]["status"] = {"Running": {}}

    def typed_profiles(self):
        config = self.directory / "native-config.json"
        queue.write(config, {"state_root": str(self.directory / "native-state")})
        config_sha = hashlib.sha256(config.read_bytes()).hexdigest()
        record = make_record(capture(turn(None)))
        run = json.loads(record["files"]["run.json"])
        run["operator_revision"] = "a" * 40
        run["identity"]["operator_revision"] = "a" * 40
        rewrite(record, run=run)
        cases = self.directory / "native-cases.json"
        cases.write_bytes(encoded(record["plan"]))
        cases.chmod(0o600)
        quality = {
            "type": "quality-batch",
            "config": str(config),
            "config_sha256": config_sha,
            "source_revision": "a" * 40,
            "candidate_revision": "a" * 40,
            "cases": str(cases),
            "cases_sha256": hashlib.sha256(cases.read_bytes()).hexdigest(),
            "plan_sha256": plan_digest(record["plan"]),
            "expected_identity_sha256": queue.digest(run["identity"]),
        }
        vector = {
            "type": "vector-pass",
            "config": str(config),
            "config_sha256": config_sha,
            "source_revision": "a" * 40,
            "catalog_revision": "b" * 40,
            "bundle_id": "1" * 64,
            "manifest_sha256": "b" * 64,
            "expected_inputs_sha256": "2" * 64,
        }
        report = {
            "schema_version": 1,
            "status": "checkpoint",
            "revision": "b" * 40,
            "bundle_id": "1" * 64,
            "manifest_sha256": "b" * 64,
            "active_changed": False,
            "runtime_verified": True,
            "host_cleanup_verified": True,
            "document_count": 10,
            "done_count": 4,
            "pending_count": 6,
            "completed_passes": 1,
            "passes_this_invocation": 1,
            "recipe_sha256": "3" * 64,
        }
        return quality, vector, record, report

    def submit_profile(self, name):
        return queue.submit(
            self.registry,
            {
                "schema_version": 1,
                "issue": 68,
                "profile": name,
                "source": str(self.source),
                "revision": "a" * 40,
            },
        )["job_id"]

    def test_snapshot_survives_caller_edit_and_checkpoint_is_clean(self):
        identifier = self.submit()
        self.program.write_text("raise RuntimeError('caller changed after enqueue')\n")
        self.start(identifier)
        result = queue.execute(self.registry, identifier)
        self.assertEqual(result["status"], "checkpoint")
        self.assertEqual(
            queue.read(self.root / "active.json"),
            {"job_id": identifier, "status": "clean"},
        )
        self.assertEqual(
            queue.result(self.registry, identifier)["status"], "checkpoint"
        )
        self.assertEqual(self.transport.calls[0][0:2], ("add", "--stashed"))
        self.assertEqual(
            shlex.split(self.transport.calls[0][-1]),
            queue.launcher(self.registry, identifier),
        )
        request = queue.job_root(self.registry, identifier) / "request.json"
        self.assertEqual(request.stat().st_mode & 0o777, 0o600)

    def test_completed_partial_and_large_checkpoint_are_valid(self):
        for status, code, failed, pending in (
            ("completed", 0, 0, 0),
            ("partial", 1, 1, 0),
            ("checkpoint", 2, 0, 20000),
        ):
            with self.subTest(status=status):
                value = {
                    **self.report,
                    "status": status,
                    "selected_count": 30000,
                    "pending_count": pending,
                    "profile_count": 10000,
                    "failed_count": failed,
                }
                execution.verify(
                    value,
                    code,
                    self.root,
                    {"revision": "a" * 40, "profile_definition": self.profile},
                )
        with self.assertRaisesRegex(pueue.JobError, "operation_result_unverified"):
            execution.verify(
                self.report,
                0,
                self.root,
                {"revision": "a" * 40, "profile_definition": self.profile},
            )

    def test_plan_drift_blocks_before_resource_process(self):
        identifier = self.submit()
        self.start(identifier)
        queue.write(
            self.state_root / "plan.json", {**self.plan, "changed": True}, replace=True
        )
        with self.assertRaisesRegex(pueue.JobError, "job_blocked"):
            queue.execute(self.registry, identifier)
        self.assertEqual(queue.status(self.registry, identifier)["status"], "blocked")
        self.assertFalse((self.root / "active.json").exists())

    def test_uncertain_job_blocks_next_state_and_preserves_evidence(self):
        first, second = self.submit(), self.submit()
        self.start(first)
        with patch.object(
            queue, "command", side_effect=pueue.JobError("job_command_timeout")
        ):
            with self.assertRaisesRegex(pueue.JobError, "job_recovery_required"):
                queue.execute(self.registry, first)
        first_root = queue.job_root(self.registry, first)
        receipt = queue.read(first_root / "result.json")
        self.assertEqual(receipt["log_status"], "unavailable")
        self.start(second)
        with self.assertRaisesRegex(pueue.JobError, "job_blocked"):
            queue.execute(self.registry, second)
        self.assertEqual(
            queue.read(self.root / "active.json"),
            {"job_id": first, "status": "running"},
        )
        self.assertEqual(queue.read(first_root / "result.json"), receipt)

    def test_global_lock_prevents_cross_job_overlap(self):
        identifier = self.submit()
        self.start(identifier)
        with queue.locked(self.root, "execution.lock"):
            with self.assertRaisesRegex(pueue.JobError, "job_blocked"):
                queue.execute(self.registry, identifier)
        self.assertFalse((self.root / "active.json").exists())

    def test_lock_proof_comes_from_held_fd_after_named_file_replacement(self):
        named = self.root / "proof.lock"
        retained = self.root / "retained.lock"
        with queue.locked(self.root, named.name) as proof:
            self.assertIsInstance(proof, tuple)
            self.assertEqual(proof, private_io.identity(named.lstat()))
            named.rename(retained)
            queue.write(named, {"replacement": True})
            self.assertNotEqual(proof[:2], private_io.identity(named.lstat())[:2])
            self.assertEqual(proof[:2], private_io.identity(retained.lstat())[:2])
            fd = os.open(retained, os.O_RDWR | os.O_NOFOLLOW)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_archive_intent_refuses_submit_and_execute_without_clearing_it(self):
        identifier = self.submit()
        self.start(identifier)
        intent = self.root / "archive-intent.json"
        intent.symlink_to(self.directory / "missing-intent")
        for action in (self.submit, lambda: queue.execute(self.registry, identifier)):
            with self.assertRaisesRegex(
                pueue.JobError, "job_archive_recovery_required"
            ):
                action()
            self.assertTrue(intent.is_symlink())
        self.assertFalse((self.root / "active.json").exists())

    def test_mixed_types_share_the_same_runner_and_verified_inventory(self):
        quality, vector, record, vector_report = self.typed_profiles()
        self.registry["profiles"].update(quality=quality, vector=vector)
        identifiers = [
            self.submit(),
            self.submit_profile("quality"),
            self.submit_profile("vector"),
        ]
        original = queue.read(
            queue.job_root(self.registry, identifiers[1]) / "quality-cases.json",
            raw=True,
        )
        Path(quality["cases"]).unlink()
        Path(quality["cases"]).symlink_to(self.directory / "missing-caller-cases")
        Path(quality["config"]).write_bytes(b"caller replaced config\n")

        def native(argv, **options):
            if "batch" in argv:
                destination = Path(argv[argv.index("--capture") + 1])
                destination.mkdir(mode=0o700)
                fd = private_io.open_directory(destination)
                try:
                    for name, raw in record["files"].items():
                        private_io.write_file(fd, name, raw, max_bytes=8 * 1024**2)
                finally:
                    os.close(fd)
                value = {
                    k: v
                    for k, v in json.loads(record["files"]["run.json"]).items()
                    if k != "identity"
                }
                return subprocess.CompletedProcess(
                    argv, 0, private_io.canonical(value), b""
                )
            if "build-vectors" in argv:
                self.assertEqual(
                    argv[argv.index("--expected-inputs-sha256") + 1],
                    vector["expected_inputs_sha256"],
                )
                return subprocess.CompletedProcess(
                    argv, 2, private_io.canonical(vector_report), b""
                )
            return self.command(argv, **options)

        with patch.object(queue, "command", side_effect=native):
            for identifier, expected in zip(
                identifiers, ("checkpoint", "completed", "checkpoint")
            ):
                self.start(identifier)
                result = queue.execute(self.registry, identifier)
                self.assertEqual(result["status"], expected)
                self.assertEqual(
                    queue.result(self.registry, identifier)["status"], expected
                )
                self.assertEqual(
                    queue.read(self.root / "active.json"),
                    {"job_id": identifier, "status": "clean"},
                )
                self.assertTrue(queue.verified_evidence(self.registry, identifier))
                task_id = str(
                    queue.read(
                        queue.job_root(self.registry, identifier) / "queue.json"
                    )["task_id"]
                )
                self.transport.tasks[task_id]["status"] = {
                    "Done": {"result": "Success"}
                }
        inventory = queue.verified_evidence(self.registry, identifiers[1])
        self.assertTrue(
            {
                "native-config.json",
                "quality-cases.json",
                "capture/run.json",
                "capture/capture.json",
                "capture/outcomes.json",
            }
            <= inventory.keys()
        )
        self.assertFalse(any(name.startswith("source/") for name in inventory))
        self.assertEqual(
            queue.read(
                queue.job_root(self.registry, identifiers[1]) / "quality-cases.json",
                raw=True,
            ),
            original,
        )
        operation = (
            queue.job_root(self.registry, identifiers[1]) / "capture/outcomes.json"
        )
        operation.write_bytes(b"{}\n")
        with self.assertRaises(ValueError):
            queue.verified_evidence(self.registry, identifiers[1])

    def test_typed_wrong_source_is_refused_before_freezing_or_enqueuing(self):
        quality, vector, _, _ = self.typed_profiles()
        for profile in (quality, vector):
            with self.subTest(profile=profile["type"]):
                self.registry["profiles"]["wrong-source"] = {
                    **profile,
                    "source_revision": "c" * 40,
                }
                with self.assertRaisesRegex(pueue.JobError, "profile_source_changed"):
                    self.submit_profile("wrong-source")
                self.assertEqual(self.transport.calls, [])

    def test_native_timeout_and_interrupt_keep_uncertainty_for_each_typed_job(self):
        quality, vector, _, _ = self.typed_profiles()
        for profile, error in (
            (quality, pueue.JobError("job_command_timeout")),
            (vector, KeyboardInterrupt()),
        ):
            with self.subTest(profile=profile["type"]):
                root = self.directory / ("queue-" + profile["type"])
                root.mkdir(mode=0o700)
                self.registry["root"] = str(root)
                self.registry["profiles"] = {"typed": profile}
                self.transport.tasks.clear()
                identifier = self.submit_profile("typed")
                self.start(identifier)
                with patch.object(queue, "command", side_effect=error):
                    with self.assertRaisesRegex(
                        pueue.JobError, "job_recovery_required"
                    ):
                        queue.execute(self.registry, identifier)
                self.assertEqual(
                    queue.read(root / "active.json"),
                    {"job_id": identifier, "status": "running"},
                )
                self.assertEqual(
                    queue.result(self.registry, identifier)["status"],
                    "recovery_required",
                )
                with self.assertRaisesRegex(pueue.JobError, "result_unverified"):
                    queue.verified_evidence(self.registry, identifier)

    def test_cancel_only_queued_and_never_sends_signal(self):
        first, second = self.submit(), self.submit()
        self.assertEqual(queue.cancel(self.registry, first)["status"], "cancelled")
        self.start(second)
        with self.assertRaisesRegex(pueue.JobError, "job_not_queued"):
            queue.cancel(self.registry, second)
        self.assertFalse(
            any(
                call[0] in {"kill", "pause", "restart", "reset"}
                for call in self.transport.calls
            )
        )
        self.assertEqual(queue.status(self.registry, first)["status"], "cancelled")

    def test_unknown_queue_peer_and_modified_launcher_are_refused(self):
        identifier = self.submit()
        self.transport.tasks["0"]["command"] += "; unexpected-command"
        with self.assertRaisesRegex(pueue.JobError, "task_identity_changed"):
            queue.status(self.registry, identifier)

    def test_persisted_environment_is_sealed_before_enqueue(self):
        self.transport.captured_extras = {
            "   ": "/fixture/pueue",
            "FIXTURE_EXTRA": "synthetic",
        }
        identifier = self.submit()
        self.assertEqual(self.transport.tasks["0"]["envs"], pueue.ENVIRONMENT)
        self.assertEqual(self.transport.calls[-1], ("enqueue", "0"))
        self.transport.tasks["0"]["envs"]["UNEXPECTED"] = "synthetic"
        with self.assertRaisesRegex(pueue.JobError, "task_environment_changed"):
            queue.status(self.registry, identifier)
        self.transport.tasks["0"]["label"] = "unknown-peer"
        with self.assertRaisesRegex(pueue.JobError, "queue_peer_invalid"):
            queue.status(self.registry, identifier)

    def test_other_repository_and_arbitrary_payload_are_refused(self):
        specification = {
            "schema_version": 1,
            "issue": 37,
            "profile": "prepared",
            "source": str(self.source),
            "revision": "a" * 40,
        }
        for field, value in (
            ("command", "arbitrary"),
            ("env", {}),
            ("max_resources", 300),
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(pueue.JobError, "request_invalid"),
            ):
                queue.submit(self.registry, {**specification, field: value})
        with patch.object(
            queue,
            "common_directory",
            side_effect=[self.directory / "other.git", self.directory / "trusted.git"],
        ):
            with self.assertRaisesRegex(pueue.JobError, "source_repository_changed"):
                queue.submit(self.registry, specification)

    def test_result_refuses_changed_operation_or_raw_bytes(self):
        identifier = self.submit()
        self.start(identifier)
        queue.execute(self.registry, identifier)
        root = queue.job_root(self.registry, identifier)
        encoded = (root / "stdout.bin").read_bytes()
        (root / "stdout.bin").write_bytes(encoded.replace(b"checkpoint", b"unexpected"))
        with self.assertRaisesRegex(pueue.JobError, "artifact_changed"):
            queue.result(self.registry, identifier)

    def test_empty_stream_cannot_gain_an_unrecorded_file(self):
        identifier = self.submit()
        self.start(identifier)
        queue.execute(self.registry, identifier)
        root = queue.job_root(self.registry, identifier)
        self.assertFalse((root / "stderr.bin").exists())
        (root / "stderr.bin").write_bytes(b"unexpected later bytes")
        with self.assertRaisesRegex(pueue.JobError, "artifact_changed"):
            queue.result(self.registry, identifier)

    def test_profile_registry_edits_do_not_rewrite_accepted_jobs(self):
        identifier = self.submit()
        self.registry["profiles"]["prepared"] = {
            **self.profile,
            "plan_sha256": "f" * 64,
        }
        self.registry["profiles"]["future"] = self.profile
        self.start(identifier)
        self.assertEqual(
            queue.execute(self.registry, identifier)["status"], "checkpoint"
        )
        root = queue.job_root(self.registry, identifier)
        self.assertEqual(
            queue.read(root / "request.json")["profile_definition"], self.profile
        )

    def test_unresolved_active_intent_overrides_completed_summary(self):
        identifier = self.submit()
        self.start(identifier)
        queue.execute(self.registry, identifier)
        queue.write(
            self.root / "active.json",
            {"job_id": identifier, "status": "running"},
            replace=True,
        )
        self.assertEqual(
            queue.status(self.registry, identifier)["status"], "recovery_required"
        )

    def test_pending_result_and_paused_queue_are_visible(self):
        identifier = self.submit()
        self.transport.group_status = "Paused"
        result = queue.result(self.registry, identifier)
        self.assertEqual(result["status"], "queued")
        self.assertTrue(result["queue_paused"])

    def test_registry_rejects_unknown_profiles_and_host_drift(self):
        filename = self.directory / "registry.json"
        queue.write(filename, self.registry)
        self.assertEqual(queue.load_registry(filename), self.registry)
        for mutation in (
            {"type": "quality"},
            {"type": "vectors"},
            {"argv": ["unexpected"]},
        ):
            changed = deepcopy(self.registry)
            changed["profiles"]["prepared"].update(mutation)
            queue.write(filename, changed, replace=True)
            with self.assertRaisesRegex(pueue.JobError, "profile_invalid"):
                queue.load_registry(filename)
        queue.write(
            filename, {**self.registry, "machine_id_sha256": "0" * 64}, replace=True
        )
        with self.assertRaisesRegex(pueue.JobError, "host_changed"):
            queue.load_registry(filename)

    def test_symlink_and_public_request_are_rejected(self):
        original = self.directory / "original.json"
        queue.write(original, {"fixture": True})
        linked = self.directory / "linked.json"
        linked.symlink_to(original)
        with self.assertRaises(pueue.JobError):
            queue.read(linked)
        original.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private_io_file_unsafe"):
            queue.read(original)


class TransportTests(unittest.TestCase):
    def test_client_uses_only_fixed_configuration_and_pinned_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            binary = root / "pueue"
            binary.write_bytes(b"synthetic executable identity")
            binary.chmod(0o700)
            registry = {
                "root": str(root),
                "runtime_root": str(root),
                "pueue": str(binary),
            }
            (root / "pueue.yml").write_bytes(pueue.configuration(registry))
            (root / "pueue.yml").chmod(0o600)
            (root / "aliases.yml").write_bytes(b"{}\n")
            (root / "aliases.yml").chmod(0o600)
            result = subprocess.CompletedProcess([], 0, b"fixture", b"")
            with (
                patch.object(
                    pueue,
                    "BINARY_SHA256",
                    hashlib.sha256(binary.read_bytes()).hexdigest(),
                ),
                patch.object(pueue, "command", return_value=result) as execute,
            ):
                self.assertEqual(
                    pueue.Pueue(registry).call("status", "--json"), b"fixture"
                )
                self.assertEqual(
                    execute.call_args.args[0],
                    [binary, "--config", root / "pueue.yml", "status", "--json"],
                )
                (root / "aliases.yml").write_bytes(b"fixture: unexpected\n")
                with self.assertRaisesRegex(pueue.JobError, "aliases_changed"):
                    pueue.Pueue(registry).call("status", "--json")
                self.assertEqual(execute.call_count, 1)

    def test_environment_is_minimal_and_output_bounded(self):
        with patch.dict(os.environ, {"JOB_FIXTURE_SECRET": "fixture-only"}):
            result = pueue.command(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    "import json,os;print(json.dumps(sorted(os.environ)))",
                ]
            )
        self.assertEqual(json.loads(result.stdout), sorted(pueue.ENVIRONMENT))
        with (
            patch.object(pueue, "MAX_OUTPUT", 64),
            self.assertRaisesRegex(pueue.JobError, "output_limit"),
        ):
            pueue.command([sys.executable, "-I", "-S", "-B", "-c", "print('x'*1000)"])

    def test_closed_pipes_timeout_stays_a_fixed_error(self):
        with self.assertRaisesRegex(pueue.JobError, "job_command_failed"):
            pueue.command(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    "-c",
                    "import os,time;os.close(1);os.close(2);time.sleep(3)",
                ],
                timeout=0.1,
            )

    def test_no_hidden_shell_hooks_or_tcp_in_generated_configuration(self):
        encoded = pueue.configuration(
            {"root": "/private/queue", "runtime_root": "/private/runtime"}
        ).decode()
        self.assertIn("use_unix_socket: true", encoded)
        self.assertIn("callback: null", encoded)
        self.assertIn("env_vars: {}", encoded)
        self.assertNotIn("host:", encoded)

    def test_parallelism_and_foreign_groups_are_rejected(self):
        value = {
            "tasks": {},
            "groups": {
                "default": {"status": "Running", "parallel_tasks": 1},
                "heavy": {"status": "Running", "parallel_tasks": 1},
            },
        }
        transport = pueue.Pueue({})
        for concurrency in (0, 2, True):
            changed = deepcopy(value)
            changed["groups"]["heavy"]["parallel_tasks"] = concurrency
            with patch.object(
                transport, "call", return_value=private_io.canonical(changed)
            ):
                with self.assertRaisesRegex(
                    pueue.JobError, "queue_parallelism_invalid"
                ):
                    transport.state()
        changed = deepcopy(value)
        changed["groups"]["another-queue"] = {"status": "Running", "parallel_tasks": 1}
        with patch.object(
            transport, "call", return_value=private_io.canonical(changed)
        ):
            with self.assertRaisesRegex(pueue.JobError, "queue_state_invalid"):
                transport.state()


if __name__ == "__main__":
    unittest.main()
