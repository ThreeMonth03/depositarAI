"""Adapt a sealed legacy trace prefix to the existing offline evaluator."""

import copy
from collections.abc import Mapping
from pathlib import Path

from search.catalog.bundle import CatalogBundle
from search.quality.contract import validate_cases
from .contract import validate_event


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _events(exported):
    if (
        type(exported) is not dict
        or set(exported) != {"trace_version", "manifest", "complete", "events"}
        or exported["trace_version"] != 1
        or exported["complete"] is not True
        or type(exported["manifest"]) is not dict
        or exported["manifest"].get("sealed") is not True
        or exported["manifest"].get("gaps") != []
        or type(exported["events"]) is not list
        or not 1 <= len(exported["events"]) <= 100
    ):
        raise ValueError("trace_quality_incomplete")
    events = [validate_event(value) for value in exported["events"]]
    turns = list(range(1, len(events) + 1))
    if (
        [event["turn"] for event in events] != turns
        or exported["manifest"].get("turns") != turns
        or any(not event["capture"]["complete"] for event in events)
        or any(
            event["test_session_id"]
            != exported["manifest"].get("test_session_id")
            for event in events
        )
    ):
        raise ValueError("trace_quality_incomplete")
    return events


def _fresh_prefix(events):
    first = events[0]
    if first["identities"]["bff_revision"] is None:
        raise ValueError("trace_quality_source_identity_unavailable")
    empty = {"topic": "", "time": None, "formats": [], "unapplied_time": None}
    if first["state_before"] != empty or first["pending_before"] is not None:
        raise ValueError("trace_quality_fresh_prefix_required")
    for event in events:
        if (
            event["epoch"] != first["epoch"]
            or event["consent"] != first["consent"]
            or event["identities"]["bff_revision"]
            != first["identities"]["bff_revision"]
            or event["identities"]["capture_config_sha256"]
            != first["identities"]["capture_config_sha256"]
        ):
            raise ValueError("trace_quality_session_identity_changed")


def _case(plan, identifier):
    validate_cases(plan)
    if len(plan["cases"]) != 1 or plan["cases"][0]["id"] != identifier:
        raise ValueError("trace_quality_one_independent_case_required")
    return plan["cases"][0]


def _reply(event, number, manifest):
    output = copy.deepcopy(event["output"])
    if (
        event["execution"]["kind"] != "legacy_conditions"
        or output["status"] == "unavailable"
        or output["version"] != number
        or event["identities"]["backend_revision"] is None
    ):
        raise ValueError("trace_quality_legacy_prefix_required")
    search = output["search"]
    if search is not None:
        if (
            search["catalog_manifest_sha256"] != manifest
            or event["identities"]["catalog_manifest_sha256"] != manifest
            or search["parsed_conditions"]
            != event["execution"]["parsed_conditions"]
            or search["applied_conditions"]
            != event["execution"]["applied_conditions"]
        ):
            raise ValueError("trace_quality_identity_mismatch")
    version = output.pop("version")
    output["revision"] = event["identities"]["backend_revision"]
    return {"version": version, "result": output}


def capture(exported, plan, identifier, bundle_directory, manifest):
    """Bind actual evidence to an independent plan and matching sealed bundle.

    Only a continuous prefix from turn one is supported. SQL fixtures and
    unavailable attempts cannot be compressed into evaluator-compatible turns.
    """
    events, case = _events(exported), _case(plan, identifier)
    _fresh_prefix(events)
    if len(case["turns"]) > len(events):
        raise ValueError("trace_quality_prefix_missing")
    bundle = CatalogBundle.load(
        Path(bundle_directory), expected_manifest_sha256=manifest
    )
    turns = []
    previous = None
    for number, expected in enumerate(case["turns"], 1):
        event = events[number - 1]
        if event["input"]["query"] != expected["query"]:
            raise ValueError("trace_quality_plan_query_mismatch")
        if previous is not None and (
            event["state_before"] != previous["state_after"]
            or event["pending_before"] != previous["pending_after"]
        ):
            raise ValueError("trace_quality_state_discontinuous")
        reply = _reply(event, number, bundle.manifest_sha256)
        search = reply["result"]["search"]
        identifiers = (
            [card["dataset_id"] for card in search["cards"]]
            if search is not None
            else []
        )
        metadata = {
            key: _plain(bundle.condition_metadata[key])
            for key in identifiers
            if key in bundle.condition_metadata
        }
        turns.append(
            {
                "query": expected["query"],
                "expected": copy.deepcopy(expected["expected"]),
                "response": reply,
                "source_metadata": metadata,
            }
        )
        previous = event
    value = {
        "schema_version": 1,
        "cases": [{"id": identifier, "turns": turns}],
    }
    return validate_cases(value, captured=True)
