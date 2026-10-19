"""Public entrance commands use their own configuration and redact failures."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from search.operations import cli
from search.operations.deployment.public_entry import host
from search.operations.deployment.public_entry import preflight
from search.operations.deployment.public_entry import updates


class PublicEntryCLITests(unittest.TestCase):
    def arguments(self, command):
        common = [
            command,
            "--entry-config",
            "/private/public.json",
        ]
        if command in {"rollback-public-entry", "recover-public-entry"}:
            return common
        return common + [
            "--release-compose",
            "/private/release/compose.json",
            "--web-image",
            "sha256:" + "a" * 64,
        ]

    def test_entry_dispatch_does_not_require_model_or_product_configuration(self):
        for command, module, action in (
            ("prepare-public-entry", host, "prepare_public_entry"),
            ("up-public-entry", host, "up_public_entry"),
            ("update-public-entry", updates, "update_public_entry"),
            ("rollback-public-entry", updates, "rollback_public_entry"),
            ("recover-public-entry", updates, "recover_public_entry"),
        ):
            output = io.StringIO()
            with (
                self.subTest(command=command),
                patch("search.operations.config.load_config") as product,
                patch.object(
                    module, action, return_value={"status": "prepared"}
                ) as entry,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(0, cli.main(self.arguments(command)))
            product.assert_not_called()
            expected = [Path("/private/public.json")]
            if command not in {"rollback-public-entry", "recover-public-entry"}:
                expected.extend(
                    [Path("/private/release/compose.json"), "sha256:" + "a" * 64]
                )
            entry.assert_called_once_with(*expected)
            self.assertEqual(json.loads(output.getvalue()), {"status": "prepared"})

    def test_public_entry_errors_do_not_disclose_credentials(self):
        output = io.StringIO()
        with (
            patch.object(
                host,
                "prepare_public_entry",
                side_effect=ValueError("private-secret-sentinel"),
            ),
            contextlib.redirect_stderr(output),
        ):
            self.assertEqual(1, cli.main(self.arguments("prepare-public-entry")))
        self.assertNotIn("sentinel", output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["active_changed"])

    def test_product_config_is_rejected_for_public_entry(self):
        for command in (
            "prepare-public-entry",
            "up-public-entry",
            "update-public-entry",
            "rollback-public-entry",
            "recover-public-entry",
        ):
            with (
                self.subTest(command=command),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    cli.main(
                        [
                            "--config",
                            "/private/product.json",
                            *self.arguments(command),
                        ]
                    )

    def test_rolled_back_failure_and_recovery_required_have_nonzero_exit(self):
        for command, module, action in (
            ("up-public-entry", host, "up_public_entry"),
            ("update-public-entry", updates, "update_public_entry"),
            ("rollback-public-entry", updates, "rollback_public_entry"),
            ("recover-public-entry", updates, "recover_public_entry"),
        ):
            for status in ("update_failed_rolled_back", "recovery_required"):
                report = {
                    "status": status,
                    "active_changed": False,
                    "entrance_stopped": False,
                }
                output = io.StringIO()
                with (
                    self.subTest(command=command, status=status),
                    patch.object(
                        module,
                        action,
                        return_value=report,
                    ),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(1, cli.main(self.arguments(command)))
                self.assertEqual(json.loads(output.getvalue()), report)

    def test_only_known_lifecycle_error_codes_are_exposed(self):
        for error, expected in (
            (
                updates.PublicEntryUpdateError("public_entry_pending_operation"),
                "public_entry_pending_operation",
            ),
            (updates.PublicEntryUpdateError("private-secret-sentinel"), None),
            (
                preflight.PublicEntryPreflightError(
                    "public_entry_backend_network_invalid"
                ),
                "public_entry_backend_network_invalid",
            ),
            (preflight.PublicEntryPreflightError("private-secret-sentinel"), None),
            (
                preflight.PublicEntryPreflightError(
                    "public_entry_backend_network_invalid", "private-secret-sentinel"
                ),
                None,
            ),
            (
                updates.PublicEntryUpdateError(
                    "public_entry_pending_operation", "private-secret-sentinel"
                ),
                None,
            ),
            (ValueError("public_entry_pending_operation"), None),
            (
                subprocess.CalledProcessError(
                    1,
                    ["docker", "private-secret-sentinel"],
                    stderr=b"private-secret-sentinel",
                ),
                None,
            ),
        ):
            output = io.StringIO()
            with (
                self.subTest(error_type=type(error).__name__, expected=expected),
                patch.object(
                    updates,
                    "update_public_entry",
                    side_effect=error,
                ),
                contextlib.redirect_stderr(output),
            ):
                self.assertEqual(1, cli.main(self.arguments("update-public-entry")))
            report = json.loads(output.getvalue())
            self.assertEqual(report.get("error_code"), expected)
            self.assertFalse(report["active_changed"])
            self.assertNotIn("sentinel", output.getvalue())

    def test_update_requires_release_and_image_before_dispatch(self):
        with (
            patch.object(updates, "update_public_entry") as action,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                cli.main(
                    ["update-public-entry", "--entry-config", "/private/public.json"]
                )
        action.assert_not_called()

    def test_recovery_and_rollback_reject_unrecorded_image_or_release_overrides(self):
        for command, action_name in (
            ("rollback-public-entry", "rollback_public_entry"),
            ("recover-public-entry", "recover_public_entry"),
        ):
            for extra in (
                ["--web-image", "sha256:" + "a" * 64],
                ["--release-compose", "/private/other/compose.json"],
            ):
                with (
                    self.subTest(command=command, extra=extra),
                    patch.object(
                        updates,
                        action_name,
                    ) as action,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    with self.assertRaises(SystemExit):
                        cli.main([*self.arguments(command), *extra])
                action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
