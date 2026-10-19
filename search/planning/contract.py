"""Validate and normalize user queries before planning or retrieval."""

import unicodedata


def checked_query(query):
    if not isinstance(query, str) or not 1 <= len(query) <= 512:
        raise ValueError("query length")
    if any(unicodedata.category(c) in {"Cf", "Cs"} for c in query):
        raise ValueError("query invisible characters")
    query = " ".join(unicodedata.normalize("NFKC", query).split())
    if not 1 <= len(query) <= 512 or any(
        unicodedata.category(c) == "Cc" for c in query
    ):
        raise ValueError("query characters")
    return query
