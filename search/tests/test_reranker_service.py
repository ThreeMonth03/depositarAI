"""Wire/HTTP boundaries without GPU, downloads, or a running search stack."""

import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from search.reranker import contract, model
from search.reranker.server import Server


class ContractTests(unittest.TestCase):
    def test_prompt_and_metadata_projection(self):
        self.assertEqual(
            contract.prompt("query", "doc"), "question:query \n \n passage:doc"
        )
        card = {
            "title": "title",
            "notes": "n" * 20000,
            "tags": ["tag"],
            "organization_title": "org",
            "resource_formats": ["PDF"],
            "resource_context": [],
        }
        self.assertEqual(
            contract.passage(card),
            (
                "Title: title\nFormats: PDF\nTags: tag\nOrganization: org\nDescription: "
                + "n" * 20000
            )[:8192],
        )

    def test_grounded_resource_content_precedes_long_metadata_notes(self):
        card = {
            "title": "title",
            "notes": "metadata " * 4000,
            "tags": ["tag"],
            "organization_title": "org",
            "resource_formats": ["CSV", "PDF"],
            "resource_context": [
                {
                    "resource_id": "00000000-0000-0000-0000-000000000001",
                    "name": "Observation table",
                    "format": "CSV",
                    "url": "https://data.depositar.io/dataset/a/resource/00000000-0000-0000-0000-000000000001",
                    "columns": ["station", "water temperature"],
                    "excerpt": "",
                },
                {
                    "resource_id": "00000000-0000-0000-0000-000000000002",
                    "name": "Observation report",
                    "format": "PDF",
                    "url": "https://data.depositar.io/dataset/a/resource/00000000-0000-0000-0000-000000000002",
                    "columns": [],
                    "excerpt": "Measured coastal water temperature.",
                },
            ],
        }
        passage = contract.passage(card)
        self.assertEqual(len(passage), contract.MAX_PASSAGE_CHARS)
        self.assertLess(passage.index("CSV columns:"), passage.index("Description:"))
        self.assertLess(passage.index("PDF excerpt:"), passage.index("Description:"))
        self.assertIn("station, water temperature", passage)
        self.assertIn("Measured coastal water temperature.", passage)
        self.assertNotIn("https://", passage)
        self.assertNotIn("00000000-", passage)

    def test_permutation_rejects_duplicates_missing_boolean_and_extra_output(self):
        good = contract.rank_scores([1, 2, 2])
        self.assertEqual(contract.validate_order(good, 3), [1, 2, 0])
        for order in ([0, 0], [0], [0, True], [0, 2], ["0", 1]):
            with self.assertRaises(ValueError):
                contract.validate_order(
                    {"order": order, "model_revision": contract.MODEL_REVISION}, 2
                )
        with self.assertRaises(ValueError):
            contract.validate_order({**good, "url": "https://evil.invalid"}, 3)
        with self.assertRaises(ValueError):
            contract.validate_order({**good, "model_revision": "0" * 40}, 3)
        for scores in ([float("nan")], [True], [float("inf")]):
            with self.assertRaises(ValueError):
                contract.rank_scores(scores)

    def test_request_bounds(self):
        for query, passages in (
            ("", ["a"]),
            ("a", []),
            ("a", ["a"] * 21),
            ("a", ["a" * 8193]),
            ("a", ["\ud800"]),
            ("a" * 513, ["a"]),
            (True, ["a"]),
        ):
            with self.assertRaises((ValueError, UnicodeError)):
                contract.validate_request({"query": query, "passages": passages})


class ModelIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.artifacts = {
            name: ("reviewed fixture: " + name).encode() for name in model.MODEL_HASHES
        }
        for name, content in self.artifacts.items():
            (self.directory / name).write_bytes(content)
        hashes = {
            name: hashlib.sha256(content).hexdigest()
            for name, content in self.artifacts.items()
        }
        pins = patch.object(model, "MODEL_HASHES", hashes)
        pins.start()
        self.addCleanup(pins.stop)
        dependencies = patch.dict("sys.modules", {"torch": None, "transformers": None})
        dependencies.start()
        self.addCleanup(dependencies.stop)

    def test_complete_artifact_inventory_verifies(self):
        model.verify_model(self.directory)

    def test_modified_artifact_is_rejected_before_model_loading(self):
        for name, content in self.artifacts.items():
            with self.subTest(artifact=name):
                path = self.directory / name
                path.write_bytes(content + b"tampered")
                try:
                    with self.assertRaisesRegex(ValueError, "model identity mismatch"):
                        model.Ranker(self.directory)
                finally:
                    path.write_bytes(content)

    def test_missing_artifact_is_rejected_before_model_loading(self):
        for name, content in self.artifacts.items():
            with self.subTest(artifact=name):
                path = self.directory / name
                path.unlink()
                try:
                    with self.assertRaises(FileNotFoundError):
                        model.Ranker(self.directory)
                finally:
                    path.write_bytes(content)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.ranker = Mock()
        self.ranker.rank.return_value = contract.rank_scores([1, 2])
        self.server = Server(("127.0.0.1", 0), self.ranker)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, raw, path="/rerank", headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(
                "POST", path, raw, headers or {"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_model_only_returns_permutation_and_errors_are_sanitized(self):
        raw = json.dumps({"query": "find data", "passages": ["one", "two"]})
        self.assertEqual(self.request(raw), (200, contract.rank_scores([1, 2])))
        self.ranker.rank.side_effect = RuntimeError("secret detail")
        status, value = self.request(raw)
        self.assertEqual(status, 503)
        self.assertNotIn("secret", str(value))
        self.assertFalse(self.server.inference.locked())

    def test_invalid_requests_do_not_invoke_model(self):
        for raw in (
            '{"query":"a","query":"b","passages":["x"]}',
            '{"query":NaN}',
            json.dumps(
                {"query": "x", "passages": ["y"], "url": "https://evil.invalid"}
            ),
        ):
            self.assertEqual(self.request(raw)[0], 400)
        self.assertEqual(self.request("{}", "/api/chat")[0], 404)
        self.assertEqual(
            self.request("{}", headers={"Content-Type": "text/plain"})[0], 400
        )
        self.ranker.rank.assert_not_called()

    def test_busy_fails_without_queue_or_releasing_existing_slot(self):
        with self.server.inference:
            self.assertEqual(
                self.request(json.dumps({"query": "x", "passages": ["y"]}))[0], 503
            )
            self.assertTrue(self.server.inference.locked())
        self.ranker.rank.assert_not_called()

    def test_watchdog_is_armed_and_cancelled_for_admitted_inference(self):
        raw = json.dumps({"query": "find data", "passages": ["one", "two"]})
        with patch("search.reranker.server.threading.Timer") as timer:
            self.assertEqual(self.request(raw)[0], 200)
        self.assertEqual(timer.call_args.args[0], 9)
        self.assertEqual(timer.call_args.kwargs["args"], (70,))
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()
