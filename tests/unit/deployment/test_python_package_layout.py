"""Guard responsibility-based packages and side-effect-free initializers."""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "search").is_dir() and (parent / "deploy").is_dir()
)
PACKAGE_PATHS = (
    "search",
    "search/http",
    "search/transport",
    "search/catalog",
    "search/quality",
    "search/operations",
    "search/operations/catalog",
    "search/operations/deployment",
    "search/operations/deployment/candidate",
    "search/operations/deployment/candidate/batch",
    "search/operations/deployment/frontend",
    "search/operations/deployment/gateway",
    "search/operations/deployment/release",
    "search/operations/deployment/public_entry",
    "search/operations/metadata",
    "search/operations/documents",
    "search/operations/shared",
    "search/api",
    "search/api/conversation",
    "search/api/retrieval",
    "search/api/shared",
)
MODULE_MOVES = {
    "search/operations/deployment/shared_backend.py": (
        "search/operations/deployment/candidate/controller.py"
    ),
    "search/operations/deployment/shared_backend_probe.py": (
        "search/operations/deployment/candidate/probe.py"
    ),
    "search/operations/deployment/shared_request_policy.py": (
        "search/operations/deployment/candidate/policy.py"
    ),
    "search/operations/deployment/shared_catalog.py": (
        "search/operations/deployment/candidate/catalog.py"
    ),
    "search/operations/deployment/shared_batch.py": (
        "search/operations/deployment/candidate/batch/execution.py"
    ),
    "search/operations/deployment/shared_batch_capture.py": (
        "search/operations/deployment/candidate/batch/capture.py"
    ),
    "search/operations/deployment/shared_batch_probe.py": (
        "search/operations/deployment/candidate/batch/probe.py"
    ),
    "search/operations/deployment/frontend_artifact.py": (
        "search/operations/deployment/frontend/artifact.py"
    ),
    "search/operations/deployment/frontend_check.py": (
        "search/operations/deployment/frontend/check.py"
    ),
    "search/operations/deployment/frontend_delivery.py": (
        "search/operations/deployment/frontend/delivery.py"
    ),
    "search/operations/deployment/frontend_policy.py": (
        "search/operations/deployment/frontend/policy.py"
    ),
    "search/operations/deployment/intranet_access.py": (
        "search/operations/deployment/frontend/access.py"
    ),
    "search/customer_service.py": "search/api/retrieval/results.py",
    "search/customer_service_ollama.py": "search/scope.py",
    "search/operations/deployment/gateway.py": (
        "search/operations/deployment/gateway/preparation.py"
    ),
    "search/operations/deployment/gateway_activation.py": (
        "search/operations/deployment/gateway/activation.py"
    ),
    "search/operations/deployment/gateway_probe_runtime.py": (
        "search/operations/deployment/gateway/probe_runtime.py"
    ),
    "search/operations/deployment/gateway_runtime.py": (
        "search/operations/deployment/gateway/runtime.py"
    ),
    "search/operations/deployment/release.py": (
        "search/operations/deployment/release/controller.py"
    ),
    "search/operations/deployment/release_probe.py": (
        "search/operations/deployment/release/probe.py"
    ),
    "search/operations/deployment/release_runtime.py": (
        "search/operations/deployment/release/runtime.py"
    ),
    "search/operations/deployment/public_entry.py": (
        "search/operations/deployment/public_entry/profile.py"
    ),
    "search/operations/deployment/public_entry_host.py": (
        "search/operations/deployment/public_entry/host.py"
    ),
    "search/operations/deployment/public_entry_preflight.py": (
        "search/operations/deployment/public_entry/preflight.py"
    ),
    "search/operations/deployment/public_entry_updates.py": (
        "search/operations/deployment/public_entry/updates.py"
    ),
    "search/operations/catalog_backend.py": "search/operations/catalog/backend.py",
    "search/operations/catalog_execution.py": "search/operations/catalog/execution.py",
    "search/operations/catalog_runtime.py": "search/operations/catalog/runtime.py",
    "search/operations/catalog_administration.py": (
        "search/operations/catalog/administration.py"
    ),
    "search/operations/catalog_runtime_policy.py": "search/operations/catalog/policy.py",
    "search/operations/catalog_results.py": "search/operations/catalog/results.py",
    "search/operations/metadata_source.py": "search/operations/metadata/source.py",
    "search/operations/catalog_sync.py": "search/operations/metadata/sync.py",
    "search/operations/private_io.py": "search/operations/shared/private_io.py",
    "search/operations/source_identity.py": "search/operations/shared/source_identity.py",
    "search/operations/runtime_probe.py": "search/operations/shared/runtime_probe.py",
    "search/api/conversation_app.py": "search/api/conversation/app.py",
    "search/api/conversation_engine.py": "search/api/conversation/engine.py",
    "search/api/conversation_worker.py": "search/api/conversation/worker.py",
    "search/api/conversation_contract.py": "search/api/conversation/contract.py",
    "search/api/conversation_time.py": "search/api/conversation/time.py",
    "search/api/conversation_calendar.py": "search/api/conversation/calendar.py",
    "search/api/planned_engine.py": "search/api/retrieval/engine.py",
    "search/api/planned_schemas.py": "search/api/retrieval/schemas.py",
    "search/api/planned_worker.py": "search/api/retrieval/worker.py",
    "search/api/config.py": "search/api/shared/config.py",
    "search/api/middleware.py": "search/api/shared/middleware.py",
    "search/api/backend.py": "search/api/shared/backend.py",
    "search/api/worker_io.py": "search/api/shared/worker_io.py",
}

