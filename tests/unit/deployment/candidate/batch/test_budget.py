"""The largest supported batch fits the bounded Docker command admission.

Use the real Docker budget with a pure fake command runner. The conservative
74-command fixed allowance covers authenticated factory/attach/final refresh
and the two complete audits. Case observations cost twelve commands and model
checkpoints one parent Docker command; their other tools have separate deadlines.
This tests coordinator scheduling, not a measured live execution receipt.
"""

from contextlib import contextmanager
import copy
import subprocess
import unittest

from search.operations.deployment.runtime_docker import Docker
from tests.unit.deployment.candidate.batch import test_execution as fixtures


class SharedBatchBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BatchTests(
            "test_multiturn_capture_can_be_scored_without_reimplementing_quality"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        fixture = self.fixture
        original = fixture.specification["cases"][0]
        fixture.specification["cases"] = [
            {
                "id": f"private-case-{number}",
                "turns": copy.deepcopy(original["turns"][:count]),
            }
            for number, count in enumerate((3, 3, 2), 1)
        ]
        fixture.write_spec()
        self.commands = []

        def runner(argv, *, timeout, environment):
            self.commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, b"{}", b"")

        self.docker = Docker(fixture.root / "fake-client", runner, max_commands=128)
        fixture.session.docker = self.docker

        def consume(count):
            for _ in range(count):
                self.docker.call(["inspect-budget-fixture"])

        self.consume = consume
        audit, observe, check = (
            fixture.verify,
            fixture.session.observe,
            fixture.check_models,
        )

        def verify(*args):
            consume(25)
            return audit(*args)

        def observe_case():
            consume(12)
            observe()

        def checkpoint(*args, **kwargs):
            consume(1)
            return check(*args, **kwargs)

        @contextmanager
        def live(*_args):
            consume(13)  # Factory: eleven current commands plus two reserved.
            yield fixture.session
            consume(7)  # Authenticated image refresh at successful live exit.
            fixture.events.append("live_exit")

        fixture.session.attach_record = lambda _record: consume(4)
        fixture.session.verify = verify
        fixture.session.observe = observe_case
        fixture.session.check_models = checkpoint
        fixture.live = live

    def test_three_cases_eight_turns_fit_without_repeating_complete_audits(self):
        result = self.fixture.run_batch()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["requested_cases"], 3)
        self.assertEqual(result["completed_turns"], 8)
        self.assertEqual(self.fixture.events.count("audit"), 2)
        self.assertEqual(self.fixture.events.count("observe"), 3)
        self.assertEqual(self.fixture.events.count("model_checkpoint"), 8)
        self.assertEqual(self.docker.calls, 118)
        self.assertEqual(len(self.commands), 118)
        self.assertEqual(self.fixture.read("run.json")["status"], "complete")

    def test_selected_catalog_image_inspections_fit_the_same_fixed_budget(self):
        # A second catalog authenticates its three core images independently.
        # Bulk snapshot collection still costs twelve commands for five projects.
        self.consume(3)
        result = self.fixture.run_batch()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["completed_turns"], 8)
        self.assertEqual(self.docker.calls, 121)

    def test_unexpected_command_pressure_cannot_publish_a_trusted_completion(self):
        # A caller's reduced capacity or future extra audit must fail closed.
        # Never replenish the counter, retry queries or raise the daemon budget.
        self.consume(20)
        result = self.fixture.run_batch()
        self.assertEqual(result["status"], "quarantined")
        self.assertFalse(result["identity_verified"])
        self.assertEqual(len(self.commands), 128)
        self.assertLessEqual(self.fixture.probe_calls, 3)
        self.assertEqual(self.fixture.read("run.json")["status"], "quarantined")
        self.assertNotIn("inspect-budget-fixture", str(result))


if __name__ == "__main__":
    unittest.main()
