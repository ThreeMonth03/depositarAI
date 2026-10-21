"""Keep product image inputs explicit, hash-locked and independent of tests."""

from __future__ import annotations

from fnmatch import fnmatchcase
import json
from pathlib import Path
import re
import shlex
import unittest

from search.operations.deployment import source_bundle


ROOT = Path(__file__).resolve().parents[3]
RUNTIME_LOCKS = {
    "deploy/docker/api/Dockerfile": "search/api/requirements-haystack.lock",
    "deploy/docker/reranker/Dockerfile": "search/reranker/requirements.lock",
    "deploy/docker/web/Dockerfile": "search/web/requirements.lock",
}


def pinned_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def copy_sources(source: str) -> list[str]:
    """Extract the simple local COPY forms used by the product images."""
    inputs = []
    for line in re.sub(r"\\\s*\n", " ", source).splitlines():
        instruction, _, arguments = line.strip().partition(" ")
        if instruction.upper() != "COPY":
            continue
        if re.search(r"(?:^|\s)--from(?:=|\s)", arguments):
            continue
        arguments = re.sub(r"^(?:--[\w-]+=\S+\s+)+", "", arguments)
        words = (
            json.loads(arguments)
            if arguments.startswith("[")
            else shlex.split(arguments)
        )
        inputs.extend(words[:-1])
    return inputs


def allowed_inputs(path: Path) -> list[str]:
    """Read leaf exceptions; directory resets are checked by layout policy.

    This helper checks declared COPY inputs, not full Dockerignore semantics.
    """
    lines = pinned_lines(path)
    if not lines or lines[0] != "**":
        raise ValueError("dependency contexts must start with deny-all")
    return [
        line[1:]
        for line in lines[1:]
        if line.startswith("!") and not line.endswith("/")
    ]


def matches_input(filename: str, pattern: str) -> bool:
    """Docker's * matches one path component, unlike fnmatch's full strings."""
    parts, patterns = filename.split("/"), pattern.split("/")
    return len(parts) == len(patterns) and all(
        fnmatchcase(part, item) for part, item in zip(parts, patterns)
    )


