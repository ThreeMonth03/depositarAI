"""Trusted, bounded multi-turn execution against one registered CPU candidate.

Scoring belongs to the quality evaluator. This coordinator amortizes full
catalog audits, retains runtime/model checkpoints, and captures raw observations
only in the explicitly selected private directory. Normal output is sanitized.
"""

from collections.abc import Mapping
import re
import time

from scripts.check_search_quality import plan_digest
from search.operations import config as product_config
from . import shared_backend as shared
from . import shared_batch_capture as capture
from . import shared_batch_probe as probe


FINAL_AUDIT_RESERVE = 300
MAX_CAPTURE_BYTES = 8 * 1024**2
UNAVAILABLE_TRACE = [
    "branch_candidates_and_scores",
    "rrf_candidate_order_and_scores",
    "reranker_scores_and_complete_order",
]


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_plain(item) for item in value]
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise shared.SharedBackendError("shared_backend_capture_metadata_invalid")


def _diagnostic(error):
    if isinstance(error, KeyboardInterrupt):
        return "shared_backend_interrupted"
    if isinstance(error, shared.SharedBackendError):
        code = str(error)
        if re.fullmatch(r"shared_backend_[a-z_]+", code):
            return code
    return "shared_backend_batch_failed"


def _observations(case, report, bundle):
    """Project exact responses into the existing offline quality input schema."""
    turns, outcomes = [], []
    observed = report["turns"]
    shared.require(
        type(observed) is list
        and len(observed) <= len(case["turns"])
        and report["status"] in {"complete", "failed"}
        and type(report["session_reset"]) is bool
        and (
            report["error"] is None
            or type(report["error"]) is str
            and re.fullmatch(r"shared_batch_[a-z_]+", report["error"])
        ),
        "batch_probe_invalid",
    )
    if report["status"] == "complete":
        shared.require(
            len(observed) == len(case["turns"])
            and all(row.get("status") == "ok" for row in observed)
            and report["session_reset"] is True
            and report["error"] is None,
            "batch_probe_invalid",
        )
    else:
        shared.require(report["error"] is not None, "batch_probe_invalid")
    for number, expected in enumerate(case["turns"]):
        if number >= len(observed):
            outcomes.append({"turn": number + 1, "status": "not_run"})
            continue
        row = observed[number]
        outcome = {key: value for key, value in row.items() if key != "response"}
        outcome["turn"] = number + 1
        outcomes.append(outcome)
        if row["status"] != "ok":
            continue
        reply = row["response"]
        search = reply["result"].get("search")
        cards = search.get("cards", []) if type(search) is dict else []
        shared.require(type(cards) is list and len(cards) <= 20, "batch_cards_invalid")
        identifiers = {
            card.get("dataset_id")
            for card in cards
            if type(card) is dict and type(card.get("dataset_id")) is str
        }
        # Missing source metadata stays missing so the evaluator can report it;
        # never manufacture dates/formats/labels for an unknown returned ID.
        metadata = {
            identifier: _plain(bundle.condition_metadata[identifier])
            for identifier in identifiers
            if identifier in bundle.condition_metadata
        }
        turns.append(
            {
                "query": expected["query"],
                "expected": expected["expected"],
                "response": reply,
                "source_metadata": metadata,
            }
        )
    return (
        {"id": case["id"], "turns": turns} if turns else None,
        {
            "id": case["id"],
            "status": report["status"],
            "error": report["error"],
            "session_reset": report["session_reset"],
            "turns": outcomes,
        },
    )


def _identity(session, record, resident, operator_revision):
    return {
        "operator_revision": operator_revision,
        "candidate_revision": record["revision"],
        "production_revision": session.context["source_revision"],
        "catalog_manifest_sha256": session.context["plan"]["manifest_sha256"],
        "bundle_id": session.context["bundle"].bundle_id,
        "request_policy_sha256": record["policy"]["request_policy_sha256"],
        "model_manifest_sha256": session.inputs.identity["model_manifest_sha256"],
        "resident_models": resident["models"],
        "gpu_compute_sha256": record["compute_sha256"],
        "gpu_image_ids": {
            role: image["Id"] for role, image in session.native.inference_images.items()
        },
        "candidate_runtime": record["runtime"],
    }


