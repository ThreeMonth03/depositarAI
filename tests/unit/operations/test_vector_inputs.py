"""Native vector lookup and precondition forwarding without service work."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.operations import cli, config as product_config, lifecycle
from search.operations.catalog import vectors
from search.operations.catalog.policy import RuntimeError as CatalogRuntimeError
from search.operations.config import GPUConfig, ProductConfig
from search.operations.state import locked_state
from tests.unit.operations.test_lifecycle import REVISION, dataset


class VectorInputLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = ProductConfig(
            2,
            self.root / "state",
            self.root / "api",
            self.root / "embedding",
            self.root / "ranking",
            GPUConfig("GPU-a", ("GPU-b", "GPU-c"), "GPU-d"),
        )
        source = patch.object(lifecycle, "revision", return_value=REVISION)
        source.start()
        self.addCleanup(source.stop)
        self.candidate = lifecycle.sync_metadata(
            self.config,
            fetch=lambda start, rows, deadline: {
                "count": 1,
                "results": [dataset()] if start == 0 else [],
            },
        )
        self.plan = {
            "schema_version": 1,
            "revision": REVISION,
            **{
                key: self.candidate[key]
                for key in ("bundle_path", "manifest_sha256", "document_count")
            },
        }
        with locked_state(self.config) as store:
            store.write("index-candidate.json", self.plan)

    def assert_product_locked(self):
        with self.assertRaisesRegex(ValueError, "product_operation_busy"):
            with locked_state(self.config):
                self.fail("product lock was not retained")

    def test_lookup_retains_product_lock_and_leaves_no_operation_or_state_writes(self):
        before = {
            p: p.read_bytes() for p in self.config.state_root.rglob("*") if p.is_file()
        }

        def lookup(config, plan, **options):
            self.assertIs(config, self.config)
            self.assertEqual(self.plan, plan)
            self.assert_product_locked()
            return {"inputs_sha256": "1" * 64, "read_only": True}

        with patch.object(vectors, "inputs", side_effect=lookup) as inputs:
            report = lifecycle.vector_inputs(self.config)
        self.assertTrue(report["read_only"])
        inputs.assert_called_once()
        self.assertEqual(
            before,
            {
                p: p.read_bytes()
                for p in self.config.state_root.rglob("*")
                if p.is_file()
            },
        )

    def test_lookup_rechecks_final_source_revision_without_writing_success(self):
        before = set(self.config.state_root.glob("op-*.json"))
        with (
            patch.object(
                lifecycle, "revision", side_effect=[REVISION, REVISION, "b" * 40]
            ),
            patch.object(vectors, "inputs", return_value={"inputs_sha256": "1" * 64}),
            self.assertRaisesRegex(ValueError, "product_invocation_source_changed"),
        ):
            lifecycle.vector_inputs(self.config)
        self.assertEqual(before, set(self.config.state_root.glob("op-*.json")))

    def test_build_forwards_optional_expectation_with_product_lock_retained(self):
        for expected in (None, "2" * 64):
            with self.subTest(expected=expected):

                def build(config, plan, **options):
                    self.assertIs(config, self.config)
                    self.assertEqual(self.plan, plan)
                    self.assertEqual(expected, options["expected_inputs_sha256"])
                    self.assert_product_locked()
                    return {
                        "schema_version": 1,
                        "status": "checkpoint",
                        "revision": REVISION,
                        "active_changed": False,
                        "runtime_verified": True,
                        "host_cleanup_verified": True,
                        "document_count": 1,
                        **{
                            key: self.candidate[key]
                            for key in ("bundle_id", "manifest_sha256")
                        },
                    }

                with patch.object(vectors, "build", side_effect=build) as domain:
                    report = lifecycle.build_vectors(
                        self.config, expected_inputs_sha256=expected
                    )
                self.assertEqual("checkpoint", report["status"])
                domain.assert_called_once()

    def test_malformed_expectation_fails_before_catalog_or_model_lookup(self):
        with (
            patch.object(vectors.execution, "load_context") as context,
            self.assertRaisesRegex(
                ValueError, "catalog_vector_expected_inputs_invalid"
            ),
        ):
            lifecycle.build_vectors(self.config, expected_inputs_sha256="A" * 64)
        context.assert_not_called()


class VectorInputCLITests(unittest.TestCase):
    def invoke(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(["--config", "/private/product.json", *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_lookup_uses_native_read_only_action_and_fixed_summary(self):
        summary = {
            "inputs_sha256": "a" * 64,
            "source_revision": "b" * 40,
            "read_only": True,
        }
        config = object()
        with (
            patch.object(product_config, "load_config", return_value=config),
            patch.object(lifecycle, "vector_inputs", return_value=summary) as lookup,
        ):
            code, output, errors = self.invoke(["vector-inputs"])
        self.assertEqual((0, ""), (code, errors))
        self.assertEqual(summary, json.loads(output))
        lookup.assert_called_once_with(config)

    def test_cli_forwards_expected_digest_and_keeps_manual_default(self):
        for expected in (None, "c" * 64):
            with self.subTest(expected=expected):
                config = object()
                arguments = [
                    "build-vectors",
                    "--max-passes",
                    "1",
                    "--wall-seconds",
                    "3600",
                ]
                if expected is not None:
                    arguments += ["--expected-inputs-sha256", expected]
                with (
                    patch.object(product_config, "load_config", return_value=config),
                    patch.object(
                        lifecycle,
                        "build_vectors",
                        return_value={"status": "checkpoint"},
                    ) as build,
                ):
                    code, output, errors = self.invoke(arguments)
                self.assertEqual((2, ""), (code, errors))
                self.assertEqual("checkpoint", json.loads(output)["status"])
                build.assert_called_once_with(
                    config,
                    max_passes=1,
                    wall_seconds=3600,
                    expected_inputs_sha256=expected,
                )

    def test_missing_admin_lookup_has_a_closed_operator_diagnostic(self):
        with (
            patch.object(product_config, "load_config", return_value=object()),
            patch.object(
                lifecycle,
                "vector_inputs",
                side_effect=CatalogRuntimeError("catalog_admin_image_evidence_missing"),
            ),
        ):
            code, output, errors = self.invoke(["vector-inputs"])
        self.assertEqual((1, ""), (code, output))
        self.assertEqual(
            "catalog_admin_image_evidence_missing", json.loads(errors)["error_code"]
        )
