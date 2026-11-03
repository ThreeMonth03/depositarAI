"""Bound raw request bytes before Django buffers the request body."""

import asyncio

from asgiref.typing import ASGIReceiveCallable


def replay_body(
    body: bytes, receive: ASGIReceiveCallable
) -> ASGIReceiveCallable:
    """Replay validated bytes once, then forward original receive events."""
    consumed = False

    async def replay():
        nonlocal consumed
        if not consumed:
            consumed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


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
            if (
                not isinstance(message, dict)
                or message.get("type") != "http.request"
            ):
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
