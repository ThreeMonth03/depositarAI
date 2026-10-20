"""Retrieve verified search state, filter before ranking and bound every stage."""

from search.catalog.query import normalize_query

from datetime import date
import time
import unicodedata

from .results import RANKING, ValidatedSearch

from search.planning.predicates import validate_conditions
from search.reranker.contract import REQUEST_DEADLINE_S, passage, validate_order
from ..shared.admission import Busy, Slots
from ..shared.backend import BackendFailure, BackendTimeout


def public_conditions(plan):
    value = plan["time"]
    interval = validate_conditions({"time": value})["time"] if value else None
    return {
        "time": {
            "start": date.fromordinal(interval[0]).isoformat()
            if value["start"]
            else None,
            "end": date.fromordinal(interval[1]).isoformat() if value["end"] else None,
            "field": "data_coverage",
            "relation": "overlaps",
            "endpoints": "inclusive",
        }
        if interval
        else None,
        "place": None,
        "formats": list(plan["formats"]),
    }


def executable_conditions(parsed):
    conditions = {}
    if parsed["time"] is not None:
        conditions["time"] = {key: parsed["time"][key] for key in ("start", "end")}
    if parsed["formats"]:
        conditions["formats"] = parsed["formats"]
    validate_conditions(conditions)
    return conditions


class RetrievalEngine:
    def __init__(self, backend, settings):
        self.backend, self.settings = backend, settings
        self.retrieval_slots = Slots(2)
        self.rerank_slots = Slots(1)

    def candidate_limit(self, request):
        return (
            max(request.top_k, self.settings.rerank_candidates)
            if self.settings.rerank_enabled
            else request.top_k
        )

    async def retrieve_candidates(self, request, result, call):
        conditions = executable_conditions(result["parsed_conditions"])
        maximum = self.candidate_limit(request)
        envelope = await call(
            "retrieve",
            {
                "query": normalize_query(request.query),
                "top_k": maximum,
                "conditions": conditions,
            },
            25,
        )
        return ValidatedSearch.from_report(
            request.query,
            envelope,
            conditions=conditions,
            manifest_sha256=self.settings.manifest_sha256,
            maximum=maximum,
        )

    async def verify_conditions(self, result, found, call):
        conditions = executable_conditions(result["parsed_conditions"])
        if not conditions:
            result["condition_counts"]["not_checked"] = len(found.rows)
            return found
        ids = [row["dataset_id"] for row in found.rows]
        states = {}
        if ids:
            response = await call(
                "condition_states", {"ids": ids, "conditions": conditions}, 10
            )
            if (
                type(response) is not dict
                or set(response) != {"states", "manifest_sha256"}
                or response["manifest_sha256"] != self.settings.manifest_sha256
            ):
                raise ValueError("condition metadata identity mismatch")
            states = response["states"]
            if (
                type(states) is not dict
                or set(states) != set(ids)
                or any(
                    type(s) is not str or s not in {"match", "outside", "unknown"}
                    for s in states.values()
                )
            ):
                raise ValueError("incomplete or invalid condition labels")
        result["_condition_states"] = states
        if "outside" in states.values():
            raise ValueError("index prefilter disagrees with sealed metadata")
        result["applied_conditions"] = result["parsed_conditions"]
        result["filter_scope"] = "index_prefilter"
        for status in states.values():
            result["condition_counts"][status] += 1
        if "unknown" in states.values():
            result["warnings"].append("unknown_condition_results_retained")
        return found

    async def search(self, request, request_id):
        start = time.monotonic()
        result = {
            "api_version": "1-draft",
            "request_id": request_id,
            "revision": self.settings.revision,
            "catalog_manifest_sha256": self.settings.manifest_sha256,
            "mode": "ai",
            "status": "ok",
            "decision": "search",
            "cards": [],
            "ranking": RANKING,
            "rerank_status": "not_run" if self.settings.rerank_enabled else "disabled",
            "candidate_count": 0,
            "warnings": [],
            "timing_ms": {},
            "context_used": None,
            "parsed_conditions": request.conditions.model_dump(),
            "applied_conditions": None,
            "filter_scope": "none",
            "condition_counts": {
                "match": 0,
                "unknown": 0,
                "outside": 0,
                "not_checked": 0,
            },
        }

        async def call(stage, payload, timeout):
            clock = time.monotonic()
            try:
                remaining = 120 - (clock - start)
                if remaining <= 0:
                    raise BackendTimeout("request deadline exhausted")
                return await self.backend.call(stage, payload, min(timeout, remaining))
            finally:
                result["timing_ms"][stage] = round((time.monotonic() - clock) * 1000, 3)

        def finish():
            result.pop("_condition_states", None)
            result["timing_ms"]["total"] = round((time.monotonic() - start) * 1000, 3)
            return result

        try:
            async with self.retrieval_slots.claim():
                found = await self.retrieve_candidates(request, result, call)
        except Busy:
            raise
        except (BackendFailure, ValueError, TypeError, KeyError):
            result.update(status="unavailable", warnings=["retrieval_unavailable"])
            return finish()
        result["candidate_count"] = len(found.rows)
        try:
            found = await self.verify_conditions(result, found, call)
        except (BackendFailure, ValueError, TypeError, KeyError):
            result.update(status="unavailable")
            result["warnings"].append("condition_filter_unavailable")
            return finish()
        if self.settings.rerank_enabled and found.rows:
            result["rerank_status"] = "fallback"
            try:
                async with self.rerank_slots.claim():
                    ranked = await call(
                        "rerank",
                        {
                            "query": unicodedata.normalize("NFKC", request.query),
                            "passages": [passage(row) for row in found.rows],
                        },
                        REQUEST_DEADLINE_S,
                    )
                order = validate_order(ranked, len(found.rows))
                found = ValidatedSearch(
                    tuple(found.rows[i] for i in order), found.query_sha256
                )
                result.update(ranking="nvidia-rerank", rerank_status="applied")
            except BackendTimeout:
                result["warnings"].append("rerank_timeout")
            except Busy:
                result["warnings"].append("rerank_busy")
            except (BackendFailure, ValueError, TypeError):
                result["warnings"].append("rerank_unavailable")
        found = ValidatedSearch(found.rows[: request.top_k], found.query_sha256)
        result["cards"] = found.cards()
        states = result.get("_condition_states", {})
        for card in result["cards"]:
            card["condition_status"] = states.get(card["dataset_id"], "not_checked")
        return finish()
