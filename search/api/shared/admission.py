"""Bound concurrent requests without queuing work on the event loop."""

from contextlib import asynccontextmanager


class Busy(RuntimeError):
    """The request cannot claim capacity immediately."""


class Slots:
    """Event-loop-owned counter: excess requests fail immediately, never queue."""

    def __init__(self, capacity):
        self.capacity, self.used = capacity, 0

    @asynccontextmanager
    async def claim(self):
        if self.used >= self.capacity:
            raise Busy("service busy")
        self.used += 1
        try:
            yield
        finally:
            self.used -= 1