def run_batch(filename, *, cases, destination, expected_revision):
    """Execute only the explicitly selected running candidate, never prepare it."""
    shared.require(
        type(expected_revision) is str and shared.HEX40.fullmatch(expected_revision),
        "batch_revision_invalid",
    )
    operator_revision = shared.require_clean_revision(shared.ROOT)
    profile = shared.load_config(filename)
    specification = capture.load_cases(cases)
    product = product_config.load_config(
        profile["production_config"], repo_root=profile["production_source"]
    )
    excluded = (
        cases,
        shared.ROOT,
        profile["state_root"],
        profile["production_source"],
        product.state_root,
        product.api_model_root,
        product.embedding_model_root,
        product.reranker_model_root,
    )
    started = time.monotonic()
    captured, outcomes = [], []
    identity = None
    error = None
    fully_verified = False
    audits = {"before_seconds": 0.0, "after_seconds": 0.0}
    with shared.journal(profile["state_root"]) as store:
        record = shared.current(store)
        shared.require(
            record is not None and record["phase"] == "running", "candidate_not_running"
        )
        shared.require(
            record["revision"] == expected_revision, "batch_candidate_changed"
        )
        with capture.capture_run(destination, excluded=excluded) as sink:
            try:
                with shared.live(profile, record) as session:
                    session.attach_record(record)
                    policy = record["policy"]["policy"]
                    before = time.monotonic()
                    resident = session.verify(policy, record["compute_sha256"])
                    audits["before_seconds"] = time.monotonic() - before
                    identity = _identity(session, record, resident, operator_revision)
                    endpoint = session.candidate_endpoint
                    query_deadline = session.docker.deadline - FINAL_AUDIT_RESERVE

                    def checkpoint():
                        sink.verify()
                        session.remaining(1, deadline=query_deadline)
                        session.check_models(
                            policy, record["compute_sha256"], deadline=query_deadline
                        )
                        session.remaining(1, deadline=query_deadline)

                    try:
                        for case in specification["cases"]:
                            sink.verify()
                            shared.require(
                                shared.require_clean_revision(shared.ROOT)
                                == operator_revision,
                                "batch_operator_changed",
                            )
                            session.remaining(1, deadline=query_deadline)
                            session.observe()
                            shared.require(
                                session.candidate_endpoint == endpoint,
                                "batch_endpoint_changed",
                            )
                            report = probe.conversation(
                                endpoint,
                                [turn["query"] for turn in case["turns"]],
                                record["revision"],
                                identity["catalog_manifest_sha256"],
                                checkpoint=checkpoint,
                                deadline=query_deadline,
                            )
                            observed, outcome = _observations(
                                case, report, session.context["bundle"]
                            )
                            outcomes.append(outcome)
                            if observed is not None:
                                captured.append(observed)
                            if report["status"] != "complete":
                                error = report["error"] or "shared_backend_batch_failed"
                                break
                    finally:
                        after = time.monotonic()
                        session.verify(policy, record["compute_sha256"])
                        audits["after_seconds"] = time.monotonic() - after
                # The retained source/model FDs and original native records must
                # close successfully before a capture can claim trusted identity.
                shared.require(
                    shared.require_clean_revision(shared.ROOT) == operator_revision,
                    "batch_operator_changed",
                )
                fully_verified = True
            except (Exception, KeyboardInterrupt) as failure:
                error = _diagnostic(failure)

            for case in specification["cases"][len(outcomes) :]:
                outcomes.append(
                    {
                        "id": case["id"],
                        "status": "not_run",
                        "error": None,
                        "session_reset": None,
                        "turns": [
                            {"turn": number, "status": "not_run"}
                            for number in range(1, len(case["turns"]) + 1)
                        ],
                    }
                )
            status = (
                "complete"
                if fully_verified and error is None
                else "failed"
                if fully_verified
                else "quarantined"
            )
            completed = sum(
                turn["status"] == "ok" for case in outcomes for turn in case["turns"]
            )
            receipt = {
                "schema_version": 1,
                "operator_revision": operator_revision,
                "status": status,
                "error": error,
                "identity_verified": fully_verified,
                "identity": identity,
                "plan_sha256": plan_digest(specification),
                "requested_cases": len(specification["cases"]),
                "requested_turns": sum(len(c["turns"]) for c in specification["cases"]),
                "completed_cases": sum(c["status"] == "complete" for c in outcomes),
                "completed_turns": completed,
                "audit_timing": audits,
                "operator_seconds": time.monotonic() - started,
                "quality_evaluation": "not_run",
                "unavailable_trace": UNAVAILABLE_TRACE,
                "models_restarted": False,
                "public_route_changed": False,
            }
            # Raw observations and case IDs stay only in the explicit capture.
            # A partial capture is not proof the requested suite completed.
            sink.write_json(
                "capture.json",
                {"schema_version": 1, "cases": captured},
                max_bytes=MAX_CAPTURE_BYTES,
            )
            sink.write_json(
                "outcomes.json",
                {"schema_version": 1, "cases": outcomes},
                max_bytes=MAX_CAPTURE_BYTES,
            )
            sink.write_json("run.json", receipt, max_bytes=64 * 1024)
    # capture_run performs its own final FD/path checks and publishes last.
    return {key: value for key, value in receipt.items() if key != "identity"}
