"""CI-only fixtures: real HTTP on both sides, deterministic inference boundary."""

from pathlib import Path

from search.api.shared.config import Settings
from search.api.conversation.calendar import calendar_text
from search.api.conversation.app import create_app as gpu_app
from search.api.conversation.time import literal_spec, relative_literals
from search.tests.fixtures import KEY, ConversationFixture, PLAN
from search.web.app import create_app as website_app
from search.web.upstream import WebSettings


def key():
    path = Path("/tmp/test-api-key")
    path.write_text(KEY + "\n")
    path.chmod(0o600)
    return path


class Backend(ConversationFixture):
    async def call(self, operation, payload, timeout):
        if operation == "scope":
            return {
                "decision": "out_of_scope" if "Python" in payload["query"] else "search"
            }
        if operation == "conversation_plan":
            query = payload["query"]
            if "附近" in query:
                return {
                    **PLAN,
                    "action": "clarify",
                    "topic": None,
                    "reason": "location_not_supported",
                }
            if calendar_text(query).lunar_date:
                return {
                    **PLAN,
                    "action": "refine" if payload["state"]["topic"] else "new_search",
                    "topic": None if payload["state"]["topic"] else "澎湖古蹟",
                    "time_op": "lunar",
                    "lunar_time": {"calendar": "lunar", "text": query},
                }
            if "農曆" in query:
                return {**PLAN, "topic": query}
            relative = relative_literals(query)
            if len(relative) == 1:
                mode, unit, value = literal_spec(relative[0])
                return {
                    **PLAN,
                    "action": "refine" if payload["state"]["topic"] else "new_search",
                    "topic": None if payload["state"]["topic"] else "氣候",
                    "time_op": "relative",
                    "relative_time": {
                        "kind": "relative",
                        "mode": mode,
                        "unit": unit,
                        "value": value,
                        "text": relative[0],
                    },
                }
            if payload["state"]["topic"]:
                result = {**PLAN, "action": "refine", "topic": None}
                if "CSV" in query:
                    result.update(formats_op="set", formats=["CSV"])
                elif "2024" in query:
                    result.update(time_op="set", time={"start": "2024", "end": "2024"})
                elif "取消時間" in query:
                    result.update(time_op="clear")
                return result
            if "澎湖" in (payload.get("pending") or query):
                return {**PLAN, "topic": "澎湖古蹟"}
            return {**PLAN, "time_op": "set", "time": {"start": "2025", "end": "2025"}}
        return await super().call(operation, payload, timeout)


def conversation():
    path = key()
    return gpu_app(
        Settings("a" * 40, "b" * 64, Path("/catalog"), path, True, 20), Backend()
    )


def website():
    return website_app(
        WebSettings(
            "http://127.0.0.1:8080", "http://conversation-api:8000", key(), "a" * 40
        )
    )
