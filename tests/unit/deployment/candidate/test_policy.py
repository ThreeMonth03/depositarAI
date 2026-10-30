"""Reviewed request compatibility is checked offline, without importing sources."""

from pathlib import Path
import shutil
import tempfile
import unittest

from search.operations.deployment.candidate import policy


ROOT = Path(__file__).resolve().parents[4]


class SharedRequestPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="shared-request-policy-")
        self.addCleanup(temporary.cleanup)
        self.baseline = Path(temporary.name) / "baseline"
        self.candidate = Path(temporary.name) / "candidate"
        for root in (self.baseline, self.candidate):
            for relative in policy.SOURCE_FILES:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)

    def invoke(self):
        return policy.validate_shared_request_policy(self.baseline, self.candidate)

    def change(self, relative, before, after):
        path = self.candidate / relative
        source = path.read_text()
        self.assertIn(before, source)
        path.write_text(source.replace(before, after, 1))

    def reset(self):
        for relative in policy.SOURCE_FILES:
            shutil.copyfile(self.baseline / relative, self.candidate / relative)

    def reject(self):
        with self.assertRaises(policy.SharedRequestPolicyError) as caught:
            self.invoke()
        self.assertRegex(str(caught.exception), r"^shared_request_policy_[a-z_]+$")
        self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))
        self.assertNotIn(str(self.candidate), str(caught.exception))

    def test_current_requests_have_exact_shared_context_and_production_residency(self):
        result = self.invoke()
        self.assertEqual(result["status"], "compatible")
        self.assertEqual(len(result["request_policy_sha256"]), 64)
        self.assertEqual(self.invoke(), result)
        projected = result["policy"]
        self.assertEqual(projected["scope"]["options"]["num_ctx"], 4096)
        self.assertEqual(projected["scope"]["options"]["num_predict"], 64)
        self.assertEqual(projected["planner"]["options"]["num_ctx"], 16384)
        self.assertEqual(projected["planner"]["options"]["num_predict"], 2048)
        self.assertEqual(projected["planner"]["think"], "low")
        for name in ("scope", "planner"):
            self.assertIs(projected[name]["stream"], False)
            self.assertEqual(projected[name]["production_keep_alive"], -1)

    def test_prompt_schema_and_other_search_flow_changes_remain_compatible(self):
        expected = self.invoke()
        path = self.candidate / "search/api/conversation/worker.py"
        path.write_text(
            path.read_text() + '\nPROMPT = PROMPT + "new reviewed examples"\n'
        )
        self.change(
            "search/scope.py",
            "dict(schema),",
            "dict(schema, description='new schema'),",
        )
        path = self.candidate / "search/planning/model.py"
        path.write_text(
            path.read_text()
            + "\ndef reviewed_flow_change():\n    return 'new query flow'\n"
        )
        self.assertEqual(self.invoke(), expected)

    def test_candidate_source_is_parsed_without_importing_or_executing_it(self):
        path = self.candidate / "search/model_policy.py"
        path.write_text("raise RuntimeError('PRIVATE_SENTINEL')\n" + path.read_text())
        self.assertEqual(self.invoke()["status"], "compatible")

    def test_model_identity_and_scope_context_or_generation_changes_reject(self):
        for before, after in (
            (
                'SCOPE_MODEL = "ministral-3:8b-instruct-2512-q4_K_M"',
                'SCOPE_MODEL = "other"',
            ),
            ('SCOPE_MODEL_DIGEST = "1922', 'SCOPE_MODEL_DIGEST = "ffff'),
            ("SCOPE_NUM_CTX = 4_096", "SCOPE_NUM_CTX = 8_192"),
            ("SCOPE_NUM_PREDICT = 64", "SCOPE_NUM_PREDICT = 128"),
            ('PLANNER_MODEL = "gpt-oss:20b"', 'PLANNER_MODEL = "other"'),
            ('"17052f91', '"ffff2f91'),
        ):
            with self.subTest(before=before):
                self.reset()
                self.change("search/model_policy.py", before, after)
                self.reject()

    def test_planner_options_and_scope_payload_settings_reject_drift(self):
        for relative, before, after in (
            ("search/planning/model.py", '"num_ctx": 16384', '"num_ctx": 8192'),
            ("search/planning/model.py", '"num_predict": 2048', '"num_predict": 4096'),
            ("search/planning/model.py", '"temperature": 0', '"temperature": 1'),
            ("search/planning/model.py", '"seed": 11', '"seed": 12'),
            ("search/scope.py", '"seed": 42', '"seed": 43'),
            ("search/scope.py", '"stream": False', '"stream": True'),
            ("search/scope.py", '"truncate": False', '"truncate": True'),
            ("search/scope.py", '"shift": False', '"shift": True'),
            ("search/scope.py", "thinking = False", "thinking = True"),
            ("search/planning/model.py", '"stream": False', '"stream": True'),
        ):
            with self.subTest(relative=relative, before=before):
                self.reset()
                self.change(relative, before, after)
                self.reject()

    def test_production_residency_and_low_thinking_are_required(self):
        for relative, before, after in (
            (
                "search/api/worker.py",
                'ModelTransport("scope", keep_alive=-1)',
                'ModelTransport("scope", keep_alive=0)',
            ),
            (
                "search/api/conversation/worker.py",
                'ModelTransport("answer", keep_alive=-1)',
                'ModelTransport("answer", keep_alive=True)',
            ),
            ("search/api/conversation/worker.py", 'think="low"', 'think="medium"'),
            (
                "search/api/shared/model_transport.py",
                'payload["keep_alive"] = -1',
                'payload["keep_alive"] = 0',
            ),
        ):
            with self.subTest(relative=relative):
                self.reset()
                self.change(relative, before, after)
                self.reject()

    def test_unknown_request_fields_dynamic_policy_and_duplicate_assignments_reject(
        self,
    ):
        for relative, before, after in (
            ("search/scope.py", '"stream": False,', '"stream": False, "tools": [],'),
            ("search/planning/model.py", '"num_ctx": 16384', '"num_ctx": int("16384")'),
            (
                "search/model_policy.py",
                "SCOPE_NUM_PREDICT = 64",
                "SCOPE_NUM_PREDICT = 64\nSCOPE_NUM_PREDICT = 64",
            ),
        ):
            with self.subTest(relative=relative):
                self.reset()
                self.change(relative, before, after)
                self.reject()

    def test_missing_malformed_or_symlinked_source_fails_with_closed_error(self):
        path = self.candidate / "search/model_policy.py"
        for mutation in ("missing", "syntax", "symlink", "oversize"):
            with self.subTest(mutation=mutation):
                self.reset()
                if mutation == "missing":
                    path.unlink()
                elif mutation == "syntax":
                    path.write_text("PRIVATE_SENTINEL = (\n")
                elif mutation == "symlink":
                    path.unlink()
                    path.symlink_to(self.baseline / "search/model_policy.py")
                else:
                    path.write_bytes(b"# PRIVATE_SENTINEL\n" * 30000)
                self.reject()
                if path.is_symlink():
                    path.unlink()


if __name__ == "__main__":
    unittest.main()
