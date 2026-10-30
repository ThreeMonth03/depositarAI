"""Validate batch evidence before evaluating a complete, unchanged quality plan."""

import hashlib
import json
import math
import re

from search.serialization import strict_json
from . import contract, evaluator


MAX_FILE_BYTES = 8 * 1024**2
MAX_RECEIPT_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 32 * 1024**2
NAMES = {"run.json", "capture.json", "outcomes.json"}
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
ERROR = re.compile(r"shared_(?:batch|backend)_[a-z_]{1,128}\Z")


class BatchEvidenceError(ValueError):
    """Use fixed diagnostics without reflecting private evidence or file paths."""


def _require(condition, code):
    if not condition:
        raise BatchEvidenceError("quality_batches_" + code)


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _identity_fields(value, depth=0):
    _require(depth <= 16, "identity_invalid")
    if type(value) is dict:
        _require(len(value) <= 128, "identity_invalid")
        for key, item in value.items():
            _require(type(key) is str and 1 <= len(key) <= 128, "identity_invalid")
            if key.endswith("_sha256") or key in {"digest", "bundle_id"}:
                _require(
                    type(item) is str and HEX64.fullmatch(item), "identity_invalid"
                )
            elif key.endswith("_revision") or key == "revision":
                _require(
                    type(item) is str and HEX40.fullmatch(item), "identity_invalid"
                )
            _identity_fields(item, depth + 1)
    elif type(value) is list:
        _require(len(value) <= 128, "identity_invalid")
        for item in value:
            _identity_fields(item, depth + 1)
    elif type(value) is str:
        _require(len(value) <= 4096, "identity_invalid")
    else:
        _require(
            value is None
            or type(value) is bool
            or type(value) in (int, float)
            and math.isfinite(value),
            "identity_invalid",
        )


def _identity(value):
    required = {
        "operator_revision",
        "candidate_revision",
        "production_revision",
        "catalog_manifest_sha256",
        "bundle_id",
        "request_policy_sha256",
        "model_manifest_sha256",
        "resident_models",
        "gpu_compute_sha256",
        "gpu_image_ids",
        "candidate_runtime",
    }
    _require(type(value) is dict and required <= value.keys(), "identity_invalid")
    for name in ("operator_revision", "candidate_revision", "production_revision"):
        _require(
            type(value[name]) is str and HEX40.fullmatch(value[name]),
            "identity_invalid",
        )
    for name in (
        "catalog_manifest_sha256",
        "bundle_id",
        "request_policy_sha256",
        "model_manifest_sha256",
        "gpu_compute_sha256",
    ):
        _require(
            type(value[name]) is str and HEX64.fullmatch(value[name]),
            "identity_invalid",
        )
    _require(
        type(value["resident_models"]) is list
        and 1 <= len(value["resident_models"]) <= 32
        and all(type(row) is dict and row for row in value["resident_models"])
        and type(value["candidate_runtime"]) is dict
        and value["candidate_runtime"]
        and type(value["gpu_image_ids"]) is dict
        and 1 <= len(value["gpu_image_ids"]) <= 8,
        "identity_invalid",
    )
    for image in value["gpu_image_ids"].values():
        _require(
            type(image) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", image),
            "identity_invalid",
        )
    # Keep every field, including future selected-catalog bindings. Projecting
    # onto known fields could accept evidence from different catalog consumers.
    _identity_fields(value)
    encoded = _canonical(value)
    _require(len(encoded) <= MAX_RECEIPT_BYTES, "identity_invalid")
    return encoded


