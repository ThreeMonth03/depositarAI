"""Keep request handlers and dependency-light primitives outside operator policy."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = next(
    parent
    for parent in Path(__file__).resolve().parents
    if (parent / "search").is_dir() and (parent / "deploy").is_dir()
)
NEUTRAL_SOURCES = (
    "search/transport/__init__.py",
    "search/transport/http.py",
    "search/serialization.py",
    "search/secrets.py",
    "search/model_policy.py",
    "search/api/shared/admission.py",
)
NEUTRAL_MODULES = {
    "search.transport",
    "search.transport.http",
    "search.serialization",
    "search.secrets",
    "search.model_policy",
    "search.api.shared.admission",
}
NEUTRAL_PARENTS = {"search", "search.api", "search.api.shared"}
# Primitive dependencies are intentionally reviewed, including on the host's
# Python 3.9, which predates sys.stdlib_module_names.
PRIMITIVE_STDLIB = {
    "__future__",
    "contextlib",
    "dataclasses",
    "json",
    "math",
    "os",
    "pathlib",
    "re",
    "stat",
    "time",
    "typing",
    "urllib",
}
OPERATOR_NAMESPACES = (
    "search.catalog.build",
    "search.operations.catalog",
    "search.operations.deployment",
    "search.operations.documents",
    "search.operations.jobs",
    "search.operations.metadata",
    "search.operations.lifecycle",
)


def module_context(relative):
    parts = Path(relative).with_suffix("").parts
    if parts[-1] == "__init__":
        module = ".".join(parts[:-1])
        return module, module
    module = ".".join(parts)
    return module, module.rpartition(".")[0]


def dependencies(tree, package):
    """Include deferred imports and members of relative ``from`` imports."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            found.add(module)
            found.update(module + "." + alias.name for alias in node.names)
    return found


def source_dependencies(path):
    _, package = module_context(path.relative_to(ROOT))
    return dependencies(ast.parse(path.read_text(encoding="utf-8")), package)


def in_namespace(name, namespace):
    return name == namespace or name.startswith(namespace + ".")


def forbidden_dependencies(found, namespaces):
    return sorted(
        name
        for name in found
        if any(in_namespace(name, namespace) for namespace in namespaces)
    )


