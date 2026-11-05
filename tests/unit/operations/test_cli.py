"""Native CLI startup and selected-command dependency boundaries."""

from pathlib import Path
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "scripts/depositarai.py"


class CLIStartupTests(unittest.TestCase):
    def run_isolated(self, body):
        program = (
            "import contextlib, importlib.abc, io, json, runpy, sys\n"
            "from pathlib import Path\n"
            f"root = Path({str(ROOT)!r})\n"
            f"launcher = {str(LAUNCHER)!r}\n"
            "sys.path.insert(0, str(root))\n"
            "output, errors = io.StringIO(), io.StringIO()\n"
            "class BlockImports(importlib.abc.MetaPathFinder):\n"
            "    def __init__(self, names): self.names = names\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if any(fullname == name or "
            "fullname.startswith(name + '.') "
            "for name in self.names):\n"
            "            raise AssertionError('unexpected dependency: "
            "' + fullname)\n" + textwrap.dedent(body)
        )
        with tempfile.TemporaryDirectory(
            prefix="depositar-cli-startup-"
        ) as cwd:
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-B", "-c", program],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        return json.loads(result.stdout)

    def test_importing_cli_does_not_load_operator_implementations(self):
        report = self.run_isolated("""
            from search.operations import cli
            loaded = sorted(name for name in sys.modules
                            if name == 'search' or name.startswith('search.'))
            print(json.dumps(loaded))
        """)
        self.assertEqual(
            ["search", "search.operations", "search.operations.cli"], report
        )

    def test_native_help_and_argument_errors_load_only_the_cli_owner(self):
        for arguments, expected_code in (
            (["--help"], 0),
            (["up-private", "--help"], 0),
            (["up-private"], 2),
            (["up-private", "--max-passes", "invalid"], 2),
            ([], 2),
            (["unknown"], 2),
            (["recover-public-entry"], 2),
            (["profile-resources", "derive"], 2),
        ):
            with self.subTest(arguments=arguments):
                report = self.run_isolated(
                    (
                        f"""
                    sys.argv = [launcher, *{arguments!r}]
                    with """
                        f"""contextlib.redirect_stdout(output), """
                        f"""contextlib.redirect_stderr(errors):
                        try:
                            runpy.run_path(launcher, run_name='__main__')
                        except SystemExit as error:
                            code = error.code
                    loaded = sorted(name for name in sys.modules
                                    if name == """
                        f"""'search' or name.startswith('search.'))
                    print(json.dumps({{'code': """
                        f"""code, 'out': output.getvalue(),
                                      'err': """
                        f"""errors.getvalue(), 'loaded': loaded}}))
                """
                    )
                )
                self.assertEqual(expected_code, report["code"])
                self.assertEqual(
                    ["search", "search.operations", "search.operations.cli"],
                    report["loaded"],
                )
                if expected_code == 0:
                    self.assertIn("usage: depositarai.py", report["out"])
                    self.assertIn("--config", report["out"])
                    self.assertEqual("", report["err"])
                else:
                    self.assertEqual("", report["out"])
                    self.assertIn("usage: depositarai.py", report["err"])

    def test_importing_launcher_does_not_expose_a_second_cli_interface(self):
        report = self.run_isolated(
            (
                """
            namespace = runpy.run_path(launcher, run_name='launcher_probe')
            exposed = sorted(name for name in ('main', """
                """'parser', 'load_config', 'lifecycle')
                             if name in namespace)
            loaded = sorted(name for name in sys.modules
                            if name == 'search' or """
                """name.startswith('search.'))
            print(json.dumps({'exposed': exposed, 'loaded': loaded}))
        """
            )
        )
        self.assertEqual({"exposed": [], "loaded": []}, report)

    def test_doctor_dispatch_avoids_lifecycle_and_deployment_imports(self):
        report = self.run_isolated(
            (
                """
            sys.meta_path.insert(0, """
                """BlockImports(('search.operations.lifecycle',
                'search.operations.deployment', """
                """'search.operations.catalog', 'search.catalog',
                'search.api', 'search.planning')))
            from search.operations import cli, config, doctor
            marker, calls = object(), []
            def load(path, *, repo_root):
                assert path == """
                """Path('/unused/product.json') and repo_root == root
                return marker
            def diagnose(value, *, repo_root, deep_verify):
                assert value is marker and repo_root == """
                """root and deep_verify is True
                calls.append('doctor')
                return {'status': 'ready'}
            config.load_config, doctor.run_doctor = load, diagnose
            with contextlib.redirect_stdout(output), """
                """contextlib.redirect_stderr(errors):
                code = cli.main(['--config', """
                """'/unused/product.json', 'doctor', '--deep'])
            print(json.dumps({'code': code, 'out': """
                """json.loads(output.getvalue()),
                              'err': errors.getvalue(), 'calls': calls}))
        """
            )
        )
        self.assertEqual(
            {
                "code": 0,
                "out": {"status": "ready"},
                "err": "",
                "calls": ["doctor"],
            },
            report,
        )

    def test_prepare_entry_does_not_load_update_or_restore_controllers(self):
        report = self.run_isolated(
            (
                """
            sys.meta_path.insert(0, """
                """BlockImports(('search.operations.lifecycle',
                'search.operations.deployment.public_entry.updates',
                'search.operations.deployment.public_entry.preflight',
                'search.operations.deployment.payload.restore',
                'search.operations.catalog', """
                """'search.catalog', 'search.api')))
            from search.operations import cli
            from search.operations.deployment.public_entry import host
            calls = []
            def prepare(config, release, image, frontend):
                assert config == Path('/unused/entry.json')
                assert release == """
                """Path('/unused/release.json') and image == 'image-fixture'
                assert frontend == 'frontend-image-fixture'
                calls.append('prepare')
                return {'status': 'prepared'}
            host.prepare_public_entry = prepare
            with contextlib.redirect_stdout(output), """
                """contextlib.redirect_stderr(errors):
                code = cli.main(['prepare-public-entry', """
                """'--entry-config', '/unused/entry.json',
                    '--release-compose', """
                """'/unused/release.json', '--web-image', 'image-fixture',
                    '--web-nginx-image', 'frontend-image-fixture'])
            print(json.dumps({'code': code, 'out': """
                """json.loads(output.getvalue()),
                              'err': errors.getvalue(), 'calls': calls}))
        """
            )
        )
        self.assertEqual(
            {
                "code": 0,
                "out": {"status": "prepared"},
                "err": "",
                "calls": ["prepare"],
            },
            report,
        )

    def test_up_private_forwards_staging_options_to_one_lifecycle_action(self):
        report = self.run_isolated(
            (
                """
            sys.meta_path.insert(0, """
                """BlockImports(('search.operations.deployment',
                'search.api', 'search.planning')))
            from search.operations import cli, config, lifecycle
            marker, calls = object(), []
            def load(path, *, repo_root):
                assert path == """
                """Path('/unused/product.json') and repo_root == root
                calls.append('config')
                return marker
            def up(value, **kwargs):
                assert value is marker
                assert kwargs == {'snapshot': """
                """Path('/private/metadata.jsonl.gz'),
                    'force': True, 'max_passes': 3, 'wall_seconds': 4800,
                    'catalog_revision': 'a' * 40}
                calls.append('up-private')
                return {'status': 'gateway_active', 'active_changed': True}
            def forbidden(*args, **kwargs):
                raise AssertionError('CLI must not """
                """implement the deployment steps')
            config.load_config, lifecycle.up_private = load, up
            lifecycle.stage_catalog = forbidden
            with contextlib.redirect_stdout(output), """
                """contextlib.redirect_stderr(errors):
                code = cli.main(['--config', """
                """'/unused/product.json', 'up-private',
                    '--snapshot', '/private/metadata.jsonl.gz', '--force',
                    '--max-passes', '3', '--wall-seconds', '4800',
                    '--catalog-revision', 'a' * 40])
            print(json.dumps({'code': code, 'out': """
                """json.loads(output.getvalue()),
                              'err': errors.getvalue(), 'calls': calls}))
        """
            )
        )
        self.assertEqual(
            {
                "code": 0,
                "out": {"status": "gateway_active", "active_changed": True},
                "err": "",
                "calls": ["config", "up-private"],
            },
            report,
        )

    def test_up_private_defaults_preserve_checkpoint_exit_code(self):
        report = self.run_isolated(
            (
                """
            from search.operations import cli, config, lifecycle
            marker, calls = object(), []
            config.load_config = lambda *args, **kwargs: marker
            def up(value, **kwargs):
                assert value is marker
                assert kwargs == {'snapshot': None, 'force': False,
                    'max_passes': 16, 'wall_seconds': 7200}
                calls.append('up-private')
                return {'operation': 'up-private', 'status': 'checkpoint',
                    'active_changed': False, 'staging_only': True}
            lifecycle.up_private = up
            with contextlib.redirect_stdout(output), """
                """contextlib.redirect_stderr(errors):
                code = cli.main(['--config', """
                """'/unused/product.json', 'up-private'])
            print(json.dumps({'code': code, 'out': """
                """json.loads(output.getvalue()),
                              'err': errors.getvalue(), 'calls': calls}))
        """
            )
        )
        self.assertEqual(2, report["code"])
        self.assertEqual("checkpoint", report["out"]["status"])
        self.assertFalse(report["out"]["active_changed"])
        self.assertTrue(report["out"]["staging_only"])
        self.assertEqual("", report["err"])
        self.assertEqual(["up-private"], report["calls"])

    def test_up_private_fixed_diagnostics_are_safe_and_command_scoped(self):
        for kind in ("allowed", "private", "subclass", "other_command"):
            with self.subTest(kind=kind):
                report = self.run_isolated(
                    (
                        f"""
                    from search.operations import """
                        f"""cli, config, doctor, lifecycle
                    class Derived(lifecycle.UpPrivateError): pass
                    Derived.__module__ = lifecycle.UpPrivateError.__module__
                    exception = (Derived if {kind!r} == 'subclass'
                                 else lifecycle.UpPrivateError)
                    detail = """
                        f"""('private-secret-fixture' if {kind!r} == 'private'
                              else 'product_private_existing_activation')
                    config.load_config = lambda *args, **kwargs: object()
                    def fail(*args, **kwargs): raise exception(detail)
                    lifecycle.up_private = doctor.run_doctor = fail
                    command = 'doctor' if """
                        f"""{kind!r} == 'other_command' else 'up-private'
                    with """
                        f"""contextlib.redirect_stdout(output), """
                        f"""contextlib.redirect_stderr(errors):
                        code = """
                        f"""cli.main(['--config', '/unused/product.json', """
                        f"""command])
                    print(json.dumps({{'code': """
                        f"""code, 'out': output.getvalue(),
                                      'err': """
                        f"""json.loads(errors.getvalue())}}))
                """
                    )
                )
                self.assertEqual(1, report["code"])
                self.assertEqual("", report["out"])
                self.assertEqual("failed", report["err"]["status"])
                self.assertEqual(
                    "product_operation_failed", report["err"]["code"]
                )
                self.assertNotIn("private-secret-fixture", json.dumps(report))
                self.assertFalse(report["err"]["active_changed"])
                if kind == "allowed":
                    self.assertEqual(
                        "product_private_existing_activation",
                        report["err"]["error_code"],
                    )
                else:
                    self.assertNotIn("error_code", report["err"])

    def test_unhandled_errors_still_propagate_without_a_failure_report(self):
        for metadata in ("ordinary", "standard", "catalog", "raising"):
            with self.subTest(metadata=metadata):
                report = self.run_isolated(
                    (
                        f"""
                    from search.operations import cli, config, doctor
                    from search.catalog.errors import CatalogError
                    reads = []
                    class Unhandled(RuntimeError):
                        @property
                        def __class__(self):
                            reads.append('class')
                            if {metadata!r} == 'raising':
                                raise """
                        f"""AssertionError('private-class-fixture')
                            return {{'ordinary': """
                        f"""Unhandled, 'standard': ValueError,
                                     'catalog': CatalogError}}[{metadata!r}]
                    config.load_config = lambda *args, **kwargs: object()
                    def fail(*args, **kwargs): """
                        f"""raise Unhandled('private-error-fixture')
                    doctor.run_doctor = fail
                    with """
                        f"""contextlib.redirect_stdout(output), """
                        f"""contextlib.redirect_stderr(errors):
                        try:
                            cli.main(['--config', """
                        f"""'/unused/product.json', 'doctor'])
                        except Unhandled:
                            raised = True
                        else:
                            raised = False
                    print(json.dumps({{'raised': """
                        f"""raised, 'out': output.getvalue(),
                                      'err': """
                        f"""errors.getvalue(), 'metadata_reads': reads}}))
                """
                    )
                )
                self.assertEqual(
                    {
                        "raised": True,
                        "out": "",
                        "err": "",
                        "metadata_reads": [],
                    },
                    report,
                )

    def test_foreign_error_metadata_cannot_break_or_forge_safe_diagnostics(
        self,
    ):
        for metadata in ("hostile", "list", "none", "forged"):
            with self.subTest(metadata=metadata):
                report = self.run_isolated(
                    (
                        f"""
                    from search.operations import cli, config, doctor
                    class Metadata(str):
                        def __eq__(self, other):
                            raise AssertionError('private-metadata-fixture')
                    class Foreign(ValueError): pass
                    Foreign.__module__ = {{
                        'hostile': """
                        f"""Metadata('search.operations.catalog.policy'),
                        'list': ['search.operations.catalog.policy'],
                        'none': None,
                        'forged': 'search.operations.catalog.policy',
                    }}[{metadata!r}]
                    config.load_config = lambda *args, **kwargs: object()
                    def fail(*args, **kwargs):
                        raise Foreign('catalog_admin_authentication_failed')
                    doctor.run_doctor = fail
                    with """
                        f"""contextlib.redirect_stdout(output), """
                        f"""contextlib.redirect_stderr(errors):
                        code = """
                        f"""cli.main(['--config', '/unused/product.json', """
                        f"""'doctor'])
                    print(json.dumps({{'code': """
                        f"""code, 'out': output.getvalue(),
                                      'err': """
                        f"""json.loads(errors.getvalue())}}))
                """
                    )
                )
                self.assertEqual(
                    {
                        "code": 1,
                        "out": "",
                        "err": {
                            "schema_version": 1,
                            "status": "failed",
                            "code": "product_operation_failed",
                            "error_type": "Foreign",
                            "active_changed": False,
                        },
                    },
                    report,
                )

    def test_provider_help_and_invalid_args_load_only_cli(self):
        control = ["--control-config", "/unused/provider-control.json"]
        bootstrap = [
            "--bootstrap",
            "/unused/bootstrap.json",
            "--expected-bootstrap-sha256",
            "a" * 64,
        ]
        for arguments, expected in (
            (["adopt-backend-provider", "--help"], 0),
            (["check-backend-provider", "--help"], 0),
            (["adopt-backend-provider"], 2),
            (["check-backend-provider"], 2),
            (["adopt-backend-provider", *control], 2),
            (["adopt-backend-provider", *control, *bootstrap[:2]], 2),
            (["adopt-backend-provider", *bootstrap], 2),
            (["adopt-backend-provider", *control, *bootstrap[2:]], 2),
            (["promote-backend-provider"], 2),
            (["recover-backend-provider"], 2),
            (
                [
                    "--config",
                    "/unused/product.json",
                    "adopt-backend-provider",
                    *control,
                    *bootstrap,
                ],
                2,
            ),
            (
                [
                    "--config",
                    "/unused/product.json",
                    "check-backend-provider",
                    *control,
                ],
                2,
            ),
        ):
            with self.subTest(arguments=arguments):
                report = self.run_isolated(f"""
                    sys.meta_path.insert(0, BlockImports((
                        'site', 'torch', 'transformers', 'django',
                        'search.api', 'search.planning', 'search.catalog',
                        'search.operations.config',
                        'search.operations.deployment.provider')))
                    sys.argv = [launcher, *{arguments!r}]
                    with contextlib.redirect_stdout(output), \\
                         contextlib.redirect_stderr(errors):
                        try:
                            runpy.run_path(launcher, run_name='__main__')
                        except SystemExit as error:
                            code = error.code
                    loaded = sorted(name for name in sys.modules
                        if name == 'search' or name.startswith('search.'))
                    print(json.dumps({{'code': code, 'loaded': loaded,
                        'out': output.getvalue(), 'err': errors.getvalue()}}))
                """)
                self.assertEqual(report["code"], expected)
                self.assertEqual(
                    report["loaded"],
                    ["search", "search.operations", "search.operations.cli"],
                )
                if expected == 0:
                    self.assertIn("--control-config", report["out"])
                    self.assertNotIn("--config", report["out"])
                    self.assertEqual(report["err"], "")
                else:
                    self.assertEqual(report["out"], "")
                    self.assertIn("usage: depositarai.py", report["err"])

    def test_provider_dispatch_forwards_paths_without_product_config_or_io(
        self,
    ):
        for command, status in (
            ("adopt-backend-provider", "adopted"),
            ("check-backend-provider", "verified"),
        ):
            with self.subTest(command=command):
                report = self.run_isolated(f"""
                    import os
                    from unittest.mock import patch
                    sys.meta_path.insert(0, BlockImports((
                        'search.operations.config',
                        'search.operations.lifecycle', 'search.api',
                        'search.planning', 'search.catalog')))
                    from search.operations import cli
                    from search.operations.deployment.provider import (
                        adoption, runtime)
                    calls = []
                    expected = {{'schema_version': 1, 'status': {status!r},
                                'epoch': 1}}
                    def adopt(config, bootstrap, *, expected_bootstrap_sha256):
                        assert config == Path('/unused/provider-control.json')
                        assert bootstrap == Path('/unused/bootstrap.json')
                        assert expected_bootstrap_sha256 == 'a' * 64
                        calls.append('adopt')
                        return expected
                    def check(config):
                        assert config == Path('/unused/provider-control.json')
                        calls.append('check')
                        return expected
                    adoption.adopt_running = adopt
                    runtime.verify_provider = check
                    arguments = [{command!r}, '--control-config',
                                 '/unused/provider-control.json']
                    if {command!r} == 'adopt-backend-provider':
                        arguments += ['--bootstrap', '/unused/bootstrap.json',
                            '--expected-bootstrap-sha256', 'a' * 64]
                    with patch.object(cli, '_runtime_report',
                                      side_effect=AssertionError('IO')), \\
                         patch.object(Path, 'open',
                                      side_effect=AssertionError('IO')), \\
                         patch.object(os, 'open',
                                      side_effect=AssertionError('IO')), \\
                         contextlib.redirect_stdout(output), \\
                         contextlib.redirect_stderr(errors):
                        code = cli.main(arguments)
                    print(json.dumps({{'code': code, 'calls': calls,
                        'out': json.loads(output.getvalue()),
                        'lines': output.getvalue().count('\\n'),
                        'err': errors.getvalue()}}))
                """)
                self.assertEqual(report["code"], 0)
                self.assertEqual(
                    report["calls"],
                    [
                        "adopt"
                        if command == "adopt-backend-provider"
                        else "check"
                    ],
                )
                self.assertEqual(
                    report["out"],
                    {"schema_version": 1, "status": status, "epoch": 1},
                )
                self.assertEqual(report["lines"], 1)
                self.assertEqual(report["err"], "")

    def test_provider_failures_report_only_closed_canonical_diagnostics(self):
        for command in ("adopt-backend-provider", "check-backend-provider"):
            for kind in ("allowed", "private", "subclass", "foreign"):
                with self.subTest(command=command, kind=kind):
                    report = self.run_isolated(f"""
                        from search.operations import cli
                        from search.operations.deployment.provider import (
                            adoption, contract, runtime)
                        class Derived(contract.ProviderError): pass
                        Derived.__module__ = contract.__name__
                        Foreign = type('ProviderError', (ValueError,),
                                       {{'__module__': contract.__name__}})
                        error_type = {{'allowed': contract.ProviderError,
                            'private': contract.ProviderError,
                            'subclass': Derived, 'foreign': Foreign}}[{kind!r}]
                        detail = ('/unused/control-private-canary'
                            if {kind!r} == 'private'
                            else 'shared_backend_provider_busy')
                        def fail(*args, **kwargs): raise error_type(detail)
                        adoption.adopt_running = runtime.verify_provider = fail
                        arguments = [{command!r}, '--control-config',
                                     '/unused/provider-control.json']
                        if {command!r} == 'adopt-backend-provider':
                            arguments += ['--bootstrap', '/unused/bootstrap',
                                '--expected-bootstrap-sha256', 'a' * 64]
                        with contextlib.redirect_stdout(output), \\
                             contextlib.redirect_stderr(errors):
                            code = cli.main(arguments)
                        print(json.dumps({{'code': code,
                            'out': output.getvalue(),
                            'err': json.loads(errors.getvalue())}}))
                    """)
                    expected = {
                        "schema_version": 1,
                        "status": "failed",
                        "code": "product_operation_failed",
                        "error_type": "Derived"
                        if kind == "subclass"
                        else "ProviderError",
                        "active_changed": False,
                    }
                    if kind == "allowed":
                        expected["error_code"] = "shared_backend_provider_busy"
                    self.assertEqual(
                        report, {"code": 1, "out": "", "err": expected}
                    )
                    self.assertNotIn(
                        "control-private-canary", json.dumps(report)
                    )


