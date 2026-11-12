#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$project_dir"
export PYTHONDONTWRITEBYTECODE=1

action=${1:-check}
if [[ $# -gt 1 || ! "$action" =~ ^(install|check|format)$ ]]; then
  echo 'Usage: python-lint.sh [install|check|format]' >&2
  exit 2
fi

tool_policy() {
  python3 -I -B - search/api/requirements-test.lock "$1" <<'PY'
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
import sys

selected = {}
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    match = re.match(r"^(ruff|vulture|tomli)==([^ ;]+)", line)
    if match is None:
        continue
    name, pinned = match.groups()
    if name in selected or not re.search(r" --hash=sha256:[0-9a-f]{64}$", line):
        raise SystemExit("Invalid lint tool requirements lock.")
    selected[name] = (pinned, line)
if not {"ruff", "vulture"} <= selected.keys():
    raise SystemExit("Lint tool pins are missing from the existing lock.")
if sys.argv[2] == "requirements":
    if sys.prefix == sys.base_prefix:
        raise SystemExit("Activate a Python virtual environment before install.")
    print("\n".join(line for _, line in selected.values()))
else:
    try:
        matched = all(
            version(name) == selected[name][0] for name in ("ruff", "vulture")
        )
    except PackageNotFoundError:
        matched = False
    if not matched:
        raise SystemExit("Install the locked lint tools before running checks.")
PY
}

if [[ "$action" == install ]]; then
  requirements_file=$(mktemp)
  trap 'rm -f "$requirements_file"' EXIT
  tool_policy requirements > "$requirements_file"
  python3 -I -B -m pip install --disable-pip-version-check --no-deps \
    --require-hashes -r "$requirements_file"
  tool_policy versions
  exit
fi

tool_policy versions
lint_roots=(search scripts tests deploy/docker)
if [[ "$action" == format ]]; then
  python3 -I -B -m ruff format --no-cache "${lint_roots[@]}"
  exit
fi

python3 -I -B -m ruff check --no-cache "${lint_roots[@]}"
python3 -I -B -m ruff format --check --no-cache "${lint_roots[@]}"
bash scripts/ci/python-unused.sh

complexity_report=${COMPLEXITY_REPORT:-}
if [[ -z "$complexity_report" ]]; then
  complexity_report=$(mktemp)
  trap 'rm -f "$complexity_report"' EXIT
fi
python3 -I -B scripts/ci/complexity.py --report "$complexity_report"
