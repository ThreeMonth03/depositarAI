"""Website response doubles and settings without loading HTTP test clients."""

from pathlib import Path

from search.api.conversation.contract import SearchState, TurnResponse
from search.web.upstream import WebSettings


ORIGIN = "http://127.0.0.1:18081"
SETTINGS = WebSettings(
    ORIGIN,
    "http://conversation-api:8000",
    Path("/unused"),
    "a" * 40,
)


class FakeUpstream:
    def __init__(self):
        self.calls = []
        self.error = None
        self.status = "ok"
        self.reason = "none"

    async def turn(self, payload):
        self.calls.append(payload)
        if self.error:
            raise self.error
        return TurnResponse(
            status=self.status,
            message="測試訊息",
            state=SearchState(topic="氣候", formats=["CSV"]),
            revision="b" * 40,
            reason=self.reason,
        )