class ReleaseDiagnosticBoundaryTests(unittest.TestCase):
    def run_error(self, command, error):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        from unittest.mock import patch

        from search.operations import cli

        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(cli, "_runtime_report", side_effect=error),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            result = cli.main(["--config", "/unused/product.json", command])
        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "")
        return json.loads(errors.getvalue())

    def test_private_runtime_commands_report_fixed_release_diagnostics(
        self,
    ):
        from search.operations.deployment.release.controller import (
            ReleaseError,
        )

        for command in (
            "up-private",
            "start-inference",
            "start-release",
            "prepare-gateway",
            "activate-gateway",
        ):
            for code in (
                "release_acceptance_blocked",
                "release_acceptance_invalid",
                "release_acceptance_probe_failed",
            ):
                with self.subTest(command=command, code=code):
                    self.assertEqual(
                        self.run_error(command, ReleaseError(code)),
                        {
                            "schema_version": 1,
                            "status": "failed",
                            "code": "product_operation_failed",
                            "error_type": "ReleaseError",
                            "active_changed": False,
                            "error_code": code,
                        },
                    )

    def test_release_diagnostics_reject_untrusted_errors_without_raw_arguments(
        self,
    ):
        from search.operations.deployment.release.controller import (
            ReleaseError,
        )

        private = "private-release-detail-must-not-be-printed"
        similar = type(
            "ReleaseError",
            (ValueError,),
            {"__module__": ReleaseError.__module__},
        )
        subclass = type(
            "DerivedReleaseError",
            (ReleaseError,),
            {"__module__": ReleaseError.__module__},
        )
        for kind, command, error in (
            ("unknown", "start-release", ReleaseError(private)),
            (
                "similar",
                "start-release",
                similar("release_acceptance_blocked"),
            ),
            (
                "subclass",
                "start-release",
                subclass("release_acceptance_blocked"),
            ),
            (
                "wrong-command",
                "doctor",
                ReleaseError("release_acceptance_blocked"),
            ),
            (
                "multiple-arguments",
                "start-release",
                ReleaseError("release_acceptance_blocked", private),
            ),
        ):
            with self.subTest(kind=kind):
                report = self.run_error(command, error)
                self.assertEqual(
                    report,
                    {
                        "schema_version": 1,
                        "status": "failed",
                        "code": "product_operation_failed",
                        "error_type": type(error).__name__,
                        "active_changed": False,
                    },
                )
                self.assertNotIn(private, json.dumps(report))


