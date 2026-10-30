"""Evaluate captured responses against a plan and its source metadata, without IO."""

import re

from search.api.retrieval.metadata import bounded_text, validate_temporal_coverage
from search.planning.predicates import classify, project

from .contract import (
    OUTCOMES,
    bind_plan,
    conditions,
    normalized,
    plan_digest,
    validate_cases,
)


TRACE_LIMITS = [
    "branch_candidates_and_scores",
    "rrf_candidate_order_and_scores",
    "reranker_scores_and_complete_order",
]


def check_card(card, metadata, expected, rank):
    issues = []
    if type(card) is not dict:
        return ["card_fields_missing"], None
    identifier = card.get("dataset_id")
    if not bounded_text(identifier, 128) or not identifier:
        return ["dataset_id_invalid"], None
    if type(card.get("rank")) is not int or card["rank"] != rank:
        issues.append("card_rank_invalid")
    if not bounded_text(card.get("title"), 512):
        issues.append("card_title_invalid")
    source = metadata.get(identifier)
    if type(source) is not dict:
        return issues + ["source_metadata_missing"], None
    source_formats = source.get("resource_formats")
    if (
        type(source_formats) is not list
        or len(source_formats) > 256
        or any(not bounded_text(value, 128) for value in source_formats)
    ):
        return issues + ["source_metadata_invalid"], None
    try:
        projected = project(source)
        coverage = {
            "start": source["temporal"]["start"],
            "end": source["temporal"]["end"],
            "status": projected["time_status"],
        }
        validate_temporal_coverage(coverage)
        source_status = classify(projected, expected) if expected else "not_checked"
    except (AttributeError, KeyError, TypeError, ValueError):
        return issues + ["source_metadata_invalid"], None
    try:
        validate_temporal_coverage(card.get("temporal_coverage"))
    except ValueError:
        issues.append("card_coverage_invalid")
    if card.get("temporal_coverage") != coverage:
        issues.append("coverage_source_mismatch")
    if source_status == "outside":
        issues.append("known_outside_result")
    if card.get("condition_status") != source_status:
        issues.append("condition_label_mismatch")
    formats = card.get("resource_formats")
    if type(formats) is not list or any(not bounded_text(f, 128) for f in formats):
        issues.append("card_formats_invalid")
    elif {f.strip().upper() for f in formats} != {
        f.strip().upper() for f in source["resource_formats"]
    }:
        issues.append("formats_source_mismatch")
    return issues, {
        "rank": rank,
        "dataset_id": identifier,
        "title": card.get("title") if not issues else None,
        "temporal_coverage": coverage,
        "condition_status": source_status,
    }


