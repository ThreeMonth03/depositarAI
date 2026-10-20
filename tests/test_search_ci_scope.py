from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LINT_ROOTS = ("search", "scripts", "tests", "deploy/docker")


def lint_roots(source: str) -> tuple[str, ...]:
    """Read the reviewed root-only array, not an unrelated filename comment."""
    match = re.search(r"(?m)^lint_roots=\(([^)]*)\)$", source)
    if match is None:
        raise AssertionError("Whole-product lint root declaration is missing")
    return tuple(shlex.split(match.group(1)))


def covered_by_roots(relative: str, roots: tuple[str, ...]) -> bool:
    path = Path(relative)
    return any(path == Path(root) or Path(root) in path.parents for root in roots)


class SearchCIScopeTests(unittest.TestCase):
    def test_private_runtime_components_are_in_the_product_gate(self) -> None:
        suite = (ROOT / "scripts/search-ci.sh").read_text(encoding="utf-8")
        for module in (
            "search.tests.test_readiness",
            "tests.unit.deployment.test_private_api_tls",
            "tests.unit.catalog.test_transport",
            "tests.unit.catalog.test_query",
            "tests.unit.catalog.test_client_lease",
            "tests.unit.deployment.test_composition",
            "tests.unit.deployment.test_gpu_ownership",
            "tests.unit.deployment.test_source_bundle",
            "tests.unit.deployment.test_image_build",
            "tests.unit.deployment.test_image_staging",
            "tests.unit.deployment.release.test_runtime",
            "tests.unit.deployment.gateway.test_runtime",
            "tests.unit.deployment.gateway.test_preparation",
            "tests.unit.deployment.release.test_host",
            "tests.unit.deployment.release.test_controller",
            "tests.unit.deployment.release.test_probe",
            "tests.unit.deployment.gateway.test_probe",
            "tests.unit.deployment.gateway.test_probe_runtime",
            "tests.unit.deployment.gateway.test_activation",
            "tests.unit.operations.test_bounded_http",
            "tests.unit.operations.test_cli",
            "tests.integration.test_product_compose",
        ):
            with self.subTest(module=module):
                self.assertIn(module, suite)
        api_gate = (ROOT / "scripts/test-search-api.sh").read_text(encoding="utf-8")
        self.assertIn("search.tests.test_readiness", api_gate)
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("product-stage-catalog", makefile)
        self.assertIn("product-build-images", makefile)
        self.assertIn("product-prepare-query", makefile)
        self.assertIn("product-start-release", makefile)
        self.assertIn("product-prepare-gateway", makefile)
        self.assertIn("product-activate-gateway", makefile)
        self.assertNotRegex(makefile, r"(?m)^product-(?:install|update|rollback):")

    def test_quality_and_native_components_remain_in_current_product_checks(
        self,
    ) -> None:
        suite = (ROOT / "scripts/search-ci.sh").read_text(encoding="utf-8")
        self.assertIn("search.tests.test_reranker_service", suite)
        style = (ROOT / "scripts/test-search-api.sh").read_text(encoding="utf-8")
        self.assertIn("search.tests.test_pipeline", style)
        self.assertEqual(LINT_ROOTS, lint_roots(style))
        for relative in (
            "scripts/depositarai.py",
            "search/api/retrieval/pipeline.py",
            "search/planning/index_filters.py",
        ):
            with self.subTest(relative=relative):
                self.assertTrue(covered_by_roots(relative, lint_roots(style)))

    def test_style_gate_covers_every_product_root_not_a_parallel_file_inventory(
        self,
    ) -> None:
        source = (ROOT / "scripts/test-search-api.sh").read_text(encoding="utf-8")
        self.assertEqual(LINT_ROOTS, lint_roots(source))
        self.assertIn('cd "$project_dir"', source)
        self.assertNotIn("api_python=", source)
        self.assertIn('"${lint_roots[@]}"', source)
        for relative in (
            "search/new_package/nested/new_component.py",
            "scripts/ci/new_operator_helper.py",
            "tests/unit/new_area/test_new_feature.py",
            "deploy/docker/new_service/healthcheck.py",
        ):
            with self.subTest(relative=relative):
                self.assertTrue(covered_by_roots(relative, lint_roots(source)))
                self.assertNotIn(relative, source)
        for outside in (
            "model-cache/model.py",
            "runtime/private_config.py",
            "legacy/notebook.py",
        ):
            with self.subTest(outside=outside):
                self.assertFalse(covered_by_roots(outside, lint_roots(source)))

    def test_workflow_runs_only_the_search_product_suite(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("make search-ci", workflow)
        self.assertNotIn("research-components:", workflow)
        self.assertNotIn("calendar-research:", workflow)
        self.assertIn("bash scripts/test-search-web.sh", workflow)
        self.assertNotIn("run: make ci", workflow)

    def test_search_suite_has_no_legacy_service_dependency(self) -> None:
        script = (ROOT / "scripts/search-ci.sh").read_text(encoding="utf-8")
        lowered = script.casefold()
        for excluded in (
            "jupyter",
            "openwebui",
            "inference-gateway",
            "long_context",
            "leetcode",
            "gptoss",
        ):
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, lowered)
        for required in (
            "search.tests.test_catalog_bundle",
            "search.tests.test_retrieval_results",
            "search.tests.test_scope",
            "search.tests.test_fess_catalog",
            "tests.test_validate_catalog_compose",
            "deploy/compose/compose.catalog.yaml",
            "python3 scripts/validate_catalog_compose.py",
        ):
            with self.subTest(required=required):
                self.assertIn(required, script)
        self.assertNotIn("compose.catalog.gpu.yaml", script)
        self.assertNotIn("SEARCH_OLLAMA_ACCELERATOR", script)
        self.assertNotIn("SEARCH_OLLAMA_GPU_DEVICE", script)

    def test_search_entrypoints_use_only_the_search_helper(self) -> None:
        entrypoints = ("bootstrap-catalog.sh",)
        for name in entrypoints:
            with self.subTest(name=name):
                source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertIn('source "$project_dir/scripts/search-lib.sh"', source)
                self.assertNotIn("scripts/lib.sh", source)

        ci_script = (ROOT / "scripts/search-ci.sh").read_text(encoding="utf-8")
        self.assertIn("scripts/search-lib.sh", ci_script)
        self.assertNotIn("scripts/lib.sh", ci_script)

        helper = (ROOT / "scripts/search-lib.sh").read_text(encoding="utf-8")
        functions = set(re.findall(r"(?m)^([a-z][a-z0-9_]*)\(\) \{$", helper))
        self.assertEqual(
            {
                "die",
                "require_command",
                "load_search_env",
                "require_env",
                "path_is_within",
            },
            functions,
        )
        self.assertIn('python3 "$parser" "$env_file"', helper)
        self.assertIn("scripts/read_env.py", helper)
        for excluded in (
            "backup",
            "jupyter",
            "openwebui",
            "inference-gateway",
            "notebook",
            "nvidia",
            "data_root",
        ):
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, helper.casefold())

    def test_project_entrypoints_expose_product_and_checks(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        targets = set(re.findall(r"^([a-z][a-z0-9-]*):", makefile, flags=re.MULTILINE))
        self.assertEqual(
            {
                "help",
                "search-ci",
                "test",
                "ci",
                "search-web-ci",
                "product-package",
                "product-activate-gateway",
            },
            targets,
        )
        self.assertRegex(makefile, r"(?m)^test: search-ci$")
        self.assertRegex(makefile, r"(?m)^ci: search-ci$")
        self.assertIn(".DEFAULT_GOAL := help", makefile)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        lowered = readme.casefold()
        for excluded in ("jupyterhub", "openwebui", "inference-gateway"):
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, lowered)


class SpecialistChangedScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "depositar_ci_changed_scope", ROOT / "scripts/ci/changed-scope.py"
        )
        cls.scope = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.scope)

    def test_only_explicitly_unrelated_paths_skip_specialist_builds(self):
        cases = (
            (["README.md", "docs/guide/architecture.md"], set()),
            (["web/src/App.tsx", "web/package-lock.json"], {"private-website"}),
            (["scripts/test-search-web.sh"], {"private-website"}),
        )
        for paths, expected in cases:
            with self.subTest(paths=paths):
                self.assertEqual(expected, self.scope.required_jobs(paths))

    def test_shared_unknown_empty_and_malformed_paths_keep_all_gates(self):
        for paths in (
            [],
            ["search/catalog/query.py"],
            ["search/api/requirements.lock"],
            ["search/api/conversation/contract.py"],
            [".github/workflows/ci.yml"],
            ["scripts/ci/changed-scope.py"],
            ["tests/unit/new_dependency.py"],
            ["Makefile"],
            [".dockerignore"],
            ["unknown/new_dependency.py"],
            ["web/src/App.tsx", "unknown/new_dependency.py"],
            ["docs/../search/catalog/query.py"],
            ["docs/report\nmalformed.md"],
        ):
            with self.subTest(paths=paths):
                self.assertEqual(self.scope.JOBS, self.scope.required_jobs(paths))

    def test_non_pr_events_and_invalid_sha_do_not_even_query_git(self):
        environments = [
            {"GITHUB_EVENT_NAME": event} for event in ("push", "workflow_dispatch", "")
        ]
        environments.append(
            {"GITHUB_EVENT_NAME": "pull_request", "CI_BASE_SHA": "main"}
        )
        with patch.object(self.scope.subprocess, "run", side_effect=AssertionError):
            for environment in environments:
                with self.subTest(environment=environment):
                    self.assertEqual(
                        self.scope.JOBS, self.scope.changed_jobs(environment, cwd=ROOT)
                    )

    def test_git_failure_and_timeout_fall_back_to_all_gates(self):
        environment = {
            "GITHUB_EVENT_NAME": "pull_request",
            "CI_BASE_SHA": "a" * 40,
            "CI_HEAD_SHA": "b" * 40,
        }
        for error in (OSError(), subprocess.TimeoutExpired("git", 10)):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.scope.subprocess, "run", side_effect=error):
                    self.assertEqual(
                        self.scope.JOBS, self.scope.changed_jobs(environment, cwd=ROOT)
                    )

    def test_real_git_diff_covers_whole_pr_deletions_and_renames(self):
        # Git only, inside a disposable repository; no Docker or remote calls.
        with tempfile.TemporaryDirectory(prefix="depositar-ci-paths-") as raw:
            directory = Path(raw)

            def git(*arguments):
                return (
                    subprocess.check_output(
                        [
                            "git",
                            "-c",
                            "commit.gpgsign=false",
                            "-c",
                            "core.hooksPath=/dev/null",
                            *arguments,
                        ],
                        cwd=directory,
                        stderr=subprocess.DEVNULL,
                    )
                    .decode()
                    .strip()
                )

            def commit():
                git("add", "--all")
                git(
                    "-c",
                    "user.name=CI Test",
                    "-c",
                    "user.email=ci@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                )
                return git("rev-parse", "HEAD")

            git("init", "-q")
            (directory / "web").mkdir()
            (directory / "search").mkdir()
            (directory / "web/App.tsx").write_text("old frontend\n")
            (directory / "search/shared.py").write_text("shared dependency\n")
            base = commit()
            (directory / "web/App.tsx").write_text("new frontend\n")
            head = commit()
            environment = {
                "GITHUB_EVENT_NAME": "pull_request",
                "CI_BASE_SHA": base,
                "CI_HEAD_SHA": head,
            }
            self.assertEqual(
                {"private-website"},
                self.scope.changed_jobs(environment, cwd=directory),
            )
            (directory / "docs").mkdir()
            (directory / "search/shared.py").rename(directory / "docs/moved.md")
            environment["CI_HEAD_SHA"] = commit()
            # Treating a rename as just its destination would miss removed code.
            self.assertEqual(
                self.scope.JOBS, self.scope.changed_jobs(environment, cwd=directory)
            )
            (directory / "docs/moved.md").write_text("later documentation edit\n")
            environment["CI_HEAD_SHA"] = commit()
            # Looking only at HEAD~1 would now miss the earlier shared removal.
            self.assertEqual(
                self.scope.JOBS, self.scope.changed_jobs(environment, cwd=directory)
            )
            environment["CI_BASE_SHA"] = "f" * 40
            self.assertEqual(
                self.scope.JOBS, self.scope.changed_jobs(environment, cwd=directory)
            )
            environment["CI_BASE_SHA"] = base
            environment["CI_HEAD_SHA"] = head
            self.assertEqual(
                self.scope.JOBS, self.scope.changed_jobs(environment, cwd=directory)
            )

    def test_workflow_keeps_four_checks_and_the_complete_validate_gate(self):
        source = (ROOT / ".github/workflows/ci.yml").read_text()
        jobs = {
            match[1]: match[2]
            for match in re.finditer(
                r"(?m)^  ([a-z][a-z-]*):\n(.*?)(?=^  [a-z][a-z-]*:|\Z)",
                source.split("\njobs:\n", 1)[1],
                re.S,
            )
        }
        website_jobs = {"private-website-oss", "private-website-browser"}
        self.assertEqual({"validate", *self.scope.JOBS, *website_jobs}, set(jobs))
        self.assertIn("run: make search-ci", jobs["validate"])
        self.assertNotIn("if:", jobs["validate"])
        for name in sorted(website_jobs):
            gate = "private-website"
            self.assertIn("fetch-depth: 2", jobs[name])
            self.assertIn(f"scripts/ci/changed-scope.py {gate}", jobs[name])
            self.assertIn(
                "CI_BASE_SHA: ${{ github.event.pull_request.base.sha }}", jobs[name]
            )
            self.assertIn("CI_HEAD_SHA: ${{ github.sha }}", jobs[name])
            self.assertIn("if: steps.scope.outputs.run != 'false'", jobs[name])
            self.assertIn("Report unaffected gate", jobs[name])
        for suite in ("oss", "browser"):
            job = jobs["private-website-" + suite]
            self.assertIn("run: bash scripts/test-search-web.sh --suite " + suite, job)
            self.assertIn("id: website", job)
            for field, value in (
                ("scope_run", "steps.scope.outputs.run"),
                ("gate_outcome", "steps.website.outcome"),
                ("source_sha", "steps.source.outputs.sha"),
                ("source_tree", "steps.source.outputs.tree"),
            ):
                self.assertIn(field + ": ${{ " + value + " }}", job)
        aggregate = jobs["private-website"]
        self.assertIn(
            "needs: [private-website-oss, private-website-browser]", aggregate
        )
        self.assertIn("if: always()", aggregate)
        self.assertNotIn("run: bash scripts/test-search-web.sh", aggregate)
        self.assertNotIn("paths-ignore:", source)

    def test_cache_is_downloads_only_bounded_and_not_a_test_skip(self):
        source = (ROOT / ".github/workflows/ci.yml").read_text()
        jobs = dict(
            re.findall(
                r"(?m)^  ([a-z][a-z-]*):\n(.*?)(?=^  [a-z][a-z-]*:|\Z)",
                source.split("\njobs:\n", 1)[1],
                re.S,
            )
        )
        for name in ("private-website-oss", "private-website-browser"):
            job = jobs[name]
            for action in ("restore", "save"):
                self.assertIn(
                    f"actions/cache/{action}@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0",
                    job,
                )
            self.assertEqual(
                2, job.count("path: ${{ runner.temp }}/depositar-npm/_cacache")
            )
            self.assertIn("hashFiles('web/package-lock.json')", job)
            self.assertIn("runner.arch", job)
            self.assertEqual(2, job.count("timeout-minutes: 2\n"))
            self.assertEqual(2, job.count("continue-on-error: true"))
            self.assertIn("SEGMENT_DOWNLOAD_TIMEOUT_MINS: 1", job)
            self.assertIn(
                "github.event.pull_request.head.repo.full_name == github.repository",
                job,
            )
            self.assertIn("success() && steps.scope.outputs.run != 'false'", job)
        self.assertEqual(4, source.count("continue-on-error: true"))
        self.assertNotIn("path: web/node_modules", source)
        self.assertNotRegex(
            source, r"if:.*cache-hit.*\n.*run: bash scripts/test-search-web"
        )


class WebsiteNpmCacheInvocationTests(unittest.TestCase):
    def run_npm_install(self, cache_path=None):
        source = (ROOT / "scripts/test-search-web.sh").read_text()
        start = source.index("npm_cache_dir=")
        end = source.index("\ndocker run --rm --network none", start)
        program = (
            'set -euo pipefail\nproject_dir="$1"\nnode_image=fixture-node\n'
            + source[start:end]
        )
        with tempfile.TemporaryDirectory(prefix="depositar-npm-cache-") as raw:
            directory = Path(raw)
            fake_docker = directory / "docker"
            fake_docker.write_text(
                f"#!{sys.executable}\n"
                "import json,os,sys\n"
                "with open(os.environ['NPM_CACHE_TEST_LOG'],'a') as stream:\n"
                "    stream.write(json.dumps(sys.argv[1:])+'\\n')\n"
            )
            fake_docker.chmod(0o700)
            log = directory / "calls.jsonl"
            environment = dict(os.environ)
            environment.pop("CI_NPM_CACHE", None)
            environment.update(
                PATH=str(directory) + os.pathsep + environment.get("PATH", ""),
                XDG_CACHE_HOME=str(directory / "cache root"),
                NPM_CACHE_TEST_LOG=str(log),
            )
            if cache_path is not None:
                environment["CI_NPM_CACHE"] = cache_path
            result = subprocess.run(
                ["bash", "-c", program, "npm-cache-test", str(ROOT)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            calls = (
                [json.loads(line) for line in log.read_text().splitlines()]
                if log.exists()
                else []
            )
            return result, calls, str(directory / "cache root/depositar-ai/npm")

    def test_actual_install_mounts_persistent_download_cache_and_still_uses_ci(self):
        result, calls, expected_cache = self.run_npm_install()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, len(calls))
        self.assertIn(f"type=bind,src={expected_cache},dst=/tmp/npm", calls[0])
        self.assertIn("npm_config_cache=/tmp/npm", calls[0])
        self.assertEqual(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], calls[0][-5:]
        )

    def test_invalid_cache_paths_fail_before_any_docker_call(self):
        for value in (
            "relative",
            "/tmp/cache,other",
            "/tmp/cache\nother",
            "/tmp/cache\rother",
        ):
            with self.subTest(value=value):
                result, calls, _ = self.run_npm_install(value)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual([], calls)

    def test_workflow_cache_override_reaches_the_actual_docker_mount(self):
        with tempfile.TemporaryDirectory(prefix="depositar-ci-npm-") as raw:
            cache = str(Path(raw) / "runner cache")
            result, calls, _ = self.run_npm_install(cache)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(f"type=bind,src={cache},dst=/tmp/npm", calls[0])
            self.assertTrue(Path(cache).is_dir())


class SearchCIStyleInvocationTests(unittest.TestCase):
    """Run the real lint-loop bytes using a fake Docker binary, never a daemon."""

    def run_lint_loop(self, failure: str = ""):
        source = (ROOT / "scripts/test-search-api.sh").read_text(encoding="utf-8")
        start = source.index("lint_roots=(")
        end = source.index("\ndone", start) + len("\ndone")
        # Only the standalone reviewed loop runs. The build/runtime/network
        # phases outside it are intentionally not simulated as acceptance.
        program = 'set -euo pipefail\nproject_dir="$1"\n' + source[start:end]
        with tempfile.TemporaryDirectory(prefix="depositar-lint-scope-") as raw:
            temporary = Path(raw)
            fake_docker = temporary / "docker"
            fake_docker.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "arguments = sys.argv[1:]\n"
                "with open(os.environ['DEPOSITAR_LINT_TEST_LOG'], 'a') as log:\n"
                "    log.write(json.dumps(arguments) + '\\n')\n"
                "failure = os.environ.get('DEPOSITAR_LINT_TEST_FAILURE', '')\n"
                "if 'ruff' in arguments and failure in {'check', 'format'}:\n"
                "    if arguments[arguments.index('ruff') + 1] == failure:\n"
                "        sys.exit(23)\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o700)
            log = temporary / "calls.jsonl"
            environment = dict(os.environ)
            environment.update(
                PATH=str(temporary) + os.pathsep + environment.get("PATH", ""),
                DEPOSITAR_LINT_TEST_LOG=str(log),
                DEPOSITAR_LINT_TEST_FAILURE=failure,
            )
            result = subprocess.run(
                ["bash", "-c", program, "lint-scope-test", str(ROOT)],
                cwd=temporary,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            calls = (
                [
                    json.loads(line)
                    for line in log.read_text(encoding="utf-8").splitlines()
                ]
                if log.exists()
                else []
            )
            return result, calls

    def test_both_actual_ruff_commands_use_all_roots_without_writing_source(
        self,
    ) -> None:
        result, calls = self.run_lint_loop()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(2, len(calls))
        for action, arguments in zip(("check", "format"), calls):
            with self.subTest(action=action):
                index = arguments.index("ruff")
                expected = ["ruff", action, "--no-cache"]
                if action == "format":
                    expected.append("--check")
                self.assertEqual(expected + list(LINT_ROOTS), arguments[index:])
                self.assertEqual("run", arguments[0])
                self.assertIn("--rm", arguments)
                self.assertEqual("none", arguments[arguments.index("--network") + 1])
                self.assertEqual("/source", arguments[arguments.index("--workdir") + 1])
                self.assertEqual(
                    f"{os.geteuid()}:{os.getegid()}",
                    arguments[arguments.index("--user") + 1],
                )
                self.assertEqual(
                    f"type=bind,src={ROOT},dst=/source,readonly",
                    arguments[arguments.index("--mount") + 1],
                )
                for forbidden in ("--fix", "--publish", "--gpus", "--privileged"):
                    self.assertNotIn(forbidden, arguments)

    def test_lint_failure_stops_before_format_and_propagates(self) -> None:
        result, calls = self.run_lint_loop("check")
        self.assertEqual(23, result.returncode)
        self.assertEqual(1, len(calls))
        self.assertEqual("check", calls[0][calls[0].index("ruff") + 1])

    def test_format_failure_is_not_ignored(self) -> None:
        result, calls = self.run_lint_loop("format")
        self.assertEqual(23, result.returncode)
        self.assertEqual(2, len(calls))
        self.assertEqual("format", calls[1][calls[1].index("ruff") + 1])


class WebsiteBrowserPullInvocationTests(unittest.TestCase):
    """Exercise the shell lifecycle with fake clients and a real GNU timeout."""

    def run_website_gate(self, mode: str, *, suite=None, arguments=None):
        with tempfile.TemporaryDirectory(prefix="depositar-web-pull-") as raw:
            temporary = Path(raw)
            log = temporary / "calls.jsonl"
            marker = temporary / "pull-pid"
            common = (
                f"#!{sys.executable}\n"
                "import json, os, pathlib, signal, sys, time\n"
                "root = pathlib.Path(os.environ['DEPOSITAR_WEB_CI_TEST_ROOT'])\n"
                "mode = os.environ['DEPOSITAR_WEB_CI_TEST_MODE']\n"
                "suite = os.environ['DEPOSITAR_WEB_CI_TEST_SUITE']\n"
                "arguments = sys.argv[1:]\n"
                "def record(kind):\n"
                "    with (root / 'calls.jsonl').open('a') as stream:\n"
                "        stream.write(json.dumps([kind, arguments]) + '\\n')\n"
            )
            fake_docker = temporary / "docker"
            fake_docker.write_text(
                common + "record('docker')\n"
                "if mode == 'style-failure' and 'ruff' in arguments and 'check' in arguments:\n"
                "    sys.exit(26)\n"
                "if mode == 'browser-failure' and arguments[-2:] == ['npm', 'test']:\n"
                "    sys.exit(27)\n"
                "if arguments[:2] == ['image', 'inspect']:\n"
                "    if 'playwright' in arguments[-1]:\n"
                "        sys.exit(0 if mode == 'cached' else 1)\n"
                "    print('sha256:' + 'a' * 64)\n"
                "if arguments[:1] == ['pull']:\n"
                "    if mode in {'early-failure', 'signal-term', 'signal-int', 'stubborn-timeout'}:\n"
                "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                # Publish readiness only after signal setup and the full PID write.
                "    ready = root / 'pull-pid.tmp'\n"
                "    ready.write_text(str(os.getpid()))\n"
                "    ready.replace(root / 'pull-pid')\n"
                "    if mode in {'early-failure', 'signal-term', 'signal-int'}:\n"
                "        time.sleep(10)\n"
                "    deadline = time.monotonic() + 3\n"
                "    while not (root / 'work-started').exists():\n"
                "        if time.monotonic() > deadline: sys.exit(24)\n"
                "        time.sleep(0.005)\n"
                "    if mode in {'timeout', 'stubborn-timeout'}:\n"
                "        time.sleep(10)\n"
                "    if mode == 'pull-failure':\n"
                "        sys.exit(23)\n"
                "    record('pull-completed')\n",
                encoding="utf-8",
            )
            fake_python = temporary / "python3"
            fake_python.write_text(
                common + "record('python')\n"
                "if mode == 'contract-failure' and 'tests.test_search_web_deploy' in arguments:\n"
                "    sys.exit(17)\n"
                "is_oss = 'tests.integration.public_entry.test_runtime' in arguments\n"
                "is_bootstrap = 'tests.unit.deployment.gateway.test_activation.NativeBootstrapTests' in arguments\n"
                "if is_oss or (suite == 'browser' and is_bootstrap):\n"
                "    (root / 'work-started').touch()\n"
                "    if suite != 'oss' and mode != 'cached':\n"
                "        deadline = time.monotonic() + 3\n"
                "        while not (root / 'pull-pid').exists():\n"
                "            if time.monotonic() > deadline: sys.exit(24)\n"
                "            time.sleep(0.005)\n"
                "    if mode == 'early-failure': sys.exit(19)\n"
                "    signals = {'signal-term': signal.SIGTERM, 'signal-int': signal.SIGINT}\n"
                "    if mode in signals:\n"
                "        os.kill(os.getppid(), signals[mode])\n"
                "        sys.exit(0)\n"
                "if arguments == ['-'] or arguments[:1] == ['scripts/validate_search_web.py']:\n"
                "    sys.stdin.read()\n",
                encoding="utf-8",
            )
            fake_timeout = temporary / "timeout"
            fake_timeout.write_text(
                common + "record('timeout')\n"
                "assert arguments[:2] == ['--kill-after=5s', '180s']\n"
                "if mode in {'timeout', 'stubborn-timeout'}: arguments[1] = '0.2s'\n"
                "if mode == 'stubborn-timeout': arguments[0] = '--kill-after=0.2s'\n"
                "os.execv('/usr/bin/timeout', ['timeout', *arguments])\n",
                encoding="utf-8",
            )
            for executable in (fake_docker, fake_python, fake_timeout):
                executable.chmod(0o700)
            environment = dict(os.environ)
            environment.update(
                PATH=str(temporary) + os.pathsep + environment.get("PATH", ""),
                CI_NPM_CACHE=str(temporary / "npm"),
                DEPOSITAR_WEB_CI_TEST_ROOT=str(temporary),
                DEPOSITAR_WEB_CI_TEST_MODE=mode,
                DEPOSITAR_WEB_CI_TEST_SUITE=suite or "all",
            )
            if arguments is None:
                arguments = [] if suite is None else ["--suite", suite]
            result = subprocess.run(
                ["bash", "scripts/test-search-web.sh", *arguments],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            calls = (
                [json.loads(line) for line in log.read_text().splitlines()]
                if log.exists()
                else []
            )
            running = False
            if marker.exists():
                process = Path("/proc") / marker.read_text() / "stat"
                # Waiting for timeout reaps its leader; killed descendants may
                # briefly remain runnable until the kernel finishes teardown.
                deadline = time.monotonic() + 1
                while True:
                    try:
                        running = process.read_text().split(")", 1)[1].split()[0] != "Z"
                    except (FileNotFoundError, ProcessLookupError):
                        running = False
                    remaining = deadline - time.monotonic()
                    if not running or remaining <= 0:
                        break
                    time.sleep(min(0.01, remaining))
            return result, calls, running

    def test_cold_pull_overlaps_oss_but_finishes_before_the_browser(self):
        result, calls, running = self.run_website_gate("cold")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(running)
        completed = next(i for i, row in enumerate(calls) if row[0] == "pull-completed")
        browser = next(
            i
            for i, row in enumerate(calls)
            if row[0] == "docker" and row[1][-2:] == ["npm", "test"]
        )
        self.assertLess(completed, browser)
        self.assertEqual(1, sum(row[0] == "timeout" for row in calls))
        phases = [
            line for line in result.stdout.splitlines() if line.startswith("CI_PHASE ")
        ]
        self.assertEqual(13, len(phases))
        for line in phases:
            self.assertRegex(line, r"^CI_PHASE phase=[a-z-]+ seconds=\d+ exit=0$")

    def test_cached_image_is_not_pulled_again(self):
        result, calls, running = self.run_website_gate("cached")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(running)
        self.assertFalse(any(row[0] == "timeout" for row in calls))
        self.assertFalse(any(row[1][:1] == ["pull"] for row in calls))
        self.assertIn("CI_PHASE phase=browser-image seconds=0 exit=0", result.stdout)

    def test_failed_or_timed_out_pull_fails_before_browser_and_cleans_fixture(self):
        for mode, status in (
            ("pull-failure", 23),
            ("timeout", 124),
            ("stubborn-timeout", 137),
        ):
            with self.subTest(mode=mode):
                result, calls, running = self.run_website_gate(mode)
                self.assertEqual(status, result.returncode, result.stderr)
                self.assertFalse(running)
                self.assertFalse(any(row[1][-2:] == ["npm", "test"] for row in calls))
                self.assertTrue(any(row[1][:2] == ["rm", "--force"] for row in calls))
                self.assertTrue(any(row[1][:2] == ["network", "rm"] for row in calls))
                self.assertRegex(
                    result.stdout,
                    rf"CI_PHASE phase=browser-image-wait seconds=\d+ exit={status}\n",
                )

    def test_earlier_oss_failure_terminates_the_running_pull_and_keeps_exit_code(self):
        result, calls, running = self.run_website_gate("early-failure")
        self.assertEqual(19, result.returncode, result.stderr)
        self.assertFalse(running, "background Docker pull survived the EXIT trap")
        self.assertFalse(any(row[1][-2:] == ["npm", "test"] for row in calls))
        self.assertRegex(
            result.stdout,
            r"CI_PHASE phase=public-entry-runtime seconds=\d+ exit=19\n",
        )
        self.assertRegex(result.stdout, r"CI_TOTAL seconds=\d+ exit=19\n")

    def test_interruption_terminates_the_pull_and_preserves_signal_exit(self):
        for mode, status in (("signal-term", 143), ("signal-int", 130)):
            with self.subTest(mode=mode):
                result, calls, running = self.run_website_gate(mode)
                self.assertEqual(status, result.returncode, result.stderr)
                self.assertFalse(running, "background pull survived interruption")
                self.assertFalse(any(row[1][-2:] == ["npm", "test"] for row in calls))
                self.assertRegex(
                    result.stdout, rf"CI_TOTAL seconds=\d+ exit={status}\n"
                )

    def test_default_and_explicit_all_execute_the_same_commands(self):
        def manifest(calls):
            return sorted(
                (
                    kind,
                    tuple(
                        re.sub(
                            r"depositar-web-(?:ci-\d+|pull-[^/]+)",
                            "depositar-web-fixture",
                            value,
                        )
                        for value in arguments
                    ),
                )
                for kind, arguments in calls
                if kind != "pull-completed"
            )

        default, original, running = self.run_website_gate("cold")
        explicit, selected, selected_running = self.run_website_gate(
            "cold", suite="all"
        )
        self.assertEqual(0, default.returncode, default.stderr)
        self.assertEqual(0, explicit.returncode, explicit.stderr)
        self.assertFalse(running or selected_running)
        self.assertEqual(manifest(original), manifest(selected))
        self.assertIn("CI_SUITE suite=all\n", default.stdout)
        self.assertIn("CI_SUITE suite=all\n", explicit.stdout)

    def test_split_suites_keep_the_common_checks_and_select_the_runtime_phases(self):
        common_phases = {
            "contracts",
            "python-images",
            "python-unit",
            "python-style",
            "npm",
            "web-image",
        }
        oss_phases = {"public-entry-images", "public-entry-runtime"}
        browser_phases = {
            "browser-image",
            "gateway-bootstrap",
            "browser-fixture",
            "browser-image-wait",
            "browser",
        }
        for suite in ("oss", "browser"):
            with self.subTest(suite=suite):
                result, calls, running = self.run_website_gate("cold", suite=suite)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertFalse(running)
                docker = [args for kind, args in calls if kind == "docker"]
                python = [args for kind, args in calls if kind == "python"]
                self.assertEqual(
                    1,
                    sum("tests.test_search_web_deploy" in args for args in python),
                )
                unit = next(
                    args for args in docker if "search.tests.test_conversation" in args
                )
                self.assertEqual(
                    [
                        "search.tests.test_conversation",
                        "search.tests.test_conversation_calendar",
                        "search.tests.test_conversation_time",
                        "search.tests.test_search_web",
                        "search.tests.test_search_web_identity",
                        "search.tests.test_worker_io",
                        "-v",
                    ],
                    unit[-7:],
                )
                self.assertEqual(4, sum("ruff" in args for args in docker))
                self.assertEqual(
                    1,
                    sum(
                        args[-5:]
                        == ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
                        for args in docker
                    ),
                )
                self.assertEqual(
                    1,
                    sum(args[-3:] == ["npm", "run", "format:check"] for args in docker),
                )
                self.assertEqual(
                    {"conversation", "web"},
                    {args[-1] for args in docker if args[0] == "run" and "-I" in args},
                )
                builds = [
                    args[args.index("-f") + 1] for args in docker if args[0] == "build"
                ]
                expected_builds = [
                    "deploy/docker/api/Dockerfile",
                    "deploy/docker/api/Dockerfile",
                    "deploy/docker/web/Dockerfile",
                    "deploy/docker/web-nginx/Dockerfile",
                ]
                self.assertEqual(expected_builds, builds)
                self.assertEqual(
                    suite == "oss",
                    any(
                        "tests.integration.public_entry.test_runtime" in args
                        for args in python
                    ),
                )
                self.assertEqual(suite == "oss", ["-"] in python)
                self.assertEqual(
                    suite == "browser",
                    any(
                        "tests.unit.deployment.gateway.test_activation.NativeBootstrapTests"
                        in args
                        for args in python
                    ),
                )
                self.assertEqual(
                    suite == "browser",
                    any(args[-2:] == ["npm", "test"] for args in docker),
                )
                self.assertEqual(
                    suite == "browser", any(args[0] == "pull" for args in docker)
                )
                gateways = [
                    args
                    for args in docker
                    if args[:2] == ["run", "-d"]
                    and "depositar-search-web-nginx:ci" in args
                ]
                self.assertEqual(1 if suite == "browser" else 0, len(gateways))
                for gateway in gateways:
                    self.assertEqual("depositar-search-web-nginx:ci", gateway[-1])
                    self.assertNotIn("--entrypoint", gateway)
                self.assertFalse(
                    any("gateway-control" in item for args in docker for item in args)
                )
                self.assertEqual(
                    suite == "browser",
                    any(args[:2] == ["network", "create"] for args in docker),
                )
                phases = re.findall(
                    r"(?m)^CI_PHASE phase=([a-z-]+) seconds=\d+ exit=0$", result.stdout
                )
                expected = common_phases | (
                    oss_phases if suite == "oss" else browser_phases
                )
                self.assertEqual(expected, set(phases))
                self.assertEqual(len(expected), len(phases))
                if suite == "oss":
                    self.assertIn("OSS runtime checks passed.", result.stdout)
                    self.assertNotIn("browser checks passed.", result.stdout)
                else:
                    self.assertIn("real HTTP browser checks passed.", result.stdout)
                    self.assertNotIn("OSS runtime checks passed.", result.stdout)

    def test_invalid_suite_arguments_fail_before_any_client_is_invoked(self):
        for arguments in (
            ["--suite"],
            ["browser"],
            ["--suite", "unknown"],
            ["--unknown", "oss"],
            ["--suite", "browser", "extra"],
            ["--suite", ""],
        ):
            with self.subTest(arguments=arguments):
                result, calls, running = self.run_website_gate(
                    "cold", arguments=arguments
                )
                self.assertEqual(64, result.returncode)
                self.assertFalse(running)
                self.assertEqual([], calls)

    def test_both_suites_propagate_contract_and_style_failures_before_runtime(self):
        for suite in ("oss", "browser"):
            for mode, status, phase in (
                ("contract-failure", 17, "contracts"),
                ("style-failure", 26, "python-style"),
            ):
                with self.subTest(suite=suite, mode=mode):
                    result, calls, running = self.run_website_gate(mode, suite=suite)
                    self.assertEqual(status, result.returncode, result.stderr)
                    self.assertFalse(running)
                    self.assertFalse(
                        any(args[-2:] == ["npm", "test"] for _, args in calls)
                    )
                    self.assertFalse(
                        any(
                            "tests.integration.public_entry.test_runtime" in args
                            for _, args in calls
                        )
                    )
                    self.assertRegex(
                        result.stdout,
                        rf"CI_PHASE phase={phase} seconds=\d+ exit={status}\n",
                    )

    def test_browser_only_pull_failures_propagate_and_clean_the_fixture(self):
        for mode, status in (
            ("pull-failure", 23),
            ("timeout", 124),
            ("stubborn-timeout", 137),
        ):
            with self.subTest(mode=mode):
                result, calls, running = self.run_website_gate(mode, suite="browser")
                self.assertEqual(status, result.returncode, result.stderr)
                self.assertFalse(running)
                self.assertFalse(any(args[-2:] == ["npm", "test"] for _, args in calls))
                self.assertTrue(any(args[:2] == ["rm", "--force"] for _, args in calls))
                self.assertTrue(any(args[:2] == ["network", "rm"] for _, args in calls))
                self.assertRegex(
                    result.stdout, rf"CI_TOTAL seconds=\d+ exit={status}\n"
                )

    def test_browser_only_bootstrap_failure_terminates_the_pull_before_fixture_start(
        self,
    ):
        result, calls, running = self.run_website_gate("early-failure", suite="browser")
        self.assertEqual(19, result.returncode, result.stderr)
        self.assertFalse(running, "background pull survived the bootstrap failure")
        self.assertFalse(any(args[:2] == ["network", "create"] for _, args in calls))
        self.assertFalse(any(args[-2:] == ["npm", "test"] for _, args in calls))
        self.assertRegex(
            result.stdout, r"CI_PHASE phase=gateway-bootstrap seconds=\d+ exit=19\n"
        )

    def test_browser_only_failed_browser_command_cleans_the_fixture(self):
        result, calls, running = self.run_website_gate(
            "browser-failure", suite="browser"
        )
        self.assertEqual(27, result.returncode, result.stderr)
        self.assertFalse(running)
        self.assertTrue(any(args[-2:] == ["npm", "test"] for _, args in calls))
        self.assertTrue(any(args[:2] == ["rm", "--force"] for _, args in calls))
        self.assertTrue(any(args[:2] == ["network", "rm"] for _, args in calls))
        self.assertRegex(result.stdout, r"CI_PHASE phase=browser seconds=\d+ exit=27\n")

    def test_browser_only_signals_terminate_the_pull_and_keep_signal_exit(self):
        for mode, status in (("signal-term", 143), ("signal-int", 130)):
            with self.subTest(mode=mode):
                result, calls, running = self.run_website_gate(mode, suite="browser")
                self.assertEqual(status, result.returncode, result.stderr)
                self.assertFalse(running, "background pull survived interruption")
                self.assertFalse(any(args[-2:] == ["npm", "test"] for _, args in calls))
                self.assertRegex(
                    result.stdout, rf"CI_TOTAL seconds=\d+ exit={status}\n"
                )


if __name__ == "__main__":
    unittest.main()
