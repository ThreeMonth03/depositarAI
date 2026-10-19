"""One classification, one bounded state patch, then existing retrieval/reranking."""

import asyncio
import logging
import re
import time

from search.planning.clock import normalize_context
from search.planning.predicates import FORMATS, date_bounds

from ..shared.admission import Slots
from ..shared.backend import BackendFailure
from .calendar import calendar_text, corrected_pending
from .time import relative_literals, resolve_relative
from .contract import (
    MESSAGES,
    Interval,
    TurnPlan,
    TurnResponse,
)
from ..retrieval.engine import RetrievalEngine, public_conditions
from ..retrieval.schemas import RetrievalRequest, RetrievalResponse

LOGGER = logging.getLogger(__name__)


def has_bare_recent(query):
    """Conservative lexical guard, not a general natural-language date parser.

    A quantified duration still goes to the planner. Unqualified recency must
    be clarified even if the model would silently drop it or invent a range.
    Inspect only this turn, so an explicit answer can resolve pending recency.
    """
    quantified = re.sub(
        r"(?:最近|近期)\s*[0-9一二兩三四五六七八九十百]+\s*(?:年|個?月|天|日|週|周)"
        r"|\brecent\s+(?:[0-9]+|one|two|three|four|five|six|seven|eight|nine|ten)"
        r"\s+(?:years?|months?|days?|weeks?)\b",
        "",
        query,
        flags=re.IGNORECASE,
    )
    return bool(re.search(r"最近|近期|\brecent(?:ly)?\b", quantified, re.IGNORECASE))


def has_spatial_constraint(query):
    """Conservatively explain unsupported spatial wording; never geocode it."""
    return bool(
        re.search(
            r"附近|鄰近|邻近|半徑|半径|方圓|方圆|"
            r"[0-9一二兩三四五六七八九十百]+\s*(?:公里|公尺)\s*(?:以?內|以?内)|"
            r"\bnearby\b|\bwithin\s+[0-9]+\s*(?:km|kilomet(?:er|re)s?|meters?)\b",
            query,
            re.IGNORECASE,
        )
    )


def short_non_date_reply(query):
    """A format or acknowledgement cannot settle a pending calendar question."""
    return bool(
        re.fullmatch(
            r"(?:(?:只要|只看|改成|改為|改为|格式改成|格式改為)\s*)?(?:"
            + "|".join(FORMATS)
            + r")|是|對|对|好|好的|yes|ok|民國|民国|西元|公元",
            query,
            re.IGNORECASE,
        )
    )


def literal_refinement(request):
    """Already-scoped session + an entire date/format literal, never free text.

    Scope classification can misclassify bare clarification replies. Such
    literals cannot contain instructions; all other text still goes through it.
    The planner and condition validator still run, even for a literal reply.
    """
    if not (request.state.topic or request.pending):
        return False
    if request.query.upper() in FORMATS:
        return True
    calendar = calendar_text(request.query)
    if not calendar.reason and calendar.literal:
        return True
    if not re.fullmatch(r"[0-9]{4}(?:年|(?:-[0-9]{1,2}){0,2})", request.query):
        return False
    try:
        date_bounds(request.query.removesuffix("年"))
    except ValueError:
        return False
    return True


