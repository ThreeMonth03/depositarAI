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
from search.operations.deployment.public_entry import remote
from search.operations.deployment.public_entry import updates


class PublicEntryCLITests(unittest.TestCase):
    def arguments(self, command):
        common = [
            command,
            "--entry-config",
            "/private/public.json",
        ]
        if command == "rollback-public-entry":
            return common
        return common + [
            "--release-compose",
            "/private/release/compose.json",
            "--web-image",
            "sha256:" + "a" * 64,
            "--web-nginx-image",
            "sha256:" + "b" * 64,
        ]

    def test_entry_dispatch_does_not_require_model_or_product_configuration(self):
        for command, module, action in (
            ("prepare-public-entry", host, "prepare_public_entry"),
            ("up-public-entry", host, "up_public_entry"),
            ("update-public-entry", updates, "update_public_entry"),
            ("rollback-public-entry", updates, "rollback_public_entry"),
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
            if command != "rollback-public-entry":
                expected.extend(
                    [
                        Path("/private/release/compose.json"),
                        "sha256:" + "a" * 64,
                        "sha256:" + "b" * 64,
                    ]
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

    def test_remote_backend_dispatch_uses_no_product_or_release_configuration(self):
        for command, module, action in (
            ("prepare-public-entry", host, "prepare_public_entry"),
            ("up-public-entry", host, "up_public_entry"),
            ("update-public-entry", updates, "update_public_entry"),
        ):
            arguments = self.arguments(command)
            arguments[arguments.index("--release-compose")] = "--backend-config"
            arguments[arguments.index("/private/release/compose.json")] = (
                "/private/backend.json"
            )
            with (
                self.subTest(command=command),
                patch("search.operations.config.load_config") as product,
                patch.object(
                    module, action, return_value={"status": "prepared"}
                ) as entry,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main(arguments), 0)
            product.assert_not_called()
            entry.assert_called_once_with(
                Path("/private/public.json"),
                None,
                "sha256:" + "a" * 64,
                "sha256:" + "b" * 64,
                backend_config=Path("/private/backend.json"),
            )

    def test_backend_source_must_be_explicit_and_unambiguous(self):
        arguments = self.arguments("prepare-public-entry")
        index = arguments.index("--release-compose")
        for invalid in (
            arguments[:index] + arguments[index + 2 :],
            arguments + ["--backend-config", "/private/backend.json"],
        ):
            with (
                self.subTest(arguments=invalid),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                cli.main(invalid)

    def test_product_config_is_rejected_for_public_entry(self):
        for command in (
            "prepare-public-entry",
            "up-public-entry",
            "update-public-entry",
            "rollback-public-entry",
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

    def test_failed_deployments_have_nonzero_exit_without_claiming_automatic_rollback(
        self,
    ):
        report = {
            "status": "deployment_failed",
            "active_changed": False,
            "entrance_stop_requested": True,
            "manual_rollback_required": True,
        }
        for command, module, action in (
            ("up-public-entry", host, "up_public_entry"),
            ("update-public-entry", updates, "update_public_entry"),
            ("rollback-public-entry", updates, "rollback_public_entry"),
        ):
            output = io.StringIO()
            with (
                self.subTest(command=command),
                patch.object(module, action, return_value=report),
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
                remote.PublicEntryRemoteError("public_entry_remote_probe_failed"),
                "public_entry_remote_probe_failed",
            ),
            (remote.PublicEntryRemoteError("private-secret-sentinel"), None),
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

    def test_pair_commands_require_every_argument_before_dispatch(self):
        for command, module, action in (
            ("prepare-public-entry", host, "prepare_public_entry"),
            ("up-public-entry", host, "up_public_entry"),
            ("update-public-entry", updates, "update_public_entry"),
        ):
            for required in (
                "--entry-config",
                "--release-compose",
                "--web-image",
                "--web-nginx-image",
            ):
                arguments = self.arguments(command)
                index = arguments.index(required)
                del arguments[index : index + 2]
                with (
                    self.subTest(command=command, missing=required),
                    patch.object(module, action) as mocked,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    with self.assertRaises(SystemExit) as error:
                        cli.main(arguments)
                self.assertEqual(error.exception.code, 2)
                mocked.assert_not_called()

    def test_rollback_rejects_unrecorded_image_or_release_overrides(self):
        for extra in (
            ["--web-image", "sha256:" + "a" * 64],
            ["--web-nginx-image", "sha256:" + "b" * 64],
            ["--release-compose", "/private/other/compose.json"],
        ):
            with (
                self.subTest(extra=extra),
                patch.object(updates, "rollback_public_entry") as action,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    cli.main([*self.arguments("rollback-public-entry"), *extra])
            action.assert_not_called()

    def test_removed_recovery_command_cannot_invoke_a_deployment(self):
        with (
            patch.object(updates, "rollback_public_entry") as rollback,
            patch.object(host, "up_public_entry") as up,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as error:
                cli.main(
                    ["recover-public-entry", "--entry-config", "/private/public.json"]
                )
        self.assertEqual(error.exception.code, 2)
        rollback.assert_not_called()
        up.assert_not_called()


if __name__ == "__main__":
    unittest.main()
