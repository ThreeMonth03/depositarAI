"""Gate accepted C901 domains and retain the complete native Ruff report."""

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
PRODUCT_PATHS = ("search", "scripts", "tests", "deploy/docker")
GATED_PATHS = (
    "search/planning",
    "search/api/retrieval",
    "search/api/conversation/calendar.py",
    "search/api/worker.py",
    "search/operations/cli.py",
    "search/operations/config.py",
    "search/operations/doctor.py",
    "search/operations/local_status.py",
    "search/operations/state.py",
    "search/operations/shared/runtime_probe.py",
    "search/operations/shared/bounded_process.py",
    "search/operations/catalog/policy.py",
    "search/catalog/session.py",
    "search/catalog/build/builder.py",
    "search/api/shared/middleware.py",
    "search/http",
    "search/web/app.py",
    "search/tests/web_fixture.py",
    "search/quality",
    "search/operations/deployment/candidate",
    "search/operations/deployment/provider",
    "search/operations/documents",
    "search/operations/jobs/archive.py",
    "search/operations/metadata/source.py",
    "search/operations/metadata/sync.py",
    "search/operations/deployment/frontend/policy.py",
    "search/operations/deployment/images/build.py",
    "search/operations/deployment/release/runtime.py",
    "search/operations/deployment/inference_runtime.py",
    "search/operations/deployment/credentials.py",
    "scripts/ci",
    "scripts/search_jobs.py",
    "scripts/search_model_source.py",
    "scripts/search_dictionary_permissions.py",
)


class ComplexityError(ValueError):
    """The complexity report did not come from a successful Ruff check."""


def run_ruff(command, paths, output_format):
    return subprocess.run(
        [
            *command,
            "check",
            "--no-cache",
            "--select",
            "C901",
            "--output-format",
            output_format,
            *paths,
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=120,
        check=False,
    )


def report_findings(command, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"")
    result = run_ruff(command, PRODUCT_PATHS, "json")
    destination.write_bytes(result.stdout)
    if result.returncode not in (0, 1):
        raise ComplexityError("Ruff could not produce the complexity report")
    try:
        findings = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise ComplexityError(
            "Ruff returned invalid complexity JSON"
        ) from None
    if type(findings) is not list or any(
        type(row) is not dict or row.get("code") != "C901" for row in findings
    ):
        raise ComplexityError("Ruff returned an invalid complexity report")
    if bool(findings) != (result.returncode == 1):
        raise ComplexityError("Ruff report disagrees with its exit status")
    return len(findings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--ruff", help="explicit Ruff executable; default: this Python's Ruff"
    )
    options = parser.parse_args(argv)
    command = (
        [options.ruff]
        if options.ruff is not None
        else [sys.executable, "-m", "ruff"]
    )
    try:
        count = report_findings(command, options.report)
        print(
            f"Whole-project C901 report: {count} findings (informational).",
            flush=True,
        )
        result = run_ruff(command, GATED_PATHS, "concise")
    except (ComplexityError, OSError, subprocess.TimeoutExpired) as error:
        print(f"Complexity check failed: {error}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    if result.returncode not in (0, 1):
        return 2
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
