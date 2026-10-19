"""Bounded, anonymous public metadata download; never fetch dataset resources."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request

from search.catalog.build import codec, normalize, policy


ENDPOINT = "https://data.depositar.io/api/3/action/package_search"
MAX_PAGE_BYTES = 8 * 1024**2
PAGE_ROWS = 100
DEADLINE_SECONDS = 600


class MetadataSourceError(ValueError):
    """Fixed operator diagnostics, never body-derived remote error messages."""


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def strict_json(raw):
    codec.check_json_nesting(raw)
    value = json.loads(
        raw.decode("utf-8", "strict"),
        object_pairs_hook=codec.unique_json_object,
        parse_constant=codec.reject_json_constant,
        parse_int=codec.parse_json_int,
        parse_float=codec.parse_json_float,
    )
    codec.reject_surrogates(value)
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MetadataSourceError("CKAN redirects are not accepted")


def fetch_page(start, rows, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MetadataSourceError("refresh deadline exceeded")
    query = urllib.parse.urlencode(
        {"include_private": "false", "sort": "id asc", "start": start, "rows": rows}
    )
    request = urllib.request.Request(
        ENDPOINT + "?" + query,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "depositar-public-metadata-sync/1",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=min(30, remaining)) as response:
        if (
            response.status != 200
            or response.headers.get_content_type() != "application/json"
            or response.headers.get("Content-Encoding", "identity") != "identity"
        ):
            raise MetadataSourceError("unexpected CKAN response headers")
        declared = response.headers.get("Content-Length")
        if declared is not None and (
            not declared.isdecimal() or int(declared) > MAX_PAGE_BYTES
        ):
            raise MetadataSourceError("CKAN page exceeds its byte limit")
        raw = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise MetadataSourceError("refresh deadline exceeded")
            chunk = response.read1(min(65536, MAX_PAGE_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_PAGE_BYTES:
                raise MetadataSourceError("CKAN page exceeds its byte limit")
        if declared is not None and int(declared) != len(raw):
            raise MetadataSourceError("CKAN response body is truncated")
    payload = strict_json(bytes(raw))
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise MetadataSourceError("CKAN action failed")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise MetadataSourceError("CKAN returned an invalid page")
    return result


def collect(fetch, deadline, *, rows=PAGE_ROWS):
    if type(rows) is not int or not 1 <= rows <= PAGE_ROWS:
        raise MetadataSourceError("page size exceeds its cap")
    records, expected, expanded = [], None, 0
    previous_id = ""
    while expected is None or len(records) < expected:
        if time.monotonic() >= deadline:
            raise MetadataSourceError("refresh deadline exceeded")
        page = fetch(len(records), rows, deadline)
        if not isinstance(page, dict):
            raise MetadataSourceError("CKAN returned an invalid page")
        count, items = page.get("count"), page.get("results")
        if (
            type(count) is not int
            or not 1 <= count <= policy.MAX_ROWS
            or not isinstance(items, list)
            or len(items) != min(rows, count - len(records))
        ):
            raise MetadataSourceError("incomplete or oversized CKAN page")
        if expected is not None and count != expected:
            raise MetadataSourceError("CKAN count changed during download")
        expected = count
        for item in items:
            if not isinstance(item, dict):
                raise MetadataSourceError("invalid CKAN record")
            identifier, _, _ = normalize.normalize_package(item, {})
            if identifier <= previous_id:
                raise MetadataSourceError(
                    "CKAN IDs are repeated or not in stable order"
                )
            encoded = canonical(item) + b"\n"
            expanded += len(encoded)
            if (
                len(encoded) > policy.MAX_LINE_BYTES
                or expanded > policy.MAX_EXPANDED_BYTES
            ):
                raise MetadataSourceError("CKAN snapshot exceeds its byte limit")
            records.append(item)
            previous_id = identifier
    return records


def records_digest(records):
    digest = hashlib.sha256()
    for item in records:
        digest.update(canonical(item) + b"\n")
    return digest.hexdigest()


def stable_download(deadline, fetch=None):
    fetch = fetch or fetch_page
    first = collect(fetch, deadline)
    first_digest = records_digest(first)
    second = collect(fetch, deadline)
    if first_digest != records_digest(second):
        raise MetadataSourceError("CKAN metadata changed between complete scans")
    return first
