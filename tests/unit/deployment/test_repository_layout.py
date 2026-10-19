"""Guard the current product layout and sealed operator build context."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import unittest


ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATHS = (
    "deploy/compose/compose.search-canary.yaml",
    "deploy/compose/compose.search-web.yaml",
)
ENV_NAMES = (
    ".env.search-canary.example",
    ".env.search-web.example",
)
PRODUCTION_DOCKERFILES = (
    "api/Dockerfile",
    "fess/Dockerfile",
    "importer/Dockerfile",
    "opensearch/Dockerfile",
    "reranker/Dockerfile",
    "web/Dockerfile",
    "web-nginx/Dockerfile",
)
RETIRED_NAMESPACES = (
    "search/api/native_search.py",
    "search/api/native_pipeline.py",
    "search/api/retrieval/fess.py",
    "search/api/provision.py",
    "search/catalog/client.py",
    "docs/openapi.json",
    "search/catalog_bundle.py",
    "search/relevance_gate.py",
    "search/tests/test_relevance_gate.py",
    "search/examples/relevance-heldout.example.jsonl",
    "search/evaluation",
    "research/docker",
    "research/deployment",
    "search/api/planned",
    "deploy/docker/api-nginx",
)


def copy_sources(source: str) -> list[str]:
    """Read local COPY inputs; build-stage --from inputs are not host paths."""
    sources = []
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
            else shlex.split(arguments, comments=True)
        )
        if len(words) < 2 or any(word.startswith("--") for word in words):
            raise ValueError(f"unsupported COPY instruction: {line}")
        sources.extend(words[:-1])
    return sources


def compose_builds(source: str) -> dict[str, tuple[str, str]]:
    """Extract the explicit build maps used by these checked-in Compose files."""
    builds = {}
    service = None
    lines = source.splitlines()
    for index, line in enumerate(lines):
        match = re.fullmatch(r"  ([\w-]+):", line)
        if match:
            service = match[1]
        if line != "    build:":
            continue
        fields = {}
        for following in lines[index + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= 4:
                break
            match = re.fullmatch(r"      (context|dockerfile): (.+)", following)
            if match:
                fields[match[1]] = shlex.split(match[2])[0]
        if service is None or "context" not in fields:
            raise ValueError("build must have a service and explicit context")
        builds[service] = fields["context"], fields.get("dockerfile", "Dockerfile")
    return builds


class RepositoryLayoutTests(unittest.TestCase):
    def test_assets_match_the_current_product_layout(self) -> None:
        new_paths = list(COMPOSE_PATHS)
        new_paths += [f"deploy/env/{name}" for name in ENV_NAMES]
        new_paths += [f"deploy/docker/{name}" for name in PRODUCTION_DOCKERFILES]
        new_paths += [
            "deploy/config/web-nginx/nginx.conf",
            "deploy/config/fess/scheduled_job.bulk",
            "deploy/config/opensearch/opensearch.yml",
            "deploy/config/opensearch/internal_users.yml.in",
            "deploy/config/opensearch/roles.yml",
            "deploy/config/opensearch/roles_mapping.yml",
            "deploy/docker/fess/entrypoint.sh",
            "deploy/docker/fess/fess_healthcheck.sh",
            "deploy/docker/opensearch/opensearch_healthcheck.py",
        ]
        for relative in new_paths:
            with self.subTest(new=relative):
                self.assertTrue((ROOT / relative).is_file(), relative)
        for relative in (*ENV_NAMES, *RETIRED_NAMESPACES):
            with self.subTest(old=relative):
                path = ROOT / relative
                self.assertFalse(path.exists() or path.is_symlink(), relative)

    def test_compose_builds_resolve_to_declared_contexts_and_dockerfiles(self) -> None:
        expected = {
            ("deploy/compose/compose.search-canary.yaml", "opensearch-search"): (
                "../docker/opensearch",
                "Dockerfile",
            ),
            ("deploy/compose/compose.search-canary.yaml", "fess-search"): (
                "../..",
                "deploy/docker/fess/Dockerfile",
            ),
            ("deploy/compose/compose.search-web.yaml", "web"): (
                "../..",
                "deploy/docker/web/Dockerfile",
            ),
            ("deploy/compose/compose.search-web.yaml", "web-nginx"): (
                "../..",
                "deploy/docker/web-nginx/Dockerfile",
            ),
        }
        actual = {}
        for name in COMPOSE_PATHS:
            path = ROOT / name
            for service, build in compose_builds(path.read_text()).items():
                actual[name, service] = build
                context, dockerfile = build
                base = path.parent
                self.assertTrue((base / context / dockerfile).is_file(), build)
        self.assertEqual(expected, actual)

    def test_local_copy_sources_exist_in_each_actual_build_context(self) -> None:
        dockerfiles = [ROOT / "deploy/docker" / name for name in PRODUCTION_DOCKERFILES]
        for dockerfile in dockerfiles:
            with self.subTest(dockerfile=str(dockerfile.relative_to(ROOT))):
                sources = copy_sources(dockerfile.read_text())
                self.assertTrue(sources)
                context = ROOT
                if dockerfile.parent.name == "opensearch":
                    context = dockerfile.parent
                for source in sources:
                    self.assertFalse(Path(source).is_absolute(), source)
                    self.assertNotIn("..", Path(source).parts, source)
                    matches = list(context.glob(source))
                    self.assertTrue(matches, f"missing COPY {source} in {context}")
                    self.assertTrue(all(path.exists() for path in matches), source)

    def test_every_docker_context_has_an_explicit_deny_all_allowlist(self) -> None:
        dockerfiles = [ROOT / "deploy/docker" / name for name in PRODUCTION_DOCKERFILES]
        for dockerfile in dockerfiles:
            with self.subTest(dockerfile=str(dockerfile.relative_to(ROOT))):
                ignore = dockerfile.with_name(dockerfile.name + ".dockerignore")
                if dockerfile.parent.name == "opensearch":
                    ignore = dockerfile.parent / ".dockerignore"
                self.assertTrue(ignore.is_file(), str(ignore))
                patterns = [
                    line.strip()
                    for line in ignore.read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                ]
                self.assertEqual("**", patterns[0])
                for index, pattern in enumerate(patterns[1:], start=1):
                    if not pattern.startswith("!"):
                        # Exclusions narrow directory exceptions before files reopen.
                        continue
                    self.assertNotIn(pattern, {"!**", "!**/", "!*", "!*/", "!./"})
                    self.assertNotIn(".env", pattern)
                    self.assertNotIn("models", Path(pattern.removeprefix("!")).parts)
                    if pattern.endswith("/"):
                        self.assertLess(index + 1, len(patterns), pattern)
                        self.assertEqual(pattern[1:] + "**", patterns[index + 1])

    def test_importer_keeps_an_explicit_canonical_package_source_closure(self) -> None:
        from search.catalog.build.identity import SOURCE_FILES
        from search.operations.catalog.administration import ADMIN_FILES
        from search.operations.catalog.backend import DATA_FILES

        files = set(ADMIN_FILES)
        self.assertLess(files, set(DATA_FILES))
        self.assertLessEqual(set(SOURCE_FILES), set(DATA_FILES))
        self.assertEqual(len(DATA_FILES), len(set(DATA_FILES)))
        self.assertTrue(
            {
                "search/__init__.py",
                "search/serialization.py",
                "search/catalog/__init__.py",
                "search/catalog/admin.py",
                "search/catalog/importer.py",
                "search/catalog/vectors.py",
                "search/catalog_admin.py",
            }
            <= files
        )
        for relative in files:
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file())
                self.assertFalse(
                    relative.startswith(
                        (
                            "search/tests/",
                            "search/evaluation/",
                            "search/api/",
                            "search/web/",
                        )
                    )
                )
                self.assertNotEqual(relative, "search/catalog/query.py")
                self.assertFalse(relative.startswith("search/catalog/build/"))
        dockerfile = (ROOT / "deploy/docker/importer/Dockerfile").read_text()
        copied = copy_sources(dockerfile)
        self.assertEqual(
            {name for name in files if name.startswith("search/")}, set(copied)
        )
        self.assertEqual(len(copied), len(set(copied)))
        self.assertIn(
            'ENTRYPOINT ["python3", "-E", "-s", "-B", "-u", "-m", "search.catalog_admin"]',
            dockerfile,
        )
        self.assertIn("WORKDIR /opt/depositar-search", dockerfile)
        self.assertIn("USER 65534:65534", dockerfile)
        ignore = (ROOT / "deploy/docker/importer/Dockerfile.dockerignore").read_text()
        allowed_files = {
            line[1:]
            for line in ignore.splitlines()
            if line.startswith("!") and not line.endswith("/")
        }
        self.assertEqual(
            {"Dockerfile"} | {name for name in files if name.startswith("search/")},
            allowed_files,
        )

    def test_website_sealed_builds_use_full_repository_archives(self) -> None:
        script = (ROOT / "scripts/search-web.sh").read_text()
        self.assertIn("for role in web web-nginx; do", script)
        self.assertIn(
            'git archive "$SOURCE_REVISION" | docker build -f "deploy/docker/$role/Dockerfile"',
            script,
        )
        self.assertNotIn("$SOURCE_REVISION:web", script)

    def test_copy_parser_skips_build_stages_and_retains_local_sources(self) -> None:
        self.assertEqual(
            ["first.lock", "second.lock", "other.lock"],
            copy_sources(
                "COPY --chown=root:root first.lock " + "\\" + "\n second.lock /tmp/\n"
                "COPY --from=assets /web/dist /app/web-dist\n"
                'COPY ["other.lock", "/tmp/other.lock"]\n'
            ),
        )


if __name__ == "__main__":
    unittest.main()
