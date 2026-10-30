"""Validate fixed job inputs and preserve their private, exact byte snapshots."""

import hashlib
import os
from pathlib import Path
import re

from search.operations.shared import private_io
from search.quality.contract import plan_digest, validate_cases
from search.serialization import strict_json

from .pueue import require


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
MAX_CONFIG_BYTES = 16 * 1024
MAX_CASE_BYTES = 64 * 1024
FIELDS = {
    "resource-pass": {"type", "bundle", "state_root", "plan_sha256", "manifest_sha256"},
    "quality-batch": {
        "type",
        "config",
        "config_sha256",
        "source_revision",
        "candidate_revision",
        "cases",
        "cases_sha256",
        "plan_sha256",
        "expected_identity_sha256",
    },
    "vector-pass": {
        "type",
        "config",
        "config_sha256",
        "source_revision",
        "catalog_revision",
        "bundle_id",
        "manifest_sha256",
        "expected_inputs_sha256",
    },
}


def validate(profile, root):
    require(
        type(profile) is dict
        and type(profile.get("type")) is str
        and profile["type"] in FIELDS
        and set(profile) == FIELDS[profile["type"]],
        "profile_invalid",
    )
    for key, value in profile.items():
        if key.endswith("_sha256") or key == "bundle_id":
            require(type(value) is str and SHA256.fullmatch(value), "profile_invalid")
        elif key.endswith("_revision"):
            require(type(value) is str and REVISION.fullmatch(value), "profile_invalid")
    for key in ("bundle", "state_root", "config", "cases"):
        if key not in profile:
            continue
        value = profile[key]
        require(
            type(value) is str
            and value.isascii()
            and all(32 <= ord(character) < 127 for character in value),
            "path_invalid",
        )
        target = Path(value)
        require(
            target.is_absolute()
            and ".." not in target.parts
            and target.as_posix() == value,
            "path_invalid",
        )
        require(
            root != target
            and root not in target.parents
            and target not in root.parents,
            "profile_overlaps_queue",
        )


def verify_source(profile, revision):
    if profile["type"] != "resource-pass":
        require(profile["source_revision"] == revision, "profile_source_changed")


def _read(filename, maximum):
    filename = Path(filename)
    fd = private_io.open_directory(filename.parent)
    try:
        raw = private_io.read_file(fd, filename.name, mode=0o600, max_bytes=maximum)
        private_io.verify_directory(fd, filename.parent)
        return raw
    finally:
        os.close(fd)


def frozen_names(profile):
    if profile["type"] == "resource-pass":
        return {}
    names = {"native-config.json": MAX_CONFIG_BYTES}
    if profile["type"] == "quality-batch":
        names["quality-cases.json"] = MAX_CASE_BYTES
    return names


def _check(profile, files):
    require(set(files) == set(frozen_names(profile)), "profile_inputs_invalid")
    if not files:
        return
    config = files["native-config.json"]
    require(
        hashlib.sha256(config).hexdigest() == profile["config_sha256"]
        and type(strict_json(config)) is dict,
        "profile_config_changed",
    )
    if profile["type"] == "quality-batch":
        raw = files["quality-cases.json"]
        require(
            hashlib.sha256(raw).hexdigest() == profile["cases_sha256"],
            "profile_cases_changed",
        )
        plan = strict_json(raw)
        validate_cases(plan)
        require(
            len(plan["cases"]) <= 3
            and sum(len(case["turns"]) for case in plan["cases"]) <= 8
            and plan_digest(plan) == profile["plan_sha256"],
            "profile_plan_changed",
        )


def load_inputs(profile):
    """Read the selected immutable inputs without any native runtime inspection."""
    if profile["type"] == "resource-pass":
        verify_resource_plan(profile)
        return {}
    files = {"native-config.json": _read(profile["config"], MAX_CONFIG_BYTES)}
    if profile["type"] == "quality-batch":
        files["quality-cases.json"] = _read(profile["cases"], MAX_CASE_BYTES)
    _check(profile, files)
    return files


def freeze(root, profile, files):
    _check(profile, files)
    fd = private_io.open_directory(root)
    try:
        for name, maximum in frozen_names(profile).items():
            private_io.write_file(fd, name, files[name], max_bytes=maximum)
        private_io.verify_directory(fd, root)
    finally:
        os.close(fd)


def read_frozen(root, profile):
    """Verify saved bytes, never substitute a modified registry/caller input."""
    files = {
        name: _read(root / name, maximum)
        for name, maximum in frozen_names(profile).items()
    }
    _check(profile, files)
    return files


def verify_resource_plan(profile):
    plan = strict_json(_read(Path(profile["state_root"]) / "plan.json", 8 * 1024**2))
    require(
        type(plan) is dict
        and hashlib.sha256(private_io.canonical(plan)).hexdigest()
        == profile["plan_sha256"]
        and plan.get("catalog_manifest_sha256") == profile["manifest_sha256"],
        "profile_plan_changed",
    )
