"""Shared release identity; refuse dirty production bytes without leaking output."""

from pathlib import Path
import re
import subprocess


MAX_OUTPUT_BYTES = 64 * 1024
# Runtime and every currently used operator helper, not research/data artifacts.
SOURCE_PATHS = (
    "Makefile",
    "search",
    "web",
    "deploy",
    "scripts/depositarai.py",
    "scripts/search-web.sh",
    "scripts/read_env.py",
    "scripts/search-lib.sh",
    "scripts/bootstrap-catalog.sh",
    "scripts/search_dictionary_permissions.py",
    "scripts/search_model_source.py",
    "scripts/validate_catalog_compose.py",
    "scripts/validate_search_web.py",
    "scripts/prepare_search_models.py",
)


def _runner(argv, *, timeout):
    return subprocess.run(
        argv,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _output(runner, argv):
    try:
        result = runner(argv, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("product_source_unavailable") from error
    if (
        result.returncode != 0
        or type(result.stdout) is not str
        or len(result.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES
    ):
        raise ValueError("product_source_unavailable")
    return result.stdout.strip()


def require_clean_revision(repo_root: Path, runner=None) -> str:
    """Return one exact clean Git revision; no mutation, shell, or raw errors.

    runner(argv, *, timeout) returns CompletedProcess, matching doctor injection.
    Secrets/runtime mounts are ignored and never read; untracked production
    source is intentionally included rather than silently omitted from release.
    """
    runner = _runner if runner is None else runner
    prefix = ["git", "-C", str(Path(repo_root))]
    revision = _output(runner, prefix + ["rev-parse", "HEAD"])
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("product_source_revision_invalid")
    if _output(
        runner,
        prefix
        + ["status", "--porcelain", "--untracked-files=all", "--", *SOURCE_PATHS],
    ):
        raise ValueError("product_source_dirty")
    return revision
