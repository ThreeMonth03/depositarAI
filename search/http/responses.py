"""Render Django JSON responses at the raw ASGI request boundary."""

from django.http import JsonResponse

from .application import configure


def json_response(data, *, status=200, headers=None):
    """Adapt a bounded JSON response for middleware before Django dispatch."""
    configure()
    response = JsonResponse(
        data,
        status=status,
        headers=headers,
        json_dumps_params={"ensure_ascii": False, "allow_nan": False},
    )

    async def send_response(scope, receive, send):
        del scope, receive
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": [
                    (name.lower().encode("ascii"), value.encode("latin-1"))
                    for name, value in response.items()
                ],
            }
        )
        await send({"type": "http.response.body", "body": response.content})

    return send_response
