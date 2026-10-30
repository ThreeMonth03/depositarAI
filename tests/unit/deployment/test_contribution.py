"""Treat contribution bodies as bounded Markdown data using only the stdlib."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.ci import contribution


REPOSITORY = "example/project"
PR_FIELDS = {
    "Issue": "Related to #7",
    "Motivation": "A year refinement should preserve the selected format.",
    "Method": "Apply the validated time patch to the committed search state.",
    "Results": "The state-patch regression passes; deployment acceptance is pending.",
}
ISSUE_FIELDS = {
    "Work type": "Bug",
    "Parent issue": "None",
    "Motivation": "The CSV format disappears after a year refinement.",
    "Example / bug reproduction": 'New session: "Climate CSV"; then "Use 2024".',
    "Expected outcome": "The topic and CSV remain selected while the year changes.",
    "Scope and coordination": "Owner: search; writes: state patch; resources: CPU tests.",
    "Acceptance criteria": "- [ ] AC-1: The follow-up retains topic and format.",
}


def pr_body(**changes):
    values = {**PR_FIELDS, **changes}
    return "\n\n".join("## " + name + "\n" + value for name, value in values.items())


def issue_body(**changes):
    values = {**ISSUE_FIELDS, **changes}
    return "\n\n".join("### " + name + "\n" + value for name, value in values.items())


class ContributionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-contribution-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def event(self, *, kind="pull_request", body=None):
        return {
            "repository": {"full_name": REPOSITORY},
            kind: {"body": pr_body() if body is None else body, "number": 9},
        }

    def cli(self, value, *, kind="pull_request", raw=False):
        path = self.root / "event.json"
        path.write_bytes(value if raw else json.dumps(value).encode())
        return self.invoke(
            ["--input", str(path), "--repository", REPOSITORY, "--kind", kind]
        )

    def invoke(self, args=(), environment=None):
        output = io.StringIO()
        with (
            patch.dict(os.environ, environment or {}, clear=True),
            redirect_stdout(output),
        ):
            code = contribution.main(list(args))
        value = json.loads(output.getvalue())
        self.assertEqual({"status", "issues"}, set(value))
        self.assertIsInstance(value["status"], str)
        self.assertIsInstance(value["issues"], list)
        self.assertTrue(
            all(isinstance(issue, str) and issue for issue in value["issues"])
        )
        return code, value, output.getvalue()

    def test_pr_accepts_bare_local_and_exact_url_references(self):
        for reference in (
            "Related to #7",
            "Closes https://github.com/example/project/issues/7",
            "[Delivered Issue](https://github.com/example/project/issues/7)",
            "See [external context](https://example.test/) and #7.",
        ):
            with self.subTest(reference=reference):
                self.assertEqual(
                    [], contribution.validate_pr(pr_body(Issue=reference), REPOSITORY)
                )

    def test_negated_partial_delivery_cannot_hide_a_closing_directive(self):
        body = pr_body(
            Issue="Related to #123",
            Results="This work does not close #123.",
        )
        self.assertIn(
            "pr_closing_directive_invalid", contribution.validate_pr(body, REPOSITORY)
        )

    def test_closing_keywords_are_standalone_positive_lines_in_the_issue_section(self):
        for keyword in (
            "close",
            "closes",
            "closed",
            "fix",
            "fixes",
            "fixed",
            "resolve",
            "resolves",
            "resolved",
        ):
            for spelling in (keyword, keyword.upper(), keyword.title() + ":"):
                for reference in (
                    "#123",
                    "https://github.com/example/project/issues/123",
                ):
                    with self.subTest(spelling=spelling, reference=reference):
                        body = pr_body(Issue=spelling + " " + reference)
                        self.assertEqual([], contribution.validate_pr(body, REPOSITORY))
        self.assertEqual(
            [],
            contribution.validate_pr(
                pr_body(Issue="Related to #123\n\nCloses: #124.\nFixes #125"),
                REPOSITORY,
            ),
        )

    def test_embedded_outside_and_conditional_closing_directives_are_rejected(self):
        for body in (
            pr_body(Issue="This change closes #123."),
            pr_body(Issue="Closes #123 if the remaining work is accepted."),
            pr_body(Issue="Closes #123, fixes #124"),
            pr_body(Issue="- Closes #123"),
            pr_body(Issue="> Closes #123"),
            pr_body(Issue="**Closes #123**"),
            "Closes #123\n\n" + pr_body(),
            pr_body(Method="Closes #123"),
            pr_body(Results="Fixed: #123"),
            pr_body() + "\n\n## Other\nResolves #123",
            pr_body() + "\n\n## Closes #123\nAn unrelated section.",
        ):
            with self.subTest(body=body):
                self.assertIn(
                    "pr_closing_directive_invalid",
                    contribution.validate_pr(body, REPOSITORY),
                )

    def test_split_and_other_reference_forms_cannot_bypass_closing_placement(self):
        for directive in (
            "Closes\n#123",
            "CLOSES:\n\n#123",
            "This does not close example/project#123.",
            "This does not resolve other/project#123.",
            "This does not fix https://github.com/example/project/issues/123.",
            "This does not fix <https://github.com/example/project/issues/123>.",
            "This does not close [#123](https://example.test/).",
            "This does not close [delivered](https://github.com/example/project/issues/123).",
        ):
            with self.subTest(directive=directive):
                self.assertIn(
                    "pr_closing_directive_invalid",
                    contribution.validate_pr(pr_body(Results=directive), REPOSITORY),
                )

    def test_closing_examples_remain_data_in_existing_markdown_boundaries(self):
        for example in (
            "```text\nThis does not close #123.\n```",
            "~~~text\nCloses #123\n~~~",
            "<!-- This does not close #123. -->\nActual result is retained.",
            "The literal example is `does not close #123`.",
            "The literal example is ``does not close #123``.",
            "    This does not close #123.\nActual result is retained.",
        ):
            with self.subTest(example=example):
                self.assertEqual(
                    [], contribution.validate_pr(pr_body(Results=example), REPOSITORY)
                )

    def test_unmatched_or_escaped_code_and_code_suffix_cannot_hide_closing_prose(self):
        for prose in (
            "An unmatched ` span does not close #123.",
            "An unmatched ` span\nThis does not close #123.",
            "An unmatched ` span\n\nThis does not close #123.\n\nAnother `.",
            "An unmatched ` span\n## Other\nThis does not close #123.\nAnother `.",
            "Escaped \\` text does not close #123 \\`.",
        ):
            with self.subTest(prose=prose):
                self.assertIn(
                    "pr_closing_directive_invalid",
                    contribution.validate_pr(pr_body(Results=prose), REPOSITORY),
                )
        self.assertIn(
            "pr_closing_directive_invalid",
            contribution.validate_pr(
                pr_body(Issue="Closes #123 `only later`"), REPOSITORY
            ),
        )

    def test_closing_guard_cli_returns_fixed_diagnostics_without_echoing_prose(self):
        prose = "private-canary does not close #123."
        code, result, output = self.cli(self.event(body=pr_body(Results=prose)))
        self.assertEqual(
            (1, {"status": "failed", "issues": ["pr_closing_directive_invalid"]}),
            (code, result),
        )
        self.assertNotIn(prose, output)

    def test_long_closing_prose_and_link_labels_remain_bounded_data(self):
        for issue in (
            "Closes #123" + " " * 50_000 + "pending",
            "Related to #123\nCloses [" + "A" * 50_000 + "](https://example.test/)",
            "Related to #123\n`" + "A" * 50_000 + " does not close #123",
        ):
            with self.subTest(prefix=issue[:20]):
                problems = contribution.validate_pr(pr_body(Issue=issue), REPOSITORY)
                self.assertEqual(
                    "https://example.test/" not in issue,
                    "pr_closing_directive_invalid" in problems,
                )

    def test_pr_requires_each_nonempty_section(self):
        for heading in PR_FIELDS:
            with self.subTest(heading=heading):
                issues = contribution.validate_pr(pr_body(**{heading: ""}), REPOSITORY)
                self.assertIn("pr_" + heading.lower() + "_missing", issues)

    def test_placeholders_and_hidden_comments_do_not_fill_required_sections(self):
        for value in (
            "TODO",
            "TBD",
            "N/A",
            "None",
            "_No response_",
            "...",
            "-",
            "[ ]",
            "<!-- Real-looking explanation that is invisible in the PR. -->",
        ):
            with self.subTest(value=value):
                self.assertIn(
                    "pr_motivation_missing",
                    contribution.validate_pr(pr_body(Motivation=value), REPOSITORY),
                )

    def test_headings_are_casefolded_but_require_the_declared_level_and_space(self):
        body = pr_body().replace("## Motivation", "## mOtIvAtIoN")
        self.assertEqual([], contribution.validate_pr(body, REPOSITORY))
        for replacement in ("##Motivation", "# Motivation", "### Motivation"):
            with self.subTest(replacement=replacement):
                self.assertIn(
                    "pr_motivation_missing",
                    contribution.validate_pr(
                        pr_body().replace("## Motivation", replacement), REPOSITORY
                    ),
                )

    def test_code_comments_and_external_markdown_labels_cannot_supply_issue_links(self):
        for reference in (
            "`#7`",
            "```text\n#7\n```",
            "~~~text\n#7\n~~~",
            "<!-- Closes #7 -->",
            "[ #7 ](https://example.test/)",
            "![#7](https://github.com/example/project/issues/7)",
            "    Related to #7",
            "#0",
            "#-7",
        ):
            with self.subTest(reference=reference):
                self.assertIn(
                    "pr_issue_link_missing",
                    contribution.validate_pr(pr_body(Issue=reference), REPOSITORY),
                )

    def test_issue_urls_must_target_the_same_repository_and_exact_issue_path(self):
        for reference in (
            "https://github.com/other/project/issues/7",
            "http://github.com/example/project/issues/7",
            "https://github.com.evil.test/example/project/issues/7",
            "https://user@github.com/example/project/issues/7",
            "https://github.com/example/project/pull/7",
            "https://github.com/example/project/issues/7?token=example",
            "https://github.com/example/project/issues/7#comment-1",
            "https://github.com/example/project/issues/7/extra",
        ):
            with self.subTest(reference=reference):
                self.assertIn(
                    "pr_issue_link_missing",
                    contribution.validate_pr(pr_body(Issue=reference), REPOSITORY),
                )

    def test_fenced_and_commented_headings_cannot_forge_missing_sections(self):
        body = pr_body(**{"Motivation": ""})
        for wrapped in (
            "```markdown\n## Motivation\nA convincing reason.\n```",
            "<!--\n## Motivation\nA convincing reason.\n-->",
        ):
            with self.subTest(wrapped=wrapped):
                self.assertIn(
                    "pr_motivation_missing",
                    contribution.validate_pr(body + "\n\n" + wrapped, REPOSITORY),
                )

    def test_duplicate_sections_are_rejected(self):
        self.assertIn(
            "pr_duplicate_section",
            contribution.validate_pr(
                pr_body() + "\n\n## Results\nAnother result.", REPOSITORY
            ),
        )

    def test_issue_supports_top_level_work_and_linked_subissues(self):
        for kind in ("Bug", "Change"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    [],
                    contribution.validate_issue(
                        issue_body(**{"Work type": kind}), REPOSITORY, 9
                    ),
                )
        for parent in ("#7", "https://github.com/example/project/issues/7"):
            with self.subTest(parent=parent):
                self.assertEqual(
                    [],
                    contribution.validate_issue(
                        issue_body(
                            **{"Work type": "Sub-issue", "Parent issue": parent}
                        ),
                        REPOSITORY,
                        9,
                    ),
                )

    def test_issue_requires_all_sections_and_a_supported_work_type(self):
        codes = {
            "Work type": "work_type",
            "Motivation": "motivation",
            "Example / bug reproduction": "example",
            "Expected outcome": "expected_outcome",
            "Scope and coordination": "scope",
            "Acceptance criteria": "acceptance",
            "Parent issue": "parent",
        }
        for heading, code in codes.items():
            with self.subTest(heading=heading):
                self.assertIn(
                    "issue_" + code + "_missing",
                    contribution.validate_issue(
                        issue_body(**{heading: ""}), REPOSITORY
                    ),
                )
        self.assertIn(
            "issue_work_type_invalid",
            contribution.validate_issue(
                issue_body(**{"Work type": "Task"}), REPOSITORY
            ),
        )

    def test_subissue_parent_is_local_nonself_and_cannot_be_none_or_code(self):
        for parent in (
            "None",
            "#9",
            "`#7`",
            "```\n#7\n```",
            "https://github.com/other/project/issues/7",
            "[#7](https://example.test/)",
        ):
            with self.subTest(parent=parent):
                self.assertTrue(
                    contribution.validate_issue(
                        issue_body(
                            **{"Work type": "Sub-issue", "Parent issue": parent}
                        ),
                        REPOSITORY,
                        9,
                    )
                )
        for number in (True, 0, -1, "9"):
            with self.subTest(number=number):
                self.assertIn(
                    "issue_number_invalid",
                    contribution.validate_issue(issue_body(), REPOSITORY, number),
                )

    def test_code_examples_remain_data_including_literal_html_comments(self):
        for example in (
            '```python\nprint("small reproduction")\n```',
            "```html\n<!-- literal markup in the reported input -->\n```",
            "```diff\n- clear formats\n+ keep formats\n```",
        ):
            with self.subTest(example=example):
                self.assertEqual(
                    [],
                    contribution.validate_issue(
                        issue_body(**{"Example / bug reproduction": example}),
                        REPOSITORY,
                    ),
                )

    def test_manual_cli_validates_both_event_kinds_with_structured_output(self):
        for kind, body in (("pull_request", pr_body()), ("issue", issue_body())):
            with self.subTest(kind=kind):
                code, result, _ = self.cli(self.event(kind=kind, body=body), kind=kind)
                self.assertEqual(
                    (0, {"status": "passed", "issues": []}), (code, result)
                )

    def test_invalid_metadata_and_empty_github_body_use_exit_one(self):
        for body in ("", pr_body(Method="TODO"), None):
            with self.subTest(body=body):
                event = self.event()
                event["pull_request"]["body"] = body
                code, result, _ = self.cli(event)
                self.assertEqual(1, code)
                self.assertEqual("failed", result["status"])
                self.assertTrue(result["issues"])

    def test_malformed_duplicate_and_nonfinite_json_or_event_shape_use_exit_two(self):
        encoded_body = json.dumps(pr_body())
        for raw in (
            b"{",
            b"[]",
            b"{}",
            b'{"repository":[]} ',
            (
                '{"pull_request":{"body":'
                + encoded_body
                + ',"body":'
                + encoded_body
                + "}}"
            ).encode(),
            ('{"pull_request":{"body":' + encoded_body + '},"ignored":NaN}').encode(),
        ):
            with self.subTest(raw=raw):
                code, result, _ = self.cli(raw, raw=True)
                self.assertEqual((2, "invalid_input"), (code, result["status"]))
        for event in (
            {"pull_request": {}},
            {"pull_request": []},
            self.event() | {"repository": {"full_name": "another/project"}},
        ):
            with self.subTest(event=event):
                self.assertEqual(2, self.cli(event)[0])

    def test_byte_bounds_and_invalid_repository_are_rejected(self):
        self.assertEqual(
            ["body_invalid"],
            contribution.validate_pr(
                pr_body() + "x" * contribution.MAX_INPUT_BYTES, REPOSITORY
            ),
        )
        self.assertEqual(
            2,
            self.cli(self.event(body=pr_body() + "x" * contribution.MAX_INPUT_BYTES))[
                0
            ],
        )
        path = self.root / "event.json"
        event = self.event()
        event.pop("repository")
        path.write_text(json.dumps(event))
        for repository in (
            "missing-slash",
            "example/..",
            "example/project;touch marker",
        ):
            with self.subTest(repository=repository):
                code, result, output = self.invoke(
                    [
                        "--input",
                        str(path),
                        "--repository",
                        repository,
                        "--kind",
                        "pull_request",
                    ]
                )
                self.assertEqual((2, "invalid_input"), (code, result["status"]))
                self.assertNotIn(repository, output)

    def test_automatic_pr_event_and_explicit_event_path_share_the_validator(self):
        path = self.root / "event.json"
        path.write_text(json.dumps(self.event()))
        environment = {
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": str(path),
            "GITHUB_REPOSITORY": REPOSITORY,
        }
        self.assertEqual(0, self.invoke(environment=environment)[0])
        environment["GITHUB_EVENT_PATH"] = str(self.root / "missing.json")
        self.assertEqual(0, self.invoke(["--event", str(path)], environment)[0])

    def test_unrelated_automatic_events_skip_without_reading_any_file(self):
        for name in ("push", "issues", "workflow_dispatch", ""):
            environment = {
                "GITHUB_EVENT_NAME": name,
                "GITHUB_EVENT_PATH": "/untrusted/not-read",
                "GITHUB_REPOSITORY": "invalid-repository",
            }
            with (
                self.subTest(name=name),
                patch.object(
                    Path, "open", side_effect=AssertionError("unexpected event read")
                ),
            ):
                code, result, _ = self.invoke(environment=environment)
                self.assertEqual(
                    (0, {"status": "skipped", "issues": []}), (code, result)
                )

    def test_shell_like_examples_are_never_executed_fetched_or_echoed(self):
        marker = self.root / "must-not-exist"
        payload = "$(touch " + str(marker) + ") `touch " + str(marker) + "`"
        body = pr_body(Method="```sh\n" + payload + "\n```")
        with (
            patch(
                "os.system", side_effect=AssertionError("unexpected shell execution")
            ),
            patch(
                "subprocess.Popen",
                side_effect=AssertionError("unexpected child process"),
            ),
            patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("unexpected URL fetch"),
            ),
            patch(
                "socket.create_connection",
                side_effect=AssertionError("unexpected network"),
            ),
        ):
            self.assertEqual([], contribution.validate_pr(body, REPOSITORY))
            code, result, output = self.cli(self.event(body=body))
        self.assertEqual((0, {"status": "passed", "issues": []}), (code, result))
        self.assertFalse(marker.exists())
        self.assertNotIn(payload, output)
        self.assertNotIn(str(marker), output)

    def test_long_unmatched_markdown_and_repeated_comments_remain_plain_data(self):
        for value in (
            "A" * 50_000,
            "[" * 50_000,
            "`" * 50_000 + "unclosed literal",
            "A" * 50_000 + "://github.com/example/project/issues/7",
        ):
            with self.subTest(prefix=value[:20]):
                self.assertIn(
                    "pr_issue_link_missing",
                    contribution.validate_pr(pr_body(Issue=value), REPOSITORY),
                )
        self.assertEqual(
            [],
            contribution.validate_pr(
                pr_body(Method="visible <!-- hidden --> " * 5_000), REPOSITORY
            ),
        )


if __name__ == "__main__":
    unittest.main()
