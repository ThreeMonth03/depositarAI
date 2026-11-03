"""Batch lifecycle, audit publication and offline quality interoperability."""

from contextlib import contextmanager, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.quality import batches, contract, evaluator
from search.operations.deployment.candidate import controller
from search.operations.deployment.candidate.batch import execution
from tests.unit.quality.fixtures import turn


REVISION = "a" * 40
MANIFEST = "b" * 64


class ControlFixture:
    def __init__(self, root):
        self.path = root / "control.json"
        self.root = root / "control"
        self.root.mkdir(mode=0o700)
        self.roles = {
            "schema_version": 1,
            "epoch": 1,
            "provider_sha256": "6" * 64,
            "active_backend_sha256": "7" * 64,
            "rollback_backend_sha256": None,
            "tls_plan_ref": {"path": "/private/tls.json", "sha256": "8" * 64},
            "pending_operation_sha256": None,
        }
        self.provider = {
            "source": {"root": str(root / "provider-source")},
            "configuration": {"path": str(root / "provider.json")},
        }

    @property
    def provider_sha256(self):
        return self.roles["provider_sha256"]

    @property
    def roles_sha256(self):
        return controller.digest(self.roles)


class BatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="shared-batch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root.chmod(0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.destination = self.root / "capture"
        self.config = self.root / "config.json"
        self.specfile = self.root / "cases.json"
        self.fixture = [
            turn({"start": "2020", "end": "2025"}),
            turn(
                {"start": "2024", "end": "2024"}, version=2, retain_topic=True
            ),
            turn(
                {"start": "2024", "end": "2024"},
                version=3,
                retain_topic=True,
                formats=["CSV"],
            ),
        ]
        self.specification = {
            "schema_version": 1,
            "cases": [
                {
                    "id": "PRIVATE_CASE",
                    "turns": [
                        {key: item[key] for key in ("query", "expected")}
                        for item in self.fixture
                    ],
                }
            ],
        }
        self.specification["cases"][0]["turns"][0]["query"] = "PRIVATE_QUERY"
        self.write_spec()
        self.profile = {
            "state_root": self.state,
            "control_config": self.root / "control.json",
            "catalog_config": self.root / "catalog.json",
            "catalog_source": self.root / "catalog-source",
        }
        self.control = ControlFixture(self.root)
        self.product = SimpleNamespace(
            **{
                name: self.root / name
                for name in (
                    "state_root",
                    "api_model_root",
                    "embedding_model_root",
                    "reranker_model_root",
                )
            }
        )
        self.selected_product = SimpleNamespace(
            **(
                vars(self.product)
                | {"state_root": self.root / "selected-state"}
            )
        )
        self.record = {
            "phase": "running",
            "revision": REVISION,
            "policy": {"policy": {}, "request_policy_sha256": "c" * 64},
            "compute_sha256": "d" * 64,
            "runtime": {"start_identity_sha256": "e" * 64},
        }
        metadata = self.fixture[0]["source_metadata"]
        self.baseline_context = {
            "source_revision": "f" * 40,
            "plan": {"manifest_sha256": MANIFEST},
            "bundle": SimpleNamespace(
                bundle_id="1" * 64,
                manifest_sha256=MANIFEST,
                condition_metadata=metadata,
            ),
        }
        self.session = SimpleNamespace(
            control=self.control,
            provider=SimpleNamespace(
                source_revision="f" * 40,
                model_manifest_sha256="2" * 64,
                inference_images={"reranker": {"Id": "sha256:" + "3" * 64}},
                provider_sha256=self.control.provider_sha256,
                backends=[SimpleNamespace(source_revision="5" * 40)],
            ),
            docker=SimpleNamespace(deadline=time.monotonic() + 1800),
            candidate_endpoint="172.20.0.4",
            attach_record=lambda _record: None,
        )
        self.session.query_context = self.baseline_context
        self.session.query_authority = {
            "catalog_identity": {"manifest_sha256": MANIFEST}
        }
        self.events = []
        self.control_events = []
        self.control_depth = 0
        self.control_exit_failure = False
        self.provider_config_failure = False
        self.audit_failure = None
        self.audit_exception = None
        self.exit_failure = False
        self.session.verify = self.verify
        self.session.observe = lambda: self.events.append("observe")
        self.session.check_models = self.check_models
        self.session.remaining = self.remaining
        self.report_error = None
        self.probe_calls = 0
        self.source_checks = 0
        self.operator_changed = False

    def source_revision(self, _root):
        self.source_checks += 1
        return (
            "8" * 40
            if self.operator_changed and self.source_checks >= 3
            else "9" * 40
        )

    def write_spec(self):
        self.specfile.write_text(
            json.dumps(self.specification), encoding="utf-8"
        )
        self.specfile.chmod(0o600)

    def verify(self, *_args):
        self.events.append("audit")
        if self.audit_exception is not None:
            raise self.audit_exception
        if self.audit_failure == self.events.count("audit"):
            raise controller.SharedBackendError(
                "shared_backend_baseline_query_audit_failed"
            )
        return {"models": [{"role": "scope", "digest": "4" * 64}]}

    def check_models(self, *_args, **_kwargs):
        self.events.append("model_checkpoint")

    def remaining(self, maximum, *, deadline=None):
        controller.require(
            deadline is None or time.monotonic() < deadline,
            "batch_deadline_exceeded",
        )
        return maximum

    @contextmanager
    def controlled(self, profile):
        self.control_depth += 1
        self.control_events.append("enter")
        try:
            yield profile | {"_control": self.control}
            if self.control_exit_failure:
                raise controller.ProviderError(
                    "shared_backend_provider_authority_changed"
                )
        finally:
            self.control_depth -= 1
            self.control_events.append("exit")

    def load_product(self, path, *, repo_root):
        self.assertEqual(path, self.profile["catalog_config"])
        self.assertEqual(repo_root, self.profile["catalog_source"])
        return self.selected_product

    def load_provider_product(self, reference, source_root):
        self.assertEqual(reference, self.control.provider["configuration"])
        self.assertEqual(source_root, self.root / "provider-source")
        self.assertEqual(self.control_depth, 1)
        if self.provider_config_failure:
            raise controller.ProviderError(
                "shared_backend_provider_authority_changed"
            )
        return self.product

    @contextmanager
    def live(self, *_args):
        yield self.session
        self.events.append("live_exit")
        if self.exit_failure:
            raise ValueError("PRIVATE_FAILED_PATH")

    def probe(
        self, _endpoint, queries, revision, manifest, *, checkpoint, deadline
    ):
        self.probe_calls += 1
        self.assertEqual(revision, REVISION)
        self.assertEqual(
            manifest, self.session.query_context["plan"]["manifest_sha256"]
        )
        self.assertLess(deadline, self.session.docker.deadline)
        rows = []
        for index, _query in enumerate(queries):
            checkpoint()
            response = copy.deepcopy(self.fixture[index]["response"])
            response["context_used"] = {"actual_reference_clock": "preserved"}
            response["result"]["search"]["cards"][0]["title"] = "PRIVATE_TITLE"
            if self.report_error is not None and index == 1:
                rows.append(
                    {
                        "status": "failed",
                        "wall_seconds": 0.3,
                        "request_seconds": 0.2,
                        "error": self.report_error,
                    }
                )
                break
            rows.append(
                {
                    "status": "ok",
                    "response": response,
                    "wall_seconds": 0.4,
                    "request_seconds": 0.2,
                }
            )
        return {
            "status": "complete" if self.report_error is None else "failed",
            "turns": rows,
            "error": self.report_error,
            "session_reset": True,
        }

    def run_batch(self, *, expected=REVISION, expected_identity_sha256=None):
        with (
            patch.object(
                controller,
                "require_clean_revision",
                side_effect=self.source_revision,
            ),
            patch.object(controller, "load_config", return_value=self.profile),
            patch.object(
                controller, "controlled", side_effect=self.controlled
            ),
            patch.object(controller, "current", return_value=self.record),
            patch.object(controller, "live", side_effect=self.live),
            patch.object(
                execution.product_config,
                "load_config",
                side_effect=self.load_product,
            ),
            patch.object(
                execution.provider_runtime,
                "configuration",
                side_effect=self.load_provider_product,
            ),
            patch.object(
                execution.probe, "conversation", side_effect=self.probe
            ),
        ):
            return execution.run_batch(
                self.config,
                cases=self.specfile,
                destination=self.destination,
                expected_revision=expected,
                expected_identity_sha256=expected_identity_sha256,
            )

    def read(self, name):
        return json.loads((self.destination / name).read_text())

    def status(self, *, batch_identity=False):
        with (
            patch.object(
                controller,
                "require_clean_revision",
                side_effect=self.source_revision,
            ),
            patch.object(controller, "load_config", return_value=self.profile),
            patch.object(
                controller, "controlled", side_effect=self.controlled
            ),
            patch.object(controller, "current", return_value=self.record),
            patch.object(controller, "live", side_effect=self.live),
        ):
            return controller.operate(
                self.config, "status", batch_identity=batch_identity
            )

    def test_multiturn_capture_can_be_scored_without_reimplementing_quality(
        self,
    ):
        result = self.run_batch()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["completed_turns"], 3)
        self.assertEqual(
            result["plan_sha256"], contract.plan_digest(self.specification)
        )
        self.assertEqual(self.events.count("audit"), 2)
        self.assertEqual(self.events.count("model_checkpoint"), 3)
        self.assertEqual(self.events[-1], "live_exit")
        captured = self.read("capture.json")
        self.assertEqual(
            self.read("run.json")["artifacts"]["capture"]["sha256"],
            hashlib.sha256(
                (self.destination / "capture.json").read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(evaluator.evaluate(captured)["status"], "passed")
        replies = captured["cases"][0]["turns"]
        self.assertEqual(
            [row["response"]["version"] for row in replies], [1, 2, 3]
        )
        self.assertEqual(
            replies[1]["response"]["context_used"],
            {"actual_reference_clock": "preserved"},
        )
        serialized = json.dumps(result) + json.dumps(self.read("run.json"))
        for private in ("PRIVATE_QUERY", "PRIVATE_CASE", "PRIVATE_TITLE"):
            self.assertNotIn(private, serialized)
        self.assertIn("PRIVATE_QUERY", json.dumps(captured))

    def expected_identity(self):
        return execution.runtime_identity(
            self.session,
            self.record,
            {"models": [{"role": "scope", "digest": "4" * 64}]},
            "9" * 40,
        )

    def test_matching_full_identity_keeps_audits_and_existing_capture_contract(
        self,
    ):
        expected = self.expected_identity()
        canonical = (
            json.dumps(
                expected,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        result = self.run_batch(
            expected_identity_sha256=hashlib.sha256(canonical).hexdigest()
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.probe_calls, 1)
        self.assertEqual(self.events.count("audit"), 2)
        self.assertEqual(self.events.count("model_checkpoint"), 3)
        receipt = self.read("run.json")
        self.assertEqual(receipt["identity"], expected)
        self.assertNotIn("expected_identity_sha256", receipt)
        report = batches.aggregate(
            self.specification,
            [
                {
                    "plan": self.specification,
                    "files": {
                        name: (self.destination / name).read_bytes()
                        for name in (
                            "run.json",
                            "capture.json",
                            "outcomes.json",
                        )
                    },
                }
            ],
        )
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["summary"]["evidence_complete_turn_count"], 3)

    def test_capture_identity_separates_provider_and_active_backend(self):
        self.assertEqual(self.run_batch()["status"], "complete")
        identity = self.read("run.json")["identity"]
        self.assertEqual(identity["production_revision"], "f" * 40)
        self.assertEqual(identity["active_backend_revision"], "5" * 40)
        self.assertNotEqual(
            identity["production_revision"],
            identity["active_backend_revision"],
        )
        self.assertEqual(identity["provider_anchor_sha256"], "6" * 64)
        self.assertEqual(identity["control_epoch"], 1)
        self.assertEqual(
            identity["control_roles_sha256"],
            controller.digest(self.control.roles),
        )
        self.assertEqual(identity["model_manifest_sha256"], "2" * 64)
        self.assertEqual(
            identity["gpu_image_ids"],
            {"reranker": "sha256:" + "3" * 64},
        )

    def test_provider_and_roles_drift_refuse_the_enqueued_identity(self):
        expected = self.expected_identity()
        retained = copy.deepcopy(expected)
        expected_digest = controller.digest(expected)
        input_bytes = self.specfile.read_bytes()
        roles = copy.deepcopy(self.control.roles)
        provider_digest = self.session.provider.provider_sha256
        active_revision = self.session.provider.backends[0].source_revision
        for field in ("provider", "epoch", "active_backend"):
            with self.subTest(field=field):
                self.destination = self.root / ("enqueued-" + field)
                self.control.roles = copy.deepcopy(roles)
                self.session.provider.provider_sha256 = provider_digest
                self.session.provider.backends[
                    0
                ].source_revision = active_revision
                if field == "provider":
                    self.control.roles["provider_sha256"] = "8" * 64
                    self.session.provider.provider_sha256 = "8" * 64
                elif field == "epoch":
                    self.control.roles["epoch"] += 1
                else:
                    self.control.roles["active_backend_sha256"] = "8" * 64
                    self.session.provider.backends[0].source_revision = (
                        "7" * 40
                    )
                result = self.run_batch(
                    expected_identity_sha256=expected_digest
                )
                self.assertEqual(result["status"], "quarantined")
                self.assertEqual(
                    result["error"], "shared_backend_batch_identity_changed"
                )
                self.assertFalse(result["identity_verified"])
                self.assertEqual(result["completed_turns"], 0)
                self.assertEqual(self.probe_calls, 0)
                self.assertEqual(self.read("capture.json")["cases"], [])
                self.assertEqual(expected, retained)
                self.assertEqual(self.specfile.read_bytes(), input_bytes)

    def test_invalid_epoch_or_mixed_provider_context_refuses_queries(self):
        for epoch in (0, -1, True, 1.0, 2**63):
            with self.subTest(epoch=epoch):
                self.control.roles["epoch"] = epoch
                with self.assertRaisesRegex(
                    controller.SharedBackendError,
                    "^shared_backend_batch_control_invalid$",
                ):
                    self.expected_identity()
        self.control.roles["epoch"] = 1
        self.session.provider.provider_sha256 = "8" * 64
        result = self.run_batch()
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(
            result["error"], "shared_backend_batch_provider_binding_changed"
        )
        self.assertEqual(self.probe_calls, 0)

    def test_largest_control_epoch_is_preserved_without_coercion(self):
        self.control.roles["epoch"] = 2**63 - 1
        self.assertEqual(self.run_batch()["status"], "complete")
        value = self.read("run.json")["identity"]["control_epoch"]
        self.assertIs(type(value), int)
        self.assertEqual(value, 2**63 - 1)

    def test_control_wraps_journal_and_capture_is_published_last(self):
        original_journal = controller.journal
        original_publish = execution.capture._Capture.publish

        @contextmanager
        def journal(root):
            self.assertEqual(self.control_depth, 1)
            self.control_events.append("journal_enter")
            with original_journal(root) as store:
                yield store
            self.control_events.append("journal_exit")

        def publish(sink):
            self.assertEqual(self.control_depth, 0)
            self.assertEqual(self.control_events[-1], "exit")
            self.control_events.append("publish")
            return original_publish(sink)

        with (
            patch.object(controller, "journal", side_effect=journal),
            patch.object(execution.capture._Capture, "publish", publish),
        ):
            self.assertEqual(self.run_batch()["status"], "complete")
        self.assertEqual(
            self.control_events,
            ["enter", "journal_enter", "journal_exit", "exit", "publish"],
        )

    def test_late_control_failure_cannot_publish_a_complete_capture(self):
        self.control_exit_failure = True
        with self.assertRaisesRegex(
            execution.capture.SharedBatchCaptureError,
            "^shared_batch_capture_failed$",
        ):
            self.run_batch()
        self.assertEqual(self.events.count("audit"), 2)
        self.assertEqual(self.probe_calls, 1)
        self.assertEqual(self.control_depth, 0)
        self.assertFalse((self.destination / "run.json").exists())
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_provider_failure_keeps_closed_capture_diagnostics(self):
        self.audit_exception = controller.ProviderError(
            "shared_backend_provider_cas_conflict"
        )
        result = self.run_batch()
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(
            result["error"], "shared_backend_provider_cas_conflict"
        )
        self.assertFalse(result["identity_verified"])
        self.assertEqual(self.probe_calls, 0)

    def test_capture_cannot_overlap_control_or_provider_dependencies(self):
        for number, protected in enumerate(
            (
                self.control.root / "capture",
                self.control.path,
                self.root / "provider.json",
                self.root / "provider-source" / "capture",
                self.product.state_root / "capture",
                self.product.api_model_root / "capture",
                self.product.embedding_model_root / "capture",
                self.product.reranker_model_root / "capture",
                self.selected_product.state_root / "capture",
                self.profile["catalog_source"] / "capture",
                self.state / "capture",
            )
        ):
            with self.subTest(protected=number):
                self.destination = protected
                with self.assertRaisesRegex(
                    execution.capture.SharedBatchCaptureError,
                    "^shared_batch_capture_excluded_overlap$",
                ):
                    self.run_batch()
                self.assertFalse(protected.exists())
                self.assertEqual(self.probe_calls, 0)

    def test_changed_provider_config_refuses_before_capture_creation(self):
        self.provider_config_failure = True
        with self.assertRaisesRegex(
            controller.ProviderError,
            "^shared_backend_provider_authority_changed$",
        ):
            self.run_batch()
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.probe_calls, 0)
        self.assertEqual(self.events, [])

    def test_readonly_status_digest_matches_batch_receipt_without_extra_audit(
        self,
    ):
        result = self.status(batch_identity=True)
        self.assertEqual(
            set(result),
            {
                "status",
                "revision",
                "models_restarted",
                "public_route_changed",
                "runtime_verified",
                "batch_identity_sha256",
            },
        )
        self.assertEqual(self.events, ["audit", "live_exit"])
        self.assertEqual(self.probe_calls, 0)
        self.assertFalse(self.destination.exists())
        self.assertEqual(
            self.run_batch(
                expected_identity_sha256=result["batch_identity_sha256"]
            )["status"],
            "complete",
        )
        self.assertEqual(
            result["batch_identity_sha256"],
            controller.digest(self.read("run.json")["identity"]),
        )

    def test_readonly_status_digest_is_bound_to_the_selected_catalog(self):
        original = self.status(batch_identity=True)["batch_identity_sha256"]
        selected = copy.deepcopy(self.baseline_context)
        selected["source_revision"] = "7" * 40
        selected["plan"]["manifest_sha256"] = "8" * 64
        selected["bundle"].manifest_sha256 = "8" * 64
        selected["bundle"].bundle_id = "9" * 64
        self.session.query_context = selected
        self.session.query_authority = {
            "catalog_identity": {"manifest_sha256": "8" * 64}
        }
        for value in self.fixture:
            value["response"]["result"]["search"][
                "catalog_manifest_sha256"
            ] = "8" * 64
        result = self.status(batch_identity=True)
        self.assertNotEqual(result["batch_identity_sha256"], original)
        self.assertEqual(self.probe_calls, 0)
        self.assertEqual(self.events.count("audit"), 2)
        self.assertEqual(
            self.run_batch(
                expected_identity_sha256=result["batch_identity_sha256"]
            )["status"],
            "complete",
        )
        identity = self.read("run.json")["identity"]
        self.assertEqual(identity["production_revision"], "f" * 40)
        self.assertEqual(identity["catalog_source_revision"], "7" * 40)
        self.assertEqual(
            result["batch_identity_sha256"], controller.digest(identity)
        )

    def test_manual_status_omits_digest_and_requires_no_operator_source_check(
        self,
    ):
        result = self.status()
        self.assertEqual(
            result,
            {
                "status": "running",
                "revision": REVISION,
                "models_restarted": False,
                "public_route_changed": False,
                "runtime_verified": True,
            },
        )
        self.assertEqual(self.source_checks, 0)
        self.assertEqual(self.events, ["audit", "live_exit"])

    def test_readonly_identity_refuses_stopped_or_empty_candidates(self):
        original = self.record
        for record in (None, original | {"phase": "stopped"}):
            self.record = record
            with self.assertRaisesRegex(
                controller.SharedBackendError,
                "^shared_backend_candidate_not_running$",
            ):
                self.status(batch_identity=True)
            self.assertEqual(
                self.status()["status"],
                "empty" if record is None else "stopped",
            )
            self.assertEqual(self.events, [])
            self.assertFalse(self.destination.exists())

    def test_readonly_identity_refuses_revision_drift_at_both_boundaries(
        self,
    ):
        for revisions in (
            ("9" * 40, "8" * 40),
            ("9" * 40, "9" * 40, "8" * 40),
        ):
            with self.subTest(checks=len(revisions)):
                values = iter(revisions)
                self.source_revision = lambda _root: next(values)
                with self.assertRaisesRegex(
                    controller.SharedBackendError,
                    "^shared_backend_batch_operator_changed$",
                ):
                    self.status(batch_identity=True)
                self.assertEqual(self.probe_calls, 0)
                self.assertFalse(self.destination.exists())

    def test_identity_status_requires_status_operation_and_boolean(
        self,
    ):
        for operation, flag in (
            ("query", True),
            ("prepare", True),
            ("start", True),
            ("stop", True),
            ("status", 1),
            ("status", None),
        ):
            with self.subTest(operation=operation, flag=flag):
                with self.assertRaisesRegex(
                    controller.SharedBackendError,
                    "^shared_backend_batch_identity_operation_invalid$",
                ):
                    controller.operate(
                        self.config, operation, batch_identity=flag
                    )

    def test_identity_drift_is_refused_before_queries_and_retains_final_audit(
        self,
    ):
        expected = self.expected_identity()
        expected_sha256 = controller.digest(expected)
        for field in (
            "catalog_manifest_sha256",
            "catalog_source_revision",
            "model_manifest_sha256",
            "production_revision",
            "operator_revision",
            "candidate_runtime",
            "provider_anchor_sha256",
            "control_roles_sha256",
            "active_backend_revision",
            "control_epoch",
        ):
            with self.subTest(field=field):
                self.destination = self.root / field
                actual = copy.deepcopy(expected)
                actual[field] = (
                    {"start_identity_sha256": "7" * 64}
                    if field == "candidate_runtime"
                    else 2
                    if field == "control_epoch"
                    else "7" * (40 if field.endswith("revision") else 64)
                )
                before = self.events.count("audit")
                with patch.object(
                    execution, "runtime_identity", return_value=actual
                ):
                    result = self.run_batch(
                        expected_identity_sha256=expected_sha256
                    )
                self.assertEqual(result["status"], "quarantined")
                self.assertEqual(
                    result["error"], "shared_backend_batch_identity_changed"
                )
                self.assertEqual(result["completed_cases"], 0)
                self.assertEqual(result["completed_turns"], 0)
                self.assertFalse(result["identity_verified"])
                self.assertEqual(self.probe_calls, 0)
                self.assertEqual(self.events.count("audit") - before, 2)
                self.assertEqual(self.read("capture.json")["cases"], [])
                self.assertEqual(self.read("run.json")["identity"], actual)
                self.assertEqual(
                    [
                        turn["status"]
                        for turn in self.read("outcomes.json")["cases"][0][
                            "turns"
                        ]
                    ],
                    ["not_run"] * 3,
                )

    def test_future_identity_fields_participate_in_the_same_full_digest(self):
        original = self.expected_identity()
        future = original | {"future_binding": {"source_sha256": "7" * 64}}
        for number, expected in enumerate((original, future)):
            with self.subTest(expected_future=number):
                self.destination = self.root / f"future-{number}"
                with patch.object(
                    execution, "runtime_identity", return_value=future
                ):
                    result = self.run_batch(
                        expected_identity_sha256=controller.digest(expected)
                    )
                self.assertEqual(
                    result["status"], "complete" if number else "quarantined"
                )
                self.assertEqual(self.probe_calls, number)
                self.assertEqual(self.read("run.json")["identity"], future)

    def test_identity_comparison_is_inside_the_existing_operation_context(
        self,
    ):
        retained = False
        original_live, original_digest = self.live, controller.digest

        @contextmanager
        def live(*args):
            nonlocal retained
            with original_live(*args) as session:
                retained = True
                try:
                    yield session
                finally:
                    retained = False

        compared = []

        def digest(value):
            if "candidate_revision" in value:
                self.assertTrue(retained)
                self.assertEqual(self.events.count("audit"), 1)
                with self.assertRaisesRegex(
                    controller.SharedBackendError, "^shared_backend_busy$"
                ):
                    with controller.journal(self.state):
                        self.fail("batch journal lock was released")
                compared.append(True)
            return original_digest(value)

        self.live = live
        expected = original_digest(self.expected_identity())
        with patch.object(controller, "digest", side_effect=digest):
            result = self.run_batch(expected_identity_sha256=expected)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(compared, [True])
        self.assertFalse(retained)

    def test_malformed_identity_digest_is_rejected_before_mutable_work(self):
        for value in (
            "",
            "a" * 63,
            "a" * 65,
            "A" * 64,
            "a" * 64 + "\n",
            " " + "a" * 64,
            b"a" * 64,
            True,
            1,
            [],
            {},
            "PRIVATE_DIGEST",
        ):
            with self.subTest(
                value_type=type(value).__name__, length=len(str(value))
            ):
                with self.assertRaisesRegex(
                    controller.SharedBackendError,
                    "^shared_backend_batch_identity_invalid$",
                ):
                    self.run_batch(expected_identity_sha256=value)
                self.assertFalse(self.destination.exists())
                self.assertEqual(self.probe_calls, 0)
                self.assertEqual(self.source_checks, 0)
                self.assertEqual(self.events, [])

    def test_selected_catalog_supplies_manifest_and_metadata_for_shared_ids(
        self,
    ):
        selected_metadata = copy.deepcopy(self.fixture[0]["source_metadata"])
        for value in selected_metadata.values():
            value["resource_formats"] = ["PDF"]
        self.session.query_context = {
            "source_revision": "7" * 40,
            "plan": {"manifest_sha256": "8" * 64},
            "bundle": SimpleNamespace(
                bundle_id="9" * 64,
                manifest_sha256="8" * 64,
                condition_metadata=selected_metadata,
            ),
        }
        self.session.query_authority = {
            "catalog_identity": {"manifest_sha256": "8" * 64}
        }
        for value in self.fixture:
            value["response"]["result"]["search"][
                "catalog_manifest_sha256"
            ] = "8" * 64
        self.assertEqual(self.run_batch()["status"], "complete")
        identity = self.read("run.json")["identity"]
        self.assertEqual(identity["production_revision"], "f" * 40)
        self.assertEqual(identity["catalog_source_revision"], "7" * 40)
        self.assertEqual(identity["catalog_manifest_sha256"], "8" * 64)
        self.assertEqual(identity["bundle_id"], "9" * 64)
        for value in self.read("capture.json")["cases"][0]["turns"]:
            self.assertEqual(value["source_metadata"], selected_metadata)
            self.assertNotEqual(
                value["source_metadata"],
                self.baseline_context["bundle"].condition_metadata,
            )

    def test_foreign_bundle_or_query_authority_quarantines_before_queries(
        self,
    ):
        for kind in ("bundle", "authority"):
            with self.subTest(kind=kind):
                self.destination = self.root / kind
                self.session.query_context = copy.deepcopy(
                    self.baseline_context
                )
                self.session.query_authority = {
                    "catalog_identity": {"manifest_sha256": MANIFEST}
                }
                if kind == "bundle":
                    self.session.query_context["bundle"].manifest_sha256 = (
                        "8" * 64
                    )
                else:
                    self.session.query_authority["catalog_identity"][
                        "manifest_sha256"
                    ] = "8" * 64
                result = self.run_batch()
                self.assertEqual(result["status"], "quarantined")
                self.assertEqual(
                    result["error"],
                    "shared_backend_batch_catalog_binding_changed",
                )
                self.assertEqual(self.probe_calls, 0)

    def test_busy_stops_remaining_turns_and_cases_without_retry(self):
        self.specification["cases"].append(
            {
                "id": "later",
                "turns": [
                    copy.deepcopy(self.specification["cases"][0]["turns"][0])
                ],
            }
        )
        self.write_spec()
        self.report_error = "shared_batch_busy"
        result = self.run_batch()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["identity_verified"])
        self.assertEqual(self.probe_calls, 1)
        self.assertEqual(result["completed_turns"], 1)
        outcomes = self.read("outcomes.json")["cases"]
        self.assertEqual(
            [row["status"] for row in outcomes[0]["turns"]],
            ["ok", "failed", "not_run"],
        )
        self.assertEqual(outcomes[1]["status"], "not_run")
        self.assertEqual(self.events.count("audit"), 2)

    def test_post_audit_or_late_fd_failure_quarantines_results(self):
        for failure in ("audit", "exit"):
            with self.subTest(failure=failure):
                self.destination = self.root / ("capture-" + failure)
                self.events.clear()
                self.audit_failure = 2 if failure == "audit" else None
                self.exit_failure = failure == "exit"
                result = self.run_batch()
                self.assertEqual(result["status"], "quarantined")
                self.assertFalse(self.read("run.json")["identity_verified"])
                self.assertEqual(len(self.read("capture.json")["cases"]), 1)
                self.assertNotIn("PRIVATE_FAILED_PATH", json.dumps(result))

    def test_wrong_candidate_or_occupied_slot_never_creates_capture(self):
        with self.assertRaisesRegex(
            controller.SharedBackendError, "batch_candidate_changed"
        ):
            self.run_batch(expected="f" * 40)
        self.assertFalse(self.destination.exists())
        with controller.journal(self.state):
            with self.assertRaisesRegex(
                controller.SharedBackendError, "shared_backend_busy"
            ):
                self.run_batch()
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.probe_calls, 0)

    def test_audit_failure_before_query_records_unexecuted_suite(self):
        self.audit_failure = 1
        result = self.run_batch()
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["completed_turns"], 0)
        self.assertEqual(self.probe_calls, 0)
        self.assertEqual(
            self.read("outcomes.json")["cases"][0]["status"], "not_run"
        )

    def test_batch_reserves_final_audit_before_starting_queries(self):
        self.session.docker.deadline = time.monotonic() + 100
        result = self.run_batch()
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["completed_turns"], 0)
        self.assertEqual(self.probe_calls, 0)
        self.assertEqual(self.events.count("audit"), 2)

    def test_changed_operator_after_execution_cannot_publish_complete_receipt(
        self,
    ):
        self.operator_changed = True
        result = self.run_batch()
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(
            result["error"], "shared_backend_batch_operator_changed"
        )
        self.assertEqual(result["operator_revision"], "9" * 40)
        self.assertFalse(self.read("run.json")["identity_verified"])

    def test_changed_bff_endpoint_prevents_query_and_retains_final_audit(self):
        self.session.observe = lambda: setattr(
            self.session, "candidate_endpoint", "172.20.0.5"
        )
        result = self.run_batch()
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(
            result["error"], "shared_backend_batch_endpoint_changed"
        )
        self.assertEqual(self.probe_calls, 0)
        self.assertEqual(self.events.count("audit"), 2)

    def test_incomplete_probe_cannot_claim_a_completed_case(self):
        report = {
            "status": "complete",
            "turns": [],
            "error": None,
            "session_reset": True,
        }
        with self.assertRaisesRegex(
            controller.SharedBackendError, "batch_probe_invalid"
        ):
            execution._observations(
                self.specification["cases"][0],
                report,
                self.baseline_context["bundle"],
            )


