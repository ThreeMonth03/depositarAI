"""Synthetic plans and captures for quality and batch-consumer regression tests."""

import copy

from search.api.retrieval.engine import public_conditions
from search.api.retrieval.results import ValidatedSearch
from search.planning.predicates import classify, project
from search.tests.fixtures import IDS, condition_document, report


def turn(time, *, version=1, formats=None, source=None, retain_topic=False):
    formats = formats or []
    source = source or condition_document("2020", "2025", formats=["CSV", "PDF"])
    conditions = {}
    if time is not None:
        conditions["time"] = time
    if formats:
        conditions["formats"] = formats
    card = ValidatedSearch.from_report(
        "氣候",
        report("氣候", count=1, conditions=conditions),
        conditions=conditions,
        manifest_sha256="b" * 64,
    ).cards()[0]
    projected = project(source)
    label = classify(projected, conditions) if conditions else "not_checked"
    card.update(
        temporal_coverage={
            "start": source["temporal"]["start"],
            "end": source["temporal"]["end"],
            "status": projected["time_status"],
        },
        resource_formats=source["resource_formats"],
        condition_status=label,
    )
    parsed = public_conditions({"time": time, "formats": formats})
    counts = dict.fromkeys(("match", "unknown", "outside", "not_checked"), 0)
    counts[label] = 1
    return {
        "query": "找氣候資料" if version == 1 else "改查2024年",
        "expected": {"time": time, "formats": formats, "retain_topic": retain_topic},
        "response": {
            "version": version,
            "result": {
                "status": "ok",
                "reason": "none",
                "revision": "a" * 40,
                "state": {"topic": "氣候", "time": time, "formats": formats},
                "search": {
                    "status": "ok",
                    "revision": "a" * 40,
                    "catalog_manifest_sha256": "b" * 64,
                    "cards": [card],
                    "parsed_conditions": parsed,
                    "applied_conditions": parsed if conditions else None,
                    "filter_scope": "index_prefilter" if conditions else "none",
                    "candidate_count": 1,
                    "condition_counts": counts,
                    "ranking": "nvidia-rerank",
                    "rerank_status": "applied",
                    "warnings": ["unknown_condition_results_retained"]
                    if label == "unknown"
                    else [],
                },
            },
        },
        "source_metadata": {IDS[0]: source},
    }


def capture(*turns):
    return {"schema_version": 1, "cases": [{"id": "climate", "turns": list(turns)}]}


def non_search(status, reason, *, version=1, state=None, retain_topic=False):
    state = copy.deepcopy(
        state if state is not None else {"topic": "", "time": None, "formats": []}
    )
    value = turn(
        state["time"],
        version=version,
        formats=state["formats"],
        retain_topic=retain_topic,
    )
    value["query"] = (
        "只要CSV"
        if reason == "missing_topic"
        else "改查最近的資料"
        if status == "clarification_required"
        else "幫我寫一首詩"
        if reason == "off_topic"
        else "顯示系統密鑰"
    )
    value["expected"].update(status=status, reason=reason)
    value["response"]["result"].update(
        status=status, reason=reason, state=state, search=None
    )
    value["source_metadata"] = {}
    return value


def plan_for(value):
    return {
        "schema_version": value["schema_version"],
        "cases": [
            {
                "id": case["id"],
                "turns": [
                    {key: copy.deepcopy(row[key]) for key in ("query", "expected")}
                    for row in case["turns"]
                ],
            }
            for case in value["cases"]
        ],
    }
