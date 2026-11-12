"""Run one admitted website turn, preserving commit and failure semantics."""

import asyncio
import time

from rest_framework.response import Response

from search.api.conversation.contract import TurnRequest
from search.planning.clock import normalize_context
from search.web.upstream import UpstreamError


def _commit(session, result):
    if result.status == "ok":
        session.state, session.pending = result.state, None
    session.version += 1


async def admitted_turn(payload, session, upstream, trace, context, problem):
    started, result, reason = time.monotonic(), None, "upstream_unavailable"
    snapshot, response = None, None
    try:
        data = TurnRequest(
            query=payload.query,
            state=session.state,
            pending=session.pending,
            context=context,
            top_k=payload.top_k,
        ).model_dump()
        try:
            snapshot = trace.begin(session, data)
        except Exception:
            trace.gap(session, "projection_invalid")
        result = await upstream.turn(data)
        if result.status == "unavailable":
            reason = "result_unavailable"
            response = problem(503, "upstream_unavailable")
        else:
            if result.status == "clarification_required":
                session.pending = session.pending or payload.query
            _commit(session, result)
            response = Response(
                {
                    "version": session.version,
                    "result": result.model_dump(),
                    "context_used": normalize_context(context),
                }
            )
    except UpstreamError as error:
        response = problem(error.status, "upstream_unavailable")
    except asyncio.CancelledError:
        reason = "cancelled"
        raise
    finally:
        session.busy = False
        trace.finish(session, snapshot, result, started, reason)
    response.data["trace"] = trace.status(session)
    return response