class BatchCLITests(unittest.TestCase):
    def arguments(self):
        return [
            "--config",
            "/private/config.json",
            "batch",
            "--cases",
            "/private/cases.json",
            "--capture",
            "/private/new-capture",
            "--expected-revision",
            REVISION,
        ]

    def test_optional_identity_preserves_manual_and_pinned_invocations(
        self,
    ):
        for expected in (None, "b" * 64):
            with self.subTest(pinned=expected is not None):
                argv = self.arguments()
                if expected is not None:
                    argv += ["--expected-identity-sha256", expected]
                output = io.StringIO()
                with (
                    patch.object(
                        execution,
                        "run_batch",
                        return_value={"status": "complete"},
                    ) as run,
                    redirect_stdout(output),
                ):
                    self.assertEqual(controller.main(argv), 0)
                self.assertEqual(
                    run.call_args.kwargs["expected_identity_sha256"], expected
                )
                self.assertEqual(
                    json.loads(output.getvalue()), {"status": "complete"}
                )

    def test_malformed_cli_digest_uses_closed_diagnostic_without_queries(self):
        output = io.StringIO()
        with (
            patch.object(controller, "load_config") as config,
            patch.object(execution.probe, "conversation") as query,
            redirect_stdout(output),
        ):
            code = controller.main(
                self.arguments()
                + ["--expected-identity-sha256", "PRIVATE_DIGEST"]
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"error": "shared_backend_batch_identity_invalid"},
        )
        self.assertNotIn("PRIVATE", output.getvalue())
        config.assert_not_called()
        query.assert_not_called()

    def test_status_cli_exposes_only_the_explicit_optional_digest(self):
        for selected in (False, True):
            with self.subTest(batch_identity=selected):
                argv = ["--config", "/private/config.json", "status"]
                if selected:
                    argv.append("--batch-identity")
                value = {"status": "running", "revision": REVISION}
                if selected:
                    value["batch_identity_sha256"] = "b" * 64
                output = io.StringIO()
                with (
                    patch.object(
                        controller, "operate", return_value=value
                    ) as operate,
                    redirect_stdout(output),
                ):
                    self.assertEqual(controller.main(argv), 0)
                self.assertIs(
                    operate.call_args.kwargs["batch_identity"], selected
                )
                self.assertEqual(json.loads(output.getvalue()), value)

    def test_dirty_source_refuses_identity_before_config_or_queries(
        self,
    ):
        output = io.StringIO()
        with (
            patch.object(
                controller,
                "require_clean_revision",
                side_effect=ValueError("product_source_dirty"),
            ),
            patch.object(controller, "load_config") as config,
            patch.object(execution.probe, "conversation") as query,
            redirect_stdout(output),
        ):
            code = controller.main(
                [
                    "--config",
                    "/private/config.json",
                    "status",
                    "--batch-identity",
                ]
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output.getvalue()), {"error": "shared_backend_failed"}
        )
        self.assertNotIn("batch_identity_sha256", output.getvalue())
        config.assert_not_called()
        query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
