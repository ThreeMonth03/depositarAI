"""Offline evidence and input checks; no model or network is used."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest


DIRECTORY = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "pilot_evaluate", DIRECTORY / "evaluate.py"
)
evaluate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluate)
DATASET = "11111111-1111-4111-8111-111111111111"
RESOURCE = "22222222-2222-4222-8222-222222222222"


def case():
    return {
        "id": "csv-header-01",
        "kind": "csv_header",
        "query": "unique_sensor_column",
        "conditions": {},
        "targets": [DATASET],
        "anchor": {"resource_id": RESOURCE, "text": "unique_sensor_column"},
    }


class CaseTests(unittest.TestCase):
    def load(self, cases):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps({"schema_version": 1, "cases": cases}))
            path.chmod(0o600)
            return evaluate.load_cases(path, {DATASET})

    def test_conditions_remain_literal_dataset_metadata(self):
        value = case()
        value["conditions"] = {
            "time": {"start": "2024", "end": "2024"},
            "formats": ["CSV", "PDF"],
        }
        self.assertEqual(self.load([value]), [value])
        for changed in (
            {"bbox": [120, 23, 121, 24]},
            {"formats": ["CSV", "CSV"]},
            {"time": {"start": "2025", "end": "2024"}},
        ):
            with self.subTest(conditions=changed), self.assertRaises(ValueError):
                self.load([{**value, "conditions": changed}])

    def test_duplicate_cases_and_unknown_targets_cannot_be_frozen(self):
        with self.assertRaises(ValueError):
            self.load([case(), case()])
        with self.assertRaises(ValueError):
            self.load([{**case(), "targets": [RESOURCE]}])

    def test_private_artifacts_cannot_be_written_into_the_checkout(self):
        with self.assertRaises(ValueError):
            evaluate.private_path(DIRECTORY / "private-results.json")

    def test_dependency_failure_cannot_leave_a_previous_complete_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text('{"status":"complete","cases":["old"]}')
            with self.assertRaises(ValueError):
                with evaluate.report_lifecycle(
                    path, {"status": "running", "cases": []}
                ):
                    self.assertEqual(json.loads(path.read_text())["status"], "running")
                    raise ValueError("private dependency sentinel")
            self.assertEqual(
                json.loads(path.read_text()),
                {"status": "failed", "cases": [], "error_type": "ValueError"},
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_unchecked_conditions_do_not_claim_metadata_match(self):
        bundle = SimpleNamespace(
            condition_metadata={
                DATASET: {
                    "temporal": {"start": None, "end": None},
                    "bbox": {"status": "absent"},
                    "resource_formats": (),
                }
            }
        )
        self.assertEqual(evaluate.condition_status(bundle, DATASET, {}), "not_checked")
        self.assertEqual(
            evaluate.condition_status(
                bundle, DATASET, {"time": {"start": "2024", "end": "2024"}}
            ),
            "unknown",
        )


class GroundingTests(unittest.TestCase):
    def evidence(self, value, source, baseline_content="", important_content=""):
        document = {
            "title": "Dataset",
            "content": source["index_text"],
            "important_content": "",
        }
        baseline = {
            "title": "Dataset",
            "content": baseline_content,
            "important_content": important_content,
        }
        return evaluate.anchor_evidence(
            value, {DATASET: document}, {DATASET: baseline}, {DATASET: [source]}
        )

    def resource(self):
        return {
            "context": {
                "resource_id": RESOURCE,
                "format": "CSV",
                "columns": ["unique_sensor_column"],
                "excerpt": "",
            },
            "index_text": "unique_sensor_column",
            "profile_sha256": "1" * 64,
            "content_sha256": "2" * 64,
        }

    def test_anchor_already_in_metadata_is_not_claimed_as_new_file_evidence(self):
        with self.assertRaises(ValueError):
            self.evidence(
                case(), self.resource(), important_content="UNIQUE_SENSOR_COLUMN"
            )
        result = self.evidence(case(), self.resource())
        self.assertEqual(result["metadata_projection_occurrences"], 0)
        self.assertEqual(result["profile_sha256"], "1" * 64)
        self.assertEqual(result["enriched_dataset_occurrences"], [DATASET])

    def test_csv_anchor_must_be_a_column_rather_than_only_narrative_text(self):
        source = self.resource()
        source["context"]["columns"] = ["another_column"]
        with self.assertRaises(ValueError):
            self.evidence(case(), source)

    def test_pdf_grounding_uses_index_text_beyond_the_short_public_excerpt(self):
        source = self.resource()
        source["context"].update(format="PDF", columns=[], excerpt="Prelude " * 40)
        source["index_text"] = "Prelude " * 60 + "unique PDF phrase"
        value = copy.deepcopy(case())
        value.update(kind="pdf_phrase", query="unique PDF phrase")
        value["anchor"]["text"] = "unique PDF phrase"
        self.assertNotIn(value["anchor"]["text"], source["context"]["excerpt"])
        self.assertTrue(self.evidence(value, source)["literal_in_query"])

    def test_empty_or_nonfinite_vectors_cannot_stand_in_for_real_embeddings(self):
        for vector in ([], [0.0] * 768, [float("nan")] * 768):
            with self.subTest(length=len(vector)), self.assertRaises(ValueError):
                evaluate.checked_vector(vector)


if __name__ == "__main__":
    unittest.main()
