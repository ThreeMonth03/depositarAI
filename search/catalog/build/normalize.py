"""Normalize public CKAN metadata and shape allowlisted Fess documents."""

import html
import math
import re
import unicodedata
import uuid
from html.parser import HTMLParser
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Tuple,
)
from urllib.parse import quote

from . import policy


_BIDI_CODEPOINTS = frozenset(
    [0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E]
    + list(range(0x2066, 0x206A))
)


# Scrub each complete at-sign token; varied public text must not leak email-like addresses.
_EMAIL_TOKEN_RE = re.compile(r"\S*@\S*")


_SUPPRESSED_TAG_TOKEN_RE = re.compile(
    r"<\s*(/?)\s*(script|style|noscript)\b[^>]*>", re.IGNORECASE
)


_WHITESPACE_RE = re.compile(r"\s+")


class _TextExtractor(HTMLParser):
    _SUPPRESSED = frozenset({"script", "style", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._suppressed: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        normalized = tag.casefold()
        if normalized in self._SUPPRESSED:
            self._suppressed.append(normalized)

    def handle_startendtag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if self._suppressed and normalized == self._suppressed[-1]:
            self._suppressed.pop()

    def handle_data(self, data: str) -> None:
        if self._suppressed:
            # HTMLParser treats script/style bodies as raw text, so a nested
            # suppressed tag can otherwise be invisible to start/end handlers.
            # A raw opening tag therefore deepens suppression.  Raw closing
            # tags must not unwind it: HTMLParser separately delivers the
            # actual raw-element close to handle_endtag.  Requiring exact LIFO
            # closes keeps malformed/unbalanced markup fail-closed; none of
            # this data is ever emitted.
            for match in _SUPPRESSED_TAG_TOKEN_RE.finditer(data):
                closing, raw_tag = match.groups()
                if not closing:
                    self._suppressed.append(raw_tag.casefold())
            return
        self.parts.append(data)


def _increment(counters: MutableMapping[str, Any], key: str, amount: int = 1) -> None:
    counters[key] = int(counters.get(key, 0)) + amount


def _clean_text(
    value: Any,
    limit_name: str,
    counters: MutableMapping[str, Any],
    field_name: str,
    *,
    required: bool = False,
    truncate: bool = True,
) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise policy.BundleError("%s must be a string or null" % field_name)

    original = value
    value = unicodedata.normalize("NFKC", html.unescape(value))
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception as exc:
        raise policy.BundleError("%s contains malformed HTML" % field_name) from exc
    extracted = " ".join(parser.parts)
    if extracted != value:
        _increment(counters, "html_fields_cleaned")

    clean_chars: List[str] = []
    control_count = 0
    bidi_count = 0
    for character in unicodedata.normalize("NFKC", extracted):
        codepoint = ord(character)
        category = unicodedata.category(character)
        if codepoint in _BIDI_CODEPOINTS:
            bidi_count += 1
            continue
        if category in ("Cc", "Cf"):
            control_count += 1
            if character in "\t\n\r\f":
                clean_chars.append(" ")
            continue
        clean_chars.append(character)
    if control_count:
        _increment(counters, "control_characters_removed", control_count)
    if bidi_count:
        _increment(counters, "bidi_characters_removed", bidi_count)

    cleaned = _WHITESPACE_RE.sub(" ", "".join(clean_chars)).strip()
    limit = policy.STRING_LIMITS[limit_name]
    if len(cleaned) > limit:
        if not truncate:
            raise policy.BundleError("%s exceeds its character limit" % field_name)
        marker = "...[truncated]"
        cleaned = cleaned[: limit - len(marker)] + marker
        _increment(counters, "truncated_fields")
    if original != cleaned:
        _increment(counters, "normalized_fields")
    if required and not cleaned:
        raise policy.BundleError(
            "%s must not be empty after normalization" % field_name
        )
    return cleaned


def _clean_string_list(
    value: Any,
    list_name: str,
    counters: MutableMapping[str, Any],
) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise policy.BundleError("%s must be an array" % list_name)
    if len(value) > policy.ARRAY_LIMITS[list_name]:
        raise policy.BundleError("%s exceeds its item limit" % list_name)
    items = [
        _clean_text(item, "list_item", counters, "%s item" % list_name, required=True)
        for item in value
    ]
    unique = {item.casefold(): item for item in items}
    if len(unique) != len(items):
        _increment(counters, "duplicate_list_items_removed", len(items) - len(unique))
    return sorted(unique.values(), key=lambda item: (item.casefold(), item))


def _clean_named_list(
    value: Any,
    list_name: str,
    counters: MutableMapping[str, Any],
) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise policy.BundleError("%s must be an array" % list_name)
    if len(value) > policy.ARRAY_LIMITS[list_name]:
        raise policy.BundleError("%s exceeds its item limit" % list_name)
    result: List[str] = []
    for item in value:
        if isinstance(item, str):
            candidate = item
        elif isinstance(item, dict):
            candidate = (
                item.get("display_name") or item.get("title") or item.get("name") or ""
            )
        else:
            raise policy.BundleError("%s items must be strings or objects" % list_name)
        result.append(
            _clean_text(
                candidate, "list_item", counters, "%s item" % list_name, required=True
            )
        )
    unique = {item.casefold(): item for item in result}
    if len(unique) != len(result):
        _increment(counters, "duplicate_list_items_removed", len(result) - len(unique))
    return sorted(unique.values(), key=lambda item: (item.casefold(), item))


def _parse_size(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise policy.BundleError("resource size must be a non-negative finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise policy.BundleError(
            "resource size must be a non-negative finite number"
        ) from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise policy.BundleError("resource size must be a non-negative finite number")
    if parsed.is_integer():
        return int(parsed)
    return parsed


def _parse_coordinate(value: Any) -> float:
    if isinstance(value, bool):
        raise policy.BundleError("bbox coordinates must be finite numbers")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise policy.BundleError("bbox coordinates must be finite numbers") from exc
    if not math.isfinite(parsed):
        raise policy.BundleError("bbox coordinates must be finite numbers")
    return 0.0 if parsed == 0 else parsed


def _normalize_bbox(package: Mapping[str, Any]) -> Dict[str, Any]:
    names = ("x_min", "y_min", "x_max", "y_max")
    raw = [package.get(name) for name in names]
    present = [item not in (None, "") for item in raw]
    if not any(present):
        return {
            "status": "absent",
            "x_min": None,
            "y_min": None,
            "x_max": None,
            "y_max": None,
        }
    if not all(present):
        coordinates: List[Optional[float]] = []
        try:
            coordinates = [
                _parse_coordinate(item) if flag else None
                for item, flag in zip(raw, present)
            ]
        except policy.BundleError:
            return {
                "status": "invalid",
                "x_min": None,
                "y_min": None,
                "x_max": None,
                "y_max": None,
            }
        return dict(zip(("status",) + names, ("partial",) + tuple(coordinates)))
    try:
        coordinates_full = [_parse_coordinate(item) for item in raw]
    except policy.BundleError:
        return {
            "status": "invalid",
            "x_min": None,
            "y_min": None,
            "x_max": None,
            "y_max": None,
        }
    if (
        coordinates_full[0] > coordinates_full[2]
        or coordinates_full[1] > coordinates_full[3]
    ):
        return {
            "status": "invalid",
            "x_min": None,
            "y_min": None,
            "x_max": None,
            "y_max": None,
        }
    return dict(zip(("status",) + names, ("complete",) + tuple(coordinates_full)))


def _normalize_temporal(
    package: Mapping[str, Any], counters: MutableMapping[str, Any]
) -> Dict[str, Any]:
    start = _clean_text(package.get("start_time"), "date", counters, "start_time")
    end = _clean_text(package.get("end_time"), "date", counters, "end_time")
    created = _clean_text(package.get("created_time"), "date", counters, "created_time")
    if start and end:
        status = "complete"
    elif start or end:
        status = "partial"
    else:
        status = "absent"
    return {
        "status": status,
        "start": start or None,
        "end": end or None,
        "created": created or None,
    }


def _normalize_resource(
    resource: Any,
    counters: MutableMapping[str, Any],
    dataset_id: str,
) -> Dict[str, Any]:
    if not isinstance(resource, dict):
        raise policy.BundleError("resources for %s must contain objects" % dataset_id)
    return {
        "id": _clean_text(
            resource.get("id"),
            "resource_id",
            counters,
            "resource.id",
            truncate=False,
        ),
        "name": _clean_text(
            resource.get("name"), "resource_name", counters, "resource.name"
        ),
        "description": _clean_text(
            resource.get("description"),
            "resource_description",
            counters,
            "resource.description",
        ),
        "format": _clean_text(
            resource.get("format"), "resource_format", counters, "resource.format"
        ),
        "mimetype": _clean_text(
            resource.get("mimetype"), "resource_mimetype", counters, "resource.mimetype"
        ),
        "url": _clean_text(
            resource.get("url"),
            "resource_url",
            counters,
            "resource.url",
            truncate=False,
        ),
        "size": _parse_size(resource.get("size")),
        "created": _clean_text(
            resource.get("created"), "date", counters, "resource.created"
        ),
        "last_modified": _clean_text(
            resource.get("last_modified"), "date", counters, "resource.last_modified"
        ),
    }


def normalize_package(
    package: Any, counters: MutableMapping[str, Any]
) -> Tuple[str, str, Dict[str, Any]]:
    if not isinstance(package, dict):
        raise policy.BundleError("each JSON Lines record must be an object")
    if package.get("private") is not False:
        raise policy.BundleError("every dataset must have private=false")
    if package.get("state") != "active":
        raise policy.BundleError("every dataset must have state=active")
    if package.get("type") != "dataset":
        raise policy.BundleError("every dataset must have type=dataset")

    raw_id = package.get("id")
    if not isinstance(raw_id, str):
        raise policy.BundleError("dataset id must be a canonical UUID string")
    try:
        dataset_id = str(uuid.UUID(raw_id))
    except (ValueError, AttributeError) as exc:
        raise policy.BundleError("dataset id must be a canonical UUID string") from exc
    if raw_id.casefold() != dataset_id:
        raise policy.BundleError("dataset id must use canonical UUID spelling")

    slug = _clean_text(
        package.get("name"),
        "slug",
        counters,
        "name",
        required=True,
        truncate=False,
    )
    if "@" in slug:
        raise policy.BundleError("dataset slug must not contain an at-sign token")
    dataset_url = policy.DATASET_URL_PREFIX + quote(slug, safe="")

    resources_raw = package.get("resources")
    if resources_raw is None:
        resources_raw = []
    if not isinstance(resources_raw, list):
        raise policy.BundleError("resources must be an array")
    if len(resources_raw) > policy.MAX_RESOURCES:
        raise policy.BundleError("dataset %s exceeds the resource limit" % dataset_id)
    resources = [
        _normalize_resource(item, counters, dataset_id) for item in resources_raw
    ]
    resources.sort(key=lambda item: (item["id"], item["url"], item["name"]))
    resource_ids = [item["id"] for item in resources if item["id"]]
    if len(set(resource_ids)) != len(resource_ids):
        raise policy.BundleError("dataset %s has duplicate resource IDs" % dataset_id)

    organization_raw = package.get("organization")
    if organization_raw is None:
        organization_raw = {}
    if not isinstance(organization_raw, dict):
        raise policy.BundleError("organization must be an object or null")
    organization = {
        "id": _clean_text(
            organization_raw.get("id"), "organization", counters, "organization.id"
        ),
        "name": _clean_text(
            organization_raw.get("name"), "organization", counters, "organization.name"
        ),
        "title": _clean_text(
            organization_raw.get("title"),
            "organization",
            counters,
            "organization.title",
        ),
    }

    bbox = _normalize_bbox(package)
    temporal = _normalize_temporal(package, counters)
    _increment(counters, "bbox_%s" % bbox["status"])
    _increment(counters, "temporal_%s" % temporal["status"])

    corpus = {
        "schema_version": 2,
        "dataset_id": dataset_id,
        "name": slug,
        "url": dataset_url,
        "title": _clean_text(package.get("title"), "title", counters, "title"),
        "notes": _clean_text(package.get("notes"), "notes", counters, "notes"),
        "remarks": _clean_text(package.get("remarks"), "remarks", counters, "remarks"),
        "process_step": _clean_text(
            package.get("process_step"), "process_step", counters, "process_step"
        ),
        "author": _clean_text(package.get("author"), "person", counters, "author"),
        "maintainer": _clean_text(
            package.get("maintainer"), "person", counters, "maintainer"
        ),
        "organization": organization,
        "tags": _clean_named_list(package.get("tags"), "tags", counters),
        "groups": _clean_named_list(package.get("groups"), "groups", counters),
        "keywords": _clean_string_list(package.get("keywords"), "keywords", counters),
        "language": _clean_string_list(package.get("language"), "language", counters),
        "data_type": _clean_string_list(
            package.get("data_type"), "data_type", counters
        ),
        "license_id": _clean_text(
            package.get("license_id"), "license", counters, "license_id"
        ),
        "license_title": _clean_text(
            package.get("license_title"), "license", counters, "license_title"
        ),
        "temporal": temporal,
        "bbox": bbox,
        "resources": resources,
        "resource_count": len(resources),
        "resource_formats": sorted(
            {item["format"] for item in resources if item["format"]},
            key=lambda item: (item.casefold(), item),
        ),
        "metadata_created": _clean_text(
            package.get("metadata_created"), "date", counters, "metadata_created"
        ),
        "metadata_modified": _clean_text(
            package.get("metadata_modified"), "date", counters, "metadata_modified"
        ),
        "visibility": "public",
        "state": "active",
        "dataset_type": "dataset",
    }
    return dataset_id, dataset_url, corpus


def _redact_emails(value: str, counters: MutableMapping[str, Any]) -> str:
    redacted, count = _EMAIL_TOKEN_RE.subn("[email removed]", value)
    if count:
        _increment(counters, "fess_email_addresses_redacted", count)
    if "@" in redacted:
        raise AssertionError("Fess email-like token escaped redaction")
    return redacted


def _bound_fess_text(
    value: str,
    limit_name: str,
    counters: MutableMapping[str, Any],
    counter_name: str,
) -> str:
    limit = policy.STRING_LIMITS[limit_name]
    if len(value) > limit:
        marker = "...[truncated]"
        value = value[: limit - len(marker)] + marker
        _increment(counters, counter_name)
    return value


def _redacted_fess_text(
    value: str,
    limit_name: str,
    counters: MutableMapping[str, Any],
    counter_name: str = "fess_fields_truncated_after_redaction",
) -> str:
    # Redact before applying the final Fess field limit.  The replacement is
    # intentionally longer than some matched tokens, so bounding first could
    # both overrun the loader contract and cut an address into an evasion.
    return _bound_fess_text(
        _redact_emails(value, counters), limit_name, counters, counter_name
    )


def _bounded_join(
    values: Iterable[str],
    limit_name: str,
    counters: MutableMapping[str, Any],
    counter_name: str,
) -> str:
    joined = _WHITESPACE_RE.sub(
        " ", " \n ".join(value for value in values if value)
    ).strip()
    return _redacted_fess_text(joined, limit_name, counters, counter_name)


def fess_document(
    corpus: Mapping[str, Any], counters: MutableMapping[str, Any]
) -> Dict[str, Any]:
    organization = corpus["organization"]
    temporal = corpus["temporal"]
    bbox = corpus["bbox"]
    resources = corpus["resources"]
    important_parts: List[str] = [
        corpus["name"],
        corpus["title"],
        organization["title"],
        organization["name"],
        corpus["license_title"],
        " ".join(corpus["tags"]),
        " ".join(corpus["keywords"]),
        " ".join(corpus["language"]),
        " ".join(corpus["data_type"]),
        " ".join(corpus["resource_formats"]),
        "temporal %s %s %s"
        % (temporal["status"], temporal["start"] or "", temporal["end"] or ""),
        "bbox %s %s %s %s %s"
        % (
            bbox["status"],
            "" if bbox["x_min"] is None else bbox["x_min"],
            "" if bbox["y_min"] is None else bbox["y_min"],
            "" if bbox["x_max"] is None else bbox["x_max"],
            "" if bbox["y_max"] is None else bbox["y_max"],
        ),
    ]
    important_parts.extend(item["name"] for item in resources)
    important_parts.extend(item["format"] for item in resources)
    # Fess's ChunkVectorHelper embeds `content`, not `important_content`.
    # Repeat the bounded discovery metadata here so every public dataset has a
    # non-empty semantic representation even when its narrative description is
    # blank.  The lexical importance field remains separate for BM25 ranking.
    content_parts: List[str] = list(important_parts)
    content_parts.extend([corpus["notes"], corpus["remarks"], corpus["process_step"]])
    content_parts.extend(item["description"] for item in resources)

    title = _redacted_fess_text(
        corpus["title"] or corpus["name"] or corpus["dataset_id"],
        "title",
        counters,
    )
    important = _bounded_join(
        important_parts,
        "fess_important_content",
        counters,
        "fess_important_content_truncated",
    )
    content = _bounded_join(
        content_parts, "fess_content", counters, "fess_content_truncated"
    )

    def safe_list(values: Iterable[str]) -> List[str]:
        redacted = [
            _redacted_fess_text(value, "list_item", counters) for value in values
        ]
        unique = {value.casefold(): value for value in redacted}
        if len(unique) != len(redacted):
            _increment(
                counters,
                "fess_duplicate_list_items_removed_after_redaction",
                len(redacted) - len(unique),
            )
        return sorted(unique.values(), key=lambda item: (item.casefold(), item))

    document = {
        "url": corpus["url"],
        "title": title,
        "important_content": important,
        "content": content,
        "mimetype": "text/plain",
        "filetype": "txt",
        "label": [policy.FIXED_LABEL],
        "lang": ["auto"],
        "role": [policy.FIXED_SEARCH_ROLE],
        "boost": 1.0,
        "virtual_host": [policy.FIXED_VIRTUAL_HOST],
        "dataset_id": corpus["dataset_id"],
        "name": corpus["name"],
        "notes": _redacted_fess_text(corpus["notes"], "notes", counters),
        "organization_id": _redacted_fess_text(
            organization["id"], "organization", counters
        ),
        "organization_name": _redacted_fess_text(
            organization["name"], "organization", counters
        ),
        "organization_title": _redacted_fess_text(
            organization["title"], "organization", counters
        ),
        "tags": safe_list(corpus["tags"]),
        "groups": safe_list(corpus["groups"]),
        "resource_formats": safe_list(corpus["resource_formats"]),
        "license_id": _redacted_fess_text(corpus["license_id"], "license", counters),
        "license_title": _redacted_fess_text(
            corpus["license_title"], "license", counters
        ),
        "metadata_created": _redacted_fess_text(
            corpus["metadata_created"], "date", counters
        ),
        "metadata_modified": _redacted_fess_text(
            corpus["metadata_modified"], "date", counters
        ),
        "resource_count": corpus["resource_count"],
    }
    if frozenset(document) != policy.FESS_FIELD_ALLOWLIST:
        raise AssertionError("Fess document escaped its field allowlist")
    if any(
        "@" in item
        for value in document.values()
        for item in (value if isinstance(value, list) else (value,))
        if isinstance(item, str)
    ):
        raise AssertionError("Fess document contains an email-like token")
    return document
