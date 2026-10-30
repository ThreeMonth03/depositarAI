"""Run fixed native commands and verify their distinct evidence contracts."""

import hashlib
import os

from search.operations.documents.sets import MAX_SELECTED
from search.operations.shared import private_io
from search.quality import batches
from search.serialization import strict_json

from . import profiles
from .pueue import require


QUALITY_FILES = {
    "capture/run.json": batches.MAX_RECEIPT_BYTES,
    "capture/capture.json": batches.MAX_FILE_BYTES,
    "capture/outcomes.json": batches.MAX_FILE_BYTES,
}
MAX_EVIDENCE_BYTES = batches.MAX_TOTAL_BYTES


def argv(root, request):
    profile = request["profile_definition"]
    prefix = ["/usr/bin/python3", "-I", "-S", "-B"]
    if profile["type"] == "quality-batch":
        return [
            *prefix,
            root / "source/scripts/share_search_backend.py",
            "--config",
            root / "native-config.json",
            "batch",
            "--cases",
            root / "quality-cases.json",
            "--capture",
            root / "capture",
            "--expected-revision",
            profile["candidate_revision"],
            "--expected-identity-sha256",
            profile["expected_identity_sha256"],
        ]
    prefix += [root / "source/scripts/depositarai.py"]
    if profile["type"] == "vector-pass":
        return [
            *prefix,
            "--config",
            root / "native-config.json",
            "build-vectors",
            "--max-passes",
            "1",
            "--wall-seconds",
            "3600",
            "--expected-inputs-sha256",
            profile["expected_inputs_sha256"],
        ]
    return [
        *prefix,
        "profile-resources",
        "run",
        "--bundle",
        profile["bundle"],
        "--state-root",
        profile["state_root"],
        "--expected-plan-sha256",
        profile["plan_sha256"],
        "--max-resources",
        "20",
    ]


def timeout(profile):
    return 4200 if profile["type"] == "vector-pass" else 3600


def native_names(profile):
    return QUALITY_FILES if profile["type"] == "quality-batch" else {}


def native_files(root, profile):
    files = {}
    for name, maximum in native_names(profile).items():
        filename = root / name
        fd = private_io.open_directory(filename.parent)
        try:
            require(
                set(os.listdir(fd)) == {"run.json", "capture.json", "outcomes.json"},
                "native_evidence_files_invalid",
            )
            files[name] = private_io.read_file(
                fd, filename.name, mode=0o600, max_bytes=maximum
            )
            private_io.verify_directory(fd, filename.parent)
        finally:
            os.close(fd)
    return files


def _resource(value, code, profile):
    require(
        type(value) is dict
        and type(value.get("schema_version")) is int
        and value["schema_version"] == 1
        and value.get("status") in {"completed", "partial", "checkpoint"}
        and value.get("plan_sha256") == profile["plan_sha256"]
        and value.get("catalog_manifest_sha256") == profile["manifest_sha256"]
        and all(
            type(value.get(key)) is int and 0 <= value[key] <= MAX_SELECTED
            for key in (
                "selected_count",
                "pending_count",
                "profile_count",
                "failed_count",
                "retryable_count",
                "attempted_count",
            )
        )
        and value["attempted_count"] <= 20
        and value["pending_count"] <= value["selected_count"]
        and value["profile_count"] <= value["selected_count"] - value["pending_count"]
        and value["failed_count"] <= value["selected_count"] - value["pending_count"]
        and value["retryable_count"] <= value["failed_count"]
        and code == {"completed": 0, "partial": 1, "checkpoint": 2}[value["status"]]
        and (value["status"] == "checkpoint") == bool(value["pending_count"])
        and (value["status"] != "completed" or value["failed_count"] == 0)
        and (value["status"] != "partial" or value["failed_count"] > 0),
        "operation_result_unverified",
    )
    return value["status"]


