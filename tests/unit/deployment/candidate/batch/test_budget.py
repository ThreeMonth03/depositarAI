"""Batch scheduling and segmented native-owner Docker command admission.

The original synthetic scheduler fixture retains its 118-command calculation
and pressure regressions. Its fixed allowances alone do not measure provider
load or model checkpoints. Native-owner segments account for the same counter
without resetting setup costs; isolated boundaries need separately measured
upper bounds. All runners are CPU fakes, without live service acceptance.
"""

from contextlib import ExitStack, contextmanager, nullcontext
import copy
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from search.operations.deployment.runtime_docker import Docker
from tests.unit.deployment.candidate.batch import test_execution as fixtures
from tests.unit.deployment.candidate.batch import test_probe as http_fixtures
from tests.unit.deployment.provider import fixtures as provider_fixtures


CONVERSATION = fixtures.execution.probe.conversation
LIVE = fixtures.controller.live


def maximum_batch_fixture(test):
    fixture = fixtures.BatchTests(
        "test_multiturn_capture_can_be_scored_without_reimplementing_quality"
    )
    test.addCleanup(fixture.doCleanups)
    fixture.setUp()
    original = fixture.specification["cases"][0]
    fixture.specification["cases"] = [
        {
            "id": f"private-case-{number}",
            "turns": copy.deepcopy(original["turns"][:count]),
        }
        for number, count in enumerate((3, 3, 2), 1)
    ]
    fixture.write_spec()
    return fixture


def bff_response(row, revision, manifest):
    value = http_fixtures.response(row["response"]["version"])
    value["result"].update(copy.deepcopy(row["response"]["result"]))
    value["result"]["revision"] = revision
    value["result"]["search"].update(
        revision=revision,
        catalog_manifest_sha256=manifest,
        context_used=value["context_used"],
        timing_ms={"retrieve": 1.25, "rerank": 2.5},
    )
    return value


def http_case(fixture, endpoint, queries, revision, manifest, **options):
    fixture.probe_calls += 1
    http = http_fixtures.FakeHTTP(
        [(200, http_fixtures.session(revision=revision))]
        + [
            (200, bff_response(row, revision, manifest))
            for row in fixture.fixture[: len(queries)]
        ]
        + [(200, {"status": "cleared"})]
    )
    probe = fixtures.execution.probe
    with (
        patch.object(probe, "_Connection", side_effect=http.connection),
        patch.object(probe.threading, "Timer", side_effect=http.timer),
    ):
        return CONVERSATION(endpoint, queries, revision, manifest, **options)


class CommandLedger:
    """Attribute the original counter without double-counting nested calls."""

    def __init__(self, docker, commands):
        self.docker, self.commands = docker, commands
        self.depth = 0
        self.phases = [("fixture-setup", docker.calls, len(commands))]

    @contextmanager
    def phase(self, name):
        before, admitted = self.docker.calls, len(self.commands)
        outer = self.depth == 0
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1
            if outer:
                self.phases.append(
                    (
                        name,
                        self.docker.calls - before,
                        len(self.commands) - admitted,
                    )
                )


