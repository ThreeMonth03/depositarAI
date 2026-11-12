"""Use actual evidence, independent expectations and one sealed catalog."""

import copy
from pathlib import Path
import tempfile
import unittest

from search.catalog.errors import BundleError
from search.quality.evaluator import evaluate
from search.trace.quality import capture
from search.trace.store import TraceStore
from .fixtures import DATASET, event, IDENTIFIER, plan, sealed_bundle


class QualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.bundle, self.manifest = sealed_bundle(self, self.base)
        self.store = TraceStore(self.base / "traces")
        self.store.append(event(manifest=self.manifest))
        self.store.append(event(2, manifest=self.manifest))
        self.assertTrue(self.store.seal(IDENTIFIER))

    def convert(self, *, exported=None, expected=None, manifest=None):
        return capture(
            exported or self.store.export(IDENTIFIER),
            expected or plan(),
            "climate",
            self.bundle,
            manifest or self.manifest,
        )

    def test_independent_two_turn_plan_matches_sealed_metadata(self):
        value = self.convert()
        report = evaluate(value, plan())
        self.assertEqual(report["status"], "passed", report)
        self.assertEqual(report["summary"]["passed_turn_count"], 2)
        metadata = value["cases"][0]["turns"][0]["source_metadata"][DATASET]
        self.assertEqual(metadata["resource_formats"], ["CSV"])

    def test_actual_card_error_survives_adapter_and_fails_evaluator(self):
        exported = self.store.export(IDENTIFIER)
        card = exported["events"][0]["output"]["search"]["cards"][0]
        card["temporal_coverage"]["start"] = "2021"
        value = self.convert(exported=exported)
        report = evaluate(value, plan())
        self.assertEqual(report["status"], "failed")
        self.assertIn(
            "coverage_source_mismatch",
            report["cases"][0]["turns"][0]["issues"],
        )
        self.assertEqual(
            value["cases"][0]["turns"][0]["response"]["result"]["search"][
                "cards"
            ][0]["temporal_coverage"]["start"],
            "2021",
        )

    def test_mismatched_plan_query_or_bundle_is_rejected(self):
        expected = plan()
        expected["cases"][0]["turns"][0]["query"] = (
            "unrelated independent query"
        )
        with self.assertRaisesRegex(ValueError, "plan_query_mismatch"):
            self.convert(expected=expected)
        with self.assertRaises(BundleError):
            self.convert(manifest="e" * 64)
        exported = self.store.export(IDENTIFIER)
        exported["events"][1]["identities"]["catalog_manifest_sha256"] = (
            "e" * 64
        )
        with self.assertRaisesRegex(ValueError, "identity_mismatch"):
            self.convert(exported=exported)

    def test_missing_source_revision_is_not_claimed_as_bound_evidence(self):
        exported = self.store.export(IDENTIFIER)
        for row in exported["events"]:
            row["identities"]["bff_revision"] = None
            row["unavailable_trace"].append("bff_identity")
        with self.assertRaisesRegex(ValueError, "source_identity_unavailable"):
            self.convert(exported=exported)

    def test_expected_is_never_derived_from_actual(self):
        expected = plan()
        expected["cases"][0]["turns"][1]["expected"]["formats"] = ["PDF"]
        value = self.convert(expected=expected)
        self.assertEqual(
            value["cases"][0]["turns"][1]["expected"]["formats"], ["PDF"]
        )
        self.assertEqual(evaluate(value, expected)["status"], "failed")

    def test_unsealed_gapped_missing_or_sql_prefix_is_rejected(self):
        base = self.store.export(IDENTIFIER)
        variants = []
        unsealed = copy.deepcopy(base)
        unsealed["manifest"]["sealed"] = False
        variants.append(unsealed)
        gapped = copy.deepcopy(base)
        gapped["manifest"]["gaps"] = [{"turn": 3, "status": "queue_full"}]
        variants.append(gapped)
        missing = copy.deepcopy(base)
        missing["events"].pop(0)
        variants.append(missing)
        sql = copy.deepcopy(base)
        sql["events"][0]["execution"] = {
            "kind": "validated_sql",
            "sql": "SELECT dataset_id FROM datasets",
            "parameters": {},
            "fixture_only": True,
        }
        variants.append(sql)
        for value in variants:
            with self.subTest(value=value["events"][0]["turn"]):
                with self.assertRaises(ValueError):
                    self.convert(exported=value)

    def test_unavailable_attempt_cannot_be_compressed_or_skip_version(self):
        exported = self.store.export(IDENTIFIER)
        exported["events"][0]["output"]["status"] = "unavailable"
        exported["events"][0]["output"]["version"] = 0
        with self.assertRaisesRegex(ValueError, "legacy_prefix_required"):
            self.convert(exported=exported)
        exported = self.store.export(IDENTIFIER)
        exported["events"][1]["output"]["version"] = 1
        with self.assertRaisesRegex(ValueError, "legacy_prefix_required"):
            self.convert(exported=exported)

    def test_full_prefix_only_and_no_arbitrary_twenty_turn_split(self):
        expected = plan()
        expected["cases"][0]["turns"] = expected["cases"][0]["turns"][:1]
        self.assertEqual(
            len(self.convert(expected=expected)["cases"][0]["turns"]), 1
        )
        expected = plan()
        expected["cases"][0]["turns"] = expected["cases"][0]["turns"] * 5
        with self.assertRaises(ValueError):
            self.convert(expected=expected)

    def test_prefix_rejects_prior_state_and_changed_consent_identity(self):
        exported = self.store.export(IDENTIFIER)
        exported["events"][0]["state_before"]["topic"] = (
            "prior unconsented topic"
        )
        with self.assertRaisesRegex(ValueError, "fresh_prefix_required"):
            self.convert(exported=exported)
        exported = self.store.export(IDENTIFIER)
        exported["events"][1]["epoch"] = 1
        with self.assertRaisesRegex(ValueError, "session_identity_changed"):
            self.convert(exported=exported)
