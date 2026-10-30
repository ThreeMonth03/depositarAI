"""Pure metadata validation shared by retrieval results and wire schemas."""

from typing import Any
import unicodedata

from search.planning.predicates import date_bounds


def bounded_text(value: Any, maximum: int) -> bool:
    if type(value) is not str or len(value) > maximum:
        return False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        return False
    return (
        encoded.decode("utf-8", "strict") == value
        and unicodedata.normalize("NFKC", value) == value
        and not any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
        )
    )


def validate_temporal_coverage(value: Any) -> dict[str, Any]:
    """Validate declared coverage without borrowing publication or query dates."""
    if type(value) is not dict or set(value) != {"start", "end", "status"}:
        raise ValueError("invalid temporal coverage")
    if any(
        endpoint is not None and (not bounded_text(endpoint, 128) or not endpoint)
        for endpoint in (value["start"], value["end"])
    ):
        raise ValueError("invalid temporal coverage endpoint")

    known = sum(value[side] is not None for side in ("start", "end"))
    expected = ("absent", "partial", "complete")[known]
    try:
        start = date_bounds(value["start"]) if value["start"] is not None else None
        end = date_bounds(value["end"]) if value["end"] is not None else None
        if start is not None and end is not None and start[0] > end[1]:
            expected = "invalid"
    except ValueError:
        expected = "invalid"
    if type(value["status"]) is not str or value["status"] != expected:
        raise ValueError("inconsistent temporal coverage status")
    return dict(value)
