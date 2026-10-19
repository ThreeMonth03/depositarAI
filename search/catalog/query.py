"""Compile literal user text into a bounded query without Fess or IO imports."""

from __future__ import annotations

import unicodedata

from .errors import UnsafeQuery
from .policy import _BIDI_CONTROLS

MAX_QUERY_CHARS = 512


MAX_QUERY_BYTES = 2_048


MAX_QUERY_TERMS = 64


MAX_TOP_K = 20


_QUERY_OPERATOR_WORDS = frozenset(("AND", "OR", "NOT", "TO"))


def normalize_query(value: str) -> str:
    if not isinstance(value, str):
        raise UnsafeQuery("query must be text")
    normalized = unicodedata.normalize("NFKC", value)
    if any(character in _BIDI_CONTROLS for character in normalized):
        raise UnsafeQuery("query contains a bidi control")
    if any(unicodedata.category(character) in ("Cc", "Cf") for character in normalized):
        raise UnsafeQuery("query contains a control character")
    if (
        len(normalized) > MAX_QUERY_CHARS
        or len(normalized.encode("utf-8")) > MAX_QUERY_BYTES
    ):
        raise UnsafeQuery("query exceeds its size cap")
    # Keep user text literal across lexical and embedding inputs. Compile to
    # Unicode letters, marks, numbers and underscore only. Punctuation becomes
    # a separator, so ordinary questions remain usable without exposing field,
    # wildcard, range, boolean, boost, or grouping operators.
    literal = "".join(
        character
        if character == "_"
        or character.isspace()
        or unicodedata.category(character)[:1] in ("L", "M", "N")
        else " "
        for character in normalized
    )
    terms = literal.split()
    terms = [
        term.casefold() if term in _QUERY_OPERATOR_WORDS else term for term in terms
    ]
    if not terms or len(terms) > MAX_QUERY_TERMS:
        raise UnsafeQuery("query term count is outside its cap")
    return " ".join(terms)
