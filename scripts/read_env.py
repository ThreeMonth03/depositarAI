#!/usr/bin/env python3
"""Parse the project's deliberately small, shell-free dotenv format."""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path
from typing import NoReturn


KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def fail(path: Path, line_number: int, message: str) -> NoReturn:
    raise ValueError(f"{path}:{line_number}: {message}")


def strip_unquoted_comment(value: str) -> str:
    for index, character in enumerate(value):
        if character == "#" and index > 0 and value[index - 1].isspace():
            return value[:index].rstrip()
    return value.strip()


def parse_quoted(path: Path, line_number: int, raw: str, quote: str) -> str:
    escaped = False
    closing_index = None
    for index in range(1, len(raw)):
        character = raw[index]
        if quote == '"' and character == "\\" and not escaped:
            escaped = True
            continue
        if character == quote and not escaped:
            closing_index = index
            break
        escaped = False

    if closing_index is None:
        fail(path, line_number, "unterminated quoted value")

    remainder = raw[closing_index + 1 :].strip()
    if remainder and not remainder.startswith("#"):
        fail(path, line_number, "unexpected text after quoted value")

    value = raw[1:closing_index]
    if quote == "'":
        return value

    # Docker Compose recognizes these escapes inside double quotes. Keeping the
    # same small set avoids shell evaluation while preserving common secrets.
    replacements = {
        r"\n": "\n",
        r"\r": "\r",
        r"\t": "\t",
        r"\"": '"',
        r"\\": "\\",
    }
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            token = value[index : index + 2]
            if token not in replacements:
                fail(path, line_number, f"unsupported escape {token!r}")
            result.append(replacements[token])
            index += 2
        else:
            result.append(value[index])
            index += 1
    return "".join(result)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            fail(path, line_number, "expected KEY=VALUE")
        key, raw = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.fullmatch(key):
            fail(path, line_number, f"invalid key {key!r}")

        raw = raw.lstrip()
        if raw.startswith("'"):
            value = parse_quoted(path, line_number, raw, "'")
        elif raw.startswith('"'):
            value = parse_quoted(path, line_number, raw, '"')
        else:
            value = strip_unquoted_comment(raw)
            if any(character.isspace() for character in value):
                fail(path, line_number, "values containing spaces must be quoted")
        values[key] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        values = parse_env(args.path)
    except (OSError, UnicodeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1

    for key, value in values.items():
        print(f"export {key}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