RETIRED_PATHS = (
    "search/operations/deployment/gateway_probe.py",
    "search/operations/deployment/gateway/probe.py",
    "search/customer_service_fess.py",
    "search/api/retrieval/fess.py",
    "search/api/app.py",
    "search/api/engine.py",
    "search/api/schemas.py",
    "search/api/selector.py",
    "search/api/planned",
    "search/api/planned_app.py",
    "research/deployment",
)


class PythonPackageLayoutTests(unittest.TestCase):
    def test_regular_packages_have_regular_source_initializers(self) -> None:
        for relative in PACKAGE_PATHS:
            with self.subTest(package=relative):
                package = ROOT / relative
                initializer = package / "__init__.py"
                self.assertTrue(package.is_dir(), relative)
                self.assertFalse(package.is_symlink(), relative)
                self.assertTrue(initializer.is_file(), str(initializer))
                self.assertFalse(initializer.is_symlink(), str(initializer))

    def test_moved_modules_exist_without_flat_duplicate_implementations(self) -> None:
        for old, new in MODULE_MOVES.items():
            with self.subTest(old=old, new=new):
                source = ROOT / new
                self.assertTrue(source.is_file(), new)
                self.assertFalse(source.is_symlink(), new)
                old_source = ROOT / old
                self.assertFalse(old_source.exists() or old_source.is_symlink(), old)

    def test_retired_entries_have_no_forwarding_files_or_packages(self):
        for relative in RETIRED_PATHS:
            with self.subTest(path=relative):
                path = ROOT / relative
                self.assertFalse(path.exists() or path.is_symlink(), relative)
        self.assertTrue((ROOT / "search/api/shared/schemas.py").is_file())

    def test_public_entry_default_paths_still_reject_repository_source(self):
        from search.operations.deployment.public_entry import profile

        for path in (ROOT, ROOT / "scripts", ROOT / "deploy"):
            with (
                self.subTest(path=str(path)),
                self.assertRaisesRegex(
                    profile.PublicEntryError, "public_entry_path_in_repository"
                ),
            ):
                profile._path(str(path), profile.ROOT)

    def test_package_initializers_contain_only_documentation(self) -> None:
        for relative in PACKAGE_PATHS:
            with self.subTest(package=relative):
                initializer = ROOT / relative / "__init__.py"
                tree = ast.parse(initializer.read_text(), filename=str(initializer))
                self.assertIsNotNone(ast.get_docstring(tree), str(initializer))
                self.assertEqual(1, len(tree.body), str(initializer))

    def test_conversation_version_import_needs_only_standard_library(self) -> None:
        transport_modules = {"search", "search.transport", "search.transport.version"}
        for module, allowed in (
            ("search.transport.version", transport_modules),
            (
                "search.api.shared.config",
                transport_modules
                | {"search.api", "search.api.shared", "search.api.shared.config"},
            ),
        ):
            script = (
                "import sys\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                f"from {module} import CONVERSATION_CONTRACT_VERSION\n"
                "assert type(CONVERSATION_CONTRACT_VERSION) is int\n"
                "assert CONVERSATION_CONTRACT_VERSION == 4\n"
                f"allowed = {allowed!r}\n"
                "loaded = {name for name in sys.modules "
                "if name == 'search' or name.startswith('search.')}\n"
                "assert loaded == allowed, sorted(loaded)\n"
            )
            completed = subprocess.run(
                [sys.executable, "-I", "-S", "-B", "-c", script],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            with self.subTest(module=module):
                self.assertEqual(0, completed.returncode, completed.stderr)

    def test_importing_packages_does_not_load_implementations_or_heavy_dependencies(
        self,
    ) -> None:
        # -I ignores caller PYTHONPATH/site settings; -S needs no installed app
        # dependencies; -B avoids creating source artifacts in the checkout.
        packages = tuple(path.replace("/", ".") for path in PACKAGE_PATHS)
        script = (
            "import importlib, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            f"packages = {packages!r}\n"
            "for name in packages:\n"
            "    importlib.import_module(name)\n"
            "importlib.import_module('search.serialization')\n"
            "importlib.import_module('search.secrets')\n"
            "source_modules = {name for name in sys.modules "
            "if name == 'search' "
            "or name.startswith('search.')}\n"
            "allowed = set(packages) | {"
            "'search.serialization', 'search.secrets'}\n"
            "assert set(packages) <= source_modules <= allowed, "
            "sorted(source_modules)\n"
            "assert 'search.operations.config' not in sys.modules\n"
            "assert 'search.transport.http' not in sys.modules\n"
            "assert 'search.operations.shared.private_io' not in sys.modules\n"
            "heavy = {'fastapi', 'django', 'rest_framework', 'adrf', 'requests', 'torch', 'numpy', 'transformers', "
            "'huggingface_hub', 'haystack', 'ollama', 'pydantic', 'httpx'}\n"
            "assert not any(name.partition('.')[0] in heavy "
            "for name in sys.modules), 'heavy dependency imported'\n"
            "print('package imports are lightweight')\n"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", script],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("package imports are lightweight\n", completed.stdout)
        self.assertEqual("", completed.stderr)


if __name__ == "__main__":
    unittest.main()
