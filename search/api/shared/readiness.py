"""Shared bounded backend readiness, separate from process liveness/warmup."""

from __future__ import annotations

import asyncio
import math
import time

from search.api.shared.admission import Slots
from search.api.shared.backend import BackendFailure, BackendTimeout


CACHE_SECONDS = 30
BACKEND_SECONDS = 25
MAX_DOCUMENTS = 10000


class BackendReadiness:
    """Cache successful query/model identity probes, not GPU residency claims.

    The worker validates the sealed bundle, Fess query credentials/results and
    model digests; native mode also probes its filtered index and embedding.
    Actual GPU isolation/residency and full HTTP acceptance remain deployment
    checks. Optional reranker failure is reported, not silently marked healthy.
    """

    def __init__(self, backend, *, clock=time.monotonic):
        self.backend, self.clock = backend, clock
        self.slots = Slots(1)
        self._checked, self._value = None, None

    async def check(self):
        tick = self.clock()
        if not math.isfinite(tick):
            raise BackendFailure("readiness clock invalid")
        if (
            self._checked is not None
            and 0 <= tick - self._checked < CACHE_SECONDS
            and self._value is not None
        ):
            return dict(self._value)
        async with self.slots.claim():
            try:
                value = await asyncio.wait_for(
                    self.backend.call("ready", {}, BACKEND_SECONDS),
                    timeout=BACKEND_SECONDS,
                )
            except asyncio.TimeoutError:
                raise BackendTimeout("readiness deadline exceeded") from None
            if (
                type(value) is not dict
                or not {"ready", "documents"} <= set(value)
                or set(value) - {"ready", "documents", "reranker_ready"}
                or value["ready"] is not True
                or type(value["documents"]) is not int
                or not 1 <= value["documents"] <= MAX_DOCUMENTS
                or "reranker_ready" in value
                and type(value["reranker_ready"]) is not bool
            ):
                raise BackendFailure("invalid readiness result")
            checked = self.clock()
            if (
                not math.isfinite(checked)
                or checked < tick
                or checked - tick >= BACKEND_SECONDS
            ):
                raise BackendFailure("readiness deadline exceeded")
            self._checked, self._value = checked, dict(value)
            return dict(value)
