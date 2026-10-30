"""Offline restore dispatch must not require runtime paths or disclose errors."""

import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from search.operations import cli
from search.operations.deployment.payload import restore as product_restore


class ProductRestoreCLITests(unittest.TestCase):
    def arguments(self):
        return [
            "restore-product",
            "--package",
            "/private/package",
            "--expected-revision",
            "a" * 40,
            "--expected-manifest-sha256",
            "b" * 64,
            "--destination",
            "/private/new-restore",
        ]

    def test_offline_restore_dispatches_without_loading_runtime_config(self):
        output = io.StringIO()
        report = {"status": "restored", "install_complete": False}
        with (
            patch("search.operations.config.load_config") as config,
            patch.object(
                product_restore, "restore_product", return_value=report
            ) as restore,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(0, cli.main(self.arguments()))
        config.assert_not_called()
        restore.assert_called_once_with(
            Path("/private/package"),
            expected_revision="a" * 40,
            expected_manifest_sha256="b" * 64,
            destination=Path("/private/new-restore"),
        )
        self.assertEqual(report, json.loads(output.getvalue()))

    def test_runtime_commands_still_require_config_before_dispatch(self):
        for command in ("status", "doctor", "sync", "start-inference", "start-release"):
            with (
                self.subTest(command=command),
                patch("search.operations.config.load_config") as config,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                cli.main([command])
            self.assertEqual(2, error.exception.code)
            config.assert_not_called()

    def test_config_is_rejected_for_restore_before_any_file_is_read(self):
        with (
            patch("search.operations.config.load_config") as config,
            patch.object(product_restore, "restore_product") as restore,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            cli.main(["--config", "/private/unused.json", *self.arguments()])
        self.assertEqual(2, error.exception.code)
        config.assert_not_called()
        restore.assert_not_called()

    def test_revision_manifest_and_destination_are_required(self):
        arguments = self.arguments()
        for flag in (
            "--package",
            "--expected-revision",
            "--expected-manifest-sha256",
            "--destination",
        ):
            index = arguments.index(flag)
            with (
                self.subTest(flag=flag),
                patch.object(product_restore, "restore_product") as restore,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                cli.main(arguments[:index] + arguments[index + 2 :])
            self.assertEqual(2, error.exception.code)
            restore.assert_not_called()

    def test_only_exact_allowlisted_restore_errors_are_reported(self):
        code = sorted(product_restore.ERROR_CODES)[0]
        errors = [
            (product_restore.ProductRestoreError(code), code),
            (product_restore.ProductRestoreError("private-token-do-not-print"), None),
            (ValueError("private-token-do-not-print"), None),
        ]
        for exception, expected in errors:
            output, error = io.StringIO(), io.StringIO()
            with (
                self.subTest(error=type(exception).__name__, expected=expected),
                patch.object(product_restore, "restore_product", side_effect=exception),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(error),
            ):
                self.assertEqual(1, cli.main(self.arguments()))
            self.assertEqual("", output.getvalue())
            self.assertNotIn("private-token", error.getvalue())
            report = json.loads(error.getvalue())
            self.assertIs(report["active_changed"], False)
            self.assertEqual(expected, report.get("error_code"))


if __name__ == "__main__":
    unittest.main()
