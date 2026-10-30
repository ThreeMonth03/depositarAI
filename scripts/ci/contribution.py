"""Validate contribution metadata as bounded data, without resolving its links."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re


MAX_INPUT_BYTES = 1024**2
PR_SECTIONS = {
    name.lower(): name.lower() for name in ("Issue", "Motivation", "Method", "Results")
}
ISSUE_SECTIONS = {
    "work type": "work_type",
    "motivation": "motivation",
    "example / bug reproduction": "example",
    "expected outcome": "expected_outcome",
    "scope and coordination": "scope",
    "acceptance criteria": "acceptance",
    "parent issue": "parent",
}
PLACEHOLDERS = {
    "todo",
    "tbd",
    "n/a",
    "na",
    "none",
    "no response",
    "...",
    "…",
    "",
    "-",
    "[ ]",
}
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}\Z")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s<>()]+")


def _visible(line, comment):
    parts, position = [], 0
    while position < len(line):
        if comment:
            end = line.find("-->", position)
            if end < 0:
                return "".join(parts), True
            position, comment = end + 3, False
        else:
            start = line.find("<!--", position)
            if start < 0:
                parts.append(line[position:])
                break
            parts.append(line[position:start])
            position, comment = start + 4, True
    return "".join(parts), comment


def _sections(body, level):
    sections, current, fence, comment, duplicate = {}, None, None, False, False
    for raw in body.splitlines():
        if fence is not None:
            marker = FENCE.fullmatch(raw)
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and not marker[2].strip()
            ):
                fence = None
            elif current is not None:
                sections[current][0].append(raw)
            continue
        line, comment = _visible(raw, comment)
        marker = FENCE.fullmatch(line)
        if marker:
            fence = (marker[1][0], len(marker[1]))
            continue
        title = line.lstrip(" ")
        prefix = "#" * level
        if len(line) - len(title) <= 3 and title.startswith(
            (prefix + " ", prefix + "\t")
        ):
            title = title[len(prefix) :].strip()
            without_hashes = title.rstrip("#")
            if without_hashes and without_hashes[-1].isspace():
                title = without_hashes.rstrip()
            current = title.casefold()
            duplicate = duplicate or current in sections
            sections.setdefault(current, ([], []))
        elif current is not None:
            sections[current][0].append(line)
            if not line.startswith(("    ", "\t")):
                sections[current][1].append(line)
    return sections, duplicate


def _field(lines):
    return "\n".join(lines).strip().strip("*_` ")


def _nonempty(lines):
    for line in lines:
        text = re.sub(r"^\s*(?:[-*+]\s+)?(?:\[[ xX]\]\s*)?", "", line)
        if text.strip("*_` ").casefold() not in PLACEHOLDERS:
            return True
    return False


def _without_inline_code(text):
    # Only matching backtick runs close a span; unmatched spans hide references.
    parts, position, delimiter = [], 0, None
    while position < len(text):
        if text[position] == "`":
            end = position + 1
            while end < len(text) and text[end] == "`":
                end += 1
            length = end - position
            if delimiter is None:
                delimiter = length
            elif length == delimiter:
                delimiter = None
            parts.append(" ")
            position = end
        else:
            if delimiter is None:
                parts.append(text[position])
            position += 1
    return "".join(parts)


def _references(lines, repository):
    text = _without_inline_code("\n".join(lines))
    references = set()
    pattern = re.compile(
        r"https://github\.com/" + re.escape(repository) + r"/issues/([1-9][0-9]*)\Z",
        re.IGNORECASE,
    )

    def record(url):
        match = pattern.fullmatch(url.rstrip(".,;:"))
        if match:
            references.add(match[1])

    def markdown(match):
        if not match[0].startswith("!"):
            target = match[1].strip().split()
            if target:
                record(target[0])
        return " "

    text = re.sub(r"!?\[[^\[\]\n]*\]\(([^()\[\]\n]*)\)", markdown, text)
    text = re.sub(r"\[[^\[\]\n]*\]\[[^\[\]\n]*\]", " ", text)
    for match in URL.finditer(text):
        record(match[0])
    text = URL.sub(" ", text)
    references.update(re.findall(r"(?<![\w/#])#([1-9][0-9]*)(?!\w)", text))
    return references


def _validate(body, repository, kind):
    if (
        type(repository) is not str
        or REPOSITORY.fullmatch(repository) is None
        or repository.split("/")[1] in {".", ".."}
    ):
        return ["repository_invalid"], {}
    if type(body) is not str:
        return ["body_invalid"], {}
    try:
        if len(body.encode("utf-8", "strict")) > MAX_INPUT_BYTES:
            return ["body_invalid"], {}
    except UnicodeError:
        return ["body_invalid"], {}
    names = PR_SECTIONS if kind == "pr" else ISSUE_SECTIONS
    sections, duplicate = _sections(body, 2 if kind == "pr" else 3)
    issues = [kind + "_duplicate_section"] if duplicate else []
    for name, code in names.items():
        content = sections.get(name, ([], []))[0]
        if not _nonempty(content) and not (
            kind == "issue"
            and name == "parent issue"
            and _field(content).casefold() == "none"
        ):
            issues.append(kind + "_" + code + "_missing")
    return issues, sections


def validate_pr(body, repository):
    """Return fixed metadata issues for a pull-request body, without fetching it."""
    issues, sections = _validate(body, repository, "pr")
    if not issues or sections:
        if not _references(sections.get("issue", ([], []))[1], repository):
            issues.append("pr_issue_link_missing")
    return sorted(set(issues))


def validate_issue(body, repository, number=None):
    """Check issue structure and a same-repository parent for sub-issues."""
    issues, sections = _validate(body, repository, "issue")
    if not sections:
        return sorted(set(issues))
    work_type = _field(sections.get("work type", ([], []))[0])
    if work_type not in {"Bug", "Change", "Sub-issue"}:
        issues.append("issue_work_type_invalid")
    parent = sections.get("parent issue", ([], []))
    references = _references(parent[1], repository)
    if not references and (
        work_type == "Sub-issue" or _field(parent[0]).casefold() != "none"
    ):
        issues.append("issue_parent_link_missing")
    if number is not None:
        if type(number) is not int or number <= 0:
            issues.append("issue_number_invalid")
        elif str(number) in references:
            issues.append("issue_parent_self")
    return sorted(set(issues))


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--input", type=Path)
    inputs.add_argument("--event", type=Path)
    parser.add_argument("--repository")
    parser.add_argument("--kind", choices=("issue", "pull_request"))
    args = parser.parse_args(argv)
    if args.kind is None and os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        print(json.dumps({"status": "skipped", "issues": []}))
        return 0
    try:
        path = args.input or args.event or Path(os.environ["GITHUB_EVENT_PATH"])
        with path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError
        event = json.loads(
            raw, object_pairs_hook=_object, parse_constant=_invalid_constant
        )
        if type(event) is not dict:
            raise ValueError
        metadata = event.get("repository", {})
        repository = (
            args.repository
            or os.environ.get("GITHUB_REPOSITORY")
            or metadata.get("full_name")
        )
        if (
            type(repository) is not str
            or REPOSITORY.fullmatch(repository) is None
            or repository.split("/")[1] in {".", ".."}
        ):
            raise ValueError
        if type(metadata) is not dict or (
            metadata.get("full_name") is not None
            and metadata["full_name"].casefold() != repository.casefold()
        ):
            raise ValueError
        kind = args.kind or "pull_request"
        payload = event[kind]
        if type(payload) is not dict or "body" not in payload:
            raise ValueError
        issues = (
            validate_pr(payload["body"], repository)
            if kind == "pull_request"
            else validate_issue(payload["body"], repository, payload.get("number"))
        )
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
    ):
        print(json.dumps({"status": "invalid_input", "issues": ["event_invalid"]}))
        return 2
    print(json.dumps({"status": "failed" if issues else "passed", "issues": issues}))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