def _error(value):
    return value is None or type(value) is str and ERROR.fullmatch(value)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _outcomes(value, plan, captured):
    _require(
        type(value) is dict
        and set(value) == {"schema_version", "cases"}
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and type(value["cases"]) is list
        and len(value["cases"]) == len(plan["cases"]),
        "outcomes_invalid",
    )
    actual = {case["id"]: case for case in captured["cases"]}
    completed_cases = completed_turns = 0
    stopped = False
    for case, expected in zip(value["cases"], plan["cases"]):
        _require(
            type(case) is dict
            and set(case) == {"id", "status", "error", "session_reset", "turns"}
            and case["id"] == expected["id"]
            and type(case["status"]) is str
            and case["status"] in {"complete", "failed", "not_run"}
            and _error(case["error"])
            and type(case["turns"]) is list
            and len(case["turns"]) == len(expected["turns"]),
            "outcomes_invalid",
        )
        _require(not stopped or case["status"] == "not_run", "outcomes_invalid")
        ok = 0
        turn_stopped = False
        for number, turn in enumerate(case["turns"], 1):
            _require(
                type(turn) is dict
                and type(turn.get("turn")) is int
                and turn["turn"] == number
                and type(turn.get("status")) is str
                and turn["status"] in {"ok", "failed", "not_run"},
                "outcomes_invalid",
            )
            status = turn["status"]
            _require(not turn_stopped or status == "not_run", "outcomes_invalid")
            if status == "not_run":
                _require(set(turn) == {"turn", "status"}, "outcomes_invalid")
                turn_stopped = True
            else:
                fields = {"turn", "status", "wall_seconds", "request_seconds"}
                if status == "failed":
                    fields.add("error")
                    _require(
                        turn.get("error") is not None and _error(turn.get("error")),
                        "outcomes_invalid",
                    )
                    turn_stopped = True
                _require(
                    set(turn) == fields
                    and _number(turn["wall_seconds"])
                    and _number(turn["request_seconds"]),
                    "outcomes_invalid",
                )
                ok += status == "ok"
        if case["status"] == "complete":
            _require(
                ok == len(case["turns"])
                and case["error"] is None
                and case["session_reset"] is True,
                "outcomes_invalid",
            )
            completed_cases += 1
        elif case["status"] == "not_run":
            _require(
                ok == 0
                and all(t["status"] == "not_run" for t in case["turns"])
                and case["error"] is None
                and case["session_reset"] is None,
                "outcomes_invalid",
            )
            stopped = True
        else:
            _require(
                case["error"] is not None and type(case["session_reset"]) is bool,
                "outcomes_invalid",
            )
            stopped = True
        observed = actual.get(case["id"])
        _require(
            (observed is not None) == bool(ok)
            and (not ok or len(observed["turns"]) == ok),
            "capture_outcomes_mismatch",
        )
        completed_turns += ok
    return completed_cases, completed_turns


def _responses(capture, identity):
    for case in capture["cases"]:
        for turn in case["turns"]:
            reply = turn["response"].get("result")
            _require(
                type(reply) is dict
                and identity is not None
                and reply.get("revision") == identity["candidate_revision"],
                "response_identity_mismatch",
            )
            search = reply.get("search")
            if search is not None:
                _require(
                    type(search) is dict
                    and search.get("revision") == identity["candidate_revision"]
                    and search.get("catalog_manifest_sha256")
                    == identity["catalog_manifest_sha256"],
                    "response_identity_mismatch",
                )


def _batch(record):
    _require(
        type(record) is dict and set(record) == {"plan", "files"}, "record_invalid"
    )
    plan, files = record["plan"], record["files"]
    contract.validate_cases(plan)
    _require(
        len(plan["cases"]) <= 3 and sum(len(c["turns"]) for c in plan["cases"]) <= 8,
        "subplan_invalid",
    )
    _require(type(files) is dict and set(files) == NAMES, "artifacts_missing")
    for name, raw in files.items():
        maximum = MAX_RECEIPT_BYTES if name == "run.json" else MAX_FILE_BYTES
        _require(
            type(raw) is bytes and 1 <= len(raw) <= maximum, "artifact_size_invalid"
        )
    run, captured, outcomes = (
        strict_json(files[name])
        for name in ("run.json", "capture.json", "outcomes.json")
    )
    _require(
        type(run) is dict
        and type(run.get("schema_version")) is int
        and run["schema_version"] == 1
        and type(run.get("status")) is str
        and run["status"] in {"complete", "failed", "quarantined"}
        and type(run.get("identity_verified")) is bool
        and _error(run.get("error"))
        and "identity" in run,
        "receipt_invalid",
    )
    _require(
        run.get("plan_sha256") == contract.plan_digest(plan), "plan_digest_mismatch"
    )
    _require(
        type(run.get("operator_revision")) is str
        and HEX40.fullmatch(run["operator_revision"]),
        "identity_invalid",
    )
    identity = run["identity"]
    if identity is not None:
        _identity(identity)
        _require(
            run["operator_revision"] == identity["operator_revision"],
            "identity_mismatch",
        )
    _require(
        (run["status"] == "quarantined") == (not run["identity_verified"])
        and (identity is not None or run["status"] == "quarantined")
        and ((run["error"] is None) == (run["status"] == "complete"))
        and run.get("models_restarted") is False
        and run.get("public_route_changed") is False
        and run.get("quality_evaluation") == "not_run",
        "receipt_invalid",
    )
    artifacts = run.get("artifacts")
    _require(
        type(artifacts) is dict and set(artifacts) == {"capture", "outcomes"},
        "artifacts_missing",
    )
    for key in ("capture", "outcomes"):
        artifact, name = artifacts[key], key + ".json"
        _require(
            type(artifact) is dict
            and set(artifact) == {"name", "sha256", "bytes"}
            and artifact["name"] == name
            and type(artifact["bytes"]) is int
            and artifact["bytes"] == len(files[name])
            and artifact["sha256"] == hashlib.sha256(files[name]).hexdigest(),
            "artifact_digest_mismatch",
        )
    contract.validate_cases(captured, captured=True, allow_empty=True)
    contract.bind_plan(captured, plan)
    completed_cases, completed_turns = _outcomes(outcomes, plan, captured)
    counts = {
        "requested_cases": len(plan["cases"]),
        "requested_turns": sum(len(c["turns"]) for c in plan["cases"]),
        "completed_cases": completed_cases,
        "completed_turns": completed_turns,
    }
    _require(
        all(
            type(run.get(key)) is int and run[key] == count
            for key, count in counts.items()
        ),
        "counts_mismatch",
    )
    _require(
        run["status"] != "complete" or completed_cases == len(plan["cases"]),
        "receipt_invalid",
    )
    _responses(captured, identity)
    return run, captured


