"""Reviewed request compatibility is checked offline, without importing
sources.
"""

import ast
import copy
from pathlib import Path
import shutil
import tempfile
import unittest

from search.operations.deployment.candidate import policy


ROOT = Path(__file__).resolve().parents[4]


class SharedRequestPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="shared-request-policy-"
        )
        self.addCleanup(temporary.cleanup)
        self.baseline = Path(temporary.name) / "baseline"
        self.candidate = Path(temporary.name) / "candidate"
        for root in (self.baseline, self.candidate):
            for relative in policy.SOURCE_FILES:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)

    def invoke(self):
        return policy.validate_shared_request_policy(
            self.baseline, self.candidate
        )

    def change(self, relative, before, after):
        path = self.candidate / relative
        source = path.read_text()
        self.assertIn(before, source)
        path.write_text(source.replace(before, after, 1))

    def reset(self):
        for relative in policy.SOURCE_FILES:
            shutil.copyfile(
                self.baseline / relative, self.candidate / relative
            )

    def reject(self):
        with self.assertRaises(policy.SharedRequestPolicyError) as caught:
            self.invoke()
        self.assertRegex(
            str(caught.exception), r"^shared_request_policy_[a-z_]+$"
        )
        self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))
        self.assertNotIn(str(self.candidate), str(caught.exception))

    def test_current_requests_keep_exact_context_and_production_residency(
        self,
    ):
        result = self.invoke()
        self.assertEqual(result["status"], "compatible")
        self.assertEqual(len(result["request_policy_sha256"]), 64)
        self.assertEqual(len(result["policy"]["reranker_wire_sha256"]), 64)
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
        self.assertEqual(
            projected["reranker"],
            {
                "model_id": "nvidia/llama-nemotron-rerank-1b-v2",
                "model_revision": "828765652b05bd439c9789d2a6d093db1caa1443",
                "max_candidates": 20,
                "max_passage_chars": 8192,
                "max_request_bytes": 1048576,
                "max_response_bytes": 4096,
                "request_deadline_seconds": 8,
                "inference_budget_seconds": 6,
            },
        )

    def test_prompt_schema_and_other_search_flow_changes_remain_compatible(
        self,
    ):
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

    def test_candidate_source_is_parsed_without_importing_or_executing_it(
        self,
    ):
        path = self.candidate / "search/model_policy.py"
        path.write_text(
            "raise RuntimeError('PRIVATE_SENTINEL')\n" + path.read_text()
        )
        self.assertEqual(self.invoke()["status"], "compatible")

    def test_model_identity_and_scope_context_or_generation_changes_reject(
        self,
    ):
        for before, after in (
            (
                'SCOPE_MODEL = "ministral-3:8b-instruct-2512-q4_K_M"',
                'SCOPE_MODEL = "other"',
            ),
            ('"1922accd', '"ffffaccd'),
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
            (
                "search/planning/model.py",
                '"num_ctx": 16384',
                '"num_ctx": 8192',
            ),
            (
                "search/planning/model.py",
                '"num_predict": 2048',
                '"num_predict": 4096',
            ),
            (
                "search/planning/model.py",
                '"temperature": 0',
                '"temperature": 1',
            ),
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
            (
                "search/api/conversation/worker.py",
                'think="low"',
                'think="medium"',
            ),
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

    def test_each_reranker_model_and_wire_limit_is_bound(self):
        for before, after in (
            (
                'MODEL_ID = "nvidia/llama-nemotron-rerank-1b-v2"',
                'MODEL_ID = "other/model"',
            ),
            (
                'MODEL_REVISION = "828765652b05bd439c9789d2a6d093db1caa1443"',
                'MODEL_REVISION = "0000000000000000000000000000000000000000"',
            ),
            ("MAX_CANDIDATES = 20", "MAX_CANDIDATES = 19"),
            ("MAX_PASSAGE_CHARS = 8192", "MAX_PASSAGE_CHARS = 4096"),
            (
                "MAX_REQUEST_BYTES = 1024 * 1024",
                "MAX_REQUEST_BYTES = 1024 * 2048",
            ),
            ("MAX_RESPONSE_BYTES = 4096", "MAX_RESPONSE_BYTES = 8192"),
            ("REQUEST_DEADLINE_S = 8", "REQUEST_DEADLINE_S = 9"),
            ("INFERENCE_BUDGET_S = 6", "INFERENCE_BUDGET_S = 5"),
        ):
            with self.subTest(before=before):
                self.reset()
                self.change("search/reranker/contract.py", before, after)
                with self.assertRaisesRegex(
                    policy.SharedRequestPolicyError,
                    "^shared_request_policy_changed$",
                ):
                    self.invoke()

    def test_matching_reranker_policy_changes_have_a_new_digest(self):
        original = self.invoke()
        self.change(
            "search/reranker/contract.py",
            "MAX_CANDIDATES = 20",
            "MAX_CANDIDATES = 19",
        )
        shutil.copyfile(
            self.candidate / "search/reranker/contract.py",
            self.baseline / "search/reranker/contract.py",
        )
        result = self.invoke()
        self.assertEqual(result["status"], "compatible")
        self.assertEqual(result["policy"]["reranker"]["max_candidates"], 19)
        self.assertNotEqual(
            result["request_policy_sha256"],
            original["request_policy_sha256"],
        )

    def test_reranker_source_format_and_prompt_changes_remain_compatible(self):
        original = self.invoke()
        self.change(
            "search/reranker/contract.py",
            "MAX_REQUEST_BYTES = 1024 * 1024",
            "MAX_REQUEST_BYTES = (1024*1024)",
        )
        self.change(
            "search/reranker/contract.py",
            'return f"question:{query}',
            'return f"reviewed question:{query}',
        )
        path = self.candidate / "search/reranker/contract.py"
        path.write_text(
            "raise RuntimeError('PRIVATE_SENTINEL')\n" + path.read_text()
        )
        self.assertEqual(self.invoke(), original)

    def test_actual_reranker_http_request_changes_are_rejected(self):
        for before, after in (
            ('+ "/rerank"', '+ "/different-rank"'),
            ('service_address("reranker", 8001)', '"http://elsewhere:8001"'),
            ("timeout=9,", "timeout=90,"),
            ("max_bytes=MAX_RESPONSE_BYTES,", "max_bytes=8192,"),
            ('"POST",', '"GET",'),
            (
                "payload, ensure_ascii=False, allow_nan=False",
                "dict(payload, query='changed'), "
                "ensure_ascii=False, allow_nan=False",
            ),
            (
                "validate_order(value, len(passages))",
                "validate_order(value, 1)",
            ),
            ("response.url != url", "False"),
        ):
            with self.subTest(before=before):
                self.reset()
                self.change("search/api/worker.py", before, after)
                self.reject()

    def test_actual_reranker_payload_deadline_and_order_are_bound(self):
        for before, after in (
            ('normalize("NFKC", request.query)', 'normalize("NFC", "x")'),
            ("[passage(row) for row in found.rows]", "[passage(row)]"),
            ("REQUEST_DEADLINE_S,", "REQUEST_DEADLINE_S * 10,"),
            ("min(timeout, remaining)", "max(timeout, remaining)"),
            (
                "validate_order(ranked, len(found.rows))",
                'ranked["order"]',
            ),
            (
                "tuple(found.rows[i] for i in order)",
                "tuple(found.rows[i] for i in reversed(order))",
            ),
        ):
            with self.subTest(before=before):
                self.reset()
                self.change("search/api/retrieval/engine.py", before, after)
                self.reject()

    def test_wire_validators_transport_and_imports_are_bound(self):
        for relative, before, after in (
            (
                "search/reranker/contract.py",
                "1 <= len(query) <= 512",
                "1 <= len(query) <= 1024",
            ),
            (
                "search/reranker/contract.py",
                "type(i) is not int",
                "not isinstance(i, int)",
            ),
            (
                "search/reranker/contract.py",
                'value["model_revision"] != MODEL_REVISION',
                "False",
            ),
            (
                "search/api/shared/backend.py",
                "asyncio.wait_for(exchange(), timeout=timeout)",
                "asyncio.wait_for(exchange(), timeout=timeout * 10)",
            ),
            (
                "search/api/shared/backend.py",
                'worker_module = "search.api.worker"',
                'worker_module = "search.api.other_worker"',
            ),
            (
                "search/api/shared/backend.py",
                "MAX_WORKER_BYTES = 4 * 1024 * 1024",
                "MAX_WORKER_BYTES = 40 * 1024 * 1024",
            ),
            (
                "search/transport/http.py",
                "self._opener.open(raw_request, timeout=timeout)",
                "self._opener.open(raw_request, timeout=60)",
            ),
            (
                "search/transport/http.py",
                "return None",
                "return request",
            ),
            (
                "search/api/shared/peers.py",
                '("reranker", 8001)',
                '("reranker", 8002)',
            ),
            (
                "search/serialization.py",
                "object_pairs_hook=pairs, parse_constant=invalid",
                "object_pairs_hook=dict",
            ),
            (
                "search/api/worker.py",
                "from search.reranker.contract import",
                "from other.reranker.contract import",
            ),
            (
                "search/api/retrieval/engine.py",
                "from search.reranker.contract import",
                "from other.reranker.contract import",
            ),
        ):
            with self.subTest(relative=relative, before=before):
                self.reset()
                self.change(relative, before, after)
                self.reject()

    def test_format_comments_and_docstrings_do_not_change_wire_policy(self):
        expected = self.invoke()
        for relative in policy.SOURCE_FILES:
            path = self.candidate / relative
            tree = ast.parse(path.read_bytes())
            for node in ast.walk(tree):
                if isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                ):
                    replacement = ast.Expr(
                        value=ast.Constant("Reviewed docs.")
                    )
                    if ast.get_docstring(node, clean=False) is None:
                        node.body.insert(0, replacement)
                    else:
                        node.body[0] = replacement
            path.write_text(
                "# Reviewed format and comment change.\n"
                + ast.unparse(tree)
                + "\n"
            )
        self.assertEqual(self.invoke(), expected)

    def test_unrelated_reranker_prompt_passage_and_search_are_allowed(self):
        expected = self.invoke()
        self.change(
            "search/reranker/contract.py",
            '"Title: " + card["title"]',
            '"Dataset title: " + card["title"]',
        )
        self.change(
            "search/reranker/contract.py",
            'return f"question:{query}',
            'return f"reviewed question:{query}',
        )
        self.change(
            "search/api/retrieval/engine.py",
            'result["filter_scope"] = "index_prefilter"',
            'result["filter_scope"] = "reviewed_prefilter"',
        )
        self.assertEqual(self.invoke(), expected)

    def test_inline_and_extracted_rerank_stage_have_same_wire_policy(self):
        expected = self.invoke()
        path = self.candidate / "search/api/retrieval/engine.py"
        tree = ast.parse(path.read_bytes())
        engine = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "RetrievalEngine"
        )
        helper = next(
            node for node in engine.body if node.name == "rerank_candidates"
        )
        search = next(node for node in engine.body if node.name == "search")
        stage = next(node for node in helper.body if isinstance(node, ast.Try))
        call_index = next(
            index
            for index, node in enumerate(search.body)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Await)
            and isinstance(node.value.value.func, ast.Attribute)
            and node.value.value.func.attr == "rerank_candidates"
        )
        search.body[call_index] = ast.If(
            test=ast.parse(
                "self.settings.rerank_enabled and found.rows", mode="eval"
            ).body,
            body=[copy.deepcopy(helper.body[2]), copy.deepcopy(stage)],
            orelse=[],
        )
        engine.body.remove(helper)
        path.write_text(ast.unparse(ast.fix_missing_locations(tree)) + "\n")
        self.assertEqual(self.invoke(), expected)

    def test_missing_duplicate_or_detached_wire_nodes_fail_closed(self):
        for relative, before, after in (
            (
                "search/reranker/contract.py",
                "def validate_order(value, count):",
                "def other_order(value, count):",
            ),
            (
                "search/api/retrieval/engine.py",
                "found = await self.rerank_candidates",
                "found = await self.other_stage",
            ),
            (
                "search/api/retrieval/engine.py",
                "rerank_candidates(request, result, found, call)",
                "rerank_candidates(request, result, found, other_call)",
            ),
            (
                "search/api/retrieval/engine.py",
                "found = await self.rerank_candidates",
                "unused = await self.rerank_candidates",
            ),
        ):
            with self.subTest(relative=relative, before=before):
                self.reset()
                self.change(relative, before, after)
                self.reject()
        self.reset()
        path = self.candidate / "search/reranker/contract.py"
        tree = ast.parse(path.read_bytes())
        node = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "validate_order"
        )
        tree.body.append(copy.deepcopy(node))
        path.write_text(ast.unparse(tree) + "\n")
        self.reject()

    def test_duplicate_reranker_call_or_worker_branch_is_not_ambiguous(self):
        for relative, owner, kind in (
            ("search/api/worker.py", "run", ast.If),
            (
                "search/api/retrieval/engine.py",
                "rerank_candidates",
                ast.Try,
            ),
        ):
            with self.subTest(relative=relative):
                self.reset()
                path = self.candidate / relative
                tree = ast.parse(path.read_bytes())
                method = next(
                    node
                    for node in ast.walk(tree)
                    if isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef)
                    )
                    and node.name == owner
                )
                selected = next(
                    node
                    for node in method.body
                    if isinstance(node, kind) and "rerank" in ast.unparse(node)
                )
                method.body.append(copy.deepcopy(selected))
                path.write_text(ast.unparse(tree) + "\n")
                self.reject()

    def test_shadowed_rerank_helper_is_not_treated_as_the_active_stage(self):
        path = self.candidate / "search/api/retrieval/engine.py"
        tree = ast.parse(path.read_bytes())
        engine = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "RetrievalEngine"
        )
        engine.body.append(
            ast.parse(
                "async def rerank_candidates(self, request, result, "
                "found, call):\n    return found\n"
            ).body[0]
        )
        path.write_text(ast.unparse(tree) + "\n")
        self.reject()

    def test_wire_module_dependencies_cannot_be_rebound(self):
        for suffix in (
            "MAX_RESPONSE_BYTES = MAX_RESPONSE_BYTES + 1",
            "MAX_RESPONSE_BYTES: int = MAX_RESPONSE_BYTES + 1",
            "MAX_RESPONSE_BYTES += 1",
            "(MAX_RESPONSE_BYTES := MAX_RESPONSE_BYTES + 1)",
            "MAX_RESPONSE_BYTES, other = (8192, None)",
            "del MAX_RESPONSE_BYTES",
            "service_address = lambda host, port: 'http://other:8001'",
            "from synthetic import other as service_address",
            "import synthetic as service_address",
            "from synthetic import *",
            "def service_address(host, port):\n    return 'http://other'",
            "class service_address:\n    pass",
            "if False:\n    MAX_RESPONSE_BYTES = 8192",
            "def unrelated(value=(MAX_RESPONSE_BYTES := 8192)):\n"
            "    return value",
        ):
            with self.subTest(suffix=suffix):
                self.reset()
                path = self.candidate / "search/api/worker.py"
                path.write_text(path.read_text() + "\n" + suffix + "\n")
                self.reject()

    def test_selected_wire_declarations_keep_their_only_module_binding(self):
        for relative, suffix in (
            (
                "search/reranker/contract.py",
                "validate_request = lambda payload: ('query', [])",
            ),
            (
                "search/reranker/contract.py",
                "MAX_RESPONSE_BYTES: int = 8192",
            ),
            (
                "search/api/shared/peers.py",
                "from synthetic import other as service_address",
            ),
            ("search/serialization.py", "strict_json = lambda raw: {}"),
            ("search/api/shared/backend.py", "SubprocessBackend = object"),
            ("search/transport/http.py", "UrllibTransport = object"),
            (
                "search/api/shared/model_transport.py",
                "ModelTransport = object",
            ),
            (
                "search/api/shared/model_transport.py",
                "service_address = lambda host, port: 'http://other'",
            ),
            ("search/api/retrieval/engine.py", "RetrievalEngine = object"),
        ):
            with self.subTest(relative=relative, suffix=suffix):
                self.reset()
                path = self.candidate / relative
                path.write_text(path.read_text() + "\n" + suffix + "\n")
                self.reject()

    def test_worker_scope_cannot_shadow_wire_dependencies(self):
        for prefix in (
            "MAX_RESPONSE_BYTES = 8192",
            "MAX_RESPONSE_BYTES: int = 8192",
            "service_address = lambda host, port: 'http://other'",
            "from synthetic import other as service_address",
            "import synthetic as service_address",
            "def service_address(host, port):\n        return 'http://other'",
            "class service_address:\n        pass",
        ):
            with self.subTest(prefix=prefix):
                self.reset()
                self.change(
                    "search/api/worker.py",
                    "def run(operation, payload, settings):\n",
                    "def run(operation, payload, settings):\n    "
                    + prefix
                    + "\n",
                )
                self.reject()
        self.reset()
        self.change(
            "search/api/worker.py",
            "def run(operation, payload, settings):",
            "def run(operation, payload, settings, service_address=None):",
        )
        self.reject()

    def test_selected_wire_objects_cannot_be_mutated_directly(self):
        for relative, suffix in (
            (
                "search/transport/http.py",
                "UrllibTransport.send = replacement",
            ),
            (
                "search/api/shared/backend.py",
                "SubprocessBackend.call = replacement",
            ),
            ("search/api/worker.py", "json.dumps = replacement"),
            ("search/api/worker.py", "json.__dict__['dumps'] = replacement"),
            ("search/transport/http.py", "del UrllibTransport.send"),
        ):
            with self.subTest(relative=relative, suffix=suffix):
                self.reset()
                path = self.candidate / relative
                path.write_text(path.read_text() + "\n" + suffix + "\n")
                self.reject()

    def test_engine_scope_cannot_shadow_its_wire_dependencies(self):
        for before, after in (
            (
                'result["rerank_status"] = "fallback"',
                "REQUEST_DEADLINE_S = 80\n        "
                'result["rerank_status"] = "fallback"',
            ),
            (
                "found = await self.rerank_candidates",
                "call = replacement\n        "
                "found = await self.rerank_candidates",
            ),
            (
                "async def search(self, request, request_id):",
                "async def search(self, request, request_id, time=None):",
            ),
        ):
            with self.subTest(before=before):
                self.reset()
                self.change("search/api/retrieval/engine.py", before, after)
                self.reject()

    def test_partial_wire_classes_keep_selected_methods_and_plain_header(
        self,
    ):
        for relative, name, method in (
            (
                "search/api/shared/model_transport.py",
                "ModelTransport",
                "send",
            ),
            (
                "search/api/retrieval/engine.py",
                "RetrievalEngine",
                "rerank_candidates",
            ),
        ):
            for mutation in ("method", "decorator", "base", "metaclass"):
                with self.subTest(relative=relative, mutation=mutation):
                    self.reset()
                    path = self.candidate / relative
                    if mutation == "method":
                        path.write_text(
                            path.read_text()
                            + "\n    "
                            + method
                            + " = replacement\n"
                        )
                    else:
                        headers = {
                            "decorator": "@replacement\nclass " + name + ":",
                            "base": "class " + name + "(replacement):",
                            "metaclass": "class "
                            + name
                            + "(metaclass=replacement):",
                        }
                        self.change(
                            relative, "class " + name + ":", headers[mutation]
                        )
                    self.reject()

    def test_unrelated_helper_locals_do_not_shadow_wire_dependencies(self):
        expected = self.invoke()
        for relative in policy.SOURCE_FILES:
            path = self.candidate / relative
            path.write_text(
                path.read_text()
                + "\ndef unrelated(MAX_RESPONSE_BYTES, service_address):\n"
                "    MAX_RESPONSE_BYTES = 2\n"
                "    from synthetic import other as service_address\n"
                "    return MAX_RESPONSE_BYTES, service_address\n"
            )
        self.assertEqual(self.invoke(), expected)

    def test_dynamic_or_invalid_reranker_constants_fail_without_execution(
        self,
    ):
        for before, after in (
            ("MAX_CANDIDATES = 20", "MAX_CANDIDATES = True"),
            ("MAX_PASSAGE_CHARS = 8192", "MAX_PASSAGE_CHARS = -1"),
            ("MAX_RESPONSE_BYTES = 4096", "MAX_RESPONSE_BYTES = 4096.0"),
            (
                "MAX_REQUEST_BYTES = 1024 * 1024",
                "MAX_REQUEST_BYTES = int('1048576')",
            ),
            (
                "MAX_REQUEST_BYTES = 1024 * 1024",
                "MAX_REQUEST_BYTES = 1024 ** 2",
            ),
            (
                "MAX_REQUEST_BYTES = 1024 * 1024",
                "MAX_REQUEST_BYTES = 1024 * (512 * 2)",
            ),
            (
                "MAX_REQUEST_BYTES = 1024 * 1024",
                "MAX_REQUEST_BYTES = True * 1048576",
            ),
            (
                "MAX_REQUEST_BYTES = 1024 * 1024",
                "MAX_REQUEST_BYTES = 2147483648 * 2",
            ),
            ("REQUEST_DEADLINE_S = 8", "REQUEST_DEADLINE_S = 4 * 2"),
            (
                "INFERENCE_BUDGET_S = 6",
                "INFERENCE_BUDGET_S = 6\nINFERENCE_BUDGET_S = 6",
            ),
            (
                'MODEL_ID = "nvidia/llama-nemotron-rerank-1b-v2"',
                'MODEL_ID = b"nvidia/llama-nemotron-rerank-1b-v2"',
            ),
            (
                'MODEL_REVISION = "828765652b05bd439c9789d2a6d093db1caa1443"',
                'MODEL_REVISION = "PRIVATE_SENTINEL"',
            ),
        ):
            with self.subTest(before=before, after=after):
                self.reset()
                self.change("search/reranker/contract.py", before, after)
                with self.assertRaisesRegex(
                    policy.SharedRequestPolicyError,
                    "^shared_request_policy_unsupported$",
                ):
                    self.invoke()

    def test_unknown_fields_dynamic_policy_or_duplicate_assignments_reject(
        self,
    ):
        for relative, before, after in (
            (
                "search/scope.py",
                '"stream": False,',
                '"stream": False, "tools": [],',
            ),
            (
                "search/planning/model.py",
                '"num_ctx": 16384',
                '"num_ctx": int("16384")',
            ),
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

    def test_missing_malformed_or_symlinked_source_fails_with_closed_error(
        self,
    ):
        for relative in policy.SOURCE_FILES:
            path = self.candidate / relative
            for mutation in ("missing", "syntax", "symlink", "oversize"):
                with self.subTest(relative=relative, mutation=mutation):
                    self.reset()
                    if mutation == "missing":
                        path.unlink()
                    elif mutation == "syntax":
                        path.write_text("PRIVATE_SENTINEL = (\n")
                    elif mutation == "symlink":
                        path.unlink()
                        path.symlink_to(self.baseline / relative)
                    else:
                        path.write_bytes(b"# PRIVATE_SENTINEL\n" * 30000)
                    self.reject()
                    if path.is_symlink():
                        path.unlink()


if __name__ == "__main__":
    unittest.main()