def _quality(value, code, root, request, files):
    profile = request["profile_definition"]
    run = strict_json(files["capture/run.json"])
    require(
        code == 0
        and type(run) is dict
        and run.get("status") == "complete"
        and run.get("identity_verified") is True
        and run.get("operator_revision") == request["revision"]
        and type(run.get("identity")) is dict
        and run["identity"].get("operator_revision") == request["revision"]
        and run["identity"].get("candidate_revision") == profile["candidate_revision"]
        and hashlib.sha256(private_io.canonical(run["identity"])).hexdigest()
        == profile["expected_identity_sha256"]
        and private_io.canonical(value)
        == private_io.canonical(
            {key: item for key, item in run.items() if key != "identity"}
        ),
        "operation_result_unverified",
    )
    plan = strict_json(profiles.read_frozen(root, profile)["quality-cases.json"])
    # The established verifier keeps the complete plan denominator. Quality
    # assertions may fail while trusted observation collection is complete.
    report = batches.aggregate(
        plan,
        [
            {
                "plan": plan,
                "files": {name.split("/")[-1]: raw for name, raw in files.items()},
            }
        ],
    )
    require(report["evidence"]["status"] == "complete", "operation_result_unverified")
    return "completed"


def _vector(value, code, profile):
    require(
        type(value) is dict
        and type(value.get("schema_version")) is int
        and value["schema_version"] == 1
        and value.get("status") in {"vectors-complete", "checkpoint"}
        and value.get("revision") == profile["catalog_revision"]
        and value.get("bundle_id") == profile["bundle_id"]
        and value.get("manifest_sha256") == profile["manifest_sha256"]
        and value.get("runtime_verified") is True
        and value.get("active_changed") is False
        and type(value.get("host_cleanup_verified")) is bool
        and type(value.get("document_count")) is int
        and value["document_count"] > 0
        and type(value.get("completed_passes")) is int
        and 0 <= value["completed_passes"] <= 128
        and type(value.get("passes_this_invocation")) is int
        and 0 <= value["passes_this_invocation"] <= 1
        and value["passes_this_invocation"] <= value["completed_passes"]
        and type(value.get("recipe_sha256")) is str
        and profiles.SHA256.fullmatch(value["recipe_sha256"]),
        "operation_result_unverified",
    )
    count, done, pending = (
        value["document_count"],
        value.get("done_count"),
        value.get("pending_count"),
    )
    counted = (
        type(done) is int
        and type(pending) is int
        and 0 <= done <= count
        and 0 <= pending <= count
        and done + pending == count
    )
    if value["status"] == "vectors-complete":
        require(
            code == 0
            and value["host_cleanup_verified"] is True
            and counted
            and done == count
            and pending == 0,
            "operation_result_unverified",
        )
        if value["passes_this_invocation"] == 0:
            require(
                value.get("read_only") is True
                and value["completed_passes"] > 0
                and all(
                    type(value.get(key)) is str
                    and profiles.SHA256.fullmatch(value[key])
                    for key in ("after_state_sha256", "runtime_sha256")
                )
                and type(value.get("admin_image_id")) is str
                and value["admin_image_id"].startswith("sha256:")
                and profiles.SHA256.fullmatch(value["admin_image_id"][7:]),
                "operation_result_unverified",
            )
        return "completed"
    require(
        code == 2
        and (
            counted
            and pending > 0
            or done is None
            and pending is None
            and value["completed_passes"] == 0
        )
        and value["host_cleanup_verified"] is bool(value["passes_this_invocation"])
        and (value["passes_this_invocation"] == 0 or counted),
        "operation_result_unverified",
    )
    # No new pass is a valid native checkpoint, not evidence of completed work.
    return "checkpoint"


def verify(value, code, root, request):
    """Return a normalized state only after verifying native completion evidence."""
    profile = request["profile_definition"]
    profiles.verify_source(profile, request["revision"])
    profiles.read_frozen(root, profile)
    if profile["type"] == "quality-batch":
        return _quality(value, code, root, request, native_files(root, profile))
    if profile["type"] == "vector-pass":
        return _vector(value, code, profile)
    return _resource(value, code, profile)


def expected_code(value, profile):
    """Recover the required native exit status when rechecking a stored result."""
    require(type(value) is dict, "operation_result_unverified")
    if profile["type"] == "quality-batch":
        return 0
    if profile["type"] == "vector-pass":
        return 2 if value.get("status") == "checkpoint" else 0
    return {"completed": 0, "partial": 1, "checkpoint": 2}.get(value.get("status"), -1)
