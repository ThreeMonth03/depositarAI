"""Keep candidate orchestration and its shared execution layer acyclic."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = next(
    path
    for path in Path(__file__).resolve().parents
    if (path / "search").is_dir() and (path / "deploy").is_dir()
)
OPERATIONS = ROOT / "search/operations/catalog"
PACKAGE = "search.operations.catalog"


def source_tree(name):
    return ast.parse((OPERATIONS / (name + ".py")).read_text(encoding="utf-8"))


def imported_module(node):
    module = node.module or ""
    return resolve_name("." * node.level + module, PACKAGE) if node.level else module


def dependencies(name):
    """Include deferred imports and member access through explicit aliases."""
    tree = source_tree(name)
    found = set()
    bindings = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
            for alias in node.names:
                bindings[alias.asname or alias.name.partition(".")[0]] = (
                    alias.name if alias.asname else alias.name.partition(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            module = imported_module(node)
            found.add(module)
            found.update(module + "." + alias.name for alias in node.names)
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

    found.update(
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and (name := qualified(node))
    )
    return found


def imports_namespace(found, namespace):
    return any(name == namespace or name.startswith(namespace + ".") for name in found)


class CatalogModuleBoundaryTests(unittest.TestCase):
    def test_vector_cache_helpers_remain_below_query_and_vector_facades(self):
        for consumer in ("chunks", "connection", "reuse"):
            found = dependencies(consumer)
            for namespace in (
                PACKAGE + ".query",
                PACKAGE + ".vectors",
                "search.operations.lifecycle",
                "search.operations.deployment",
                "search.api",
                "torch",
                "transformers",
            ):
                with self.subTest(consumer=consumer, forbidden=namespace):
                    self.assertFalse(imports_namespace(found, namespace))

    def test_import_parser_keeps_absolute_relative_and_aliased_boundaries(self):
        for source, expected in (
            ("from . import runtime", PACKAGE + ".runtime"),
            ("from .runtime import start", PACKAGE + ".runtime"),
            ("from .. import lifecycle", "search.operations.lifecycle"),
            (
                "import search.operations.catalog.runtime as entry",
                PACKAGE + ".runtime",
            ),
        ):
            with (
                self.subTest(source=source),
                patch(__name__ + ".source_tree", return_value=ast.parse(source)),
            ):
                self.assertTrue(imports_namespace(dependencies("ignored"), expected))

    def test_namespace_matching_does_not_confuse_similarly_named_modules(self):
        self.assertFalse(
            imports_namespace({PACKAGE + ".runtime_utils"}, PACKAGE + ".runtime")
        )
        self.assertTrue(
            imports_namespace({PACKAGE + ".runtime.nested"}, PACKAGE + ".runtime")
        )

    def test_command_parser_resolves_legacy_and_canonical_aliases(self):
        for source, expected in (
            (
                "from . import backend as old\nold._command(runner, argv)",
                PACKAGE + ".backend._command",
            ),
            (
                "from .backend import _runner as factory",
                PACKAGE + ".backend._runner",
            ),
            (
                "def later():\n    import search.operations.catalog.backend as old\n"
                "    return old._command",
                PACKAGE + ".backend._command",
            ),
            (
                "from . import commands as command_io\n"
                "command_io.command_output(runner, argv)",
                PACKAGE + ".commands.command_output",
            ),
        ):
            with (
                self.subTest(source=source),
                patch(__name__ + ".source_tree", return_value=ast.parse(source)),
            ):
                self.assertIn(expected, dependencies("ignored"))

    def test_commands_do_not_import_orchestration_or_models(self):
        found = dependencies("commands")
        forbidden = tuple(
            PACKAGE + "." + name
            for name in (
                "backend",
                "execution",
                "runtime",
                "administration",
                "vectors",
                "volumes",
            )
        ) + (
            "search.operations.lifecycle",
            "search.operations.deployment",
            "search.api",
            "search.planning.model",
            "search.scope",
            "search.reranker",
            "search.evaluation",
            "torch",
            "transformers",
            "haystack",
        )
        for namespace in forbidden:
            with self.subTest(namespace=namespace):
                self.assertFalse(imports_namespace(found, namespace))

    def test_external_command_consumers_use_canonical_helpers(self):
        # Data-bound administration/vectors deliberately retain backend aliases.
        for name in ("execution", "runtime", "volumes"):
            with self.subTest(consumer=name):
                found = dependencies(name)
                for helper in ("_runner", "_command"):
                    self.assertNotIn(PACKAGE + ".backend." + helper, found)
                self.assertIn(PACKAGE + ".commands.command_output", found)
                if name == "execution":
                    self.assertIn(PACKAGE + ".commands.default_runner", found)

    def test_shared_execution_does_not_import_its_orchestrators(self):
        found = dependencies("execution")
        for namespace in (
            PACKAGE + ".runtime",
            PACKAGE + ".administration",
            "search.operations.lifecycle",
        ):
            with self.subTest(namespace=namespace):
                self.assertFalse(imports_namespace(found, namespace))

    def test_catalog_storage_does_not_load_metadata_sync_orchestration(self):
        for name in ("backend", "execution"):
            with self.subTest(module=name):
                found = dependencies(name)
                self.assertFalse(
                    imports_namespace(found, "search.operations.metadata.sync")
                )
                self.assertIn("search.operations.metadata.storage", found)

    def test_administration_never_borrows_runtime_private_functions(self):
        found = dependencies("administration")
        self.assertFalse(imports_namespace(found, PACKAGE + ".runtime"))
        self.assertIn(PACKAGE + ".execution", found)

    def test_runtime_and_administration_share_one_execution_layer(self):
        for name in ("runtime", "administration"):
            with self.subTest(module=name):
                self.assertIn(PACKAGE + ".execution", dependencies(name))

    def test_pure_result_and_resource_policy_do_not_import_execution(self):
        forbidden = {
            PACKAGE + ".execution",
            PACKAGE + ".runtime",
            PACKAGE + ".administration",
            "search.operations.lifecycle",
            "subprocess",
        }
        for name in ("results", "policy"):
            with self.subTest(module=name):
                found = dependencies(name)
                self.assertFalse(
                    any(imports_namespace(found, namespace) for namespace in forbidden)
                )


if __name__ == "__main__":
    unittest.main()