def aggregate(full_plan, records, *, baseline=None):
    """Reject mixed evidence and retain every planned turn, including missing batches.

    Records contain a decoded subplan and the exact three artifact byte strings.
    Only complete, identity-verified batches contribute observations. The return
    value contains private case/card data; callers must restrict its publication.
    """
    try:
        contract.validate_cases(full_plan)
        _require(
            type(records) in (list, tuple) and len(records) <= 32, "records_invalid"
        )
        expected = {case["id"]: case for case in full_plan["cases"]}
        selected, accepted, receipts = {}, [], []
        bound = _identity(baseline) if baseline is not None else None
        total_bytes = 0
        for number, record in enumerate(records, 1):
            run, captured = _batch(record)
            total_bytes += sum(len(raw) for raw in record["files"].values())
            _require(total_bytes <= MAX_TOTAL_BYTES, "input_too_large")
            for case in record["plan"]["cases"]:
                _require(case["id"] not in selected, "duplicate_case")
                _require(expected.get(case["id"]) == case, "subplan_mismatch")
                selected[case["id"]] = {"batch": number, "status": run["status"]}
            if run["identity_verified"] and bound is None:
                baseline, bound = run["identity"], _identity(run["identity"])
            receipts.append(
                {
                    "batch": number,
                    "status": run["status"],
                    "identity_verified": run["identity_verified"],
                    "identity": run["identity"],
                    "plan_sha256": run["plan_sha256"],
                    "run_sha256": hashlib.sha256(
                        record["files"]["run.json"]
                    ).hexdigest(),
                    "artifacts": run["artifacts"],
                    "case_ids": [case["id"] for case in record["plan"]["cases"]],
                }
            )
            if run["status"] == "complete":
                accepted.extend(captured["cases"])
        identities = {
            _identity(receipt["identity"])
            for receipt in receipts
            if receipt["identity"] is not None
        }
        if bound is not None:
            identities.add(bound)
        _require(len(identities) <= 1, "mixed_identity")
        report = evaluator.evaluate({"schema_version": 1, "cases": accepted}, full_plan)
        trusted = {case["id"] for case in accepted}
        trusted_turns = sum(len(case["turns"]) for case in accepted)
        quality_failed = sum(
            bool(turn["issues"])
            for case in report["cases"]
            if case["id"] in trusted
            for turn in case["turns"]
        )
        missing = report["summary"]["turn_count"] - trusted_turns
        report["summary"].update(
            evidence_complete_turn_count=trusted_turns,
            evidence_missing_turn_count=missing,
            quality_failed_turn_count=quality_failed,
        )
        report["evidence"] = {
            "status": "incomplete" if missing else "complete",
            "baseline": baseline,
            "batches": receipts,
            "cases": [
                {
                    "id": case["id"],
                    **selected.get(case["id"], {"batch": None, "status": "missing"}),
                }
                for case in full_plan["cases"]
            ],
        }
        if missing:
            report["status"] = "incomplete"
        return report
    except BatchEvidenceError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        raise BatchEvidenceError("quality_batches_input_invalid") from None