class NativeBatch:
    """Join real owners; isolate file/model/data inputs, not their counters.

    This is a segmented source-call measurement, not full Provider.load,
    adoption, source provenance or live-service acceptance. The isolated GPU
    and catalog inspections use independent native-caller bounds asserted in
    ProviderRuntimeTests.test_native_gpu_image_owner_performs_four_bounded_
    inspections and test_native_catalog_selection_inspects_three_existing_
    core_images. The CPU image factories and initial collection are already
    charged by shared_checkpoint_evidence and are never subtracted.
    """

    def __init__(self, test, *, separate_catalogs=False, pressure=False):
        self.test = test
        self.fixture = maximum_batch_fixture(test)
        storage_edges = ExitStack()
        test.addCleanup(storage_edges.close)
        storage = fixtures.controller.catalog.execution
        storage_edges.enter_context(
            patch.object(storage, "require_storage", return_value=object())
        )
        storage_edges.enter_context(
            patch.object(storage, "validate_storage_mounts")
        )
        self.evidence = provider_fixtures.shared_checkpoint_evidence(
            test, separate_catalogs=separate_catalogs
        )
        self.provider = self.evidence.provider
        self.session = self.evidence.session
        self.ledger = CommandLedger(
            self.provider.docker, self.evidence.commands
        )
        self.bounds = {"gpu-initial-images": 4}
        self.bounds.update(
            ("catalog-initial-images-" + str(index), 3)
            for index, _binding in enumerate(self.evidence.catalogs)
        )
        self.events = []
        self.drift_after_cases = None
        self._configure()
        self._measure(pressure)

    def _configure(self):
        fixture, evidence = self.fixture, self.evidence
        fixture.profile, fixture.record = evidence.profile, evidence.record
        fixture.control, fixture.session = self.provider.control, self.session
        fixture.live, fixture.probe = LIVE, self.probe
        fixture.load_provider_product = (
            fixtures.execution.provider_runtime.configuration
        )
        fixture.load_product = fixtures.execution.product_config.load_config
        fixture.source_revision = lambda root: (
            "9" * 40
            if Path(root) == fixtures.controller.ROOT
            else evidence.sources[Path(root)]
        )
        identity = evidence.query_catalog.authority["catalog_identity"]
        self.session.query_context["bundle"] = SimpleNamespace(
            bundle_id=identity["bundle_id"],
            manifest_sha256=identity["manifest_sha256"],
            condition_metadata=fixture.fixture[0]["source_metadata"],
        )

    def _wrapper(self, name, operation):
        def measured(*args, **kwargs):
            self.events.append(name)
            with self.ledger.phase(name):
                return operation(*args, **kwargs)

        return measured

    def _measure(self, pressure):
        native_check = self.session.check_models
        check = native_check
        if pressure:

            def check_with_full_audit(*args, **kwargs):
                self.provider.refresh()
                return native_check(*args, **kwargs)

            check = check_with_full_audit
        for name, operation in (
            ("verify", self.session.verify),
            ("observe", self.session.observe),
            ("check_models", check),
            ("attach_record", self.session.attach_record),
        ):
            setattr(self.session, name, self._wrapper(name, operation))
        self.provider.refresh = self._wrapper(
            "full-images", self.provider.refresh
        )

    def provider_factory(self, control, stack, deadline):
        self.test.assertIs(control, self.provider.control)
        self.test.assertLessEqual(self.provider.deadline, deadline)
        self.provider.stack = stack
        return self.provider

    def candidate_inputs(self, record):
        self.test.assertEqual(record, self.evidence.record)
        candidate = self.evidence.candidate
        return (
            candidate.compose,
            candidate.image_binding.images,
            candidate.image_binding.proof,
        )

    def probe(self, *args, **kwargs):
        report = http_case(self.fixture, *args, **kwargs)
        if self.fixture.probe_calls == self.drift_after_cases:
            from search.operations.deployment.images import build

            witness = self.evidence.images[
                self.evidence.candidate.source_revision
            ]
            tag = build.TAGS["web"] + self.evidence.candidate.source_revision
            changed = copy.deepcopy(witness.inspections[tag])
            changed["Id"] = "sha256:" + "0" * 64
            witness.inspections[tag] = changed
        return report

    def run(self):
        controller = fixtures.controller
        runtime = fixtures.execution.provider_runtime
        with ExitStack() as stack:
            for owner, name, options in (
                (runtime, "Provider", {"side_effect": self.provider_factory}),
                (self.provider, "load", {"return_value": None}),
                (controller, "Session", {"return_value": self.session}),
                (
                    controller.catalog,
                    "select",
                    {"return_value": self.evidence.query_catalog},
                ),
                (
                    self.session,
                    "candidate_inputs",
                    {"side_effect": self.candidate_inputs},
                ),
                (
                    controller,
                    "journal",
                    {"return_value": nullcontext(object())},
                ),
            ):
                stack.enter_context(patch.object(owner, name, **options))
            return self.fixture.run_batch(
                expected=self.evidence.record["revision"]
            )

    def report(self):
        return {
            "max_commands": self.provider.docker.max_commands,
            "docker_calls": self.provider.docker.calls,
            "admitted_commands": len(self.evidence.commands),
            "phases": list(self.ledger.phases),
            "isolated_native_bounds": dict(self.bounds),
            "conservative_total": self.provider.docker.calls
            + sum(self.bounds.values()),
            "native_events": list(self.events),
            "probe_calls": self.fixture.probe_calls,
        }


class NativeBatchBudgetTests(unittest.TestCase):
    def run_case(self, *, separate=False, pressure=False, drift=False):
        owner = unittest.TestCase()
        try:
            driver = NativeBatch(
                owner, separate_catalogs=separate, pressure=pressure
            )
            if drift:
                driver.drift_after_cases = 3
            result = driver.run()
            report = driver.report()
            report["receipt"] = driver.fixture.read("run.json")
            return result, report
        finally:
            owner.doCleanups()

    def test_same_and_dual_catalog_eight_turns_fit_segmented_bound(self):
        for separate in (False, True):
            with self.subTest(separate=separate):
                result, report = self.run_case(separate=separate)
                self.assertEqual(result["status"], "complete", report)
                self.assertEqual(result["requested_cases"], 3)
                self.assertEqual(result["requested_turns"], 8)
                self.assertEqual(result["completed_turns"], 8)
                self.assertTrue(result["identity_verified"])
                self.assertEqual(report["max_commands"], 128)
                self.assertEqual(
                    report["phases"][0], ("fixture-setup", 12, 12)
                )
                self.assertEqual(
                    report["docker_calls"], report["admitted_commands"]
                )
                self.assertEqual(
                    sum(row[1] for row in report["phases"]),
                    report["docker_calls"],
                )
                self.assertEqual(
                    sum(row[2] for row in report["phases"]),
                    report["admitted_commands"],
                )
                self.assertLessEqual(report["conservative_total"], 128)
                self.assertEqual(report["native_events"].count("verify"), 2)
                self.assertEqual(
                    report["native_events"].count("check_models"), 10
                )
                self.assertEqual(report["native_events"].count("observe"), 7)
                self.assertEqual(
                    report["native_events"].count("full-images"), 2
                )
                self.assertEqual(report["probe_calls"], 3)

    def test_late_floating_tag_drift_quarantines_all_observed_turns(self):
        result, report = self.run_case(separate=True, drift=True)
        self.assertEqual(result["completed_turns"], 8, report)
        self.assertEqual(result["status"], "quarantined")
        self.assertFalse(result["identity_verified"])
        self.assertEqual(report["receipt"]["status"], "quarantined")
        self.assertEqual(report["native_events"].count("full-images"), 2)
        self.assertEqual(report["native_events"].count("check_models"), 10)
        self.assertEqual(report["phases"][-1][0], "full-images")

    def test_extra_native_full_audits_exhaust_the_original_counter(self):
        result, report = self.run_case(pressure=True)
        self.assertEqual(result["status"], "quarantined", report)
        self.assertFalse(result["identity_verified"])
        self.assertEqual(report["max_commands"], 128)
        self.assertEqual(report["admitted_commands"], 128)
        self.assertGreater(report["docker_calls"], 128)
        self.assertLess(result["completed_turns"], 8)
        self.assertEqual(report["receipt"]["status"], "quarantined")


class SharedBatchBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = maximum_batch_fixture(self)
        fixture = self.fixture
        self.commands = []

        def runner(argv, *, timeout, environment):
            self.commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, b"{}", b"")

        self.docker = Docker(
            fixture.root / "fake-client", runner, max_commands=128
        )
        self.ledger = CommandLedger(self.docker, self.commands)
        fixture.session.docker = self.docker

        def consume(count, *, phase="pressure"):
            with self.ledger.phase(phase):
                for _ in range(count):
                    self.docker.call(["inspect-budget-fixture"])

        self.consume = consume
        audit, observe, check = (
            fixture.verify,
            fixture.session.observe,
            fixture.check_models,
        )

        def verify(*args):
            consume(25, phase="audit")
            return audit(*args)

        def observe_case():
            consume(12, phase="case-observe")
            observe()

        def checkpoint(*args, **kwargs):
            consume(1, phase="model-checkpoint")
            return check(*args, **kwargs)

        @contextmanager
        def live(*_args):
            consume(13, phase="factory-allowance")
            yield fixture.session
            consume(7, phase="exit-allowance")
            fixture.events.append("live_exit")

        fixture.session.attach_record = lambda _record: consume(
            4, phase="attach-allowance"
        )
        fixture.session.verify = verify
        fixture.session.observe = observe_case
        fixture.session.check_models = checkpoint
        fixture.live = live
        fixture.probe = lambda *args, **kwargs: http_case(
            fixture, *args, **kwargs
        )

    def test_three_cases_eight_turns_fit_without_repeating_complete_audits(
        self,
    ):
        result = self.fixture.run_batch()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["requested_cases"], 3)
        self.assertEqual(result["completed_turns"], 8)
        self.assertEqual(self.fixture.events.count("audit"), 2)
        self.assertEqual(self.fixture.events.count("observe"), 3)
        self.assertEqual(self.fixture.events.count("model_checkpoint"), 8)
        self.assertEqual(self.docker.calls, 118)
        self.assertEqual(len(self.commands), 118)
        self.assertEqual(sum(row[1] for row in self.ledger.phases), 118)
        self.assertEqual(sum(row[2] for row in self.ledger.phases), 118)
        self.assertEqual(self.fixture.read("run.json")["status"], "complete")

    def test_selected_catalog_image_inspections_fit_the_same_fixed_budget(
        self,
    ):
        # A second catalog authenticates its three core images independently.
        # Keep the original synthetic per-case observation allowance unchanged.
        self.consume(3)
        result = self.fixture.run_batch()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["completed_turns"], 8)
        self.assertEqual(self.docker.calls, 121)

    def test_unexpected_command_pressure_cannot_publish_a_trusted_completion(
        self,
    ):
        # A caller's reduced capacity or future extra audit must fail closed.
        # Never replenish the counter, retry queries or raise the daemon
        # budget.
        self.consume(20)
        result = self.fixture.run_batch()
        self.assertEqual(result["status"], "quarantined")
        self.assertFalse(result["identity_verified"])
        self.assertEqual(len(self.commands), 128)
        self.assertLessEqual(self.fixture.probe_calls, 3)
        self.assertEqual(
            self.fixture.read("run.json")["status"], "quarantined"
        )
        self.assertNotIn("inspect-budget-fixture", str(result))

    def test_late_control_failure_revokes_capture_without_extra_commands(self):
        self.fixture.control_exit_failure = True
        with self.assertRaises(
            fixtures.execution.capture.SharedBatchCaptureError
        ):
            self.fixture.run_batch()
        self.assertEqual(self.docker.calls, 118)
        self.assertEqual(len(self.commands), 118)
        self.assertFalse((self.fixture.destination / "run.json").exists())
        self.assertEqual(self.fixture.control_depth, 0)


if __name__ == "__main__":
    unittest.main()
