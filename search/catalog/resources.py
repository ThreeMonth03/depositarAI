"""Bounded resource context shared by catalog, retrieval and public cards."""

import re
import unicodedata
from urllib.parse import quote, unquote, urlsplit


MAX_RESOURCES = 3
MAX_COLUMNS = 32
MAX_COLUMN_CHARS = 128
MAX_COLUMN_TOTAL_CHARS = 2048
MAX_EXCERPT_CHARS = 320
MAX_RESOURCE_NAME_CHARS = 160
MAX_INDEX_TEXT_CHARS = 8192
MAX_URL_CHARS = 2048

_UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CONTEXT_FIELDS = {"resource_id", "name", "format", "url", "columns", "excerpt"}
_ENRICHMENT_FIELDS = {
    "context",
    "index_text",
    "profile_sha256",
    "source_manifest_sha256",
    "content_sha256",
    "extractor_image_id",
    "extractor_source_sha256",
    "acquired_at",
    "truncated",
}


def _text(value, maximum, *, required=False):
    if (
        type(value) is not str
        or len(value) > maximum
        or (required and not value.strip())
        or unicodedata.normalize("NFKC", value) != value
        or "@" in value
        or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)
    ):
        raise ValueError("invalid resource context text")
    return value


def _uuid(value):
    if type(value) is not str or _UUID.fullmatch(value) is None:
        raise ValueError("invalid resource UUID")
    return value


def _sha256(value):
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError("invalid resource provenance digest")
    return value


def resource_url(dataset_url, resource_id):
    """Return the canonical public CKAN landing URL, never a download URL."""
    _uuid(resource_id)
    _text(dataset_url, MAX_URL_CHARS, required=True)
    parsed = urlsplit(dataset_url)
    prefix = "/dataset/"
    slug = parsed.path[len(prefix) :] if parsed.path.startswith(prefix) else ""
    try:
        decoded = unquote(slug, errors="strict")
    except UnicodeError as error:
        raise ValueError("invalid dataset landing URL") from error
    if (
        parsed.scheme != "https"
        or parsed.netloc != "data.depositar.io"
        or parsed.query
        or parsed.fragment
        or not slug
        or "/" in slug
        or decoded in {".", ".."}
        or quote(decoded, safe="") != slug
        or not dataset_url.isascii()
    ):
        raise ValueError("invalid dataset landing URL")
    result = dataset_url + "/resource/" + resource_id
    if len(result) > MAX_URL_CHARS:
        raise ValueError("resource landing URL exceeds limit")
    return result


def validate_resource_context(value):
    """Validate the closed public schema and return an independent JSON copy."""
    if type(value) is not list or len(value) > MAX_RESOURCES:
        raise ValueError("invalid resource context list")
    output = []
    identifiers = []
    for item in value:
        if type(item) is not dict or set(item) != _CONTEXT_FIELDS:
            raise ValueError("invalid resource context fields")
        identifier = _uuid(item["resource_id"])
        name = _text(item["name"], MAX_RESOURCE_NAME_CHARS)
        file_format = item["format"]
        if type(file_format) is not str or file_format not in {"CSV", "PDF"}:
            raise ValueError("unsupported resource context format")
        url = _text(item["url"], MAX_URL_CHARS, required=True)
        suffix = "/resource/" + identifier
        if (
            not url.endswith(suffix)
            or resource_url(url[: -len(suffix)], identifier) != url
        ):
            raise ValueError("invalid resource landing URL")
        columns = item["columns"]
        if type(columns) is not list or len(columns) > MAX_COLUMNS:
            raise ValueError("invalid resource columns")
        columns = [_text(column, MAX_COLUMN_CHARS, required=True) for column in columns]
        if sum(map(len, columns)) > MAX_COLUMN_TOTAL_CHARS:
            raise ValueError("resource columns exceed total limit")
        excerpt = _text(item["excerpt"], MAX_EXCERPT_CHARS)
        if (file_format == "CSV" and (excerpt or not columns)) or (
            file_format == "PDF" and (columns or not excerpt.strip())
        ):
            raise ValueError("resource context disagrees with its format")
        output.append(
            {
                "resource_id": identifier,
                "name": name,
                "format": file_format,
                "url": url,
                "columns": columns,
                "excerpt": excerpt,
            }
        )
        identifiers.append(identifier)
    if identifiers != sorted(set(identifiers)):
        raise ValueError("resource contexts must have unique sorted UUIDs")
    return output


def validate_enrichment_resources(value):
    """Validate sealed private provenance and its exact public projection."""
    if type(value) is not list or len(value) > MAX_RESOURCES:
        raise ValueError("invalid resource enrichment list")
    if any(type(item) is not dict or set(item) != _ENRICHMENT_FIELDS for item in value):
        raise ValueError("invalid resource enrichment fields")
    contexts = validate_resource_context([item["context"] for item in value])
    output = []
    for item, context in zip(value, contexts):
        index_text = _text(item["index_text"], MAX_INDEX_TEXT_CHARS, required=True)
        if (
            context["format"] == "CSV" and index_text != " ".join(context["columns"])
        ) or (
            context["format"] == "PDF"
            and context["excerpt"] != index_text[:MAX_EXCERPT_CHARS]
        ):
            raise ValueError("resource index text disagrees with public context")
        image = item["extractor_image_id"]
        if type(image) is not str or not image.startswith("sha256:"):
            raise ValueError("invalid resource extractor image")
        _sha256(image[7:])
        if type(item["acquired_at"]) is not int or item["acquired_at"] < 0:
            raise ValueError("invalid resource acquisition time")
        if type(item["truncated"]) is not bool:
            raise ValueError("invalid resource truncation flag")
        copied = {
            "context": context,
            "index_text": index_text,
            "extractor_image_id": image,
            "acquired_at": item["acquired_at"],
            "truncated": item["truncated"],
        }
        for name in (
            "profile_sha256",
            "source_manifest_sha256",
            "content_sha256",
            "extractor_source_sha256",
        ):
            copied[name] = _sha256(item[name])
        output.append(copied)
    return output
