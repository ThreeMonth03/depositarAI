"""Single bounded operation; model text and exceptions never reach stdout."""

from search.catalog.bundle import CatalogBundle

import json

from search.scope import OllamaScopeClassifier
from search.model_policy import (
    OLLAMA_BASE_URL,
    PLANNER_MODEL,
    PLANNER_MODEL_DIGEST,
)

from search.reranker.contract import (
    MAX_RESPONSE_BYTES,
    MODEL_REVISION,
    validate_order,
    validate_request,
)
from .shared.config import Settings
from .shared.model_transport import ModelTransport
from .shared.peers import service_address
from search.serialization import strict_json
from .shared.backend import SubprocessBackend
from search.transport.http import HTTPRequest, UrllibTransport
from .shared.worker_io import run_worker


def run(operation, payload, settings):
    if operation not in SubprocessBackend.operations:
        raise ValueError("unsupported operation")
    if operation == "rerank":
        if not settings.rerank_enabled:
            raise ValueError("reranker is disabled")
        _, passages = validate_request(payload)
        url = service_address("reranker", 8001) + "/rerank"
        response = UrllibTransport().send(
            HTTPRequest(
                "POST",
                url,
                (("Content-Type", "application/json"),),
                json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(),
            ),
            # The parent process owns the shorter eight-second hard deadline.
            timeout=9,
            max_bytes=MAX_RESPONSE_BYTES,
        )
        if response.status != 200 or response.url != url:
            raise ValueError("reranker unavailable")
        value = strict_json(response.body)
        validate_order(value, len(passages))
        return value
    if operation == "scope":
        return {
            "decision": OllamaScopeClassifier(
                transport=ModelTransport("scope", keep_alive=-1)
            ).classify(payload["query"])
        }
    if operation == "retrieve":
        from .retrieval.index import search

        return search(payload, settings)
    if operation == "ready":
        from .retrieval.index import search

        bundle = CatalogBundle.load(
            settings.bundle_path, expected_manifest_sha256=settings.manifest_sha256
        )
        # Both requests use the same query-only index and catalog authority.
        for conditions in ({}, {"time": {"start": "2025", "end": "2025"}}):
            search({"query": "氣候", "top_k": 1, "conditions": conditions}, settings)
        for role, model_name, model_digest in (
            ("scope", OllamaScopeClassifier.model, OllamaScopeClassifier.model_digest),
            ("answer", PLANNER_MODEL, PLANNER_MODEL_DIGEST),
        ):
            transport = ModelTransport(role)
            response = transport.send(
                HTTPRequest("GET", OLLAMA_BASE_URL + "/api/tags", ()),
                timeout=3,
                max_bytes=128 * 1024,
            )
            data = strict_json(response.body)
            if response.status != 200 or not any(
                model.get("name") == model_name and model.get("digest") == model_digest
                for model in data.get("models", [])
            ):
                raise ValueError("model identity mismatch")
        result = {"ready": True, "documents": len(bundle.documents)}
        if settings.rerank_enabled:
            result["reranker_ready"] = False
            try:
                url = service_address("reranker", 8001) + "/healthz"
                response = UrllibTransport().send(
                    HTTPRequest("GET", url, ()), timeout=2, max_bytes=MAX_RESPONSE_BYTES
                )
                result["reranker_ready"] = (
                    response.status == 200
                    and response.url == url
                    and strict_json(response.body)
                    == {"ready": True, "model_revision": MODEL_REVISION}
                )
            except Exception:
                pass  # Optional ranking may degrade without taking search offline.
        return result
    raise ValueError("unknown worker operation")


def main():
    return run_worker(
        lambda operation, payload: run(operation, payload, Settings.from_environment())
    )


if __name__ == "__main__":
    raise SystemExit(main())
