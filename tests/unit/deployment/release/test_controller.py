"""Private release orchestration with real private receipts and fake IO edges.

Source, image, runtime and query validators have separate drift regressions.
These tests exercise mutation ordering, mandatory acceptance, completed reuse
and interrupted-state publication without Docker or reading actual credentials.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import replace
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from search.operations.deployment.release import controller as release
from search.operations.shared import private_io
from search.operations.state import locked_state
from tests.unit.deployment.test_composition import product
from tests.unit.deployment.release.test_runtime import evidence


class Docker:
    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []
        self.started = False
        self.fail_up = False
        self.ostype = "linux"

    def call(self, arguments, **options):
        self.calls.append(list(arguments))
        if arguments[0] == "info":
            return json.dumps(self.ostype).encode()
        if arguments[0] != "compose":
            raise AssertionError(arguments)
        slot = Path(arguments[arguments.index("--file") + 1]).parent
        self.fixture.assertTrue((slot / "started.json").is_file())
        self.fixture.assertFalse((slot / "complete.json").exists())
        if "up" in arguments:
            self.fixture.assertIn("--no-build", arguments)
            self.fixture.assertEqual("never", arguments[arguments.index("--pull") + 1])
            self.fixture.assertEqual(
                set(arguments[-3:]), release.release_runtime.SERVICES
            )
            self.started = True
            if self.fail_up:
                raise ValueError("PRIVATE_SENTINEL compose failed")
        return b""

    def inspect(self, identifier):
        return {"Id": identifier, "Config": {"Cmd": ["fixture"], "Entrypoint": None}}


@unittest.skipIf(os.geteuid() == 0, "release operator requires non-root")
class PrivateReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-private-release-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = product()
        self.config = replace(config, state_root=self.root / "state")
        with locked_state(self.config, create=True):
            pass
        self.values = evidence()
        self.compose = self.values["compose"]
        self.revision = self.values["lease"]["revision"]
        self.project = self.compose["name"]
        self.plan = {
            "project": self.values["catalog_identity"]["catalog_project"],
            "manifest_sha256": self.values["lease"]["manifest_sha256"],
            "work_root": str(self.config.state_root / "catalogs" / "fixture"),
            "data_root": str(self.config.state_root / "catalogs" / "fixture" / "data"),
        }
        self.context = {
            "plan": self.plan,
            "bundle": SimpleNamespace(bundle_id=self.values["lease"]["bundle_id"]),
        }
        self.images = self.values["images"]
        self.authority = {
            "catalog_identity": self.values["catalog_identity"],
            "runtime": {"container_ids": {"fixture": "1" * 64}, "network_id": "2" * 64},
        }
        self.image_authority = {
            "source_manifest_sha256": "3" * 64,
            "images": {
                "conversation-api": self.values["image_proof"],
                "web": {
                    "source_sha256": self.compose["services"]["web"]["labels"][
                        "io.depositar.ai.source.sha256"
                    ]
                },
            },
        }
        self.proof = {
            "container_ids": {
                row["Config"]["Labels"]["com.docker.compose.service"]: row["Id"]
                for row in self.values["containers"]
            },
            "network_ids": self.values["lease"]["network_ids"],
            "start_identity_sha256": "4" * 64,
            "lease_sha256": release.client_lease.lease_digest(self.values["lease"]),
        }
        self.gpu_compose = {
            "name": "depositar-ai-inference-" + "5" * 12,
            "services": {"scope-model": {}},
            "networks": {},
        }
        self.gpu_images = {"scope-model": self.images["filtered-embedding"]}
        self.docker = Docker(self)
        self.queries = 0
        self.query_fail_after_start = False
        self.key_drift_after_start = False
        self.close_failure = False
        self.model_checks = 0
        self.runtime_calls = 0
        self.on_observe = None
        self.source_changed = False
        self.existing = False
        self.acceptance = {
            "status": "ready",
            "model_warmup_verified": True,
            "readiness_verified": True,
            "two_turn_search_verified": True,
            "session_reset_verified": True,
            "search_quality_verified": False,
            "gateway_verified": False,
        }
        self.addCleanup(mock.patch.stopall)
        patches = (
            (release.execution, "load_context", self.context),
            (release.execution, "operation_lock", nullcontext()),
            (
                release.source_bundle,
                "committed_source_plan",
                {"revision": self.revision},
            ),
            (release.image_evidence, "read_completed_images", self.image_authority),
            (release.runtime_docker, "Docker", self.docker),
            (release.inference, "_images", self.gpu_images),
            (
                release,
                "_images",
                {role: self.images[role] for role in ("web", "conversation-api")},
            ),
            (
                release.execution,
                "load_images",
                {"fixture": {"image_id": "sha256:" + "6" * 64}},
            ),
            (
                release.composition,
                "render_composition",
                {"inference": self.gpu_compose, "release": self.compose},
            ),
            (release.inference, "_runtime_identity", {"fixture": "inference-identity"}),
            (release, "_sealed_inference", lambda proof: None),
            (release.inference, "_gpu", None),
            (release.inference_runtime, "ownership_proofs", []),
        )
        for owner, name, value in patches:
            if owner is release and name == "_images":
                mock.patch.object(
                    owner,
                    name,
                    side_effect=lambda *args, **kwargs: {
                        role: copy.deepcopy(self.images[role])
                        for role in ("web", "conversation-api")
                    },
                ).start()
            else:
                mock.patch.object(owner, name, return_value=value).start()
        mock.patch.object(
            release.inference, "_source_revision", side_effect=self.source
        ).start()
        mock.patch.object(
            release.query,
            "read_authority",
            side_effect=lambda context: copy.deepcopy(self.authority),
        ).start()
        self.probe = mock.patch.object(
            release.release_probe,
            "verify_private_release",
            side_effect=lambda *args, **kwargs: dict(self.acceptance),
        ).start()
        mock.patch.object(
            release.credentials, "read_api_key_authority", side_effect=self.key
        ).start()
        mock.patch.object(
            release.inference, "_verified_models", side_effect=self.models
        ).start()
        mock.patch.object(
            release.inference, "_project_inventory", side_effect=self.inventory
        ).start()
        mock.patch.object(
            release.query, "verify_authority", side_effect=self.query
        ).start()
        fixture = self

        class Host:
            def __init__(self, *args):
                self.lease = None
                self.last = None

            def observe(self, *args, **kwargs):
                fixture.runtime_calls += 1
                self.last = (
                    {"fixture": "gpu-proof"},
                    copy.deepcopy(fixture.proof) if self.lease else None,
                )
                if fixture.on_observe:
                    fixture.on_observe(fixture.runtime_calls)
                return {}, [], {}

            def collect(self, *, clients):
                fixture.assertTrue(clients)
                return {}, {}

            def make_lease(self, snapshot):
                return copy.deepcopy(fixture.values["lease"])

        mock.patch.object(release, "_Host", Host).start()

    @contextmanager
    def models(self, *args, **options):
        self.assertTrue(options["include_embedding"])

        def verify():
            self.model_checks += 1

        yield (
            {role: {"fixture": True} for role in ("api", "embedding", "reranker")},
            verify,
        )
        if self.close_failure:
            raise ValueError("PRIVATE_SENTINEL source handle changed")

    def key(self, *args):
        return {
            "credential_sha256": (
                "b" if self.key_drift_after_start and self.docker.started else "a"
            )
            * 64
        }

    def source(self, *args):
        if self.source_changed:
            raise ValueError("PRIVATE_SENTINEL source changed")

    def inventory(self, docker, project):
        if self.existing:
            return {"f" * 64}, set()
        return set(), set()

    def query(self, context, *, observe_runtime):
        self.queries += 1
        observe_runtime()
        if self.query_fail_after_start and self.docker.started:
            raise ValueError("PRIVATE_SENTINEL changed query privileges")
        return self.authority

    def invoke(self):
        with locked_state(self.config) as store:
            return release.start_private_release(
                self.config, store, self.revision, self.plan, repo_root=self.root
            )

    def slot(self):
        return (
            self.config.state_root
            / "releases"
            / self.project.removeprefix("depositar-ai-")
        )

    def test_fresh_start_seals_private_receipts_only_after_required_probe(self):
        result = self.invoke()
        self.assertEqual("private_release_running", result["status"])
        self.assertFalse(result["runtime_reused"])
        self.assertFalse(result["active_changed"])
        self.assertFalse(result["gateway_verified"])
        self.assertFalse(result["search_quality_verified"])
        self.assertFalse(result["ports_published"])
        self.assertTrue(result["two_turn_search_verified"])
        self.assertTrue(result["model_warmup_verified"])
        self.assertEqual(1, self.probe.call_count)
        self.assertEqual(2, self.queries)
        self.assertGreaterEqual(self.runtime_calls, 5)
        self.assertEqual(release.FILES, set(os.listdir(self.slot())))
        for path in self.slot().iterdir():
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertNotIn("PRIVATE_SENTINEL", path.read_text())
        with locked_state(self.config) as store:
            expected = dict(result)
            expected.pop("runtime_reused")
            self.assertEqual(expected, store.read("release-candidate.json"))
        self.assertFalse((self.config.state_root / "active.json").exists())

    def test_completed_reuse_reobserves_authority_and_probe_without_up(self):
        first = self.invoke()
        calls = copy.deepcopy(self.docker.calls)
        second = self.invoke()
        self.assertTrue(second["runtime_reused"])
        self.assertEqual(
            {key: value for key, value in first.items() if key != "runtime_reused"},
            {key: value for key, value in second.items() if key != "runtime_reused"},
        )
        self.assertEqual(2, self.probe.call_count)
        self.assertEqual(4, self.queries)
        self.assertEqual(
            [call for call in calls if call[0] == "compose"],
            [call for call in self.docker.calls if call[0] == "compose"],
        )

    def test_failed_up_keeps_started_intent_and_refuses_blind_retry(self):
        self.docker.fail_up = True
        with self.assertRaises(release.ReleaseError):
            self.invoke()
        self.assertEqual({"started.json", "compose.json"}, set(os.listdir(self.slot())))
        self.assertFalse((self.config.state_root / "release-candidate.json").exists())
        self.docker.fail_up = False
        with self.assertRaisesRegex(release.ReleaseError, "partial_state"):
            self.invoke()
        self.assertEqual(1, sum("up" in call for call in self.docker.calls))
        self.probe.assert_not_called()

    def test_mandatory_probe_flags_cannot_publish_complete(self):
        accepted = dict(self.acceptance)
        for field in (
            "model_warmup_verified",
            "readiness_verified",
            "two_turn_search_verified",
            "session_reset_verified",
        ):
            with self.subTest(field=field):
                self.config = replace(
                    self.config, state_root=self.root / ("state-" + field)
                )
                with locked_state(self.config, create=True):
                    pass
                self.docker.started = False
                self.acceptance = dict(accepted)
                self.acceptance[field] = False
                with self.assertRaisesRegex(release.ReleaseError, "acceptance_blocked"):
                    self.invoke()
                self.assertFalse((self.slot() / "complete.json").exists())

    def test_probe_cannot_claim_gateway(self):
        self.acceptance["gateway_verified"] = True
        with self.assertRaisesRegex(release.ReleaseError, "acceptance_invalid"):
            self.invoke()
        self.assertEqual(
            release.FILES - {"complete.json"}, set(os.listdir(self.slot()))
        )

    def test_post_start_query_drift_keeps_no_completion(self):
        self.query_fail_after_start = True
        with self.assertRaises(release.ReleaseError):
            self.invoke()
        self.assertTrue(self.docker.started)
        self.assertFalse((self.slot() / "complete.json").exists())
        self.assertFalse((self.config.state_root / "release-candidate.json").exists())

    def test_post_start_key_authority_drift_keeps_no_completion(self):
        self.key_drift_after_start = True
        with self.assertRaisesRegex(release.ReleaseError, "authority_changed"):
            self.invoke()
        self.assertTrue(self.docker.started)
        self.assertFalse((self.slot() / "complete.json").exists())

    def test_failed_model_handle_exit_cannot_publish_complete(self):
        self.close_failure = True
        with self.assertRaises(release.ReleaseError):
            self.invoke()
        self.assertTrue(self.docker.started)
        self.assertFalse((self.slot() / "complete.json").exists())

    def test_final_runtime_observation_query_authority_drift_blocks_publication(self):
        def drift(count):
            if count == 5:
                self.authority["fixture_changed"] = True

        self.on_observe = drift
        with self.assertRaises(release.ReleaseError):
            self.invoke()
        self.assertTrue(self.docker.started)
        self.assertFalse((self.slot() / "complete.json").exists())
        self.assertFalse((self.config.state_root / "release-candidate.json").exists())

    def test_final_runtime_observation_source_drift_blocks_publication(self):
        def drift(count):
            if count == 5:
                self.source_changed = True

        self.on_observe = drift
        with self.assertRaises(release.ReleaseError):
            self.invoke()
        self.assertTrue(self.docker.started)
        self.assertFalse((self.slot() / "complete.json").exists())
        self.assertFalse((self.config.state_root / "release-candidate.json").exists())

    def test_final_runtime_observation_key_drift_blocks_publication(self):
        def drift(count):
            if count == 5:
                self.key_drift_after_start = True

        self.on_observe = drift
        with self.assertRaises(release.ReleaseError):
            self.invoke()
        self.assertTrue(self.docker.started)
        self.assertFalse((self.slot() / "complete.json").exists())
        self.assertFalse((self.config.state_root / "release-candidate.json").exists())

    def test_unowned_existing_runtime_is_not_adopted(self):
        self.existing = True
        with self.assertRaisesRegex(
            release.ReleaseError, "existing_runtime_not_adopted"
        ):
            self.invoke()
        self.assertFalse(self.docker.started)
        self.assertFalse(self.slot().exists())
        self.probe.assert_not_called()

    def test_completed_start_identity_drift_is_rejected_before_probe(self):
        self.invoke()
        self.proof["start_identity_sha256"] = "c" * 64
        with self.assertRaisesRegex(release.ReleaseError, "completed_runtime_changed"):
            self.invoke()
        self.assertEqual(1, self.probe.call_count)

    def test_missing_completed_pointer_is_not_reconstructed(self):
        self.invoke()
        (self.config.state_root / "release-candidate.json").unlink()
        with self.assertRaises(release.ReleaseError):
            self.invoke()
        self.assertEqual(1, self.probe.call_count)

    def test_receipt_tamper_and_unexpected_slot_files_are_rejected(self):
        self.invoke()
        path = self.slot() / "unexpected.json"
        path.write_bytes(private_io.canonical({"fixture": True}))
        path.chmod(0o600)
        with self.assertRaisesRegex(release.ReleaseError, "partial_state"):
            self.invoke()
        self.assertEqual(1, self.probe.call_count)

    def test_unsupported_daemon_does_not_create_release_slot(self):
        self.docker.ostype = "windows"
        with self.assertRaisesRegex(release.ReleaseError, "daemon_unsupported"):
            self.invoke()
        self.assertFalse((self.config.state_root / "releases").exists())


if __name__ == "__main__":
    unittest.main()