class ActivationDiagnosticBoundaryTests(unittest.TestCase):
    def test_only_closed_activation_errors_can_claim_a_changed_pointer(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        from unittest.mock import patch

        from search.operations import cli
        from search.operations.state import ActivationOutcomeError

        private = "private-storage-detail-must-not-be-printed"
        wrong_arguments = ActivationOutcomeError(active_changed=True)
        wrong_arguments.args = (private,)
        wrong_flag = ActivationOutcomeError(active_changed=True)
        wrong_flag.active_changed = 1
        missing_flag = ActivationOutcomeError(active_changed=True)
        del missing_flag.active_changed
        forged_type = type(
            "ActivationOutcomeError",
            (ValueError,),
            {"__module__": "search.operations.state"},
        )
        forged = forged_type("product_activation_outcome_unconfirmed")
        forged.active_changed = True
        subclass = type(
            "DerivedActivationError",
            (ActivationOutcomeError,),
            {"__module__": "search.operations.state"},
        )(active_changed=True)
        cases = [
            ("up-private", error)
            for error in (
                wrong_arguments,
                wrong_flag,
                missing_flag,
                forged,
                subclass,
            )
        ]
        cases.append(("sync", ActivationOutcomeError(active_changed=True)))
        for command, error in cases:
            with self.subTest(
                command=command, error_type=type(error).__name__
            ):
                output, errors = io.StringIO(), io.StringIO()
                with (
                    patch.object(cli, "_runtime_report", side_effect=error),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    result = cli.main(
                        ["--config", "/unused/product.json", command]
                    )
                self.assertEqual(result, 1)
                self.assertEqual(output.getvalue(), "")
                report = json.loads(errors.getvalue())
                self.assertEqual(report["status"], "failed")
                self.assertIs(report["active_changed"], False)
                self.assertNotIn("error_code", report)
                self.assertNotIn("activation_performed", report)
                self.assertNotIn("confers_runtime_authority", report)
                self.assertNotIn(private, errors.getvalue())


class ResourceBatchCLITests(unittest.TestCase):
    def test_root_run_is_rejected_before_state_or_resource_operations(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        from unittest.mock import patch

        from search.operations import cli
        from search.operations.documents import batches, extractor, source

        identities = {
            "getuid": 1001,
            "getgid": 1002,
            "geteuid": 1003,
            "getegid": 1004,
        }
        for zero in (*identities, "all"):
            values = {
                name: (
                    lambda value=0 if zero in (name, "all") else value: value
                )
                for name, value in identities.items()
            }
            output, errors = io.StringIO(), io.StringIO()
            with (
                self.subTest(zero=zero),
                patch.multiple(extractor.os, **values),
                patch.object(batches, "_state") as state,
                patch.object(extractor, "build_image") as build,
                patch.object(source, "acquire") as acquire,
                patch.object(cli, "_runtime_report") as runtime,
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                code = cli.main(
                    [
                        "profile-resources",
                        "run",
                        "--bundle",
                        "/private/never-read-bundle",
                        "--state-root",
                        "/private/never-read-state",
                        "--expected-plan-sha256",
                        "a" * 64,
                    ]
                )
            self.assertEqual(1, code)
            self.assertEqual("", output.getvalue())
            self.assertEqual(
                {
                    "schema_version": 1,
                    "status": "failed",
                    "code": "product_operation_failed",
                    "error_type": "ExtractorError",
                    "active_changed": False,
                    "error_code": (
                        "document_extractor_operator_must_be_nonroot"
                    ),
                },
                json.loads(errors.getvalue()),
            )
            for operation in (state, build, acquire, runtime):
                operation.assert_not_called()
            self.assertNotIn("/private/", errors.getvalue())

    def test_extractor_diagnostic_is_limited_to_exact_type_command_and_code(
        self,
    ):
        from search.operations import cli
        from search.operations.documents import extractor

        class OtherExtractorError(extractor.ExtractorError):
            pass

        code = "document_extractor_operator_must_be_nonroot"
        self.assertEqual(
            code,
            cli._operator_error_code(
                extractor.ExtractorError(code), "profile-resources"
            ),
        )
        for error, command in (
            (OtherExtractorError(code), "profile-resources"),
            (
                extractor.ExtractorError("/private/resource token=secret"),
                "profile-resources",
            ),
            (extractor.ExtractorError(code), "sync"),
        ):
            with self.subTest(error=type(error).__name__, command=command):
                self.assertIsNone(cli._operator_error_code(error, command))

    def invoke(self, arguments, action, result, *, owner=None):
        from contextlib import redirect_stdout
        import io
        from unittest.mock import patch

        from search.operations import cli
        from search.operations.documents import batches

        owner = batches if owner is None else owner
        output = io.StringIO()
        with (
            patch.object(owner, action, return_value=result) as called,
            patch.object(
                cli, "_runtime_report", side_effect=AssertionError("runtime")
            ),
            redirect_stdout(output),
        ):
            code = cli.main(["profile-resources", *arguments])
        self.assertEqual(result, json.loads(output.getvalue()))
        return code, called

    def test_prepare_is_config_free_and_forwards_explicit_selection_policy(
        self,
    ):
        from search.operations import cli

        code, called = self.invoke(
            [
                "prepare",
                "--bundle",
                "/private/bundle",
                "--expected-manifest-sha256",
                "a" * 64,
                "--state-root",
                "/private/profiles",
                "--resource-id-file",
                "/private/selection.json",
                "--encoding",
                "cp950",
                "--header-policy",
                "absent",
            ],
            "prepare",
            {"status": "prepared", "plan_sha256": "b" * 64},
        )
        self.assertEqual(0, code)
        called.assert_called_once_with(
            Path("/private/bundle"),
            "a" * 64,
            Path("/private/profiles"),
            Path("/private/selection.json"),
            encoding="cp950",
            header_policy="absent",
            source_root=cli.ROOT,
        )

    def test_run_distinguishes_checkpoint_completion_and_partial_outcomes(
        self,
    ):
        from search.operations import cli

        for status, expected in (
            ("checkpoint", 2),
            ("completed", 0),
            ("partial", 1),
        ):
            with self.subTest(status=status):
                code, called = self.invoke(
                    [
                        "run",
                        "--bundle",
                        "/private/bundle",
                        "--state-root",
                        "/private/profiles",
                        "--expected-plan-sha256",
                        "b" * 64,
                        "--max-resources",
                        "7",
                        "--retry-failed",
                    ],
                    "run",
                    {"status": status},
                )
                self.assertEqual(expected, code)
                called.assert_called_once_with(
                    Path("/private/bundle"),
                    Path("/private/profiles"),
                    "b" * 64,
                    max_resources=7,
                    retry_failed=True,
                    source_root=cli.ROOT,
                )

    def test_status_does_not_start_processing_or_require_a_bundle(self):
        code, called = self.invoke(
            [
                "status",
                "--state-root",
                "/private/profiles",
                "--expected-plan-sha256",
                "b" * 64,
            ],
            "status",
            {"status": "checkpoint", "pending_count": 3},
        )
        self.assertEqual(0, code)
        called.assert_called_once_with(Path("/private/profiles"), "b" * 64)

    def test_derive_forwards_frozen_identities_and_limits_without_config(self):
        from search.operations import cli
        from search.operations.documents import derivation

        arguments = [
            "derive",
            "--source-bundle",
            "/private/source-bundle",
            "--source-state-root",
            "/private/source-profiles",
            "--expected-source-plan-sha256",
            "a" * 64,
            "--bundle",
            "/private/target-bundle",
            "--state-root",
            "/private/target-profiles",
            "--expected-plan-sha256",
            "b" * 64,
            "--max-resources",
            "7",
            "--max-bytes",
            "4096",
            "--wall-seconds",
            "60",
        ]
        for status, expected in (
            ("checkpoint", 2),
            ("completed", 0),
            ("partial", 1),
        ):
            with self.subTest(status=status):
                code, called = self.invoke(
                    arguments,
                    "derive",
                    {"status": status},
                    owner=derivation,
                )
                self.assertEqual(expected, code)
                called.assert_called_once_with(
                    Path("/private/source-bundle"),
                    Path("/private/source-profiles"),
                    "a" * 64,
                    Path("/private/target-bundle"),
                    Path("/private/target-profiles"),
                    "b" * 64,
                    max_resources=7,
                    max_bytes=4096,
                    wall_seconds=60,
                    source_root=cli.ROOT,
                )

    def test_derivation_failure_exposes_only_a_closed_diagnostic(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        from unittest.mock import patch

        from search.operations import cli
        from search.operations.documents import derivation

        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(
                cli,
                "_profile_report",
                side_effect=derivation.DerivationError(
                    "document_derivation_cache_invalid"
                ),
            ),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            code = cli.main(
                [
                    "profile-resources",
                    "derive",
                    "--source-bundle",
                    "/private/source-bundle",
                    "--source-state-root",
                    "/private/source-profiles",
                    "--expected-source-plan-sha256",
                    "a" * 64,
                    "--bundle",
                    "/private/target-bundle",
                    "--state-root",
                    "/private/target-profiles",
                    "--expected-plan-sha256",
                    "b" * 64,
                ]
            )
        self.assertEqual(1, code)
        self.assertEqual("", output.getvalue())
        report = json.loads(errors.getvalue())
        self.assertEqual(
            "document_derivation_cache_invalid", report["error_code"]
        )
        self.assertFalse(report["active_changed"])
        self.assertNotIn("/private/", errors.getvalue())

    def test_profile_set_and_individual_files_are_mutually_exclusive(self):
        from contextlib import redirect_stderr
        import io

        from search.operations import cli

        for command in ("sync", "stage-catalog", "up-private"):
            with self.subTest(command=command), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cli.parser().parse_args(
                        [
                            "--config",
                            "/private/product.json",
                            command,
                            "--profile-set",
                            "/private/profile-set.json",
                            "--profile-file",
                            "/private/profile.json",
                        ]
                    )
                self.assertEqual(2, caught.exception.code)

    def test_profile_set_reaches_each_existing_lifecycle_owner(self):
        from contextlib import redirect_stdout
        import io
        from unittest.mock import patch

        from search.operations import cli, config, lifecycle

        for command, action in (
            ("sync", "sync_metadata"),
            ("stage-catalog", "stage_catalog"),
            ("up-private", "up_private"),
        ):
            with (
                self.subTest(command=command),
                patch.object(config, "load_config", return_value=object()),
                patch.object(
                    lifecycle, action, return_value={"status": "ready"}
                ) as run,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    0,
                    cli.main(
                        [
                            "--config",
                            "/private/product.json",
                            command,
                            "--profile-set",
                            "/private/profile-set.json",
                        ]
                    ),
                )
                self.assertEqual(
                    Path("/private/profile-set.json"),
                    run.call_args.kwargs["profile_set"],
                )
                self.assertNotIn("profile_files", run.call_args.kwargs)

    def test_profile_baseline_reaches_each_existing_lifecycle_owner(self):
        from contextlib import redirect_stdout
        import io
        from unittest.mock import patch

        from search.operations import cli, config, lifecycle

        for command, action in (
            ("sync", "sync_metadata"),
            ("stage-catalog", "stage_catalog"),
            ("up-private", "up_private"),
        ):
            with (
                self.subTest(command=command),
                patch.object(config, "load_config", return_value=object()),
                patch.object(
                    lifecycle, action, return_value={"status": "ready"}
                ) as run,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    0,
                    cli.main(
                        [
                            "--config",
                            "/private/product.json",
                            command,
                            "--profile-set",
                            "/private/profile-set.json",
                            "--profile-baseline-bundle",
                            "/private/captured-metadata",
                        ]
                    ),
                )
                self.assertEqual(
                    Path("/private/profile-set.json"),
                    run.call_args.kwargs["profile_set"],
                )
                self.assertEqual(
                    Path("/private/captured-metadata"),
                    run.call_args.kwargs["profile_baseline_bundle"],
                )
                self.assertNotIn("profile_files", run.call_args.kwargs)

    def test_profile_baseline_requires_a_set_before_lifecycle_dispatch(self):
        from contextlib import redirect_stderr
        import io
        from unittest.mock import patch

        from search.operations import cli, config, lifecycle

        for command, action in (
            ("sync", "sync_metadata"),
            ("stage-catalog", "stage_catalog"),
            ("up-private", "up_private"),
        ):
            for selection in ([], ["--profile-file", "/private/profile.json"]):
                with (
                    self.subTest(command=command, selection=selection),
                    patch.object(config, "load_config") as load,
                    patch.object(lifecycle, action) as run,
                    redirect_stderr(io.StringIO()),
                ):
                    with self.assertRaises(SystemExit) as caught:
                        cli.main(
                            [
                                "--config",
                                "/private/product.json",
                                command,
                                "--profile-baseline-bundle",
                                "/private/captured-metadata",
                                *selection,
                            ]
                        )
                    self.assertEqual(2, caught.exception.code)
                    load.assert_not_called()
                    run.assert_not_called()


class OperatorErrorOwnerTests(unittest.TestCase):
    def owners(self):
        from search.operations import lifecycle
        from search.operations.catalog import access, policy, projection
        from search.operations.deployment import (
            inference_runtime,
            runtime_docker,
        )
        from search.operations.deployment.payload import restore
        from search.operations.deployment.provider import contract as provider
        from search.operations.deployment.public_entry import (
            preflight,
            remote,
            updates,
        )
        from search.operations.deployment.release import controller as release
        from search.operations.documents import (
            batches,
            controller,
            derivation,
            enrichment,
            extractor,
            sets,
        )

        return (
            (policy.RuntimeError, "catalog_runtime_not_healthy", None),
            (access.AccessError, "catalog_access_binding_invalid", None),
            (
                projection.ProjectionError,
                "catalog_projection_index_changed",
                None,
            ),
            (
                runtime_docker.InferenceError,
                "inference_command_failed",
                "up-private",
            ),
            (
                inference_runtime.InferenceRuntimeError,
                "inference_runtime_snapshot_invalid",
                "start-inference",
            ),
            (
                release.ReleaseError,
                "release_acceptance_invalid",
                "start-release",
            ),
            (
                lifecycle.UpPrivateError,
                "product_private_existing_activation",
                "up-private",
            ),
            (
                restore.ProductRestoreError,
                "product_restore_manifest_invalid",
                "restore-product",
            ),
            (
                provider.ProviderError,
                "shared_backend_provider_not_adopted",
                "adopt-backend-provider",
            ),
            (
                provider.ProviderError,
                "shared_backend_provider_not_adopted",
                "check-backend-provider",
            ),
            (
                preflight.PublicEntryPreflightError,
                "public_entry_backend_plan_invalid",
                "prepare-public-entry",
            ),
            (
                remote.PublicEntryRemoteError,
                "public_entry_remote_probe_failed",
                "up-public-entry",
            ),
            (
                updates.PublicEntryUpdateError,
                "public_entry_runtime_changed",
                "update-public-entry",
            ),
            (
                extractor.ExtractorError,
                "document_extractor_operator_must_be_nonroot",
                "profile-resources",
            ),
            (
                controller.ProfileError,
                "document_state_busy",
                "profile-resources",
            ),
            (
                batches.BatchError,
                "document_batch_plan_changed",
                "profile-resources",
            ),
            (
                derivation.DerivationError,
                "document_derivation_identity_changed",
                "profile-resources",
            ),
            (sets.ProfileSetError, "document_profile_set_invalid", "sync"),
            (
                enrichment.EnrichmentError,
                "document_enrichment_source_mismatch",
                "stage-catalog",
            ),
        )

    def test_fixed_owners_keep_their_exact_class_and_command_boundaries(self):
        from search.operations import cli

        for error_type, code, command in self.owners():
            with self.subTest(owner=error_type.__module__):
                for allowed in (command,) if command else ("doctor", "sync"):
                    self.assertEqual(
                        code,
                        cli._operator_error_code(error_type(code), allowed),
                    )
                if command:
                    self.assertIsNone(
                        cli._operator_error_code(error_type(code), "doctor")
                    )

    def test_matching_module_and_name_or_inheritance_do_not_grant_ownership(
        self,
    ):
        from search.operations import cli

        for error_type, code, command in self.owners():
            for base, owner in (
                (ValueError, error_type.__module__),
                (error_type, error_type.__module__),
                (error_type, "untrusted.fixture"),
            ):
                with self.subTest(
                    owner=error_type.__module__, base=base, claimed=owner
                ):
                    forged = type(
                        error_type.__name__, (base,), {"__module__": owner}
                    )
                    self.assertIsNone(
                        cli._operator_error_code(
                            forged(code), command or "doctor"
                        )
                    )

    def test_provider_codes_are_limited_to_the_two_canonical_commands(self):
        from search.operations import cli
        from search.operations.deployment.provider import contract

        for code in contract.ERROR_CODES:
            error = contract.ProviderError(code)
            for command in (
                "adopt-backend-provider",
                "check-backend-provider",
            ):
                self.assertEqual(
                    cli._operator_error_code(error, command), code
                )
            for command in (
                "doctor",
                "sync",
                "profile-resources",
                "up-private",
                "promote-backend-provider",
                "recover-backend-provider",
                "check-backend-provider-extra",
            ):
                self.assertIsNone(cli._operator_error_code(error, command))

        for command in ("adopt-backend-provider", "check-backend-provider"):
            self.assertIsNone(
                cli._operator_error_code(
                    contract.ProviderError("shared_backend_provider_forged"),
                    command,
                )
            )

    def test_only_one_exact_public_string_argument_can_be_a_diagnostic(self):
        from search.operations import cli

        class HostileText(str):
            def __eq__(self, other):
                raise AssertionError(
                    "must not compare untrusted string subclasses"
                )

            def __hash__(self):
                raise AssertionError(
                    "must not hash untrusted string subclasses"
                )

        private = "private-error-detail-fixture"
        for error_type, code, command in self.owners():
            for arguments in (
                (),
                (private,),
                (HostileText(code),),
                (code, private),
            ):
                with self.subTest(
                    owner=error_type.__module__, arity=len(arguments)
                ):
                    self.assertIsNone(
                        cli._operator_error_code(
                            error_type(*arguments), command or "doctor"
                        )
                    )

    def test_unknown_or_nonexact_module_metadata_is_never_an_import_target(
        self,
    ):
        import builtins
        import importlib
        from unittest.mock import patch

        from search.operations import cli

        class HostileOwner(str):
            def __eq__(self, other):
                raise AssertionError(
                    "must not compare untrusted module metadata"
                )

            def __hash__(self):
                raise AssertionError("must not hash untrusted module metadata")

        for owner in (
            "untrusted.fixture.side_effect",
            "search.operations.catalog.policy.extra",
            None,
            17,
            ["search.operations.catalog.policy"],
            HostileOwner("search.operations.catalog.policy"),
        ):
            foreign = type(
                "RuntimeError", (ValueError,), {"__module__": owner}
            )
            error = foreign("catalog_runtime_not_healthy")
            with (
                self.subTest(metadata_type=type(owner).__name__),
                patch.object(
                    builtins,
                    "__import__",
                    side_effect=AssertionError("import tripwire"),
                ) as direct,
                patch.object(
                    importlib,
                    "import_module",
                    side_effect=AssertionError("import tripwire"),
                ) as dynamic,
            ):
                diagnostic = cli._operator_error_code(error, "doctor")
            self.assertIsNone(diagnostic)
            direct.assert_not_called()
            dynamic.assert_not_called()


class ExtractorDiagnosticCLITests(unittest.TestCase):
    CODES = frozenset(
        {
            "document_extractor_build_failed",
            "document_extractor_cleanup_failed",
            "document_extractor_command_failed",
            "document_extractor_command_timeout",
            "document_extractor_image_invalid",
            "document_extractor_input_changed",
            "document_extractor_input_invalid",
            "document_extractor_inspect_failed",
            "document_extractor_operator_must_be_nonroot",
            "document_extractor_output_limit",
            "document_extractor_report_invalid",
            "document_extractor_settings_invalid",
            "document_extractor_source_invalid",
        }
    )

    def invoke_failure(self, error, command):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        from unittest.mock import patch

        from search.operations import cli
        from search.operations.documents import batches

        arguments = [
            "profile-resources",
            "run",
            "--bundle",
            "/private/never-read-bundle",
            "--state-root",
            "/private/never-read-state",
            "--expected-plan-sha256",
            "a" * 64,
        ]
        owner, action = batches, "run"
        if command == "sync":
            arguments = ["--config", "/private/never-read-config", "sync"]
            owner, action = cli, "_runtime_report"
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(owner, action, side_effect=error),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            status = cli.main(arguments)
        return status, output.getvalue(), errors.getvalue()

    def assert_failure(self, error, code=None, command="profile-resources"):
        status, output, errors = self.invoke_failure(error, command)
        expected = {
            "schema_version": 1,
            "status": "failed",
            "code": "product_operation_failed",
            "error_type": type(error).__name__,
            "active_changed": False,
        }
        if code is not None:
            expected["error_code"] = code
        self.assertEqual(1, status)
        self.assertEqual("", output)
        self.assertEqual(expected, json.loads(errors))
        for private in ("/private/", "example.test", "raw-docker-output"):
            self.assertNotIn(private, errors)

    def test_registered_codes_are_exact_reviewed_literals(self):
        from search.operations.documents import extractor

        self.assertEqual(self.CODES, extractor.ERROR_CODES)

    def test_fixed_codes_reach_actual_cli_failure_json_and_exit(self):
        from search.operations.documents import extractor

        for code in sorted(self.CODES):
            with self.subTest(code=code):
                self.assert_failure(extractor.ExtractorError(code), code)

    def test_unknown_text_arguments_and_noncanonical_types_stay_private(self):
        from search.operations.documents import extractor

        class DerivedExtractorError(extractor.ExtractorError):
            pass

        class HostileText(str):
            def __eq__(self, other):
                raise AssertionError("must not compare nonexact strings")

            def __hash__(self):
                raise AssertionError("must not hash nonexact strings")

            def __str__(self):
                raise AssertionError("must not stringify exception arguments")

        code = "document_extractor_command_timeout"
        forged = type(
            "ExtractorError", (ValueError,), {"__module__": extractor.__name__}
        )
        foreign = type(
            "ExtractorError",
            (extractor.ExtractorError,),
            {"__module__": "untrusted.fixture"},
        )
        for error in (
            extractor.ExtractorError("document_extractor_unknown"),
            extractor.ExtractorError("/private/resource token=secret"),
            extractor.ExtractorError("https://example.test/private"),
            extractor.ExtractorError("raw-docker-output token=secret"),
            extractor.ExtractorError(),
            extractor.ExtractorError(code, "/private/extra-argument"),
            extractor.ExtractorError(None),
            extractor.ExtractorError(HostileText(code)),
            DerivedExtractorError(code),
            forged(code),
            foreign(code),
        ):
            with self.subTest(error_type=type(error).__name__):
                self.assert_failure(error)

    def test_wrong_command_keeps_fixed_extractor_codes_generic(self):
        from search.operations.documents import extractor

        for code in sorted(self.CODES):
            with self.subTest(code=code):
                self.assert_failure(
                    extractor.ExtractorError(code), command="sync"
                )

    def test_synthetic_nul_report_preserves_its_fixed_cli_diagnostic(self):
        from search.operations.documents import extractor
        from tests.unit.operations.documents.test_extractor import report

        value = report()
        value["text"] = "station\x00value"
        with self.assertRaises(extractor.ExtractorError) as caught:
            extractor.validate_report(value)
        self.assertEqual(
            "document_extractor_report_invalid", str(caught.exception)
        )
        self.assert_failure(
            caught.exception, "document_extractor_report_invalid"
        )