class ConversationEngine:
    def __init__(self, settings, backend):
        self.settings, self.backend = settings, backend
        self.search = RetrievalEngine(backend, settings)
        self.slots = Slots(1)

    async def turn(self, request, request_id):
        async with self.slots.claim():
            start, timing = time.monotonic(), {}
            stage = "scope"

            async def call(operation, payload, timeout):
                nonlocal stage
                stage = operation
                tick = time.monotonic()
                try:
                    return await self.backend.call(operation, payload, timeout)
                finally:
                    timing[operation] = round((time.monotonic() - tick) * 1000, 3)

            def response(
                status, message, state=None, search=None, reason="none", notices=()
            ):
                timing["total"] = round((time.monotonic() - start) * 1000, 3)
                return TurnResponse(
                    status=status,
                    message=message,
                    state=state or request.state,
                    search=search,
                    reason=reason,
                    timing_ms=timing,
                    revision=self.settings.revision,
                    notices=list(notices),
                )

            try:
                async with asyncio.timeout(120):
                    # Latest request must stay intact. Context only helps classify a
                    # short follow-up; it does not confer trust on the latest text.
                    scope_query = request.query
                    if request.state.topic or request.pending:
                        prefix = f"找資料集：{request.state.topic or request.pending}；本輪搜尋條件："
                        budget = max(0, 512 - len(scope_query))
                        scope_query = prefix[:budget] + scope_query
                    verdict = (
                        {"decision": "search"}
                        if literal_refinement(request)
                        else await call("scope", {"query": scope_query}, 20)
                    )
                    if (
                        type(verdict) is not dict
                        or set(verdict) != {"decision"}
                        or verdict["decision"]
                        not in {"search", "unsafe", "out_of_scope"}
                    ):
                        raise ValueError("invalid scope verdict")
                    if verdict["decision"] != "search":
                        reason = (
                            "unsafe" if verdict["decision"] == "unsafe" else "off_topic"
                        )
                        return response("rejected", MESSAGES[reason], reason=reason)
                    if has_spatial_constraint(request.query):
                        return response(
                            "clarification_required",
                            MESSAGES["location_not_supported"],
                            reason="location_not_supported",
                        )
                    calendar = calendar_text(request.query)
                    if calendar.reason:
                        return response(
                            "clarification_required",
                            calendar.message,
                            reason=calendar.reason,
                        )
                    # A bare format cannot resolve a pending calendar ambiguity.
                    # Keep the original pending query until a supported date or a
                    # complete new subject is supplied; never guess its year.
                    if request.pending and short_non_date_reply(request.query):
                        pending_calendar = calendar_text(request.pending)
                        if pending_calendar.reason:
                            return response(
                                "clarification_required",
                                pending_calendar.message,
                                reason=pending_calendar.reason,
                            )
                    if has_bare_recent(request.query):
                        return response(
                            "clarification_required",
                            MESSAGES["ambiguous_time"],
                            reason="ambiguous_time",
                        )
                    relative_cues = (
                        relative_literals(request.query)
                        if not calendar.lunar_date
                        else []
                    )
                    if relative_cues and request.context is None:
                        return response(
                            "clarification_required",
                            MESSAGES["ambiguous_time"],
                            reason="ambiguous_time",
                        )
                    planning_request = request.model_copy(
                        update={
                            "query": calendar.query,
                            "pending": corrected_pending(request.pending, calendar),
                        }
                    )
                    output = await call(
                        "conversation_plan", planning_request.model_dump(), 60
                    )
                    if type(output) is dict and output.get("error") in {
                        "planner_incomplete",
                        "planner_invalid_output",
                    }:
                        LOGGER.warning(
                            "conversation_plan_failed request_id=%s code=%s",
                            request_id,
                            output["error"],
                        )
                        raise BackendFailure(
                            "planner did not produce an executable plan"
                        )
                    plan = TurnPlan.model_validate(output)
                    if plan.action in {"reject", "clarify"}:
                        return response(
                            "rejected"
                            if plan.action == "reject"
                            else "clarification_required",
                            MESSAGES[plan.reason],
                            reason=plan.reason,
                        )
                    if plan.action == "refine" and not request.state.topic:
                        # A model cannot refine a subject that was never committed.
                        # The generation schema also excludes this action. Keep a
                        # safe clarification if an adapter violates that contract.
                        return response(
                            "clarification_required",
                            MESSAGES["missing_topic"],
                            reason="missing_topic",
                        )
                    if plan.relative_time is not None:
                        try:
                            if calendar.lunar_date or calendar.exact:
                                raise ValueError(
                                    "relative and explicit calendar conditions conflict"
                                )
                            interval = resolve_relative(
                                plan.relative_time,
                                request.context.model_dump()
                                if request.context
                                else None,
                                request.query,
                            )
                        except (ValueError, OverflowError):
                            return response(
                                "clarification_required",
                                MESSAGES["ambiguous_time"],
                                reason="ambiguous_time",
                            )
                        plan = TurnPlan.model_validate(
                            {
                                **plan.model_dump(),
                                "time_op": "set",
                                "time": interval,
                                "relative_time": None,
                            }
                        )
                    elif relative_cues:
                        explicit_clear = plan.time_op == "clear" and re.search(
                            r"取消|不限|移除|清除|\b(?:remove|clear)\b",
                            request.query,
                            re.IGNORECASE,
                        )
                        if not explicit_clear:
                            # No fallback to dates invented by the model, or a
                            # silent keep that drops a relative restriction.
                            return response(
                                "clarification_required",
                                MESSAGES["ambiguous_time"],
                                reason="ambiguous_time",
                            )
                    if plan.lunar_time:
                        # The model may select words, not invent a lunar date.
                        sources = (request.query, planning_request.pending or "")
                        if not any(plan.lunar_time.text in text for text in sources):
                            raise ValueError("lunar text was not supplied by user")
                        # Store selected time words only in their own field, so
                        # a later civil correction cannot leave them in topic.
                        if plan.topic and plan.lunar_time.text in plan.topic:
                            topic = plan.topic.replace(plan.lunar_time.text, "").strip()
                            if not topic:
                                return response(
                                    "clarification_required",
                                    MESSAGES["missing_topic"],
                                    reason="missing_topic",
                                )
                            plan = TurnPlan.model_validate(
                                {**plan.model_dump(), "topic": topic}
                            )
                    if calendar.lunar_date and plan.time_op == "set":
                        # A valid Gregorian-shaped value is still an invented
                        # conversion when the source is an explicit lunar date.
                        raise ValueError("cannot apply civil interval to lunar input")
                    if calendar.exact and not plan.lunar_time:
                        # This narrow, deterministic date result is more reliable
                        # than asking the model to do ROC arithmetic. Qualifiers,
                        # ranges and non-search actions never take this path.
                        plan = TurnPlan.model_validate(
                            {
                                **plan.model_dump(),
                                "time_op": "set",
                                "time": Interval(
                                    start=calendar.exact, end=calendar.exact
                                ),
                            }
                        )
                    stage = "state_application"
                    state = plan.apply(request.state)
                    retrieval_query = state.topic
                    notices = [] if state.unapplied_time else list(calendar.notices)
                    if state.unapplied_time:
                        retrieval_query += " " + state.unapplied_time.text
                        notices.append(MESSAGES["lunar_time_unapplied"])
                    # Retrieval accepts only verified state, not another LLM request.
                    query = RetrievalRequest(
                        query=retrieval_query,
                        top_k=request.top_k,
                        conditions=public_conditions(
                            {
                                "time": state.time.model_dump() if state.time else None,
                                "formats": state.formats,
                            }
                        ),
                    )
                    stage = "retrieval_pipeline"
                    result = await self.search.search(query, request_id)
                    result["context_used"] = normalize_context(
                        request.context.model_dump() if request.context else None
                    )
                    search = RetrievalResponse.model_validate(result)
                    timing.update(
                        {"search_" + k: v for k, v in result["timing_ms"].items()}
                    )
                    if result["status"] != "ok":
                        LOGGER.warning(
                            "conversation_failed request_id=%s stage=retrieval_pipeline",
                            request_id,
                        )
                        return response(
                            "unavailable",
                            "搜尋暫時無法完成；本輪未更改搜尋條件，請稍後重試。",
                        )
                    return response(
                        "ok",
                        f"找到 {len(search.cards)} 筆候選資料集。你可以繼續調整時間、格式或搜尋主題。",
                        state,
                        search,
                        notices=notices,
                    )
            except (
                BackendFailure,
                ValueError,
                TypeError,
                KeyError,
                TimeoutError,
            ) as error:
                # No query, model output, credentials or exception text in logs.
                LOGGER.warning(
                    "conversation_failed request_id=%s stage=%s kind=%s",
                    request_id,
                    stage,
                    type(error).__name__,
                )
                return response(
                    "unavailable", "搜尋暫時無法完成；本輪未更改搜尋條件，請稍後重試。"
                )
