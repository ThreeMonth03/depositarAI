from __future__ import annotations

from itertools import product
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import re
import unittest


ROOT = Path(__file__).resolve().parents[3]


def literal_run_blocks(source: str) -> list[str]:
    """Extract literal shell blocks from this repository's workflow files."""
    lines = source.splitlines()
    blocks = []
    for index, line in enumerate(lines):
        if line.strip().removeprefix("- ") not in {"run: |", "run: |-", "run: |+"}:
            continue
        indentation = len(line) - len(line.lstrip())
        body = []
        for following in lines[index + 1 :]:
            if following.strip():
                current = len(following) - len(following.lstrip())
                if current <= indentation:
                    break
            body.append(following)
        blocks.append(textwrap.dedent("\n".join(body)) + "\n")
    return blocks


class WorkflowSyntaxTests(unittest.TestCase):
    def test_feature_updates_use_pr_ci_without_duplicate_push_runs(self) -> None:
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("  push:\n    branches: [main]\n  pull_request:\n", ci)
        self.assertIn("  workflow_dispatch:", ci)
        self.assertNotIn("pull_request_target", ci)

    def test_literal_run_blocks_are_valid_bash(self) -> None:
        for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
            blocks = literal_run_blocks(path.read_text(encoding="utf-8"))
            for index, block in enumerate(blocks):
                with self.subTest(workflow=path.name, block=index):
                    result = subprocess.run(
                        ["bash", "-n"],
                        input=block,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(0, result.returncode, result.stderr)

    def test_extractor_preserves_an_unclosed_loop(self) -> None:
        source = "steps:\n  - run: |\n      for value in a b; do\n        echo value\n"
        block = literal_run_blocks(source)[0]
        result = subprocess.run(
            ["bash", "-n"], input=block, capture_output=True, text=True, check=False
        )
        self.assertNotEqual(0, result.returncode)

    def test_compose_jobs_and_source_package_share_the_pinned_installer(self) -> None:
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        package = (ROOT / ".github/workflows/package.yml").read_text(encoding="utf-8")
        installer = "run: bash scripts/ci/install-compose.sh"
        self.assertEqual(3, ci.count(installer))
        self.assertEqual(2, package.count(installer))

    def test_new_ci_helpers_are_in_the_core_syntax_and_unit_checks(self) -> None:
        suite = (ROOT / "scripts/search-ci.sh").read_text(encoding="utf-8")
        for required in (
            "scripts/search-ci.sh",
            "scripts/ci/install-compose.sh",
            "tests.unit.deployment.test_ci_compose_install",
            "tests.unit.deployment.test_ci_workflows",
        ):
            with self.subTest(required=required):
                self.assertIn(required, suite)


class SourcePackageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.package = (ROOT / ".github/workflows/package.yml").read_text(
            encoding="utf-8"
        )
        self.ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    @staticmethod
    def jobs(source):
        return {
            match[1]: match[2]
            for match in re.finditer(
                r"(?m)^  ([a-z][a-z-]*):\n(.*?)(?=^  [a-z][a-z-]*:|\Z)",
                source.split("\njobs:\n", 1)[1],
                re.S,
            )
        }

    def test_product_and_website_gates_must_pass_before_artifact_is_created(self):
        jobs = self.jobs(self.package)
        self.assertEqual(
            {
                "validate",
                "private-website",
                "package",
            },
            set(jobs),
        )
        self.assertIn(
            "needs: [validate, private-website]",
            jobs["package"],
        )
        self.assertNotIn("always()", jobs["package"])
        for name, command in (
            ("validate", "make search-ci"),
            ("private-website", "bash scripts/test-search-web.sh"),
        ):
            with self.subTest(job=name):
                self.assertIn("run: " + command, jobs[name])
                if name != "private-website":
                    self.assertIn("run: " + command, self.jobs(self.ci)[name])
        ci_jobs = self.jobs(self.ci)
        for suite in ("oss", "browser"):
            self.assertIn(
                "run: bash scripts/test-search-web.sh --suite " + suite,
                ci_jobs["private-website-" + suite],
            )
        self.assertIn(
            "needs: [private-website-oss, private-website-browser]",
            ci_jobs["private-website"],
        )
        self.assertIn("if: always()", ci_jobs["private-website"])

    def test_jobs_work_in_public_repositories_and_use_explicit_event_sha(self):
        for workflow, source in (("CI", self.ci), ("package", self.package)):
            for name, block in self.jobs(source).items():
                with self.subTest(workflow=workflow, job=name):
                    self.assertIn("ref: ${{ github.sha }}", block)
                    self.assertIn("persist-credentials: false", block)
            self.assertNotIn("ref: main", source)
            self.assertNotIn("github.event.pull_request.head.sha", source)
        self.assertIn("fetch-depth: 0", self.jobs(self.package)["package"])
        self.assertNotIn("github.event.repository.private", self.package)
        self.assertIn("name: Source package\n", self.package)

    def test_scanning_and_exact_restore_precede_source_upload(self):
        block = self.jobs(self.package)["package"]
        ordered = (
            "bash scripts/ci/install-gitleaks.sh",
            "bash scripts/ci/source-package.sh",
            "bash scripts/ci/source-restore.sh",
            "uses: actions/upload-artifact@",
        )
        positions = [block.index(value) for value in ordered]
        self.assertEqual(sorted(positions), positions)
        self.assertIn('"$GITHUB_SHA" "$GITLEAKS_BINARY"', block)
        self.assertIn('"$RUNNER_TEMP/depositar-ai-release" "$GITHUB_SHA"', block)
        self.assertIn("path: ${{ runner.temp }}/depositar-ai-release/", block)
        self.assertIn("if-no-files-found: error", block)
        self.assertIn("retention-days: 7", block)

    def test_release_workflow_never_deploys_or_uses_licensed_secret_action(self):
        self.assertIn("contents: read", self.package)
        for forbidden in (
            "contents: write",
            "pull_request_target",
            "gitleaks/gitleaks-action",
            "GITLEAKS_LICENSE",
            "docker push",
            "gh release",
            "ssh production-host",
            "search-web-up",
            "search-api-deploy",
            "continue-on-error",
        ):
            with self.subTest(value=forbidden):
                self.assertNotIn(forbidden, self.package)


class WebsiteAggregateTests(unittest.TestCase):
    def setUp(self):
        self.jobs = SourcePackageWorkflowTests.jobs(
            (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        )
        blocks = [
            block
            for block in literal_run_blocks(self.jobs["private-website"])
            if "OSS_RESULT" in block
        ]
        self.assertEqual(1, len(blocks))
        self.aggregate = blocks[0]

    @staticmethod
    def environment(*, unaffected=False):
        sha, tree = "a" * 40, "b" * 40
        return {
            "GITHUB_SHA": sha,
            "GITHUB_EVENT_NAME": "pull_request",
            "EXPECTED_SHA": sha,
            "EXPECTED_TREE": tree,
            "OSS_RESULT": "success",
            "BROWSER_RESULT": "success",
            "OSS_SCOPE_RUN": "false" if unaffected else "true",
            "BROWSER_SCOPE_RUN": "false" if unaffected else "true",
            "OSS_GATE_OUTCOME": "skipped" if unaffected else "success",
            "BROWSER_GATE_OUTCOME": "skipped" if unaffected else "success",
            "OSS_SOURCE_SHA": sha,
            "BROWSER_SOURCE_SHA": sha,
            "OSS_SOURCE_TREE": tree,
            "BROWSER_SOURCE_TREE": tree,
        }

    def run_aggregate(self, values, *, missing=()):
        environment = dict(os.environ)
        environment.update(values)
        for key in missing:
            environment.pop(key, None)
        return subprocess.run(
            ["bash", "-c", self.aggregate],
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    def test_outputs_and_aggregate_environment_use_the_actual_step_evidence(self):
        aggregate = self.jobs["private-website"]
        self.assertIn("timeout-minutes: 3", aggregate)
        self.assertIn("id: aggregate\n        if: always() && !cancelled()", aggregate)
        self.assertIn(
            "- name: Reject cancelled workflow\n        if: cancelled()\n        run: exit 1",
            aggregate,
        )
        self.assertNotIn("WORKFLOW_CANCELLED", aggregate)
        self.assertNotIn("continue-on-error", aggregate)
        self.assertNotIn("${{", self.aggregate)
        for suite, prefix in (("oss", "OSS"), ("browser", "BROWSER")):
            block = self.jobs["private-website-" + suite]
            self.assertNotIn("needs:", block)
            for output, expression in (
                ("scope_run", "steps.scope.outputs.run"),
                ("gate_outcome", "steps.website.outcome"),
                ("source_sha", "steps.source.outputs.sha"),
                ("source_tree", "steps.source.outputs.tree"),
            ):
                with self.subTest(suite=suite, output=output):
                    self.assertIn(output + ": ${{ " + expression + " }}", block)
            for variable, expression in (
                ("RESULT", "result"),
                ("SCOPE_RUN", "outputs.scope_run"),
                ("GATE_OUTCOME", "outputs.gate_outcome"),
                ("SOURCE_SHA", "outputs.source_sha"),
                ("SOURCE_TREE", "outputs.source_tree"),
            ):
                self.assertIn(
                    prefix
                    + "_"
                    + variable
                    + ": ${{ needs.private-website-"
                    + suite
                    + "."
                    + expression
                    + " }}",
                    aggregate,
                )
        for variable, expression in (
            ("EXPECTED_SHA", "steps.source.outputs.sha"),
            ("EXPECTED_TREE", "steps.source.outputs.tree"),
        ):
            self.assertIn(variable + ": ${{ " + expression + " }}", aggregate)

    def test_affected_and_confirmed_unaffected_cases_pass_with_distinct_reports(self):
        for event in ("pull_request", "push", "workflow_dispatch"):
            with self.subTest(event=event):
                values = self.environment()
                values["GITHUB_EVENT_NAME"] = event
                result = self.run_aggregate(values)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("Both website suites passed", result.stdout)
        result = self.run_aggregate(self.environment(unaffected=True))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Confirmed unaffected", result.stdout)

    def test_failed_cancelled_skipped_or_unknown_jobs_cannot_pass(self):
        for key, value, unaffected in product(
            ("OSS_RESULT", "BROWSER_RESULT"),
            ("failure", "cancelled", "skipped", "", "unknown"),
            (False, True),
        ):
            with self.subTest(key=key, value=value, unaffected=unaffected):
                values = self.environment(unaffected=unaffected)
                values[key] = value
                self.assertNotEqual(0, self.run_aggregate(values).returncode)

    def test_successful_jobs_still_require_the_actual_suite_step_outcomes(self):
        for unaffected in (False, True):
            expected = "skipped" if unaffected else "success"
            for key, value in product(
                ("OSS_GATE_OUTCOME", "BROWSER_GATE_OUTCOME"),
                ("success", "failure", "cancelled", "skipped", "", "unknown"),
            ):
                if value == expected:
                    continue
                with self.subTest(key=key, value=value, unaffected=unaffected):
                    values = self.environment(unaffected=unaffected)
                    values[key] = value
                    self.assertNotEqual(0, self.run_aggregate(values).returncode)

    def test_scopes_must_agree_and_use_the_two_valid_boolean_values(self):
        for oss, browser in product(
            ("true", "false", "", "unknown", "True", "0"), repeat=2
        ):
            if (oss, browser) in (("true", "true"), ("false", "false")):
                continue
            with self.subTest(oss=oss, browser=browser):
                values = self.environment()
                values.update(OSS_SCOPE_RUN=oss, BROWSER_SCOPE_RUN=browser)
                self.assertNotEqual(0, self.run_aggregate(values).returncode)

    def test_unaffected_requires_a_pull_request(self):
        for event in ("push", "workflow_dispatch", "", "unknown"):
            with self.subTest(event=event):
                values = self.environment(unaffected=True)
                values["GITHUB_EVENT_NAME"] = event
                self.assertNotEqual(0, self.run_aggregate(values).returncode)

    def test_source_evidence_must_match_for_both_scope_paths(self):
        for unaffected in (False, True):
            for key, value in product(
                (
                    "EXPECTED_SHA",
                    "OSS_SOURCE_SHA",
                    "BROWSER_SOURCE_SHA",
                    "EXPECTED_TREE",
                    "OSS_SOURCE_TREE",
                    "BROWSER_SOURCE_TREE",
                ),
                ("", "c" * 40, "invalid"),
            ):
                with self.subTest(key=key, value=value, unaffected=unaffected):
                    values = self.environment(unaffected=unaffected)
                    values[key] = value
                    self.assertNotEqual(0, self.run_aggregate(values).returncode)
        for keys, value in product(
            (
                ("GITHUB_SHA", "EXPECTED_SHA", "OSS_SOURCE_SHA", "BROWSER_SOURCE_SHA"),
                ("EXPECTED_TREE", "OSS_SOURCE_TREE", "BROWSER_SOURCE_TREE"),
            ),
            ("", "a" * 39, "g" * 40, "A" * 40),
        ):
            with self.subTest(keys=keys, value=value):
                values = self.environment()
                values.update(dict.fromkeys(keys, value))
                self.assertNotEqual(0, self.run_aggregate(values).returncode)

    def test_missing_evidence_cannot_pass(self):
        for unaffected in (False, True):
            values = self.environment(unaffected=unaffected)
            for key in values:
                if key == "GITHUB_EVENT_NAME" and not unaffected:
                    continue
                with self.subTest(missing=key, unaffected=unaffected):
                    self.assertNotEqual(
                        0, self.run_aggregate(values, missing=(key,)).returncode
                    )

    def test_source_steps_read_actual_git_head_and_tree_before_emitting_outputs(self):
        with tempfile.TemporaryDirectory(prefix="website-ci-source-") as raw:
            root = Path(raw)
            for arguments in (
                ("init", "--quiet"),
                (
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@invalid",
                    "commit",
                    "--allow-empty",
                    "--quiet",
                    "-m",
                    "Fixture",
                ),
            ):
                subprocess.run(
                    ["git", "-C", str(root), *arguments],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            sha = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, timeout=10
            ).strip()
            tree = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
                text=True,
                timeout=10,
            ).strip()
            for name in (
                "private-website-oss",
                "private-website-browser",
                "private-website",
            ):
                blocks = [
                    block
                    for block in literal_run_blocks(self.jobs[name])
                    if "git rev-parse HEAD" in block
                ]
                self.assertEqual(1, len(blocks), name)
                for expected in (sha, "f" * 40):
                    with self.subTest(job=name, expected=expected):
                        output = root / (name + "." + expected + ".outputs")
                        environment = dict(
                            os.environ, GITHUB_SHA=expected, GITHUB_OUTPUT=str(output)
                        )
                        result = subprocess.run(
                            ["bash", "-c", blocks[0]],
                            cwd=root,
                            env=environment,
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=5,
                        )
                        if expected == sha:
                            self.assertEqual(0, result.returncode, result.stderr)
                            self.assertEqual(
                                {"sha": sha, "tree": tree},
                                dict(
                                    line.split("=", 1)
                                    for line in output.read_text().splitlines()
                                ),
                            )
                            self.assertIn(
                                "CI_SOURCE sha=" + sha + " tree=" + tree, result.stdout
                            )
                        else:
                            self.assertNotEqual(0, result.returncode)
                            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
