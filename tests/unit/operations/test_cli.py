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
            "        if any(fullname == name or fullname.startswith(name + '.') "
            "for name in self.names):\n"
            "            raise AssertionError('unexpected dependency: ' + fullname)\n"
            + textwrap.dedent(body)
        )
        with tempfile.TemporaryDirectory(prefix="depositar-cli-startup-") as cwd:
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
        ):
            with self.subTest(arguments=arguments):
                report = self.run_isolated(f"""
                    sys.argv = [launcher, *{arguments!r}]
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                        try:
                            runpy.run_path(launcher, run_name='__main__')
                        except SystemExit as error:
                            code = error.code
                    loaded = sorted(name for name in sys.modules
                                    if name == 'search' or name.startswith('search.'))
                    print(json.dumps({{'code': code, 'out': output.getvalue(),
                                      'err': errors.getvalue(), 'loaded': loaded}}))
                """)
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
        report = self.run_isolated("""
            namespace = runpy.run_path(launcher, run_name='launcher_probe')
            exposed = sorted(name for name in ('main', 'parser', 'load_config', 'lifecycle')
                             if name in namespace)
            loaded = sorted(name for name in sys.modules
                            if name == 'search' or name.startswith('search.'))
            print(json.dumps({'exposed': exposed, 'loaded': loaded}))
        """)
        self.assertEqual({"exposed": [], "loaded": []}, report)

    def test_doctor_dispatch_avoids_lifecycle_and_deployment_imports(self):
        report = self.run_isolated("""
            sys.meta_path.insert(0, BlockImports(('search.operations.lifecycle',
                'search.operations.deployment', 'search.operations.catalog', 'search.catalog',
                'search.api', 'search.planning')))
            from search.operations import cli, config, doctor
            marker, calls = object(), []
            def load(path, *, repo_root):
                assert path == Path('/unused/product.json') and repo_root == root
                return marker
            def diagnose(value, *, repo_root, deep_verify):
                assert value is marker and repo_root == root and deep_verify is True
                calls.append('doctor')
                return {'status': 'ready'}
            config.load_config, doctor.run_doctor = load, diagnose
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = cli.main(['--config', '/unused/product.json', 'doctor', '--deep'])
            print(json.dumps({'code': code, 'out': json.loads(output.getvalue()),
                              'err': errors.getvalue(), 'calls': calls}))
        """)
        self.assertEqual(
            {"code": 0, "out": {"status": "ready"}, "err": "", "calls": ["doctor"]},
            report,
        )

    def test_prepare_entry_does_not_load_update_or_restore_controllers(self):
        report = self.run_isolated("""
            sys.meta_path.insert(0, BlockImports(('search.operations.lifecycle',
                'search.operations.deployment.public_entry.updates',
                'search.operations.deployment.public_entry.preflight',
                'search.operations.deployment.product_restore',
                'search.operations.catalog', 'search.catalog', 'search.api')))
            from search.operations import cli
            from search.operations.deployment.public_entry import host
            calls = []
            def prepare(config, release, image, frontend):
                assert config == Path('/unused/entry.json')
                assert release == Path('/unused/release.json') and image == 'image-fixture'
                assert frontend == 'frontend-image-fixture'
                calls.append('prepare')
                return {'status': 'prepared'}
            host.prepare_public_entry = prepare
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = cli.main(['prepare-public-entry', '--entry-config', '/unused/entry.json',
                    '--release-compose', '/unused/release.json', '--web-image', 'image-fixture',
                    '--web-nginx-image', 'frontend-image-fixture'])
            print(json.dumps({'code': code, 'out': json.loads(output.getvalue()),
                              'err': errors.getvalue(), 'calls': calls}))
        """)
        self.assertEqual(
            {"code": 0, "out": {"status": "prepared"}, "err": "", "calls": ["prepare"]},
            report,
        )

    def test_up_private_forwards_staging_options_to_one_lifecycle_action(self):
        report = self.run_isolated("""
            sys.meta_path.insert(0, BlockImports(('search.operations.deployment',
                'search.api', 'search.planning')))
            from search.operations import cli, config, lifecycle
            marker, calls = object(), []
            def load(path, *, repo_root):
                assert path == Path('/unused/product.json') and repo_root == root
                calls.append('config')
                return marker
            def up(value, **kwargs):
                assert value is marker
                assert kwargs == {'snapshot': Path('/private/metadata.jsonl.gz'),
                    'force': True, 'max_passes': 3, 'wall_seconds': 4800,
                    'catalog_revision': 'a' * 40}
                calls.append('up-private')
                return {'status': 'gateway_active', 'active_changed': True}
            def forbidden(*args, **kwargs):
                raise AssertionError('CLI must not implement the deployment steps')
            config.load_config, lifecycle.up_private = load, up
            lifecycle.stage_catalog = forbidden
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = cli.main(['--config', '/unused/product.json', 'up-private',
                    '--snapshot', '/private/metadata.jsonl.gz', '--force',
                    '--max-passes', '3', '--wall-seconds', '4800',
                    '--catalog-revision', 'a' * 40])
            print(json.dumps({'code': code, 'out': json.loads(output.getvalue()),
                              'err': errors.getvalue(), 'calls': calls}))
        """)
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
        report = self.run_isolated("""
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
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = cli.main(['--config', '/unused/product.json', 'up-private'])
            print(json.dumps({'code': code, 'out': json.loads(output.getvalue()),
                              'err': errors.getvalue(), 'calls': calls}))
        """)
        self.assertEqual(2, report["code"])
        self.assertEqual("checkpoint", report["out"]["status"])
        self.assertFalse(report["out"]["active_changed"])
        self.assertTrue(report["out"]["staging_only"])
        self.assertEqual("", report["err"])
        self.assertEqual(["up-private"], report["calls"])

    def test_up_private_fixed_diagnostics_are_safe_and_command_scoped(self):
        for kind in ("allowed", "private", "subclass", "other_command"):
            with self.subTest(kind=kind):
                report = self.run_isolated(f"""
                    from search.operations import cli, config, doctor, lifecycle
                    class Derived(lifecycle.UpPrivateError): pass
                    Derived.__module__ = lifecycle.UpPrivateError.__module__
                    exception = (Derived if {kind!r} == 'subclass'
                                 else lifecycle.UpPrivateError)
                    detail = ('private-secret-fixture' if {kind!r} == 'private'
                              else 'product_private_existing_activation')
                    config.load_config = lambda *args, **kwargs: object()
                    def fail(*args, **kwargs): raise exception(detail)
                    lifecycle.up_private = doctor.run_doctor = fail
                    command = 'doctor' if {kind!r} == 'other_command' else 'up-private'
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                        code = cli.main(['--config', '/unused/product.json', command])
                    print(json.dumps({{'code': code, 'out': output.getvalue(),
                                      'err': json.loads(errors.getvalue())}}))
                """)
                self.assertEqual(1, report["code"])
                self.assertEqual("", report["out"])
                self.assertEqual("failed", report["err"]["status"])
                self.assertEqual("product_operation_failed", report["err"]["code"])
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
                report = self.run_isolated(f"""
                    from search.operations import cli, config, doctor
                    from search.catalog.errors import CatalogError
                    reads = []
                    class Unhandled(RuntimeError):
                        @property
                        def __class__(self):
                            reads.append('class')
                            if {metadata!r} == 'raising':
                                raise AssertionError('private-class-fixture')
                            return {{'ordinary': Unhandled, 'standard': ValueError,
                                     'catalog': CatalogError}}[{metadata!r}]
                    config.load_config = lambda *args, **kwargs: object()
                    def fail(*args, **kwargs): raise Unhandled('private-error-fixture')
                    doctor.run_doctor = fail
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                        try:
                            cli.main(['--config', '/unused/product.json', 'doctor'])
                        except Unhandled:
                            raised = True
                        else:
                            raised = False
                    print(json.dumps({{'raised': raised, 'out': output.getvalue(),
                                      'err': errors.getvalue(), 'metadata_reads': reads}}))
                """)
                self.assertEqual(
                    {"raised": True, "out": "", "err": "", "metadata_reads": []},
                    report,
                )

    def test_foreign_error_metadata_cannot_break_or_forge_safe_diagnostics(self):
        for metadata in ("hostile", "list", "none", "forged"):
            with self.subTest(metadata=metadata):
                report = self.run_isolated(f"""
                    from search.operations import cli, config, doctor
                    class Metadata(str):
                        def __eq__(self, other):
                            raise AssertionError('private-metadata-fixture')
                    class Foreign(ValueError): pass
                    Foreign.__module__ = {{
                        'hostile': Metadata('search.operations.catalog.policy'),
                        'list': ['search.operations.catalog.policy'],
                        'none': None,
                        'forged': 'search.operations.catalog.policy',
                    }}[{metadata!r}]
                    config.load_config = lambda *args, **kwargs: object()
                    def fail(*args, **kwargs):
                        raise Foreign('catalog_admin_authentication_failed')
                    doctor.run_doctor = fail
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                        code = cli.main(['--config', '/unused/product.json', 'doctor'])
                    print(json.dumps({{'code': code, 'out': output.getvalue(),
                                      'err': json.loads(errors.getvalue())}}))
                """)
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