def imported_attributes(tree, package):
    """Resolve member access through explicit import aliases."""
    bindings = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name.partition(".")[0]] = (
                    alias.name if alias.asname else alias.name.partition(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            for alias in node.names:
                bindings[alias.asname or alias.name] = module + "." + alias.name

    def qualified(node):
        if isinstance(node, ast.Name):
            return bindings.get(node.id)
        if isinstance(node, ast.Attribute):
            parent = qualified(node.value)
            if parent:
                return parent + "." + node.attr
        return None

    return {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and (name := qualified(node))
    }


def neutral_dependency(name):
    if name.partition(".")[0] in PRIMITIVE_STDLIB:
        return True
    if name in NEUTRAL_PARENTS:
        return True
    return any(in_namespace(name, module) for module in NEUTRAL_MODULES)


class ProductModuleBoundaryTests(unittest.TestCase):
    def test_import_parser_resolves_package_and_module_contexts(self):
        for relative, expected in (
            ("search/transport/__init__.py", ("search.transport", "search.transport")),
            ("search/transport/http.py", ("search.transport.http", "search.transport")),
            (
                "search/api/shared/readiness.py",
                ("search.api.shared.readiness", "search.api.shared"),
            ),
        ):
            with self.subTest(path=relative):
                self.assertEqual(expected, module_context(relative))

    def test_import_parser_catches_absolute_relative_aliased_and_deferred_edges(self):
        for source, package, forbidden in (
            (
                "import search.operations.catalog.runtime as runtime",
                "search.web",
                "search.operations.catalog",
            ),
            (
                "from search.operations import deployment as deploy",
                "search.web",
                "search.operations.deployment",
            ),
            (
                "from ...operations.metadata import source",
                "search.api.shared",
                "search.operations.metadata",
            ),
            (
                "def later():\n    from ..operations import lifecycle",
                "search.web",
                "search.operations.lifecycle",
            ),
            (
                "from ..engine import Slots as Admission",
                "search.api.shared",
                "search.api.engine",
            ),
            (
                "from search.api import engine",
                "search.web",
                "search.api.engine",
            ),
        ):
            with self.subTest(source=source):
                found = dependencies(ast.parse(source), package)
                self.assertTrue(forbidden_dependencies(found, (forbidden,)))

    def test_namespace_policy_preserves_shared_probe_and_similar_names(self):
        found = dependencies(
            ast.parse(
                "from search.operations.shared import runtime_probe\n"
                "import search.operations.catalog_utils\n"
                "import search.operations.documents_utils\n"
                "import search.api.engineering"
            ),
            "search.web",
        )
        self.assertEqual([], forbidden_dependencies(found, OPERATOR_NAMESPACES))
        self.assertEqual([], forbidden_dependencies(found, ("search.api.engine",)))

    def test_request_handlers_cannot_import_operator_jobs_directly_or_lazily(self):
        for source in (
            "import search.operations.documents.batches",
            "from search.operations import documents",
            "from ...operations.documents import batches",
            "def later():\n    from ...operations.documents import controller",
            "from ...operations.jobs import queue",
            "def later():\n    from ...operations.jobs import execution",
        ):
            with self.subTest(source=source):
                found = dependencies(ast.parse(source), "search.api.shared")
                self.assertTrue(forbidden_dependencies(found, OPERATOR_NAMESPACES))

    def test_neutral_policy_rejects_application_operator_research_and_model_edges(self):
        for namespace in (
            "search.api.app",
            "search.api.engine",
            "search.api.shared.config",
            "search.scope",
            "search.operations.shared.runtime_probe",
            "search.operations.catalog",
            "search.operations.documents",
            "search.evaluation.query_design_worker",
            "search.planning.model",
            "research",
            "torch",
            "transformers",
            "haystack",
            "fastapi",
            "django",
            "rest_framework",
            "adrf",
        ):
            with self.subTest(namespace=namespace):
                self.assertFalse(neutral_dependency(namespace))

    def test_neutral_sources_depend_only_on_stdlib_or_neutral_primitives(self):
        for relative in NEUTRAL_SOURCES:
            with self.subTest(source=relative):
                unexpected = sorted(
                    name
                    for name in source_dependencies(ROOT / relative)
                    if not neutral_dependency(name)
                )
                self.assertEqual([], unexpected, relative)

    def test_neutral_imports_need_no_site_packages_or_application_modules(self):
        # A fresh interpreter for each primitive catches transitive imports and
        # package initializer side effects, without installed model/framework IO.
        for module in sorted(NEUTRAL_MODULES):
            with self.subTest(module=module):
                script = (
                    "import importlib, json, sys, sysconfig\n"
                    "from pathlib import Path\n"
                    f"sys.path.insert(0, {str(ROOT)!r})\n"
                    f"importlib.import_module({module!r})\n"
                    "stdlib = {Path(sysconfig.get_path(key)).resolve() "
                    "for key in ('stdlib', 'platstdlib')}\n"
                    "loaded = sorted(name for name in sys.modules "
                    "if name == 'search' or name.startswith('search.'))\n"
                    "foreign = []\n"
                    "for name, item in sys.modules.items():\n"
                    "    if name == '__main__' or name.partition('.')[0] == 'search':\n"
                    "        continue\n"
                    "    origin = getattr(getattr(item, '__spec__', None), 'origin', None)\n"
                    # Python 3.9 typing.io/typing.re are virtual children of a
                    # stdlib module, without their own module specifications.
                    "    if origin is None and '.' in name:\n"
                    "        parent = sys.modules.get(name.partition('.')[0])\n"
                    "        origin = getattr(getattr(parent, '__spec__', None), 'origin', None)\n"
                    "    if origin in ('built-in', 'frozen'):\n"
                    "        continue\n"
                    "    path = Path(origin).resolve() if origin else None\n"
                    "    if path is None or "
                    "{'site-packages', 'dist-packages'}.intersection(path.parts) "
                    "or not any(path.is_relative_to(root) for root in stdlib):\n"
                    "        foreign.append(name)\n"
                    "print(json.dumps({'loaded': loaded, 'foreign': foreign}))\n"
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
                self.assertEqual("", completed.stderr)
                result = json.loads(completed.stdout)
                self.assertIn(module, result["loaded"])
                self.assertEqual([], result["foreign"])
                self.assertLessEqual(
                    set(result["loaded"]), NEUTRAL_MODULES | NEUTRAL_PARENTS
                )

    def test_request_modules_do_not_import_operator_control_planes(self):
        # Shared runtime_probe remains an allowed operator/request integration;
        # catalog, deployment, metadata and lifecycle authority stay separate.
        for directory in ("search/http", "search/api", "search/web", "search/reranker"):
            paths = sorted((ROOT / directory).rglob("*.py"))
            self.assertTrue(paths, directory)
            for path in paths:
                with self.subTest(source=str(path.relative_to(ROOT))):
                    self.assertEqual(
                        [],
                        forbidden_dependencies(
                            source_dependencies(path), OPERATOR_NAMESPACES
                        ),
                    )

    def test_shared_http_does_not_import_application_domains(self):
        paths = sorted((ROOT / "search/http").rglob("*.py"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(source=str(path.relative_to(ROOT))):
                self.assertEqual(
                    [],
                    forbidden_dependencies(
                        source_dependencies(path),
                        (
                            "search.api",
                            "search.web",
                            "search.planning",
                            "search.catalog",
                            "search.operations",
                            "search.reranker",
                        ),
                    ),
                )

    def test_runtime_callers_do_not_import_retired_api_modules(self):
        retired = (
            "fastapi",
            "starlette",
            "search.api.app",
            "search.api.engine",
            "search.api.schemas",
            "search.api.selector",
            "search.api.planned",
            "research.deployment",
        )
        paths = sorted((ROOT / "search").rglob("*.py"))
        paths += sorted((ROOT / "scripts").rglob("*.py"))
        for path in paths:
            if path.is_relative_to(ROOT / "search/tests"):
                continue
            with self.subTest(source=str(path.relative_to(ROOT))):
                self.assertEqual(
                    [], forbidden_dependencies(source_dependencies(path), retired)
                )

    def test_bff_and_readiness_do_not_import_execution_engines(self):
        paths = sorted((ROOT / "search/web").rglob("*.py"))
        self.assertTrue(paths)
        paths.append(ROOT / "search/api/shared/readiness.py")
        for path in paths:
            with self.subTest(source=str(path.relative_to(ROOT))):
                self.assertEqual(
                    [],
                    forbidden_dependencies(
                        source_dependencies(path),
                        (
                            "search.api.conversation.engine",
                            "search.api.retrieval.engine",
                            "search.api.engine",
                        ),
                    ),
                )


class HostToolBoundaryTests(unittest.TestCase):
    def test_quality_domain_is_independent_of_cli_operators_and_model_execution(self):
        paths = sorted((ROOT / "search/quality").glob("*.py"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(source=str(path.relative_to(ROOT))):
                self.assertEqual(
                    [],
                    forbidden_dependencies(
                        source_dependencies(path),
                        (
                            "scripts",
                            "search.operations",
                            "search.evaluation",
                            "subprocess",
                            "torch",
                            "transformers",
                            "haystack",
                        ),
                    ),
                )

    def test_batch_quality_consumers_do_not_depend_on_script_entry_points(self):
        for relative in (
            "search/operations/deployment/candidate/batch/execution.py",
            "search/operations/deployment/candidate/batch/capture.py",
        ):
            with self.subTest(source=relative):
                self.assertEqual(
                    [],
                    forbidden_dependencies(
                        source_dependencies(ROOT / relative), ("scripts",)
                    ),
                )

    def test_host_json_consumers_do_not_import_api_configuration(self):
        for relative in (
            "scripts/depositarai.py",
            "scripts/prepare_search_models.py",
            "search/operations/cli.py",
        ):
            with self.subTest(tool=relative):
                self.assertEqual(
                    [],
                    forbidden_dependencies(
                        source_dependencies(ROOT / relative),
                        ("search.api",),
                    ),
                )

    def test_model_and_metadata_tools_do_not_load_api_modules(self):
        for module in ("scripts.prepare_search_models",):
            with self.subTest(module=module):
                script = (
                    "import importlib, sys\n"
                    f"sys.path.insert(0, {str(ROOT)!r})\n"
                    f"importlib.import_module({module!r})\n"
                    "loaded = sorted(name for name in sys.modules "
                    "if name == 'search.api' or name.startswith('search.api.'))\n"
                    "assert not loaded, loaded\n"
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
                self.assertEqual("", completed.stdout)
                self.assertEqual("", completed.stderr)


class RuntimeDockerBoundaryTests(unittest.TestCase):
    def test_runtime_client_does_not_import_reverse_orchestration_or_models(self):
        forbidden = (
            "search.operations.deployment.inference",
            "search.operations.deployment.release",
            "search.operations.deployment.gateway",
            "search.operations.deployment.gateway.activation",
            "search.operations.deployment.gpu_ownership",
            "search.operations.lifecycle",
            "search.operations.doctor",
            "scripts.prepare_search_models",
            "scripts.search_model_source",
            "search.api",
            "search.planning.model",
            "search.scope",
            "search.reranker",
            "search.evaluation",
            "torch",
            "transformers",
            "haystack",
        )
        for source in (
            "import torch",
            "from transformers import AutoModel",
            "def later():\n    import haystack",
        ):
            with self.subTest(source=source):
                self.assertTrue(
                    forbidden_dependencies(
                        dependencies(ast.parse(source), "search.operations.deployment"),
                        forbidden,
                    )
                )
        self.assertEqual(
            [],
            forbidden_dependencies(
                source_dependencies(
                    ROOT / "search/operations/deployment/runtime_docker.py"
                ),
                forbidden,
            ),
        )

    def test_client_member_parser_catches_legacy_aliases_without_banning_builds(self):
        package = "search.operations.deployment"
        forbidden = (package + ".inference._Docker",)
        for source in (
            "import search.operations.deployment.inference\n"
            "client = search.operations.deployment.inference._Docker",
            "from . import inference as legacy\nclient = legacy._Docker",
            "from .inference import _Docker as Client",
            "def later():\n    from . import inference as legacy\n"
            "    return legacy._Docker",
        ):
            with self.subTest(source=source):
                tree = ast.parse(source)
                found = dependencies(tree, package) | imported_attributes(tree, package)
                self.assertTrue(forbidden_dependencies(found, forbidden))
        tree = ast.parse("from .images import build\nclient = build._Docker")
        found = dependencies(tree, package) | imported_attributes(tree, package)
        self.assertEqual([], forbidden_dependencies(found, forbidden))

    def test_external_runtime_controllers_use_canonical_client(self):
        for name in (
            "inference",
            "release/controller",
            "gateway/preparation",
            "gateway/activation",
        ):
            relative = Path(f"search/operations/deployment/{name}.py")
            _, package = module_context(relative)
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            imports = dependencies(tree, package)
            references = imports | imported_attributes(tree, package)
            with self.subTest(controller=name):
                self.assertEqual(
                    [],
                    forbidden_dependencies(
                        references,
                        ("search.operations.deployment.inference._Docker",),
                    ),
                )
                self.assertIn("search.operations.deployment.runtime_docker", imports)


class ImageAuthorityBoundaryTests(unittest.TestCase):
    def test_completed_image_reader_does_not_import_controllers_or_models(self):
        package = "search.operations.deployment"
        forbidden = tuple(
            package + "." + name
            for name in (
                "inference",
                "release",
                "gateway",
                "gateway.activation",
                "payload",
                "gpu_ownership",
            )
        ) + (
            "search.operations.lifecycle",
            "search.operations.doctor",
            "scripts.prepare_search_models",
            "scripts.search_model_source",
            "search.api",
            "search.planning.model",
            "search.scope",
            "search.reranker",
            "search.evaluation",
            "torch",
            "transformers",
            "haystack",
        )
        self.assertEqual(
            [],
            forbidden_dependencies(
                source_dependencies(
                    ROOT / "search/operations/deployment/images/authority.py"
                ),
                forbidden,
            ),
        )

    def test_image_reader_parser_resolves_legacy_and_canonical_aliases(self):
        package = "search.operations.deployment"
        legacy = package + ".inference._image_authority"
        for source in (
            "from . import inference as old\nreader = old._image_authority",
            "from .inference import _image_authority as reader",
            "def later():\n    import search.operations.deployment.inference as old\n"
            "    return old._image_authority",
        ):
            with self.subTest(source=source):
                tree = ast.parse(source)
                found = dependencies(tree, package) | imported_attributes(tree, package)
                self.assertIn(legacy, found)
        tree = ast.parse(
            "from .images import authority as image_evidence\n"
            "image_evidence.read_completed_images(config, store, plan)"
        )
        self.assertIn(
            package + ".images.authority.read_completed_images",
            imported_attributes(tree, package),
        )

    def test_external_image_consumers_use_canonical_reader(self):
        owner = "search.operations.deployment"
        for name in (
            "inference",
            "release/controller",
            "gateway/preparation",
            "gateway/activation",
            "payload/collect",
        ):
            relative = Path(f"search/operations/deployment/{name}.py")
            _, package = module_context(relative)
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            references = dependencies(tree, package) | imported_attributes(
                tree, package
            )
            with self.subTest(consumer=name):
                self.assertNotIn(owner + ".inference._image_authority", references)
                self.assertIn(
                    owner + ".images.authority.read_completed_images", references
                )


class ImageFixtureConsumerBoundaryTests(unittest.TestCase):
    def test_inference_and_authority_do_not_borrow_staging_test_case(self):
        owner = "tests.unit.deployment.images"
        for name in ("test_inference.py", "images/test_authority.py"):
            path = ROOT / "tests/unit/deployment" / name
            _, package = module_context(path.relative_to(ROOT))
            tree = ast.parse(path.read_text(encoding="utf-8"))
            references = dependencies(tree, package) | imported_attributes(
                tree, package
            )
            with self.subTest(consumer=name):
                self.assertNotIn(
                    owner + ".test_staging.ProductImageStagingTests", references
                )
                self.assertIn(owner + ".fixtures.ImageStagingFixture", references)


if __name__ == "__main__":
    unittest.main()
