"""Shared wire limits and server-owned metadata projection, without ML imports."""

import math

MODEL_ID = "nvidia/llama-nemotron-rerank-1b-v2"
MODEL_REVISION = "828765652b05bd439c9789d2a6d093db1caa1443"
MAX_CANDIDATES = 20
MAX_PASSAGE_CHARS = 8192
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 4096
REQUEST_DEADLINE_S = 8
INFERENCE_BUDGET_S = 6


def passage(card):
    # Same ordering/template as the measured metadata-only experiment. Bounds
    # protect tokenizer CPU work before its independent 512-token truncation.
    return "\n".join(
        [
            "Title: " + card["title"],
            "Formats: " + ", ".join(card["resource_formats"]),
            "Tags: " + ", ".join(card["tags"]),
            "Organization: " + card["organization_title"],
            "Description: " + card["notes"],
        ]
    )[:MAX_PASSAGE_CHARS]


def prompt(query, document):
    return f"question:{query} \n \n passage:{document}"


def validate_request(value):
    if type(value) is not dict or set(value) != {"query", "passages"}:
        raise ValueError("invalid ranking request")
    query, passages = value["query"], value["passages"]
    if type(query) is not str or not 1 <= len(query) <= 512:
        raise ValueError("invalid ranking query")
    if type(passages) is not list or not 1 <= len(passages) <= MAX_CANDIDATES:
        raise ValueError("invalid ranking candidates")
    for text, maximum in [
        (query, 512),
        *((text, MAX_PASSAGE_CHARS) for text in passages),
    ]:
        if type(text) is not str or not 1 <= len(text) <= maximum or "\x00" in text:
            raise ValueError("invalid ranking text")
        text.encode("utf-8", "strict")
    return query, passages


def validate_order(value, count):
    if (
        type(value) is not dict
        or set(value) != {"order", "model_revision"}
        or value["model_revision"] != MODEL_REVISION
    ):
        raise ValueError("invalid ranking response")
    order = value["order"]
    if (
        type(order) is not list
        or len(order) != count
        or any(type(i) is not int for i in order)
        or sorted(order) != list(range(count))
    ):
        raise ValueError("ranking is not a complete permutation")
    return order


def rank_scores(scores):
    if not scores or any(
        type(s) not in (float, int) or not math.isfinite(s) for s in scores
    ):
        raise ValueError("invalid ranking scores")
    return {
        "order": sorted(range(len(scores)), key=lambda i: (-scores[i], i)),
        "model_revision": MODEL_REVISION,
    }
