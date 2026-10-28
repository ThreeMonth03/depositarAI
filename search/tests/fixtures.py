"""Deterministic dataset reports and backend doubles shared by CPU tests."""

from __future__ import annotations

from types import SimpleNamespace

from search.catalog.query import normalize_query

import copy
import hashlib

from search.api.shared.backend import BackendFailure
from search.api.retrieval.results import RANKING, TOP_K

from search.reranker.contract import MODEL_REVISION


KEY = "1" * 64
IDS = [f"00000000-0000-0000-0000-{index:012d}" for index in range(1, 8)]
PLAN = {
    "action": "new_search",
    "topic": "氣候",
    "time_op": "keep",
    "time": None,
    "formats_op": "keep",
    "formats": [],
    "reason": "none",
}


def result(index: int, score: float) -> dict:
    return {
        "dataset_id": IDS[index - 1],
        "name": f"dataset-{index}",
        "title": f"第 {index} 個氣候資料集",
        "url": f"https://data.depositar.io/dataset/dataset-{index}",
        "notes": f"這是第 {index} 個候選資料的連續證據,包含 ERA5 與 WRF。",
        "organization_id": f"org-{index}",
        "organization_name": f"organization-{index}",
        "organization_title": f"研究機構 {index}",
        "tags": ["氣候", "臺灣"],
        "groups": ["研究資料"],
        "resource_formats": ["CSV", "PDF"],
        "resource_context": [],
        "license_id": "cc-by",
        "license_title": "Creative Commons Attribution",
        "metadata_created": "2026-01-01T00:00:00",
        "metadata_modified": "2026-09-10T00:00:00",
        "temporal_coverage": {"start": "2020", "end": "2025", "status": "complete"},
        "resource_count": 2,
        "rank_fusion_score": score,
        "searchers": ["opensearch-bm25", "opensearch-knn"],
    }


def report(query: str, count: int = TOP_K, *, conditions=None) -> dict:
    normalized = normalize_query(query)
    rows = [result(index, 1.0 / index) for index in range(1, count + 1)]
    return {
        "manifest_sha256": "b" * 64,
        "query_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "conditions": copy.deepcopy(conditions) if conditions is not None else {},
        "ranking": RANKING,
        "results": rows,
    }


def condition_document(start=None, end=None, bbox=None, formats=None):
    return {
        "temporal": {"start": start, "end": end},
        "bbox": {
            "status": "complete" if bbox is not None else "absent",
            **dict(zip(("x_min", "y_min", "x_max", "y_max"), bbox or [None] * 4)),
        },
        "resource_formats": formats or [],
    }


CONDITIONS = {"time": {"start": "2025-01-01", "end": "2025-12-31"}}


class RetrievalFixture:
    def __init__(self):
        self.calls = []
        self.retrieval = None
        self.rerank = {"order": [4, 3, 2, 1, 0], "model_revision": MODEL_REVISION}
        self.states = {
            IDS[0]: "unknown",
            IDS[1]: "match",
            IDS[2]: "match",
            IDS[3]: "match",
            IDS[4]: "match",
        }
        self.state_digest = "b" * 64
        self.state_error = None

    async def call(self, operation, payload, timeout):
        self.calls.append((operation, payload, timeout))
        if operation == "retrieve":
            value = (
                self.retrieval
                if self.retrieval is not None
                else report(payload["query"], conditions=payload["conditions"])
            )
        else:
            value = {
                "rerank": self.rerank,
                "condition_states": self.state_error
                or {
                    "states": self.states,
                    "manifest_sha256": self.state_digest,
                },
                "ready": {"ready": True, "documents": 5},
            }[operation]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class ConversationFixture:
    def __init__(self):
        self.calls = []
        self.plan = copy.deepcopy(PLAN)
        self.scope = {"decision": "search"}
        self.fail = None
        self.retrieval = None

    async def call(self, operation, payload, timeout):
        self.calls.append((operation, copy.deepcopy(payload), timeout))
        if operation == self.fail:
            raise BackendFailure("private error must not escape")
        if operation == "ready":
            return {"ready": True, "documents": 5, "reranker_ready": True}
        if operation == "scope":
            return self.scope
        if operation == "conversation_plan":
            return copy.deepcopy(self.plan)
        if operation == "retrieve":
            if isinstance(self.retrieval, Exception):
                raise self.retrieval
            return (
                copy.deepcopy(self.retrieval)
                if self.retrieval is not None
                else report(payload["query"], conditions=payload["conditions"])
            )
        if operation == "condition_states":
            return {
                "manifest_sha256": "b" * 64,
                "states": {
                    k: "unknown" if i == 0 else "match"
                    for i, k in enumerate(payload["ids"])
                },
            }
        if operation == "rerank":
            return {
                "model_revision": MODEL_REVISION,
                "order": list(range(len(payload["passages"]))),
            }
        raise AssertionError(operation)


class ConversationHTTPFixture:
    """A plain HTTP fixture whose owning test controls client/file cleanup."""

    def __init__(self, parent, backend=None, **settings):
        from pathlib import Path
        import tempfile

        from search.tests.asgi_client import ASGITestClient
        from search.api.conversation.app import create_app
        from search.api.shared.config import Settings

        temporary = tempfile.TemporaryDirectory()
        parent.addCleanup(temporary.cleanup)
        self.key_file = Path(temporary.name) / "key"
        self.key_file.write_text(KEY + "\n")
        self.key_file.chmod(0o600)
        self.settings = Settings(
            "a" * 40,
            "b" * 64,
            Path("/catalog"),
            self.key_file,
            **{"rerank_enabled": True, **settings},
        )
        self.backend = backend or ConversationFixture()
        self.client = ASGITestClient(
            create_app(self.settings, self.backend), raise_app_exceptions=False
        )
        parent.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer " + KEY}

    def turn(self, **body):
        return self.client.post(
            "/v1/turn", headers=self.headers, json={"query": "找氣候資料", **body}
        )


class ClosingResponse:
    """Model Python 3.13 clearing HTTPResponse.fp after a complete read1."""

    def __init__(
        self, body: bytes, url: str, *, declared_length: int | None = None
    ) -> None:
        self.code = 200
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(
                len(body) if declared_length is None else declared_length
            ),
        }
        self._body = body
        self._url = url
        self._closed = False
        self.fp = SimpleNamespace(
            raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=lambda _value: None))
        )

    def isclosed(self):
        return self._closed

    def read1(self, _size):
        body, self._body = self._body, b""
        if body:
            self._closed = True
            self.fp = None
        return body

    def geturl(self):
        return self._url

    def close(self):
        self._closed = True


class StaticOpener:
    def __init__(self, response) -> None:
        self.response = response

    def open(self, _request, timeout):
        self.timeout = timeout
        return self.response


class SequenceOpener:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)

    def open(self, _request, timeout):
        self.timeout = timeout
        return self.responses.pop(0)
