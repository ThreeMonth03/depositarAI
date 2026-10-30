"""Validate ordered quality cases and bind their unchanged query/expectation data."""

import hashlib
import json

from search.api.retrieval.metadata import bounded_text
from search.planning.contract import checked_query
from search.planning.predicates import validate_conditions


OUTCOMES = {
    "ok": {"none"},
    "clarification_required": {
        "ambiguous_time",
        "missing_topic",
        "unsupported_condition",
        "location_not_supported",
    },
    "rejected": {"off_topic", "unsafe"},
}


def conditions(value):
    """Use production predicates to compare date meaning, not string padding."""
    if type(value) is not dict or not {"time", "formats"} <= set(value):
        raise ValueError("condition fields missing")
    if value.get("place") is not None or type(value["formats"]) is not list:
        raise ValueError("unsupported conditions")
    result = {}
    if value["time"] is not None:
        interval = value["time"]
        if type(interval) is not dict or not {"start", "end"} <= set(interval):
            raise ValueError("time fields missing")
        for key, expected in (
            ("field", "data_coverage"),
            ("relation", "overlaps"),
            ("endpoints", "inclusive"),
        ):
            if key in interval and interval[key] != expected:
                raise ValueError("unsupported temporal semantics")
        result["time"] = {key: interval[key] for key in ("start", "end")}
    if value["formats"]:
        result["formats"] = value["formats"]
    validate_conditions(result)
    return result


def normalized(value):
    """Compare supported time bounds and format sets using production predicates."""
    result = validate_conditions(conditions(value))
    if "formats" in result:
        result["formats"] = tuple(sorted(result["formats"]))
    return result


def validate_cases(value, *, captured=False, allow_empty=False):
    """Validate the shared ordered case contract before inspecting any reply."""
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "cases"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["cases"]) is not list
        or not (0 if allow_empty else 1) <= len(value["cases"]) <= 32
    ):
        raise ValueError("invalid cases")
    seen = set()
    for case in value["cases"]:
        if (
            type(case) is not dict
            or set(case) != {"id", "turns"}
            or not bounded_text(case["id"], 128)
            or not case["id"]
            or case["id"] in seen
            or type(case["turns"]) is not list
            or not (0 if allow_empty else 1) <= len(case["turns"]) <= 8
        ):
            raise ValueError("invalid case")
        seen.add(case["id"])
        for number, turn in enumerate(case["turns"], 1):
            fields = {"query", "expected"}
            if captured:
                fields |= {"response", "source_metadata"}
            if (
                type(turn) is not dict
                or set(turn) != fields
                or type(turn["expected"]) is not dict
            ):
                raise ValueError("invalid turn")
            checked_query(turn["query"])
            expected = turn["expected"]
            required = {"time", "formats", "retain_topic"}
            if (
                not required
                <= set(expected)
                <= required | {"status", "reason", "topic_changed"}
                or type(expected["retain_topic"]) is not bool
                or (number == 1 and expected["retain_topic"])
                or type(expected.get("topic_changed", False)) is not bool
                or (
                    expected.get("topic_changed")
                    and (number == 1 or expected["retain_topic"])
                )
            ):
                raise ValueError("invalid expectation")
            status = expected.get("status", "ok")
            if (
                type(status) is not str
                or status not in OUTCOMES
                or type(expected.get("reason", "none")) is not str
                or expected.get("reason", "none") not in OUTCOMES[status]
                or (status != "ok" and expected.get("topic_changed"))
            ):
                raise ValueError("invalid outcome")
            conditions(expected)
            if captured and (
                type(turn["response"]) is not dict
                or type(turn["source_metadata"]) is not dict
                or len(turn["source_metadata"]) > 20
            ):
                raise ValueError("invalid capture turn")
    return value


def plan_digest(plan):
    """Bind expectations and query order independently of JSON whitespace."""
    validate_cases(plan)
    raw = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def bind_plan(capture, plan):
    """Reject changed expectations while retaining missing turns in the denominator."""
    validate_cases(plan)
    planned = {case["id"]: case for case in plan["cases"]}
    for case in capture["cases"]:
        expected_case = planned.get(case["id"])
        if expected_case is None or len(case["turns"]) > len(expected_case["turns"]):
            raise ValueError("unplanned case or turn")
        for actual, expected in zip(case["turns"], expected_case["turns"]):
            if {key: actual[key] for key in ("query", "expected")} != expected:
                raise ValueError("query or expectation differs from plan")