class RuntimeDependencyTests(unittest.TestCase):
    def test_runtime_dependencies_pin_versions_and_wheel_sha256(self) -> None:
        for relative in set(RUNTIME_LOCKS.values()):
            source = re.sub(r"\\\s*\n\s*", " ", (ROOT / relative).read_text())
            entries = [
                line
                for line in source.splitlines()
                if line.strip() and not line.startswith("#")
            ]
            self.assertTrue(entries, relative)
            for line in entries:
                with self.subTest(lock=relative, entry=line):
                    package, *hashes = line.split()
                    self.assertRegex(
                        package, r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s]+$"
                    )
                    self.assertTrue(hashes)
                    for digest in hashes:
                        self.assertRegex(digest, r"^--hash=sha256:[0-9a-f]{64}$")

    def test_product_dockerfiles_do_not_copy_research_paths(self) -> None:
        for dockerfile in sorted((ROOT / "deploy/docker").rglob("*Dockerfile")):
            with self.subTest(dockerfile=dockerfile.relative_to(ROOT).as_posix()):
                for source in copy_sources(dockerfile.read_text(encoding="utf-8")):
                    self.assertFalse(source.startswith("search/evaluation/"))
                    self.assertFalse(source.startswith("research/"))

    def test_product_images_copy_the_formal_lock_and_include_it_in_context(
        self,
    ) -> None:
        for image, lock in RUNTIME_LOCKS.items():
            with self.subTest(image=image):
                dockerfile = ROOT / image
                source = dockerfile.read_text(encoding="utf-8")
                self.assertTrue((ROOT / lock).is_file())
                self.assertEqual(copy_sources(source).count(lock), 1)
                exceptions = allowed_inputs(Path(str(dockerfile) + ".dockerignore"))
                self.assertTrue(any(matches_input(lock, item) for item in exceptions))
                self.assertIn("--require-hashes", source)

    def test_sealed_recipes_include_every_local_copy_input(self) -> None:
        for name, recipe in source_bundle.RECIPES.items():
            dockerfile = ROOT / recipe["dockerfile"]
            exceptions = allowed_inputs(Path(str(dockerfile) + ".dockerignore"))
            for source in copy_sources(dockerfile.read_text(encoding="utf-8")):
                with self.subTest(image=name, source=source):
                    copied = set()
                    for path in ROOT.glob(source):
                        candidates = path.rglob("*") if path.is_dir() else (path,)
                        for candidate in candidates:
                            relative = candidate.relative_to(ROOT).as_posix()
                            if candidate.is_file() and any(
                                matches_input(relative, item) for item in exceptions
                            ):
                                copied.add(relative)
                    self.assertTrue(copied, "COPY source has no allowed files")
                    self.assertFalse(
                        copied - recipe["inputs"],
                        "sealed context omits Docker COPY inputs: "
                        + repr(sorted(copied - recipe["inputs"])),
                    )

    def test_conversation_image_has_the_product_entrypoint_and_limits(self) -> None:
        dockerfile = ROOT / "deploy/docker/api/Dockerfile"
        source = dockerfile.read_text(encoding="utf-8")
        commands = re.findall(r"(?m)^CMD (.+)$", source)
        self.assertEqual(len(commands), 1)
        command = json.loads(commands[0])
        self.assertEqual(
            command[:4],
            ["python", "-m", "uvicorn", "search.api.conversation.app:create_app"],
        )
        self.assertIn("--factory", command)
        self.assertEqual(command[command.index("--workers") + 1], "1")
        self.assertEqual(command[command.index("--limit-concurrency") + 1], "16")
        self.assertNotIn("search.api.planned.app", source)
        self.assertIn("USER 1000:1000", source)
        self.assertIn("org.opencontainers.image.revision=$SOURCE_REVISION", source)

    def test_conversation_context_excludes_tests_research_and_private_files(
        self,
    ) -> None:
        ignore = ROOT / "deploy/docker/api/Dockerfile.dockerignore"
        exceptions = allowed_inputs(ignore)
        for excluded in (
            "search/evaluation/calendar_intent.py",
            "search/tests/test_conversation_engine.py",
            "tests/unit/deployment/test_runtime_dependencies.py",
            "search/secrets/api_key",
            "search/models/model.bin",
            "search/data/datasets.jsonl",
            ".env.search-api",
        ):
            with self.subTest(excluded=excluded):
                self.assertFalse(
                    any(matches_input(excluded, item) for item in exceptions)
                )

    def test_runtime_recipe_contains_current_retrieval_and_shared_contract(self):
        recipe = source_bundle.RECIPES["conversation-api"]
        for source in (
            "search/api/retrieval/__init__.py",
            "search/api/retrieval/engine.py",
            "search/api/retrieval/schemas.py",
            "search/api/retrieval/worker.py",
            "search/api/shared/schemas.py",
        ):
            with self.subTest(source=source):
                self.assertIn(source, recipe["inputs"])
        for source in recipe["inputs"]:
            with self.subTest(source=source):
                self.assertFalse(source.startswith("search/api/planned/"))
                self.assertNotIn(
                    source,
                    {
                        "search/api/app.py",
                        "search/api/engine.py",
                        "search/api/schemas.py",
                        "search/api/selector.py",
                    },
                )

    def test_web_contract_shares_metadata_without_retrieval_implementation(self):
        metadata = "search/api/retrieval/metadata.py"
        results = "search/api/retrieval/results.py"
        for service in ("conversation-api", "web"):
            with self.subTest(service=service):
                self.assertIn(metadata, source_bundle.RECIPES[service]["inputs"])
        self.assertIn(results, source_bundle.RECIPES["conversation-api"]["inputs"])
        self.assertNotIn(results, source_bundle.RECIPES["web"]["inputs"])

    def test_api_test_target_cannot_supply_test_files_to_production(self) -> None:
        source = (ROOT / "deploy/docker/api/Dockerfile").read_text()
        runtime, test_stage = source.split("FROM runtime AS tests\n")
        test_stage, production = test_stage.split("FROM runtime AS production\n")
        lock = "search/api/requirements-test.lock"
        self.assertIn("--mount=type=bind,source=" + lock, test_stage)
        self.assertIn("target=/tmp/test.lock,readonly", test_stage)
        self.assertIn("--require-hashes", test_stage)
        self.assertEqual([], copy_sources(test_stage))
        self.assertEqual("", production.strip())
        for path in copy_sources(runtime):
            with self.subTest(path=path):
                self.assertNotEqual("search/api", path.rstrip("/"))
                self.assertNotEqual(lock, path)
                self.assertFalse(path.startswith("search/tests"))
        recipe = source_bundle.RECIPES["conversation-api"]
        self.assertNotIn(lock, recipe["inputs"])
        self.assertFalse(
            any(path.startswith("search/tests/") for path in recipe["inputs"])
        )
        self.assertTrue(
            any(
                matches_input(lock, item)
                for item in allowed_inputs(
                    ROOT / (recipe["dockerfile"] + ".dockerignore")
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
