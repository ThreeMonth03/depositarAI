"""Strict catalog result validation and server-owned dataset cards."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
from typing import Any, Mapping, Optional, Tuple
import unicodedata
import urllib.parse

from search.catalog.query import MAX_TOP_K, normalize_query


TOP_K = 5
RANKING = "opensearch-haystack-rrf"


MAX_SNIPPET_CHARS = 2_000


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


_DATASET_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


METADATA_FIELDS = (
    "dataset_id",
    "name",
    "title",
    "url",
    "notes",
    "organization_id",
    "organization_name",
    "organization_title",
    "tags",
    "groups",
    "resource_formats",
    "license_id",
    "license_title",
    "metadata_created",
    "metadata_modified",
    "resource_count",
)
_RESULT_FIELDS = frozenset(
    (*METADATA_FIELDS, "raw_score", "rank_fusion_score", "searchers")
)


_RETRIEVAL_FIELDS = frozenset(
    {
        "manifest_sha256",
        "query_sha256",
        "conditions",
        "ranking",
        "results",
    }
)


_BIDI_CONTROLS = frozenset(
    chr(value)
    for value in (
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    )
)


_RESULT_TEXT_LIMITS = {
    "dataset_id": 36,
    "name": 256,
    "title": 512,
    "url": 4_096,
    "notes": 32_768,
    "organization_id": 512,
    "organization_name": 512,
    "organization_title": 512,
    "license_id": 512,
    "license_title": 512,
    "metadata_created": 128,
    "metadata_modified": 128,
}


_RESULT_ARRAY_LIMITS = {"tags": 128, "groups": 64, "resource_formats": 256}


class ResultError(ValueError):
    """An injected adapter returned data outside its exact boundary."""


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str]) -> bool:
    try:
        return set(value) == expected
    except (TypeError, ValueError):
        return False


def _bounded_text(value: Any, maximum: int) -> bool:
    if type(value) is not str or len(value) > maximum:
        return False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        return False
    return (
        encoded.decode("utf-8", "strict") == value
        and unicodedata.normalize("NFKC", value) == value
        and not any(character in _BIDI_CONTROLS for character in value)
        and not any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
        )
    )


def _exact_int(value: Any, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _finite_score(value: Any, *, positive: bool = False) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(converted) and (converted > 0 if positive else converted >= 0)


def _canonical_dataset_url(value: Any, slug: Any) -> bool:
    if (
        not _bounded_text(value, 4_096)
        or not _bounded_text(slug, 256)
        or not slug
        or slug in (".", "..")
        or "@" in slug
    ):
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
        canonical_path = "/dataset/" + urllib.parse.quote(slug, safe="")
    except (TypeError, ValueError, UnicodeError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc == "data.depositar.io"
        and parsed.hostname == "data.depositar.io"
        and port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path == canonical_path
        and value == "https://data.depositar.io" + canonical_path
    )


def validate_result(row: Any) -> dict[str, Any]:
    if type(row) is not dict or not _exact_keys(row, _RESULT_FIELDS):
        raise ResultError("invalid retrieval result")
    if (
        type(row.get("dataset_id")) is not str
        or _DATASET_ID_RE.fullmatch(row["dataset_id"]) is None
        or not _canonical_dataset_url(row.get("url"), row.get("name"))
        or not _exact_int(row.get("resource_count"), 0, 100_000)
        or not _finite_score(row.get("raw_score"))
        or not _finite_score(row.get("rank_fusion_score"), positive=True)
    ):
        raise ResultError("invalid retrieval result value")

    for field in ("tags", "groups", "resource_formats", "searchers"):
        items = row.get(field)
        maximum_items = _RESULT_ARRAY_LIMITS.get(field, 2)
        if (
            type(items) is not list
            or len(items) > maximum_items
            or not all(_bounded_text(item, 1_024) for item in items)
            or len(items) != len(set(items))
        ):
            raise ResultError("invalid retrieval result array")
    provenance = (
        ["opensearch-bm25"],
        ["opensearch-knn"],
        ["opensearch-bm25", "opensearch-knn"],
    )
    if row["searchers"] not in provenance:
        raise ResultError("invalid retrieval provenance")

    if not all(
        _bounded_text(row.get(field), maximum)
        for field, maximum in _RESULT_TEXT_LIMITS.items()
    ):
        raise ResultError("invalid retrieval text")

    # Copy every nested collection before an injected adapter can mutate it.
    copied = dict(row)
    for field in ("tags", "groups", "resource_formats", "searchers"):
        copied[field] = list(row[field])
    return copied


def _validated_rows(
    report: Any,
    expected_query_sha256: str,
    conditions: dict[str, Any],
    manifest_sha256: str,
    maximum: int,
) -> list[dict[str, Any]]:
    if not _exact_int(maximum, 1, MAX_TOP_K):
        raise ResultError("invalid candidate bound")
    if type(report) is not dict or not _exact_keys(report, _RETRIEVAL_FIELDS):
        raise ResultError("invalid retrieval envelope")
    results = report.get("results")
    if (
        type(report.get("manifest_sha256")) is not str
        or report["manifest_sha256"] != manifest_sha256
        or _HEX64_RE.fullmatch(report["manifest_sha256"]) is None
        or type(report.get("query_sha256")) is not str
        or report.get("query_sha256") != expected_query_sha256
        or _HEX64_RE.fullmatch(report.get("query_sha256")) is None
        or type(report.get("conditions")) is not dict
        or report["conditions"] != conditions
        or type(report.get("ranking")) is not str
        or report.get("ranking") != RANKING
        or type(results) is not list
        or len(results) > maximum
    ):
        raise ResultError("invalid retrieval envelope value")

    copied: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    previous_score: Optional[float] = None
    for raw in results:
        row = validate_result(raw)
        dataset_id = row["dataset_id"]
        url = row["url"]
        score = float(row["rank_fusion_score"])
        if (
            dataset_id in seen_ids
            or url in seen_urls
            or (previous_score is not None and score > previous_score)
        ):
            raise ResultError("duplicate or unordered retrieval result")
        seen_ids.add(dataset_id)
        seen_urls.add(url)
        previous_score = score
        copied.append(row)
    return copied


def _card(row: Mapping[str, Any], rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "dataset_id": row["dataset_id"],
        "title": row["title"],
        "url": row["url"],
        "snippet": row["notes"][:MAX_SNIPPET_CHARS],
        "organization_title": row["organization_title"],
        "resource_formats": list(row["resource_formats"]),
        "metadata_modified": row["metadata_modified"],
    }


@dataclass(frozen=True)
class ValidatedSearch:
    """Validated catalog rows and their canonical query identity."""

    rows: Tuple[Mapping[str, Any], ...]
    query_sha256: str

    @classmethod
    def from_report(
        cls,
        query: str,
        report: Mapping[str, Any],
        *,
        conditions: dict[str, Any],
        manifest_sha256: str,
        maximum: int = TOP_K,
    ) -> "ValidatedSearch":
        digest = hashlib.sha256(normalize_query(query).encode("utf-8")).hexdigest()
        return cls(
            tuple(
                _validated_rows(report, digest, conditions, manifest_sha256, maximum)
            ),
            digest,
        )

    def cards(self) -> list[dict[str, Any]]:
        return [_card(row, rank) for rank, row in enumerate(self.rows, 1)]
