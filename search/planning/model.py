"""Bounded native Ollama planner; the worker owns transport and deadlines."""

import hashlib
import json
import time

from search.model_policy import (
    PLANNER_MODEL,
    PLANNER_MODEL_DIGEST,
    OLLAMA_BASE_URL,
)

from search.transport.http import HTTPRequest

OPTIONS = {"num_ctx": 16384, "num_predict": 2048, "temperature": 0, "seed": 11}


class LocalPlanner:
    """Generate with a pinned local model and the caller's prompt and schema."""

    def __init__(self, transport):
        self.transport = transport
        response = self.transport.send(
            HTTPRequest("GET", OLLAMA_BASE_URL + "/api/tags", ()),
            timeout=5,
            max_bytes=65536,
        )
        if response.status != 200 or not any(
            m.get("name") == PLANNER_MODEL and m.get("digest") == PLANNER_MODEL_DIGEST
            for m in json.loads(response.body).get("models", [])
        ):
            raise ValueError("model digest mismatch")

    def generate(self, prompt, query, schema, *, think="medium"):
        """Use the same pinned model, bounded transport and completion checks."""
        body = {
            "model": PLANNER_MODEL,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": query},
            ],
            "stream": False,
            "think": think,
            "format": schema,
            "options": OPTIONS,
            "keep_alive": "10m",
        }
        tick = time.perf_counter()
        response = self.transport.send(
            HTTPRequest(
                "POST",
                OLLAMA_BASE_URL + "/api/chat",
                (("Content-Type", "application/json"),),
                json.dumps(body, ensure_ascii=False).encode(),
            ),
            timeout=90,
            max_bytes=128 * 1024,
        )
        if response.status != 200:
            raise ValueError("model HTTP failure")
        raw = json.loads(response.body)
        visible = raw.get("message", {}).get("content", "")
        complete = raw.get("done") is True and raw.get("done_reason") == "stop"
        return {
            "status": "ok" if complete else "incomplete",
            "visible_output": visible[:8192],
            "thinking_characters": len(raw.get("message", {}).get("thinking", "")),
            "model_http_ms": (time.perf_counter() - tick) * 1000,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "options": OPTIONS,
            "think": think,
            **{
                key: raw.get(key)
                for key in (
                    "done_reason",
                    "prompt_eval_count",
                    "eval_count",
                    "load_duration",
                    "prompt_eval_duration",
                    "eval_duration",
                )
            },
        }
