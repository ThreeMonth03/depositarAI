"""Offline proofs for isolated startup and non-retrying catalog administration."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from search.catalog.bundle import CatalogBundle
from search.catalog.errors import BundleError
from search.catalog import errors as catalog
from search.operations.catalog import backend, commands, execution, runtime
from search.operations.catalog.administration import _admin_failure_diagnostic
from search.operations.catalog.policy import (
    _duration,
    _memory_bytes,
    catalog_bundle_target,
    validate_core_containers,
    validate_runtime,
)
from tests.unit.catalog import test_backend as backend_fixture

REVISION, ROOT = backend_fixture.REVISION, backend_fixture.ROOT


class CatalogRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = backend_fixture.CatalogBackendTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

        def compose_normalized_values(compose):
            for name, service in compose["services"].items():
                for field in ("mem_limit", "memswap_limit"):
                    service[field] = str(service[field])
                service["healthcheck"].update(
                    interval="20s",
                    timeout="10s" if name == "ollama-embedding" else "5s",
                    start_period={
                        "fess-search": "2m0s",
                        "opensearch-search": "1m30s",
                        "ollama-embedding": "30s",
                    }[name],
                )

        self.fixture.compose_mutation = compose_normalized_values
        self.plan = self.fixture.prepare()
        self.source_revision = REVISION
        self.root = Path(self.plan["work_root"])
        self.compose = json.loads(Path(self.plan["compose_file"]).read_text())
        self.calls = []
        self.images = {}
        self.containers = []
        self.network = None
        self.helpers = {}
        self.helper_bundle_loads = []
        self.helper_exit = 0
        self.helper_report_mutation = None
        self.helper_outcome_mutation = None
        self.startup_mutation = None
        self.image_metadata_mutation = None
        self.helper_mutation = None
        self.helper_timeout = False
        self.cleanup_timeout = False
        self.after_build_mutation = None
        self.build_failure_service = None
        reference = self.compose["services"]["ollama-embedding"]["image"]
        self.images[reference] = {
            "Id": "sha256:" + "4" * 64,
            "RepoDigests": [
                reference.split(":", 1)[0] + "@" + reference.split("@", 1)[1]
            ],
            "Config": {"Labels": {}, "Cmd": None, "Entrypoint": None},
        }
        self.images[self.images[reference]["Id"]] = self.images[reference]

    def completed(self, argv, value="", *, code=0, stderr=""):
        raw = value if type(value) is str else json.dumps(value)
        return subprocess.CompletedProcess(argv, code, raw, stderr)

    def runner(self, argv, *, timeout):
        self.calls.append((argv, timeout))
        if argv[0] == "git":
            if argv[-2:] == ["rev-parse", "HEAD"]:
                return self.completed(argv, self.source_revision + "\n")
            if "status" in argv:
                return self.completed(argv)
            return self.fixture.runner(argv, timeout=timeout)
        self.assertEqual("docker", argv[0])
        if argv[1] == "build":
            tag = argv[argv.index("--tag") + 1]
            service = next(
                name
                for name in (*runtime.SERVICES, "catalog-admin")
                if tag.endswith("-" + name + ":1")
            )
            if service == self.build_failure_service:
                return self.completed(argv, code=1, stderr="private build failure")
            number = {
                "fess-search": "1",
                "opensearch-search": "2",
                "catalog-admin": "3",
            }[service]
            image_id = "sha256:" + number * 64
            iid = Path(argv[argv.index("--iidfile") + 1])
            iid.unlink()  # Both real Docker builders remove the old iidfile first.
            descriptor = os.open(iid, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            os.fchmod(descriptor, 0o644)  # Observed builder mode, independent of umask.
            with os.fdopen(descriptor, "w") as stream:
                stream.write(image_id)  # Actual Docker IID is 71 bytes without LF.
            labels = dict(
                argv[number + 1].split("=", 1)
                for number, argument in enumerate(argv)
                if argument == "--label"
            )
            image = {
                "Id": image_id,
                "Config": {"Labels": labels, "Cmd": None, "Entrypoint": None},
            }
            if service == "catalog-admin":
                image["Config"]["Entrypoint"] = [
                    "python3",
                    "-E",
                    "-s",
                    "-B",
                    "-u",
                    "-m",
                    "search.catalog_admin",
                ]
                image["Config"]["WorkingDir"] = "/opt/depositar-search"
            self.images[tag] = self.images[image_id] = image
            if service == "fess-search" and self.after_build_mutation:
                self.after_build_mutation()
            return self.completed(argv)
        if argv[1:3] == ["image", "inspect"]:
            if argv[-1] not in self.images:
                return self.completed(argv, code=1, stderr="image unavailable")
            image = copy.deepcopy(self.images[argv[-1]])
            if self.image_metadata_mutation:
                self.image_metadata_mutation(image)
            return self.completed(argv, [image])
        if argv[1] == "compose":
            if "config" in argv:
                normalized = copy.deepcopy(self.compose)
                overlay = json.loads((self.root / "image-lock.json").read_text())
                for name, entry in overlay["services"].items():
                    normalized["services"][name].update(entry)
                return self.completed(argv, normalized)
            self.assertIn("up", argv)
            self.assertIn("--no-build", argv)
            self.assertEqual("never", argv[argv.index("--pull") + 1])
            self.assertEqual(660, timeout)
            self.create_services()
            if self.startup_mutation:
                self.startup_mutation()
            return self.completed(argv)
        if argv[1] == "ps":
            label = argv[-1]
            ids = (
                list(self.helpers)
                if runtime.LABEL_PREFIX + "admin-project" in label
                else [row["Id"] for row in self.containers]
            )
            return self.completed(argv, "\n".join(ids))
        if argv[1] == "inspect":
            rows = {row["Id"]: row for row in self.containers}
            rows.update(self.helpers)
            return self.completed(argv, [rows[value] for value in argv[2:]])
        if argv[1:3] == ["network", "inspect"]:
            return self.completed(argv, [self.network])
        if argv[1] == "create":
            self.create_helper(argv)
            if self.helper_mutation:
                self.helper_mutation(self.helpers["d" * 64])
            return self.completed(argv, "d" * 64 + "\n")
        if argv[1:3] == ["start", "--attach"]:
            row = self.helpers[argv[-1]]
            if self.helper_timeout:
                row["State"] = {"Status": "running", "Running": True}
                raise subprocess.TimeoutExpired(argv, timeout)
            row["State"] = {
                "Status": "exited",
                "Running": False,
                "ExitCode": self.helper_exit,
            }
            operation = row["Config"]["Labels"][
                runtime.LABEL_PREFIX + "admin-operation"
            ]
            self.assertEqual(840 if operation == "import" else 420, timeout)
            report = {
                "schema_version": 1,
                "status": "imported" if operation == "import" else "reconciled-active",
                "bundle_id": self.fixture.bundle["bundle_id"],
                "manifest_sha256": self.plan["manifest_sha256"],
                "document_count": 1,
                "batch_count": 1 if operation == "import" else 0,
                "elapsed_seconds": 0.5,
                "inventory_verified": True,
            }
            if self.helper_report_mutation:
                self.helper_report_mutation(report)
            outcome = self.completed(
                argv,
                json.dumps(report) + "\n",
                code=self.helper_exit,
                stderr="private failure" if self.helper_exit else "",
            )
            if self.helper_outcome_mutation:
                self.helper_outcome_mutation(outcome)
            return outcome
        if argv[1] == "rm":
            if self.cleanup_timeout:
                raise subprocess.TimeoutExpired(argv, timeout)
            self.assertEqual(3, len(argv))
            del self.helpers[argv[2]]
            return self.completed(argv)
        self.fail("Unexpected command: " + repr(argv))

    def create_services(self):
        ids = {
            "fess-search": "a" * 64,
            "opensearch-search": "b" * 64,
            "ollama-embedding": "c" * 64,
        }
        network = self.plan["project"] + "_search_backend"
        self.containers = []
        for name, expected in self.compose["services"].items():
            image_id = {
                "fess-search": "1",
                "opensearch-search": "2",
                "ollama-embedding": "4",
            }[name]
            mounts = [
                {
                    "Type": entry["type"],
                    "Source": entry["source"],
                    "Destination": entry["target"],
                    "RW": not entry.get("read_only", False),
                }
                for entry in expected["volumes"]
            ]
            mounts.extend(
                {
                    "Type": "bind",
                    "Source": self.compose["secrets"][entry["source"]]["file"],
                    "Destination": "/run/secrets/" + entry["target"],
                    "RW": False,
                }
                for entry in expected.get("secrets", [])
            )
            row = {
                "Id": ids[name],
                "Image": "sha256:" + image_id * 64,
                "Config": {
                    "User": expected["user"],
                    "Env": [
                        key + "=" + str(value)
                        for key, value in expected["environment"].items()
                    ],
                    "Labels": {
                        "com.docker.compose.service": name,
                        "com.docker.compose.project": self.plan["project"],
                        "com.docker.compose.oneoff": "False",
                    },
                    "Cmd": None,
                    "Entrypoint": None,
                    "Healthcheck": {
                        "Test": expected["healthcheck"]["test"],
                        "Retries": expected["healthcheck"]["retries"],
                        # Docker inspection values are independent literal ns;
                        # reusing the parser here previously hid serialization gaps.
                        "Interval": 20_000_000_000,
                        "Timeout": 10_000_000_000
                        if name == "ollama-embedding"
                        else 5_000_000_000,
                        "StartPeriod": {
                            "fess-search": 120_000_000_000,
                            "opensearch-search": 90_000_000_000,
                            "ollama-embedding": 30_000_000_000,
                        }[name],
                        "StartInterval": 0,
                    },
                },
                "HostConfig": {
                    "Runtime": "runc",
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "Init": True,
                    "NanoCpus": int(expected["cpus"] * 10**9),
                    "Memory": int(expected["mem_limit"]),
                    "MemorySwap": int(expected["memswap_limit"]),
                    "PidsLimit": expected["pids_limit"],
                    "RestartPolicy": {"Name": expected["restart"]},
                    "LogConfig": {
                        "Type": expected["logging"]["driver"],
                        "Config": expected["logging"]["options"],
                    },
                    "Tmpfs": {
                        entry.split(":", 1)[0]: entry.split(":", 1)[1]
                        for entry in expected["tmpfs"]
                    },
                    "Ulimits": [
                        {"Name": key, "Soft": entry["soft"], "Hard": entry["hard"]}
                        for key, entry in expected.get("ulimits", {}).items()
                    ],
                    "NetworkMode": "container:" + ids["fess-search"]
                    if name == "ollama-embedding"
                    else network,
                },
                "Mounts": mounts,
                "State": {"Running": True, "Health": {"Status": "healthy"}},
                "NetworkSettings": {
                    "Networks": {} if name == "ollama-embedding" else {network: {}}
                },
            }
            self.containers.append(row)
        self.network = {
            "Id": "e" * 64,
            "Name": network,
            "Internal": True,
            "Attachable": False,
            "Containers": {
                ids[name]: {} for name in ("fess-search", "opensearch-search")
            },
            "Labels": {"com.docker.compose.project": self.plan["project"]},
        }

    def create_helper(self, argv):
        labels = dict(
            argv[index + 1].split("=", 1)
            for index, argument in enumerate(argv)
            if argument == "--label"
        )
        image = next(
            argument for argument in argv if runtime.IMAGE_ID.fullmatch(argument)
        )
        mounts = []
        for index, argument in enumerate(argv):
            if argument == "--mount":
                fields = dict(
                    entry.split("=", 1)
                    for entry in argv[index + 1].split(",")
                    if "=" in entry
                )
                mounts.append(
                    {
                        "Type": fields["type"],
                        "Source": fields["src"],
                        "Destination": fields["dst"],
                        "RW": False,
                    }
                )
        command = argv[argv.index(image) + 1 :]
        bundle_target = Path(command[command.index("--bundle") + 1])
        bundle_mount = next(
            mount for mount in mounts if mount["Destination"] == str(bundle_target)
        )
        helper_roots = self.fixture.root / "helper-roots"
        helper_roots.mkdir(mode=0o700, exist_ok=True)
        container_root = helper_roots / labels[runtime.LABEL_PREFIX + "admin-run"]
        container_root.mkdir(mode=0o700)
        mounted_bundle = container_root / bundle_target.relative_to("/")
        mounted_bundle.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copytree(bundle_mount["Source"], mounted_bundle)
        # Model the actual mount/argv together and run the real loader before
        # returning any canned import result. /catalog alone must not pass.
        loaded = CatalogBundle.load(
            mounted_bundle,
            expected_manifest_sha256=command[command.index("--manifest-sha256") + 1],
        )
        self.helper_bundle_loads.append((bundle_target, bundle_mount, loaded))
        self.helpers["d" * 64] = {
            "Id": "d" * 64,
            "Image": image,
            "Config": {
                "User": f"{os.geteuid()}:{os.getegid()}",
                "Env": ["NVIDIA_VISIBLE_DEVICES=void", "CUDA_VISIBLE_DEVICES="],
                "Labels": labels,
                "Entrypoint": [
                    "python3",
                    "-E",
                    "-s",
                    "-B",
                    "-u",
                    "-m",
                    "search.catalog_admin",
                ],
                "WorkingDir": "/opt/depositar-search",
                "Cmd": command,
            },
            "HostConfig": {
                "Runtime": "runc",
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Init": True,
                "NanoCpus": 10**9,
                "Memory": 1024**3,
                "MemorySwap": 1024**3,
                "PidsLimit": 64,
                "RestartPolicy": {"Name": "no"},
                "LogConfig": {"Type": "none"},
                "Ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 1024}],
                "Tmpfs": {"/tmp": argv[argv.index("--tmpfs") + 1].split(":", 1)[1]},
                "NetworkMode": argv[argv.index("--network") + 1],
            },
            "Mounts": mounts,
            "State": {"Status": "created", "Running": False},
            "NetworkSettings": {"Networks": {}},
        }

    def prebuilt_receipt(self):
        images = {}
        for name, number in (("opensearch-search", "2"), ("fess-search", "1")):
            sources = {
                path: self.plan["source_sha256"][path]
                for path in execution.service_sources(name)
            }
            base = next(
                value
                for key, value in self.compose["services"][name]["build"][
                    "args"
                ].items()
                if key.endswith("BASE_IMAGE")
            )
            labels = {
                "org.opencontainers.image.revision": REVISION,
                runtime.LABEL_PREFIX + "project": self.plan["project"],
                runtime.LABEL_PREFIX + "revision": REVISION,
                runtime.LABEL_PREFIX + "service": name,
                runtime.LABEL_PREFIX + "source-sha256": execution.digest_record(
                    sources
                ),
                runtime.LABEL_PREFIX + "base": base,
            }
            image_id = "sha256:" + number * 64
            reference = self.plan["project"] + "-" + name + ":1"
            self.images[reference] = self.images[image_id] = {
                "Id": image_id,
                "Config": {"Labels": labels, "Cmd": None, "Entrypoint": None},
            }
            images[name] = {
                "image_id": image_id,
                "reference": reference,
                "labels": copy.deepcopy(labels),
                "sources": sources,
            }
        reference = self.compose["services"]["ollama-embedding"]["image"]
        images["ollama-embedding"] = {
            "image_id": self.images[reference]["Id"],
            "reference": reference,
            "labels": {},
            "sources": {},
        }
        return {
            "schema_version": 1,
            "project": self.plan["project"],
            "revision": REVISION,
            "images": images,
        }

    def start(self, *, prebuilt_images=None):
        return runtime.start(
            self.fixture.config,
            self.plan,
            repo_root=ROOT,
            runner=self.runner,
            prebuilt_images=prebuilt_images,
        )

    def stage_images(self):
        return runtime.stage_images(
            self.fixture.config,
            self.plan,
            repo_root=ROOT,
            runner=self.runner,
        )

    def admin(self, operation):
        return runtime.admin(
            self.fixture.config,
            self.plan,
            repo_root=ROOT,
            operation=operation,
            runner=self.runner,
        )

    def test_cross_commit_context_and_start_keep_origin_runtime_and_admin_receipts(
        self,
    ):
        receipt = self.start()
        self.admin("import")
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        self.source_revision = "b" * 40
        context = execution.load_context(
            self.fixture.config, self.plan, repo_root=ROOT, runner=self.runner
        )
        self.assertEqual(REVISION, context["plan"]["revision"])
        self.assertEqual(self.source_revision, context["source_revision"])
        previous_calls = len(self.calls)
        self.assertEqual(receipt, self.start())
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertFalse(
            any(
                argv[0] == "docker"
                and (
                    argv[1] in {"build", "pull", "create", "start", "rm"}
                    or "up" in argv
                )
                for argv, _ in self.calls[previous_calls:]
            )
        )
        self.assertEqual(1, len(self.helper_bundle_loads))
        self.assertEqual(1, sum("up" in argv for argv, _ in self.calls))

    def test_cross_commit_runtime_reuse_rejects_actual_resource_and_peer_drift(self):
        self.start()
        self.source_revision = "b" * 40
        containers, network = (
            copy.deepcopy(self.containers),
            copy.deepcopy(self.network),
        )
        for drift in ("memory", "peer"):
            with self.subTest(drift=drift):
                self.containers, self.network = (
                    copy.deepcopy(containers),
                    copy.deepcopy(network),
                )
                if drift == "memory":
                    self.containers[0]["HostConfig"]["Memory"] += 1
                else:
                    self.network["Containers"]["f" * 64] = {
                        "IPv4Address": "172.19.0.15/16"
                    }
                with self.assertRaisesRegex(
                    runtime.RuntimeError,
                    "catalog_runtime_(resource_policy|network_peers)_changed",
                ):
                    self.start()
        self.assertEqual(1, sum("up" in argv for argv, _ in self.calls))

    def test_cross_commit_context_rejects_source_drift_during_sealed_plan_load(self):
        self.start()
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        with patch.object(
            execution, "require_clean_revision", side_effect=["b" * 40, "c" * 40]
        ):
            with self.assertRaisesRegex(
                execution.RuntimeError, "source_revision_changed"
            ):
                execution.load_context(
                    self.fixture.config, self.plan, repo_root=ROOT, runner=self.runner
                )
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(1, sum("up" in argv for argv, _ in self.calls))

    def test_cross_commit_recipe_change_cannot_reuse_existing_runtime(self):
        self.start()
        self.source_revision = "b" * 40
        relative = "deploy/config/fess/scheduled_job.bulk"
        self.fixture.original_sources[(REVISION, relative)] = (
            ROOT / relative
        ).read_text() + "\n# Different catalog recipe\n"
        with self.assertRaisesRegex(backend.BackendError, "recipe changed"):
            self.start()
        self.assertEqual(1, sum("up" in argv for argv, _ in self.calls))

    def test_default_context_retains_runner_for_later_runtime_observations(self):
        self.start()
        runner = self.runner
        with patch.object(commands, "default_runner", return_value=runner) as factory:
            context = execution.load_context(
                self.fixture.config, self.plan, repo_root=ROOT
            )
        factory.assert_called_once_with(ROOT)
        self.assertIs(context["runner"], runner)
        with execution.operation_lock(context):
            observed, _images = execution.require_runtime(context)
        self.assertEqual("running", observed["status"])

    def test_start_and_import_share_default_context_runner(self):
        runner = self.runner
        with patch.object(commands, "default_runner", return_value=runner) as factory:
            started = runtime.start(self.fixture.config, self.plan, repo_root=ROOT)
            imported = runtime.admin(
                self.fixture.config, self.plan, repo_root=ROOT, operation="import"
            )
        self.assertEqual(2, factory.call_count)
        self.assertEqual("running", started["status"])
        self.assertEqual("imported", imported["result"]["status"])

    def test_noncallable_context_runner_rejected_before_source_or_docker(self):
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "catalog_runner_invalid"):
            execution.load_context(
                self.fixture.config, self.plan, repo_root=ROOT, runner=False
            )
        self.assertEqual([], self.calls)

    def test_falsey_callable_context_runner_is_never_replaced(self):
        self.start()
        owner = self

        class FalseyRunner:
            def __bool__(self):
                return False

            def __call__(self, argv, *, timeout):
                return owner.runner(argv, timeout=timeout)

        runner = FalseyRunner()
        with patch.object(
            commands, "default_runner", side_effect=AssertionError("fallback")
        ):
            context = execution.load_context(
                self.fixture.config, self.plan, repo_root=ROOT, runner=runner
            )
            with execution.operation_lock(context):
                observed, _images = execution.require_runtime(context)
        self.assertIs(context["runner"], runner)
        self.assertEqual("running", observed["status"])

    def test_start_proves_three_cpu_services_and_no_active_switch(self):
        receipt = self.start()
        self.assertEqual("running", receipt["status"])
        self.assertFalse(receipt["active_changed"])
        self.assertEqual(runtime.SERVICES, set(receipt["image_ids"]))
        self.assertFalse((self.fixture.config.state_root / "active.json").exists())
        for row in self.containers:
            name = row["Config"]["Labels"]["com.docker.compose.service"]
            for actual, expected in (
                ("Memory", "mem_limit"),
                ("MemorySwap", "memswap_limit"),
            ):
                self.assertIs(type(self.compose["services"][name][expected]), str)
                self.assertIs(type(row["HostConfig"][actual]), int)
                self.assertEqual(
                    int(self.compose["services"][name][expected]),
                    row["HostConfig"][actual],
                )
        self.assertEqual(receipt, self.start())
        self.assertEqual(1, sum("up" in argv for argv, _ in self.calls))

    def test_prebuilt_start_and_reuse_never_build_or_pull(self):
        prebuilt = self.prebuilt_receipt()
        original = copy.deepcopy(prebuilt)
        receipt = self.start(prebuilt_images=prebuilt)
        self.assertEqual("running", receipt["status"])
        self.assertFalse(receipt["active_changed"])
        self.assertEqual(prebuilt, json.loads((self.root / "images.json").read_text()))
        self.assertEqual(receipt, self.start(prebuilt_images=prebuilt))
        self.assertEqual(receipt, self.start())
        self.assertEqual(original, prebuilt)
        docker_calls = [argv for argv, _ in self.calls if argv[0] == "docker"]
        self.assertFalse(
            any(argv[1] in {"build", "pull", "tag"} for argv in docker_calls)
        )
        self.assertEqual(1, sum("up" in argv for argv in docker_calls))
        self.assertFalse(any(self.root.glob("build-*")))
        self.assertFalse(any(self.root.glob("image-*.iid")))

    def test_image_stage_builds_and_reverifies_without_starting_or_switching(self):
        result = self.stage_images()
        self.assertEqual("images_staged", result["status"])
        self.assertIs(result["services_started"], False)
        self.assertIs(result["active_changed"], False)
        self.assertEqual(runtime.SERVICES, set(result["images"]))
        self.assertEqual("sha256:" + "3" * 64, result["admin_image"]["image_id"])
        self.assertEqual(
            result,
            json.loads((self.root / "image-stage-complete.json").read_text()),
        )
        for name in (
            "image-stage-started.json",
            "image-stage-complete.json",
            "images.json",
            "admin-image.json",
        ):
            self.assertEqual(0o600, (self.root / name).stat().st_mode & 0o777)
        self.assertEqual(
            3, sum(argv[:2] == ["docker", "build"] for argv, _ in self.calls)
        )
        self.assertFalse(
            any(
                argv[1] in {"compose", "create", "start", "rm"}
                for argv, _ in self.calls
                if argv[0] == "docker"
            )
        )
        self.assertEqual([], self.containers)
        self.assertFalse((self.root / "runtime.json").exists())
        self.assertFalse((self.fixture.config.state_root / "active.json").exists())

        self.calls.clear()
        self.assertEqual(result, self.stage_images())
        self.assertFalse(
            any(
                argv[1] in {"build", "pull", "tag", "compose", "create", "start"}
                for argv, _ in self.calls
                if argv[0] == "docker"
            )
        )

    def test_image_stage_retains_default_runner_and_start_uses_staged_images(self):
        with patch.object(
            commands, "default_runner", return_value=self.runner
        ) as factory:
            staged = runtime.stage_images(
                self.fixture.config, self.plan, repo_root=ROOT
            )
        factory.assert_called_once_with(ROOT)
        explicit = json.loads((self.root / "images.json").read_text())
        self.calls.clear()
        started = self.start()
        self.assertEqual("running", started["status"])
        self.assertEqual(started, self.start(prebuilt_images=explicit))
        self.assertEqual(staged, self.stage_images())
        self.assertFalse(
            any(
                argv[1] in {"build", "pull", "tag"}
                for argv, _ in self.calls
                if argv[0] == "docker"
            )
        )
        self.assertEqual(1, sum("up" in argv for argv, _ in self.calls))
        # services_started=False records the stage operation, even after start.
        self.assertIs(staged["services_started"], False)

    def test_failed_admin_image_stage_cannot_retry_or_be_bypassed_by_start(self):
        self.build_failure_service = "catalog-admin"
        with self.assertRaisesRegex(backend.BackendError, "command failed"):
            self.stage_images()
        self.assertTrue((self.root / "image-stage-started.json").exists())
        self.assertTrue((self.root / "images.json").exists())
        self.assertFalse((self.root / "admin-image.json").exists())
        self.assertFalse((self.root / "image-stage-complete.json").exists())
        self.build_failure_service = None
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "explicit_recovery"):
            self.stage_images()
        with self.assertRaisesRegex(ValueError, "explicit_recovery"):
            self.start()
        with self.assertRaisesRegex(ValueError, "explicit_recovery"):
            self.start(prebuilt_images=self.prebuilt_receipt())
        self.assertFalse((self.root / "start-started.json").exists())
        self.assertFalse(
            any(
                argv[1] in {"build", "pull", "compose", "create", "start", "rm"}
                for argv, _ in self.calls
                if argv[0] == "docker"
            )
        )

    def test_image_stage_rejects_unknown_build_evidence_before_intent(self):
        for name in (
            "images.json",
            "admin-image.json",
            "build-fess-search",
            "image-fess-search.iid",
        ):
            path = self.root / name
            path.write_text("unreviewed evidence\n")
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "fresh_prepared_slot"),
            ):
                self.stage_images()
            self.assertFalse((self.root / "image-stage-started.json").exists())
            path.unlink()
        self.start()
        with self.assertRaisesRegex(ValueError, "fresh_prepared_slot"):
            self.stage_images()
        self.assertFalse((self.root / "image-stage-started.json").exists())

    def test_image_stage_does_not_adopt_orphan_service_or_helper_containers(self):
        for helper in (False, True):
            if helper:
                self.helpers["d" * 64] = {}
            else:
                self.containers = [{"Id": "a" * 64}]
            with (
                self.subTest(helper=helper),
                self.assertRaisesRegex(ValueError, "fresh_prepared_slot"),
            ):
                self.stage_images()
            self.assertFalse((self.root / "image-stage-started.json").exists())
            self.containers.clear()
            self.helpers.clear()

    def test_image_stage_receipt_mutations_are_rejected_without_rebuilding(self):
        result = self.stage_images()
        for mutation in (
            lambda value: value.update(schema_version=True),
            lambda value: value.update(services_started=0),
            lambda value: value.update(active_changed=0),
            lambda value: value.update(revision="9" * 40),
            lambda value: value.update(extra=True),
            lambda value: value["images"]["fess-search"].update(
                image_id="sha256:" + "9" * 64
            ),
            lambda value: value["admin_image"].update(reference="unreviewed:1"),
        ):
            changed = copy.deepcopy(result)
            mutation(changed)
            execution.write_record(
                self.root, "image-stage-complete.json", changed, replace=True
            )
            with (
                self.subTest(mutation=mutation),
                self.assertRaisesRegex(ValueError, "stage_receipt_changed"),
            ):
                self.stage_images()
        execution.write_record(
            self.root, "image-stage-complete.json", result, replace=True
        )
        started = json.loads((self.root / "image-stage-started.json").read_text())
        started["schema_version"] = True
        execution.write_record(
            self.root, "image-stage-started.json", started, replace=True
        )
        with self.assertRaisesRegex(ValueError, "stage_receipt_changed"):
            self.start()
        self.assertFalse((self.root / "start-started.json").exists())
        self.assertEqual(
            3, sum(argv[:2] == ["docker", "build"] for argv, _ in self.calls)
        )

    def test_image_stage_reuse_requires_actual_images_and_admin_evidence(self):
        staged = self.stage_images()
        self.calls.clear()
        fess = staged["images"]["fess-search"]
        original = self.images.pop(fess["reference"])
        with self.assertRaisesRegex(backend.BackendError, "command failed"):
            self.stage_images()
        self.images[fess["reference"]] = original
        admin = staged["admin_image"]
        image = self.images[admin["image_id"]]
        image["Config"]["Labels"][runtime.LABEL_PREFIX + "revision"] = "9" * 40
        with self.assertRaisesRegex(ValueError, "source_labels_changed"):
            self.stage_images()
        image["Config"]["Labels"] = copy.deepcopy(admin["labels"])
        (self.root / "admin-image.json").unlink()
        with self.assertRaisesRegex(ValueError, "admin_image_evidence_missing"):
            self.stage_images()
        self.assertFalse(
            any(
                argv[1] in {"build", "pull", "tag", "compose", "create", "start"}
                for argv, _ in self.calls
                if argv[0] == "docker"
            )
        )

    def test_invalid_prebuilt_source_or_shape_is_rejected_before_intent(self):
        valid = self.prebuilt_receipt()
        mutations = (
            lambda value: value.update(revision="9" * 40),
            lambda value: value.update(project="different-project"),
            lambda value: value.update(schema_version=True),
            lambda value: value.update(extra=True),
            lambda value: value["images"].pop("fess-search"),
            lambda value: value["images"]["fess-search"].update(image_id="mutable:tag"),
            lambda value: value["images"]["fess-search"]["sources"].update(
                {next(iter(value["images"]["fess-search"]["sources"])): "0" * 64}
            ),
        )
        for mutation in mutations:
            supplied = copy.deepcopy(valid)
            mutation(supplied)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.start(prebuilt_images=supplied)
            self.assertFalse((self.root / "start-started.json").exists())
            self.assertFalse((self.root / "start-failure.json").exists())
            self.assertFalse((self.root / "images.json").exists())
        self.assertFalse(any("up" in argv for argv, _ in self.calls))

    def test_prebuilt_missing_image_has_no_build_or_pull_fallback(self):
        prebuilt = self.prebuilt_receipt()
        reference = prebuilt["images"]["fess-search"]["reference"]
        del self.images[reference]
        with self.assertRaisesRegex(backend.BackendError, "command failed"):
            self.start(prebuilt_images=prebuilt)
        self.assertFalse((self.root / "start-started.json").exists())
        self.assertFalse((self.root / "start-failure.json").exists())
        self.assertFalse(
            any(
                argv[1] in {"build", "pull", "tag"} or "up" in argv
                for argv, _ in self.calls
                if argv[0] == "docker"
            )
        )

    def test_prebuilt_actual_source_labels_and_registry_digest_are_required(self):
        prebuilt = self.prebuilt_receipt()
        fess = self.images[prebuilt["images"]["fess-search"]["image_id"]]
        labels = copy.deepcopy(fess["Config"]["Labels"])
        for key in (
            runtime.LABEL_PREFIX + "revision",
            runtime.LABEL_PREFIX + "project",
        ):
            with self.subTest(label=key):
                fess["Config"]["Labels"] = {**labels, key: "wrong"}
                with self.assertRaisesRegex(ValueError, "source_labels_changed"):
                    self.start(prebuilt_images=prebuilt)
                self.assertFalse((self.root / "start-started.json").exists())
        fess["Config"]["Labels"] = labels
        ollama = self.images[prebuilt["images"]["ollama-embedding"]["image_id"]]
        ollama["RepoDigests"] = ["ollama/ollama@sha256:" + "0" * 64]
        with self.assertRaisesRegex(ValueError, "ollama_registry_digest_changed"):
            self.start(prebuilt_images=prebuilt)
        self.assertFalse((self.root / "start-started.json").exists())

    def test_prebuilt_image_change_at_startup_is_quarantined(self):
        prebuilt = self.prebuilt_receipt()
        reference = prebuilt["images"]["fess-search"]["reference"]
        self.startup_mutation = lambda: self.images[reference].update(
            Id="sha256:" + "9" * 64
        )
        with self.assertRaisesRegex(ValueError, "identity_changed"):
            self.start(prebuilt_images=prebuilt)
        self.assertTrue((self.root / "start-started.json").exists())
        self.assertTrue((self.root / "start-failure.json").exists())
        self.assertFalse((self.root / "runtime.json").exists())
        self.assertEqual(3, len(self.containers))

    def test_prebuilt_cannot_replace_or_bypass_existing_runtime_choice(self):
        prebuilt = self.prebuilt_receipt()
        self.start(prebuilt_images=prebuilt)
        changed = copy.deepcopy(prebuilt)
        new_id = "sha256:" + "9" * 64
        original = changed["images"]["fess-search"]["image_id"]
        reference = changed["images"]["fess-search"]["reference"]
        changed["images"]["fess-search"]["image_id"] = new_id
        self.images[new_id] = {**self.images[original], "Id": new_id}
        self.images[reference] = self.images[new_id]
        observations = 0

        def restore_original_tag(image):
            nonlocal observations
            if image["Id"] == new_id:
                observations += 1
                if observations == 2:
                    # The supplied image proves locally valid, then another
                    # actor restores the sealed runtime's tag before reuse.
                    self.images[reference] = self.images[original]

        self.image_metadata_mutation = restore_original_tag
        with self.assertRaisesRegex(ValueError, "prebuilt_images_differ_from_runtime"):
            self.start(prebuilt_images=changed)
        self.assertEqual(1, sum("up" in argv for argv, _ in self.calls))
        self.assertFalse((self.root / "start-failure.json").exists())
        self.assertEqual(original, self.images[reference]["Id"])

    def test_prebuilt_manifest_mutation_does_not_change_validated_selection(self):
        prebuilt = self.prebuilt_receipt()
        original = copy.deepcopy(prebuilt)

        def mutate_caller_manifest(image):
            prebuilt["images"]["fess-search"]["image_id"] = "sha256:" + "9" * 64

        self.image_metadata_mutation = mutate_caller_manifest
        receipt = self.start(prebuilt_images=prebuilt)
        self.assertEqual(original, json.loads((self.root / "images.json").read_text()))
        self.assertEqual(
            original["images"]["fess-search"]["image_id"],
            receipt["image_ids"]["fess-search"],
        )

    def test_memory_malformed_or_changed_values_are_rejected_for_every_service(self):
        self.start()
        images = json.loads((self.root / "images.json").read_text())["images"]
        for index, original in enumerate(self.containers):
            name = original["Config"]["Labels"]["com.docker.compose.service"]
            for actual, expected in (
                ("Memory", "mem_limit"),
                ("MemorySwap", "memswap_limit"),
            ):
                rows = copy.deepcopy(self.containers)
                rows[index]["HostConfig"][actual] += 1
                with (
                    self.subTest(service=name, field=actual, changed=True),
                    self.assertRaisesRegex(
                        runtime.RuntimeError, "resource_policy_changed"
                    ),
                ):
                    validate_runtime(
                        self.plan, self.compose, images, rows, self.network
                    )
                for value in (
                    True,
                    float(original["HostConfig"][actual]),
                    "8589934592 ",
                    None,
                ):
                    for side in ("actual", "expected", "both"):
                        rows, compose = (
                            copy.deepcopy(self.containers),
                            copy.deepcopy(self.compose),
                        )
                        if side != "expected":
                            rows[index]["HostConfig"][actual] = value
                        if side != "actual":
                            compose["services"][name][expected] = value
                        with (
                            self.subTest(
                                service=name, field=actual, side=side, value=value
                            ),
                            self.assertRaisesRegex(
                                runtime.RuntimeError, "memory_bytes_invalid"
                            ),
                        ):
                            validate_runtime(
                                self.plan, compose, images, rows, self.network
                            )

    def test_shared_core_proof_makes_no_network_peer_or_runtime_receipt_claim(self):
        self.create_services()
        images = {
            row["Config"]["Labels"]["com.docker.compose.service"]: {
                "image_id": row["Image"]
            }
            for row in self.containers
        }
        before = copy.deepcopy((self.plan, self.compose, images, self.containers))
        result = validate_core_containers(
            self.plan, self.compose, images, self.containers
        )
        self.assertEqual(
            {
                "fess-search": "a" * 64,
                "opensearch-search": "b" * 64,
                "ollama-embedding": "c" * 64,
            },
            result,
        )
        self.assertEqual(before, (self.plan, self.compose, images, self.containers))
        self.assertNotIn("network_id", result)
        self.assertNotIn("status", result)

    def test_shared_core_proof_still_requires_every_and_only_three_roles(self):
        self.create_services()
        images = {
            row["Config"]["Labels"]["com.docker.compose.service"]: {
                "image_id": row["Image"]
            }
            for row in self.containers
        }
        for rows in (self.containers[:-1], self.containers + [self.containers[0]]):
            with self.subTest(count=len(rows)):
                with self.assertRaisesRegex(
                    runtime.RuntimeError, "exactly_three_services"
                ):
                    validate_core_containers(self.plan, self.compose, images, rows)
        rows = copy.deepcopy(self.containers)
        rows[0]["Config"]["Labels"]["com.docker.compose.service"] = "conversation-api"
        with self.assertRaisesRegex(runtime.RuntimeError, "identity_changed"):
            validate_core_containers(self.plan, self.compose, images, rows)

    def test_named_index_core_proof_uses_actual_volume_name_driver_and_nocopy(self):
        self.create_services()
        compose = backend._index_volume(self.compose, self.plan["project"])
        rows = copy.deepcopy(self.containers)
        row = next(
            value
            for value in rows
            if value["Config"]["Labels"]["com.docker.compose.service"]
            == "opensearch-search"
        )
        mount = next(
            value
            for value in row["Mounts"]
            if value["Destination"] == "/usr/share/opensearch/data"
        )
        volume_name = self.plan["project"] + "-index"
        mount.update(
            Type="volume",
            Name=volume_name,
            Driver="local",
            Source="/docker/volumes/opaque/_data",
        )
        row["HostConfig"]["Mounts"] = [
            {
                "Type": "volume",
                "Source": volume_name,
                "Target": "/usr/share/opensearch/data",
                "VolumeOptions": {"NoCopy": True},
            }
        ]
        images = {
            value["Config"]["Labels"]["com.docker.compose.service"]: {
                "image_id": value["Image"]
            }
            for value in rows
        }
        self.assertEqual(
            {"fess-search", "opensearch-search", "ollama-embedding"},
            set(validate_core_containers(self.plan, compose, images, rows)),
        )
        for field, value in (
            ("Name", volume_name + "-other"),
            ("Driver", "nfs"),
            ("RW", False),
            ("Source", "relative"),
        ):
            with self.subTest(field=field):
                old = mount[field]
                mount[field] = value
                with self.assertRaises(runtime.RuntimeError):
                    validate_core_containers(self.plan, compose, images, rows)
                mount[field] = old
        host_mount = row["HostConfig"]["Mounts"][0]
        for field, value in (
            ("VolumeOptions", {}),
            ("VolumeOptions", {"NoCopy": 1}),
            ("VolumeOptions", {"NoCopy": True, "Subpath": "other"}),
            ("ReadOnly", True),
            ("Source", volume_name + "-other"),
        ):
            with self.subTest(host_field=field, value=value):
                old = dict(host_mount)
                host_mount[field] = value
                with self.assertRaises(runtime.RuntimeError):
                    validate_core_containers(self.plan, compose, images, rows)
                host_mount.clear()
                host_mount.update(old)

    def test_baseline_cannot_hide_extra_peers_via_shared_core_proof(self):
        self.create_services()
        images = {
            row["Config"]["Labels"]["com.docker.compose.service"]: {
                "image_id": row["Image"]
            }
            for row in self.containers
        }
        expected = validate_runtime(
            self.plan, self.compose, images, self.containers, self.network
        )
        self.network["Containers"]["f" * 64] = {}
        self.assertEqual(
            expected["container_ids"],
            validate_core_containers(self.plan, self.compose, images, self.containers),
        )
        with self.assertRaisesRegex(runtime.RuntimeError, "network_peers_changed"):
            validate_runtime(
                self.plan, self.compose, images, self.containers, self.network
            )

    def test_changed_healthcheck_duration_is_rejected_for_every_service(self):
        self.start()
        images = json.loads((self.root / "images.json").read_text())["images"]
        for index, original in enumerate(self.containers):
            name = original["Config"]["Labels"]["com.docker.compose.service"]
            for field in ("Interval", "Timeout", "StartPeriod", "StartInterval"):
                rows = copy.deepcopy(self.containers)
                rows[index]["Config"]["Healthcheck"][field] += 1
                with (
                    self.subTest(service=name, field=field),
                    self.assertRaisesRegex(runtime.RuntimeError, "healthcheck_changed"),
                ):
                    validate_runtime(
                        self.plan, self.compose, images, rows, self.network
                    )

    def test_changed_resource_or_port_rejects_existing_runtime(self):
        self.start()
        original = copy.deepcopy(self.containers)
        mutations = (
            lambda row: row["HostConfig"].update(Memory=1),
            lambda row: row["HostConfig"].update(DeviceRequests=[{"Driver": "nvidia"}]),
            lambda row: row["HostConfig"].update(
                PortBindings={"8080/tcp": [{"HostPort": "8080"}]}
            ),
            lambda row: row["Config"].update(Cmd=["sh"]),
            lambda row: row["Mounts"][0].update(RW=True),
        )
        for mutation in mutations:
            self.containers = copy.deepcopy(original)
            mutation(self.containers[0])
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(runtime.RuntimeError),
            ):
                self.start()

    def test_start_failure_is_quarantined_without_deleting_containers(self):
        self.startup_mutation = lambda: self.network.update(Internal=False)
        with self.assertRaises(runtime.RuntimeError):
            self.start()
        failure = json.loads((self.root / "start-failure.json").read_text())
        self.assertEqual("quarantined", failure["status"])
        self.assertEqual(3, len(self.containers))
        with self.assertRaisesRegex(runtime.RuntimeError, "quarantined"):
            self.start()
        self.assertFalse(
            any(
                "down" in argv or argv[1] == "rm"
                for argv, _ in self.calls
                if argv[0] == "docker"
            )
        )

    def test_retagged_image_rejected_on_every_start(self):
        self.start()
        reference = self.plan["project"] + "-fess-search:1"
        self.images[reference] = {**self.images[reference], "Id": "sha256:" + "5" * 64}
        with self.assertRaisesRegex(runtime.RuntimeError, "identity_changed"):
            self.start()

    def test_image_receipt_cannot_drop_a_required_source(self):
        self.start()
        path = self.root / "images.json"
        value = json.loads(path.read_text())
        value["images"]["fess-search"]["sources"] = {}
        path.write_text(json.dumps(value))
        with self.assertRaises(runtime.RuntimeError):
            self.start()

    def test_import_bounded_helper_is_removed_but_evidence_retained(self):
        self.start()
        receipt = self.admin("import")
        self.assertEqual("completed", receipt["status"])
        self.assertEqual("imported", receipt["result"]["status"])
        self.assertFalse(receipt["active_changed"])
        self.assertEqual({}, self.helpers)
        create = next(
            argv for argv, _ in self.calls if argv[:2] == ["docker", "create"]
        )
        self.assertEqual("container:" + "a" * 64, create[create.index("--network") + 1])
        self.assertIn("--read-only", create)
        self.assertNotIn("--privileged", create)
        self.assertEqual(2, create.count("--mount"))
        self.assertEqual("runc", create[create.index("--runtime") + 1])

    def test_helper_mount_and_argv_keep_the_real_generated_bundle_loadable(self):
        self.start()
        for operation in ("import", "reconcile"):
            self.admin(operation)
        self.assertEqual(2, len(self.helper_bundle_loads))
        for target, mount, loaded in self.helper_bundle_loads:
            with self.subTest(target=target):
                source = Path(self.plan["bundle_path"])
                self.assertEqual("/catalog/" + source.name, str(target))
                self.assertEqual(source.name, target.name)
                self.assertEqual(str(source), mount["Source"])
                self.assertIs(mount["RW"], False)
                self.assertEqual(self.fixture.bundle["bundle_id"], loaded.bundle_id)
                self.assertEqual(self.plan["manifest_sha256"], loaded.manifest_sha256)
        invalid_root = self.fixture.root / "old-helper-mount"
        invalid_root.mkdir(mode=0o700)
        invalid_bundle = invalid_root / "catalog"
        shutil.copytree(self.plan["bundle_path"], invalid_bundle)
        with self.assertRaisesRegex(BundleError, "bundle manifest values are invalid"):
            CatalogBundle.load(
                invalid_bundle, expected_manifest_sha256=self.plan["manifest_sha256"]
            )

    def test_exit_three_never_retries_import_and_requires_explicit_reconcile(self):
        self.start()
        self.helper_exit = 3
        with self.assertRaisesRegex(runtime.RuntimeError, "operation_failed"):
            self.admin("import")
        count = sum(argv[1:3] == ["start", "--attach"] for argv, _ in self.calls)
        with self.assertRaisesRegex(runtime.RuntimeError, "readonly_reconcile"):
            self.admin("import")
        self.assertEqual(
            count, sum(argv[1:3] == ["start", "--attach"] for argv, _ in self.calls)
        )
        self.helper_exit = 0
        receipt = self.admin("reconcile")
        self.assertEqual("reconciled-active", receipt["result"]["status"])
        self.assertEqual(
            1, len(list((self.root / "admin-operations").glob("*-failure.json")))
        )
        self.admin("import")

    def test_unstructured_helper_failure_is_generic_without_changing_quarantine(self):
        self.start()
        self.helper_exit = 3

        def failure(outcome):
            outcome.stdout = ""
            outcome.stderr = "catalog-admin failed: InventoryConflict: private-response-not-for-output\n"

        self.helper_outcome_mutation = failure
        with self.assertRaises(runtime.RuntimeError) as caught:
            self.admin("import")
        self.assertEqual(("catalog_admin_operation_failed",), caught.exception.args)
        directory = self.root / "admin-operations"
        failure_path = next(directory.glob("*-failure.json"))
        failure_record = json.loads(failure_path.read_text())
        started = json.loads(next(directory.glob("*-started.json")).read_text())
        self.assertEqual(
            {**started, "status": "quarantined", "error_type": "RuntimeError"},
            failure_record,
        )
        self.assertNotIn("private-response", failure_path.read_text())
        self.assertFalse(list(directory.glob("*-complete.json")))
        self.assertFalse(self.helpers)
        count = sum(argv[1:3] == ["start", "--attach"] for argv, _ in self.calls)
        with self.assertRaisesRegex(runtime.RuntimeError, "readonly_reconcile"):
            self.admin("import")
        self.assertEqual(
            count, sum(argv[1:3] == ["start", "--attach"] for argv, _ in self.calls)
        )
        self.helper_exit = 0
        self.helper_outcome_mutation = None
        self.assertEqual("completed", self.admin("reconcile")["status"])

    def test_ambiguous_helper_output_or_exit_remains_generic(self):
        self.start()
        self.helper_exit = 3
        known = "catalog-admin failed: InventoryConflict: private-value\n"
        cases = (
            ("", known.replace("InventoryConflict", "UnknownError"), 3, 3),
            ("", known.replace("InventoryConflict", "InventoryConflictSubclass"), 3, 3),
            ("", "prefix " + known, 3, 3),
            ("", known + "private-extra-line\n", 3, 3),
            ("", known + "\n", 3, 3),
            ("", known.replace("private-value", "private\rvalue"), 3, 3),
            ("", known.replace("private-value", "private\u2028value"), 3, 3),
            ("", known.replace("private-value", "\x1b[31mprivate-value"), 3, 3),
            ("", known + "x" * 4096, 3, 3),
            ("unexpected stdout", known, 3, 3),
            (None, known, 3, 3),
            ("", known.encode(), 3, 3),
            ("", known, 4, 4),
            ("", known, 2, 2),
            ("", known, 3, 0),
            ("", known, 0, 3),
            ("", known, 3, True),
        )
        for position, (stdout, stderr, returncode, exitcode) in enumerate(cases):
            with self.subTest(position=position):

                def failure(outcome):
                    outcome.stdout, outcome.stderr = stdout, stderr
                    outcome.returncode = returncode
                    self.helpers["d" * 64]["State"]["ExitCode"] = exitcode

                self.helper_outcome_mutation = failure
                with self.assertRaises(runtime.RuntimeError) as caught:
                    self.admin("reconcile")
                self.assertEqual(
                    ("catalog_admin_operation_failed",), caught.exception.args
                )
        self.assertFalse(self.helpers)
        self.assertFalse(list((self.root / "admin-operations").glob("*-complete.json")))

    def test_closed_helper_json_preserves_diagnostics_without_changing_quarantine(self):
        self.start()
        primary = catalog._diagnosed(
            catalog.ImportFailed("private-value"),
            stage="bulk_dispatched",
            cause="http_503",
        )
        cleanup = catalog.TokenCleanupError("private-value", possibly_polluted=True)
        cleanup.primary_diagnostic = catalog._failure_detail(primary)
        cleanup.cleanup_diagnostic = catalog._failure_detail(
            catalog.ProtocolError("private-value"), stage="token_cleanup"
        )
        catalog._diagnosed(cleanup, stage="token_cleanup", cause="response_invalid")
        for position, (error, polluted) in enumerate(
            (
                (catalog.TransportError("private-value"), False),
                (primary, True),
                (cleanup, True),
            )
        ):
            record = catalog.failure_record(error, possibly_polluted=polluted)
            with self.subTest(error_type=record["error_type"]):
                self.helper_exit = 4 if polluted else 3

                def failure(outcome):
                    outcome.stdout = ""
                    outcome.stderr = json.dumps(record) + "\n"

                self.helper_outcome_mutation = failure
                with self.assertRaises(runtime.RuntimeError) as caught:
                    self.admin("import" if position == 0 else "reconcile")
                self.assertEqual(
                    ("catalog_admin_diagnostic_failed",), caught.exception.args
                )
                self.assertEqual(record, caught.exception.catalog_diagnostic)
        directory = self.root / "admin-operations"
        for path in directory.glob("*-failure.json"):
            failure_record = json.loads(path.read_text())
            started = json.loads(
                path.with_name(path.name.replace("-failure", "-started")).read_text()
            )
            self.assertEqual(
                {**started, "status": "quarantined", "error_type": "RuntimeError"},
                failure_record,
            )
            self.assertNotIn("diagnostic", path.read_text())
            self.assertNotIn("private-value", path.read_text())
        self.assertFalse(self.helpers)
        self.assertFalse(list(directory.glob("*-complete.json")))
        count = sum(argv[1:3] == ["start", "--attach"] for argv, _ in self.calls)
        with self.assertRaisesRegex(runtime.RuntimeError, "readonly_reconcile"):
            self.admin("import")
        self.assertEqual(
            count, sum(argv[1:3] == ["start", "--attach"] for argv, _ in self.calls)
        )

    def test_malformed_helper_json_or_native_exit_is_generic(self):
        error = catalog._diagnosed(
            catalog.ImportFailed("private-value"),
            stage="bulk_dispatched",
            cause="http_503",
        )
        record = catalog.failure_record(error, possibly_polluted=True)
        good = json.dumps(record) + "\n"
        invalid = []
        for mutate in (
            lambda r: r.update(schema_version=True),
            lambda r: r.update(schema_version=2),
            lambda r: r.update(error_type="ImportFailedSubclass"),
            lambda r: r.update(possibly_polluted=False),
            lambda r: r.update(possibly_polluted=1),
            lambda r: r.update(private_value="private-value"),
            lambda r: r["diagnostic"].update(stage="private-value"),
            lambda r: r["diagnostic"].update(cause="private-value"),
            lambda r: r["diagnostic"].update(error_type="ProtocolError"),
            lambda r: r["diagnostic"].update(private_value="private-value"),
            lambda r: r.update(primary_diagnostic=r["diagnostic"]),
        ):
            bad = copy.deepcopy(record)
            mutate(bad)
            invalid.append(json.dumps(bad) + "\n")
        cleanup = catalog.TokenCleanupError("private-value", possibly_polluted=True)
        cleanup.primary_diagnostic = catalog._failure_detail(error)
        cleanup.cleanup_diagnostic = catalog._failure_detail(
            catalog.TransportError("private-value"), stage="token_cleanup"
        )
        catalog._diagnosed(cleanup, stage="token_cleanup", cause="transport")
        cleanup_record = catalog.failure_record(cleanup, possibly_polluted=True)
        for mutate in (
            lambda r: r.update(possibly_polluted=False),
            lambda r: r["diagnostic"].update(cause="http_503"),
        ):
            bad = copy.deepcopy(cleanup_record)
            mutate(bad)
            invalid.append(json.dumps(bad) + "\n")
        invalid += [
            good.rstrip("\n"),
            good + "\n",
            good + "private-value\n",
            good.replace(
                '"schema_version": 1', '"schema_version": 1,"schema_version": 1'
            ),
            good.replace('"schema_version": 1', '"schema_version": NaN'),
            "[null]\n",
            "x" * 4097 + "\n",
            "é" * 3000 + "\n",
            good.encode(),
        ]
        for raw in invalid:
            with self.subTest(raw_type=type(raw).__name__):
                outcome = subprocess.CompletedProcess([], 4, "", raw)
                self.assertIsNone(
                    _admin_failure_diagnostic(
                        outcome, {"Status": "exited", "ExitCode": 4}
                    )
                )
        for original in (record, cleanup_record):
            bad = copy.deepcopy(original)
            bad["possibly_polluted"] = False
            outcome = subprocess.CompletedProcess([], 3, "", json.dumps(bad) + "\n")
            self.assertIsNone(
                _admin_failure_diagnostic(outcome, {"Status": "exited", "ExitCode": 3})
            )
        for returned, native, status, stdout in (
            (3, 3, "exited", ""),
            (4, 3, "exited", ""),
            (3, 4, "exited", ""),
            (4, 4, "running", ""),
            (True, 4, "exited", ""),
            (4, True, "exited", ""),
            (2, 2, "exited", ""),
            (4, 4, "exited", "unexpected stdout"),
            (4, 4, "exited", None),
        ):
            with self.subTest(returned=returned, native=native, status=status):
                outcome = subprocess.CompletedProcess([], returned, stdout, good)
                self.assertIsNone(
                    _admin_failure_diagnostic(
                        outcome, {"Status": status, "ExitCode": native}
                    )
                )

    def test_boolean_schema_or_wrong_count_is_not_a_success(self):
        self.start()
        self.helper_report_mutation = lambda value: value.update(schema_version=True)
        with self.assertRaises(ValueError):
            self.admin("import")
        self.assertFalse(list((self.root / "admin-operations").glob("*-complete.json")))

    def test_running_timed_out_helper_is_not_killed_or_automatically_retried(self):
        self.start()
        self.helper_timeout = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.admin("import")
        self.assertTrue(self.helpers["d" * 64]["State"]["Running"])
        self.assertFalse(any(argv[:2] == ["docker", "rm"] for argv, _ in self.calls))
        with self.assertRaisesRegex(
            runtime.RuntimeError, "helper_requires_explicit_recovery"
        ):
            self.admin("reconcile")
        self.assertEqual(
            1, sum(argv[1:3] == ["start", "--attach"] for argv, _ in self.calls)
        )

    def test_namespace_peer_sharing_and_nonrunc_are_rejected_for_every_service(self):
        self.start()
        original = copy.deepcopy(self.containers)
        for index in range(3):
            for field in ("PidMode", "IpcMode", "UTSMode", "UsernsMode"):
                for value in ("host", "container:" + "f" * 64):
                    self.containers = copy.deepcopy(original)
                    self.containers[index]["HostConfig"][field] = value
                    with (
                        self.subTest(service=index, field=field, value=value),
                        self.assertRaisesRegex(
                            runtime.RuntimeError, "resource_policy_changed"
                        ),
                    ):
                        self.start()
            self.containers = copy.deepcopy(original)
            self.containers[index]["HostConfig"]["Runtime"] = "nvidia"
            with (
                self.subTest(service=index),
                self.assertRaisesRegex(runtime.RuntimeError, "resource_policy_changed"),
            ):
                self.start()

    def test_helper_changed_working_directory_is_rejected_before_start(self):
        self.start()
        self.helper_mutation = lambda row: row["Config"].update(WorkingDir="/tmp")
        with self.assertRaisesRegex(runtime.RuntimeError, "capabilities_changed"):
            self.admin("reconcile")
        self.assertFalse(
            any(argv[1:3] == ["start", "--attach"] for argv, _ in self.calls)
        )
        self.assertFalse(list((self.root / "admin-operations").glob("*-complete.json")))

    def test_helper_namespace_peer_sharing_is_rejected_before_start(self):
        self.start()
        self.helper_mutation = lambda row: row["HostConfig"].update(
            IpcMode="container:" + "f" * 64
        )
        with self.assertRaisesRegex(runtime.RuntimeError, "capabilities_changed"):
            self.admin("import")
        self.assertFalse(
            any(argv[1:3] == ["start", "--attach"] for argv, _ in self.calls)
        )

    def test_cleanup_timeout_cannot_replace_a_completed_outcome(self):
        self.start()
        self.cleanup_timeout = True
        result = self.admin("import")
        self.assertEqual("completed", result["status"])
        self.assertTrue(list((self.root / "admin-operations").glob("*-complete.json")))
        with self.assertRaisesRegex(
            runtime.RuntimeError, "helper_requires_explicit_recovery"
        ):
            self.admin("reconcile")

    def test_cleanup_timeout_cannot_replace_the_original_import_failure(self):
        self.start()
        self.helper_exit = 3
        self.cleanup_timeout = True
        with self.assertRaisesRegex(runtime.RuntimeError, "operation_failed"):
            self.admin("import")
        self.assertTrue(list((self.root / "admin-operations").glob("*-failure.json")))

    def test_orphan_admin_receipt_is_not_ignored(self):
        self.start()
        directory = self.root / "admin-operations"
        directory.mkdir(mode=0o700)
        path = directory / ("a" * 32 + "-container.json")
        path.write_text("{}\n")
        path.chmod(0o600)
        with self.assertRaisesRegex(runtime.RuntimeError, "orphan_evidence"):
            self.admin("import")

    def test_admin_failure_evidence_cannot_be_rebound_to_another_run(self):
        self.start()
        self.helper_exit = 3
        with self.assertRaises(runtime.RuntimeError):
            self.admin("import")
        path = next((self.root / "admin-operations").glob("*-failure.json"))
        value = json.loads(path.read_text())
        value["run_id"] = "f" * 32
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(runtime.RuntimeError, "failure_evidence_changed"):
            self.admin("reconcile")

    def test_nonobject_admin_evidence_uses_fixed_errors_not_attribute_errors(self):
        self.start()
        self.helper_exit = 3
        with self.assertRaises(runtime.RuntimeError):
            self.admin("import")
        directory = self.root / "admin-operations"
        for suffix, code in (
            ("container", "container_evidence_changed"),
            ("failure", "failure_evidence_changed"),
            ("reconciled", "reconciliation_identity_changed"),
        ):
            path = (
                directory / "reconciled.json"
                if suffix == "reconciled"
                else next(directory.glob("*-" + suffix + ".json"))
            )
            original = path.read_bytes() if path.exists() else None
            path.write_text("[]\n")
            path.chmod(0o600)
            with (
                self.subTest(suffix=suffix),
                self.assertRaisesRegex(runtime.RuntimeError, code),
            ):
                self.admin("reconcile")
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)

    def test_wrong_canonical_registry_digest_cannot_start_services(self):
        reference = self.compose["services"]["ollama-embedding"]["image"]
        self.images[reference]["RepoDigests"] = ["ollama/ollama@sha256:" + "0" * 64]
        with self.assertRaisesRegex(runtime.RuntimeError, "registry_digest_changed"):
            self.start()
        self.assertFalse(self.containers)

    def test_secret_changed_during_build_prevents_compose_up(self):
        secret = Path(self.plan["data_root"]) / "secrets/fess_initial_admin_password"
        self.after_build_mutation = lambda: secret.write_text("f" * 64 + "\n")
        with self.assertRaisesRegex(runtime.RuntimeError, "security_material_changed"):
            self.start()
        self.assertFalse(any("up" in argv for argv, _ in self.calls))
        self.assertFalse((self.root / "runtime.json").exists())

    def test_secret_changed_during_admin_is_detected_before_completion(self):
        self.start()
        secret = Path(self.plan["data_root"]) / "secrets/fess_initial_admin_password"
        self.helper_report_mutation = lambda report: secret.write_text("f" * 64 + "\n")
        with self.assertRaisesRegex(runtime.RuntimeError, "secret_changed"):
            self.admin("import")
        self.assertFalse(list((self.root / "admin-operations").glob("*-complete.json")))

    def test_extra_secret_or_docker_socket_mount_blocks_helper_before_start(self):
        self.start()
        self.helper_mutation = lambda row: row["Mounts"].append(
            {
                "Type": "bind",
                "Source": "/var/run/docker.sock",
                "Destination": "/var/run/docker.sock",
                "RW": True,
            }
        )
        with self.assertRaisesRegex(runtime.RuntimeError, "capabilities_changed"):
            self.admin("import")
        self.assertFalse(
            any(argv[1:3] == ["start", "--attach"] for argv, _ in self.calls)
        )
        self.assertIn("d" * 64, self.helpers)

    def test_fake_reconcile_pointer_cannot_unlock_a_failed_import(self):
        self.start()
        self.helper_exit = 3
        with self.assertRaises(ValueError):
            self.admin("import")
        path = self.root / "admin-operations/reconciled.json"
        path.write_text(
            json.dumps(
                {
                    "bundle_id": self.fixture.bundle["bundle_id"],
                    "manifest_sha256": self.plan["manifest_sha256"],
                    "resolved_import_runs": [],
                }
            )
        )
        path.chmod(0o600)
        with self.assertRaisesRegex(
            runtime.RuntimeError, "reconciliation_identity_changed"
        ):
            self.admin("import")

    def test_runtime_receipt_bool_schema_or_image_proof_change_is_rejected(self):
        self.start()
        path = self.root / "runtime.json"
        original = json.loads(path.read_text())
        for mutation in (
            lambda value: value.update(schema_version=True),
            lambda value: value.update(images_sha256="0" * 64),
            lambda value: value.update(active_changed=True),
        ):
            altered = copy.deepcopy(original)
            mutation(altered)
            path.write_text(json.dumps(altered))
            with (
                self.subTest(mutation=mutation),
                self.assertRaisesRegex(runtime.RuntimeError, "receipt_changed"),
            ):
                self.start()

    def test_wrong_document_count_rejected_after_readonly_reconcile(self):
        self.start()
        self.helper_report_mutation = lambda value: value.update(document_count=2)
        with self.assertRaisesRegex(runtime.RuntimeError, "document_count_changed"):
            self.admin("reconcile")
        self.assertFalse((self.root / "admin-operations/reconciled.json").exists())

    def test_vector_and_query_commands_are_out_of_scope(self):
        for operation in ("vectors", "query", "delete", "promote"):
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(runtime.RuntimeError, "not_supported"),
            ):
                self.admin(operation)
        self.assertEqual([], self.calls)


class CatalogCanonicalResourceTests(unittest.TestCase):
    def test_bundle_target_preserves_the_strict_content_addressed_basename(self):
        bundle_id = "a" * 64
        self.assertEqual(
            "/catalog/sha256-" + bundle_id, catalog_bundle_target(bundle_id)
        )

    def test_bundle_target_rejects_noncanonical_or_pathlike_identities(self):
        for value in (
            None,
            True,
            1,
            "",
            "a" * 63,
            "a" * 65,
            "A" * 64,
            "../" + "a" * 64,
            "a" * 64 + "\n",
            "a" * 64 + "/file",
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(runtime.RuntimeError, "bundle_id_invalid"),
            ):
                catalog_bundle_target(value)

    def test_memory_bytes_accepts_positive_canonical_int_or_decimal_strings(self):
        for number in (1, 8589934592, 2**63 - 1):
            for value in (number, str(number)):
                with self.subTest(value=value):
                    result = _memory_bytes(value)
                    self.assertIs(type(result), int)
                    self.assertEqual(number, result)

    def test_memory_bytes_rejects_noncanonical_malformed_and_out_of_bounds(self):
        for value in (
            0,
            -1,
            True,
            False,
            1.0,
            float("inf"),
            float("nan"),
            None,
            "",
            "0",
            "01",
            "00",
            "-1",
            "+1",
            "1.0",
            "1e9",
            "8g",
            "8GiB",
            " 1",
            "1 ",
            "1\n",
            "\t1",
            "1_000",
            "١",
            "９",
            2**63,
            str(2**63),
            "9" * 1000,
            [],
            {},
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(runtime.RuntimeError, "memory_bytes_invalid"),
            ):
                _memory_bytes(value)

    def test_duration_accepts_ordered_compounds_and_bounded_nanoseconds(self):
        for value, expected in (
            ("0s", 0),
            ("1ns", 1),
            ("1us", 1000),
            ("1ms", 10**6),
            ("20s", 20 * 10**9),
            ("2m0s", 120 * 10**9),
            ("1m30s", 90 * 10**9),
            ("1h2m3s4ms5us6ns", 3723004005006),
            ("9223372036854775807ns", 2**63 - 1),
            ("9223372036s854775807ns", 2**63 - 1),
        ):
            with self.subTest(value=value):
                self.assertEqual(expected, _duration(value))

    def test_duration_rejects_malformed_repeated_reordered_and_overflow(self):
        for value in (
            None,
            1,
            True,
            1.0,
            "",
            "1",
            " 1s",
            "1s ",
            "1s\n",
            "-1s",
            "+1s",
            "1.5s",
            "NaNs",
            "infs",
            "01s",
            "1m00s",
            "1e9ns",
            "1s1s",
            "1s1m",
            "1ms1s",
            "1us1ms",
            "1ns1us",
            "١s",
            "1µs",
            "9223372036854775808ns",
            "9223372036s854775808ns",
            "9999999999999999999h",
            "9" * 1000 + "ns",
            "0s" * 7,
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    runtime.RuntimeError, "healthcheck_duration_invalid"
                ),
            ):
                _duration(value)


if __name__ == "__main__":
    unittest.main()
