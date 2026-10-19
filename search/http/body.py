"""Bound raw request bytes before Django buffers the request body."""

import asyncio

from asgiref.typing import ASGIReceiveCallable


async def bounded_body(
    receive: ASGIReceiveCallable, *, limit: int, timeout: float = 5
) -> bytes:
    """Read one body within an actual byte limit and an overall deadline.

    Raise ValueError for excess bytes, disconnects or invalid ASGI messages.
    TimeoutError and caller cancellation propagate after receive cleanup.
    """

    async def collect():
        body = bytearray()
        while True:
            message = await receive()
            if not isinstance(message, dict) or message.get("type") != "http.request":
                raise ValueError("Invalid request body stream")
            chunk = message.get("body", b"")
            more_body = message.get("more_body", False)
            if not isinstance(chunk, bytes) or not isinstance(more_body, bool):
                raise ValueError("Invalid request body stream")
            if len(body) + len(chunk) > limit:
                raise ValueError("Request body exceeds limit")
            body.extend(chunk)
            if not more_body:
                return bytes(body)

    return await asyncio.wait_for(collect(), timeout=timeout)
