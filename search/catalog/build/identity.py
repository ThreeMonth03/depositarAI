"""Bind metadata output to the complete, explicitly reviewed builder sources."""

import hashlib
from pathlib import Path

from . import codec, policy


SOURCE_FILES = tuple(
    sorted(
        (
            "search/__init__.py",
            "search/catalog/__init__.py",
            "search/catalog/policy.py",
            "search/catalog/resources.py",
            "search/catalog/build/__init__.py",
            "search/catalog/build/__main__.py",
            "search/catalog/build/policy.py",
            "search/catalog/build/normalize.py",
            "search/catalog/build/codec.py",
            "search/catalog/build/storage.py",
            "search/catalog/build/snapshot.py",
            "search/catalog/build/identity.py",
            "search/catalog/build/builder.py",
            "search/catalog/build/cli.py",
        )
    )
)
ROOT = Path(__file__).resolve().parents[3]


def source_digest() -> str:
    """Hash each producing input, including its relative path, in canonical JSON."""
    # A new helper requires an explicit closure review rather than silently
    # disappearing from the source identity.
    actual = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "search/catalog/build").rglob("*.py")
    }
    expected = {path for path in SOURCE_FILES if "/build/" in path}
    if actual != expected:
        raise policy.BundleError("metadata builder source closure mismatch")
    source_bytes = {path: (ROOT / path).read_bytes() for path in SOURCE_FILES}
    digests = {
        path: hashlib.sha256(source_bytes[path]).hexdigest() for path in SOURCE_FILES
    }
    return hashlib.sha256(codec.canonical_json(digests)).hexdigest()