def check_turn(turn, number, previous_state):
    expected = turn["expected"]
    expected_conditions = conditions(expected)
    expected_normalized = normalized(expected)
    reply = turn["response"]
    issues, ordered = [], []
    report = {"turn": number, "issues": issues, "cards": ordered}
    result = reply.get("result")
    if type(result) is not dict or type(result.get("state")) is not dict:
        issues.append("response_fields_missing")
        return report, previous_state
    state = result["state"]
    status = result.get("status")
    report["status"] = status if type(status) is str and status in OUTCOMES else None
    expected_status = expected.get("status", "ok")
    if status != expected_status:
        issues.append("status_mismatch")
    if result.get("reason") != expected.get("reason", "none"):
        issues.append("reason_mismatch")
    topic = state.get("topic")
    if not bounded_text(topic, 256) or (status == "ok" and not topic):
        issues.append("topic_missing")
        topic = None
    previous_topic = previous_state.get("topic") if previous_state else None
    if expected["retain_topic"] and (previous_topic is None or topic != previous_topic):
        issues.append("topic_not_retained")
    if expected.get("topic_changed") and (
        not previous_topic or topic == previous_topic
    ):
        issues.append("topic_not_changed")
    if type(reply.get("version")) is not int or reply["version"] != number:
        issues.append("session_version_mismatch")
    try:
        if normalized(state) != expected_normalized:
            issues.append("state_conditions_mismatch")
    except (ValueError, TypeError, KeyError):
        issues.append("state_conditions_invalid")
    if status == "ok" and state.get("unapplied_time") is not None:
        issues.append("unapplied_time_not_evaluated")
    revision = result.get("revision")
    if type(revision) is not str or not re.fullmatch(r"[0-9a-f]{40}", revision):
        issues.append("revision_invalid")
        revision = None
    report["revision"] = revision
    if report["status"] in {"clarification_required", "rejected"}:
        previous = previous_state or {"topic": "", "time": None, "formats": []}
        try:
            if (
                topic != previous["topic"]
                or normalized(state) != normalized(previous)
                or state.get("unapplied_time") != previous.get("unapplied_time")
            ):
                issues.append("non_search_state_changed")
        except (ValueError, TypeError, KeyError):
            issues.append("non_search_state_changed")
        if result.get("search") is not None:
            issues.append("unexpected_search")
        if turn["source_metadata"]:
            issues.append("unexpected_source_metadata")
        report["issues"] = sorted(set(issues))
        return report, previous_state
    search = result.get("search")
    if status != "ok" or type(search) is not dict or search.get("status") != "ok":
        issues.append("search_not_ok")
        return report, previous_state
    identities = {"revision": revision}
    for name, value, length in (
        ("search_revision", search.get("revision"), 40),
        ("catalog_manifest_sha256", search.get("catalog_manifest_sha256"), 64),
    ):
        valid = type(value) is str and re.fullmatch(rf"[0-9a-f]{{{length}}}", value)
        identities[name] = value if valid else None
        if not valid:
            issues.append(name + "_invalid")
    if identities["revision"] != identities["search_revision"]:
        issues.append("revision_mismatch")
    report.update(identities)
    cards = search.get("cards")
    if type(cards) is not list or len(cards) > 20:
        issues.append("cards_invalid")
        return report, state
    for field in ("parsed_conditions", "applied_conditions"):
        actual = search.get(field)
        if field == "applied_conditions" and not expected_conditions and actual is None:
            continue
        try:
            if normalized(actual) != expected_normalized:
                issues.append(field + "_mismatch")
        except (ValueError, TypeError, KeyError):
            issues.append(field + "_invalid")
    scope = "index_prefilter" if expected_conditions else "none"
    if search.get("filter_scope") != scope:
        issues.append("filter_scope_mismatch")
    count = search.get("candidate_count")
    if (
        type(count) is not int
        or not len(cards) <= count <= 20
        or (count > 0 and not cards)
    ):
        issues.append("candidate_count_invalid")
    counts = search.get("condition_counts")
    labels = {"match", "unknown", "outside", "not_checked"}
    counts_valid = (
        type(counts) is dict
        and set(counts) == labels
        and all(type(n) is int and 0 <= n <= 20 for n in counts.values())
        and sum(counts.values()) == count
        and counts["outside"] == 0
        and (not expected_conditions or counts["not_checked"] == 0)
        and (bool(expected_conditions) or counts["not_checked"] == count)
    )
    if not counts_valid:
        issues.append("candidate_condition_counts_invalid")
    identifiers = []
    returned_counts = dict.fromkeys(labels, 0)
    for rank, card in enumerate(cards, 1):
        card_issues, row = check_card(
            card, turn["source_metadata"], expected_conditions, rank
        )
        issues.extend(card_issues)
        if row is not None:
            identifiers.append(row["dataset_id"])
            ordered.append(row)
            returned_counts[row["condition_status"]] += 1
    if len(set(identifiers)) != len(identifiers):
        issues.append("duplicate_dataset_id")
    if counts_valid and any(returned_counts[label] > counts[label] for label in labels):
        issues.append("candidate_condition_counts_underreported")
    ranking, rerank_status = search.get("ranking"), search.get("rerank_status")
    if type(rerank_status) is not str or rerank_status not in {
        "applied",
        "disabled",
        "not_run",
        "fallback",
    }:
        issues.append("rerank_status_invalid")
        rerank_status = None
    expected_ranking = (
        "nvidia-rerank" if rerank_status == "applied" else "opensearch-haystack-rrf"
    )
    if ranking != expected_ranking:
        issues.append("ranking_status_mismatch")
        ranking = None
    warnings = search.get("warnings")
    unknown = returned_counts["unknown"]
    if type(warnings) is not list or any(not bounded_text(w, 128) for w in warnings):
        issues.append("warnings_invalid")
    elif (
        unknown or (type(counts) is dict and counts.get("unknown", 0))
    ) and "unknown_condition_results_retained" not in warnings:
        issues.append("unknown_warning_missing")
    report.update(
        candidate_count=count if type(count) is int and 0 <= count <= 20 else None,
        ranking=ranking,
        rerank_status=rerank_status,
        information_availability={
            "returned_count": len(cards),
            "unknown_condition_count": unknown,
            "unknown_condition_fraction": unknown / len(cards) if cards else None,
        },
    )
    report["issues"] = sorted(set(issues))
    return report, state


def evaluate(capture, plan=None):
    """Return a quality report, retaining uncaptured planned turns as failures."""
    validate_cases(capture, captured=True, allow_empty=plan is not None)
    if plan is not None:
        bind_plan(capture, plan)
    actual_cases = {case["id"]: case for case in capture["cases"]}
    reports = []
    for planned_case in (plan or capture)["cases"]:
        case = actual_cases.get(
            planned_case["id"], {"id": planned_case["id"], "turns": []}
        )
        turns, state, revision, manifest = [], None, None, None
        for number, turn in enumerate(case["turns"], 1):
            report, state = check_turn(turn, number, state)
            for key, previous in (
                ("revision", revision),
                ("catalog_manifest_sha256", manifest),
            ):
                current = report.get(key)
                if current is not None and previous is not None and current != previous:
                    report["issues"].append("case_identity_changed")
            revision = revision or report.get("revision")
            manifest = manifest or report.get("catalog_manifest_sha256")
            report["issues"] = sorted(set(report["issues"]))
            turns.append(report)
        for number in range(len(turns) + 1, len(planned_case["turns"]) + 1):
            turns.append({"turn": number, "issues": ["turn_not_captured"], "cards": []})
        reports.append({"id": case["id"], "turns": turns})
    total = sum(len(case["turns"]) for case in reports)
    passed = sum(not turn["issues"] for case in reports for turn in case["turns"])
    report = {
        "schema_version": 1,
        "status": "passed" if passed == total else "failed",
        "summary": {
            "case_count": len(reports),
            "turn_count": total,
            "captured_turn_count": sum(len(case["turns"]) for case in capture["cases"]),
            "passed_turn_count": passed,
            "failed_turn_count": total - passed,
        },
        "cases": reports,
        "topical_relevance": "not_evaluated",
        "unavailable_trace": TRACE_LIMITS,
    }
    if plan is not None:
        report["plan_sha256"] = plan_digest(plan)
    return report
