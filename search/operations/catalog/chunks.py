"""Validate Fess's ordered, non-overlapping native chunk/vector representation."""

from __future__ import annotations

import hashlib
import math

from search.catalog.build.policy import STRING_LIMITS
from search.operations.shared import private_io


DIMENSIONS = 768
MAX_CONTENT_CHARACTERS = STRING_LIMITS["fess_content"]
PROFILE = {
    "chunker.name": "length",
    "length.chunk_size": "800",
    "length.overlap": "0",
    "length.boundary.enabled": "true",
    "length.boundary.lookback_percent": "20",
    "length.boundary.lookahead_percent": "5",
    "max_chunks_per_document": "1000",
}
MAX_CHUNKS = int(PROFILE["max_chunks_per_document"])
JAVA_OPTIONS = tuple(
    "-Dfess.system.content_chunker." + key + "=" + value
    for key, value in PROFILE.items()
)


def _finite(number):
    if type(number) not in {int, float}:
        return False
    try:
        return math.isfinite(number)
    except OverflowError:
        return False


def validate(content_chunks, vectors, original_content):
    """Copy aligned native arrays whose text exactly reproduces the sealed input.

    The fixed Fess length-chunker profile has zero overlap and preserves every
    character. This verifies its result without implementing Java chunking in
    Python; alternate profiles must not use this validator or its cache key.
    """
    if (
        type(original_content) is not str
        or not 1 <= len(original_content) <= MAX_CONTENT_CHARACTERS
        or type(content_chunks) is not list
        or not 1 <= len(content_chunks) <= MAX_CHUNKS
        or any(type(text) is not str or not text for text in content_chunks)
        or sum(len(text) for text in content_chunks) != len(original_content)
        or "".join(content_chunks) != original_content
    ):
        raise ValueError("catalog_chunk_content_invalid")
    try:
        original_content.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("catalog_chunk_content_invalid") from None
    if type(vectors) is not list or len(vectors) != len(content_chunks):
        raise ValueError("catalog_chunk_vectors_invalid")
    copied = []
    for item in vectors:
        vector = item.get("vector") if type(item) is dict else None
        if (
            type(item) is not dict
            or set(item) != {"vector"}
            or type(vector) is not list
            or len(vector) != DIMENSIONS
            or any(not _finite(number) for number in vector)
            or not any(vector)
        ):
            raise ValueError("catalog_chunk_vectors_invalid")
        copied.append({"vector": list(vector)})
    return {"content": list(content_chunks), "content_chunk_vector": copied}


def digest(payload):
    """Hash a validated native payload, binding text boundaries to vector order."""
    return hashlib.sha256(private_io.canonical(payload)).hexdigest()
