"""Current catalog-admin reports are bounded, strict and tied to import authority."""

import json
import unittest

from search.operations.catalog import results


BUNDLE_ID = "a" * 64
MANIFEST_SHA256 = "b" * 64


def valid_import_report():
    return {
        "schema_version": 1,
        "status": "imported",
        "bundle_id": BUNDLE_ID,
        "manifest_sha256": MANIFEST_SHA256,
        "document_count": 3_566,
        "batch_count": 36,
        "elapsed_seconds": 12.5,
        "inventory_verified": True,
    }


class CatalogResultTests(unittest.TestCase):
    def test_single_record_parser_preserves_the_import_report(self):
        report = valid_import_report()
        raw = json.dumps(report).encode("utf-8") + b"\n"
        self.assertEqual(report, results.parse_record(raw))
        results.validate_import(report, BUNDLE_ID, MANIFEST_SHA256, operation="import")

    def test_writing_idempotent_and_reconciled_reports_match_the_requested_operation(
        self,
    ):
        for operation, status, batches in (
            ("import", "imported", 36),
            ("import", "already-active-no-op", 0),
            ("reconcile", "reconciled-active", 0),
        ):
            report = valid_import_report()
            report.update(status=status, batch_count=batches)
            with self.subTest(operation=operation, status=status):
                results.validate_import(report, BUNDLE_ID, MANIFEST_SHA256)
                results.validate_import(
                    report, BUNDLE_ID, MANIFEST_SHA256, operation=operation
                )
                other_operation = "import" if operation == "reconcile" else "reconcile"
                with self.assertRaisesRegex(results.ResultError, "requested operation"):
                    results.validate_import(
                        report, BUNDLE_ID, MANIFEST_SHA256, operation=other_operation
                    )

    def test_schema_identity_inventory_and_numeric_values_fail_closed(self):
        for key, value in (
            ("schema_version", True),
            ("bundle_id", "c" * 64),
            ("manifest_sha256", "d" * 64),
            ("inventory_verified", False),
            ("document_count", True),
            ("document_count", 0),
            ("document_count", 10_001),
            ("batch_count", -1),
            ("elapsed_seconds", True),
            ("elapsed_seconds", float("inf")),
            ("elapsed_seconds", float("nan")),
            ("elapsed_seconds", 10**400),
            ("response_body", "unapproved field"),
        ):
            report = valid_import_report()
            report[key] = value
            with (
                self.subTest(field=key, value=value),
                self.assertRaises(results.ResultError),
            ):
                results.validate_import(report, BUNDLE_ID, MANIFEST_SHA256)

    def test_nonwriting_reports_cannot_claim_batches_or_unknown_operations(self):
        for status in ("already-active-no-op", "reconciled-active"):
            report = valid_import_report()
            report.update(status=status, batch_count=1)
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(results.ResultError, "cannot contain batches"),
            ):
                results.validate_import(report, BUNDLE_ID, MANIFEST_SHA256)
        with self.assertRaisesRegex(results.ResultError, "operation is invalid"):
            results.validate_import(
                valid_import_report(), BUNDLE_ID, MANIFEST_SHA256, operation="unknown"
            )

    def test_parser_rejects_unbounded_duplicate_nonfinite_or_multiple_records(self):
        for raw in (
            b"{}",
            b"{}\n{}\n",
            b'{"a":1,"a":2}\n',
            b'{"a":NaN}\n',
            b'{"a":Infinity}\n',
            b'{"a":"\xff"}\n',
            b"[]\n",
            b"null\n",
            b"[" * 2_000 + b"0" + b"]" * 2_000 + b"\n",
            b"x" * (results.MAX_BYTES + 1),
            "{}\n",
        ):
            with (
                self.subTest(raw_type=type(raw)),
                self.assertRaises(results.ResultError),
            ):
                results.parse_record(raw)
