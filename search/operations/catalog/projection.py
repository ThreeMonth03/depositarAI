"""Operator-only conditions/source projection of a complete vector-ready catalog.

The caller holds the catalog operation lock and supplies a private, bounded
transport. Its request(method, path, payload=None, **kwargs) returns (status,
parsed_json). This module never accepts URLs, credentials, query DSL or index
names from an HTTP caller, and never imports research/deployment scripts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

from search.catalog.bundle import CatalogBundle
from search.catalog.codec import _json_wire_copy
from search.catalog.inventory import InventorySnapshot
from search.catalog.policy import MAX_DOCUMENTS
from search.catalog.vectors import VectorProgressSnapshot
from search.operations.shared import private_io
from search.planning import index_filters as filters


PHYSICAL_INDEX = re.compile(r"fess\.[0-9]{14,20}\Z")
PAGE_SIZE = 50
BULK_SIZE = 50
VECTOR_DIMENSION = 768
# Fess 15.8 ChunkVectorHelper.DEFAULT_MAX_CHUNKS_PER_DOCUMENT; callers cannot
# widen this recipe. The transport independently caps each response's bytes.
MAX_CHUNKS_PER_DOCUMENT = 1000
SOURCE_FIELDS = (
    "dataset_id",
    "url",
    "role",
    "label",
    "virtual_host",
    "depositar_bundle_id",
    "depositar_document_sha256",
    "content_chunk_status",
    "resource_context",
    filters.VECTOR_PATH,
    filters.FIELD,
)


class ProjectionError(ValueError):
    """A fixed-code failure; never includes transport output or catalog text."""


def require_index(index):
    if type(index) is not str or PHYSICAL_INDEX.fullmatch(index) is None:
        raise ProjectionError("catalog_projection_index_invalid")
    return index


def request(client, method, path, payload=None, **kwargs):
    """Sanitize transport failures; raw upstream output never enters exceptions."""
    try:
        response = client.request(method, path, payload, **kwargs)
    except Exception:
        raise ProjectionError("catalog_projection_transport_failed") from None
    if (
        type(response) is not tuple
        or len(response) != 2
        or type(response[0]) is not int
        or not 100 <= response[0] <= 599
        or response[1] is not None
        and type(response[1]) is not dict
    ):
        raise ProjectionError("catalog_projection_response_invalid")
    return response


def _ok(client, method, path, payload=None, **kwargs):
    status, value = request(client, method, path, payload, **kwargs)
    if status != 200 or type(value) is not dict:
        raise ProjectionError("catalog_projection_request_failed")
    return value


def same_json(left, right):
    try:
        return private_io.canonical(left) == private_io.canonical(right)
    except (ValueError, TypeError, OverflowError, RecursionError):
        return False


def _bundle(bundle):
    if (
        not isinstance(bundle, CatalogBundle)
        or not 1 <= len(bundle.documents) <= MAX_DOCUMENTS
        or len({row["dataset_id"] for row in bundle.documents}) != len(bundle.documents)
    ):
        raise ProjectionError("catalog_projection_bundle_invalid")


def _condition_mapping(actual):
    """Accept only the fixed schema, with OpenSearch's implicit object type.

    GET _mapping may omit the top-level ``type: object`` even though PUT sent
    it. Restore only that absent key for comparison; retain every other key and
    value, and never normalize nested fields or an explicitly different type.
    """
    if type(actual) is not dict:
        return False
    if "type" not in actual:
        actual = {**actual, "type": "object"}
    return same_json(actual, filters.mapping()["properties"][filters.FIELD])


def _mapping(client, index=None, *, projected=False, max_url_length):
    value = _ok(client, "GET", "/" + (index or "fess.search") + "/_mapping")
    if len(value) != 1:
        raise ProjectionError("catalog_projection_requires_one_index")
    physical = require_index(next(iter(value)))
    if index is not None and physical != index:
        raise ProjectionError("catalog_projection_index_changed")
    try:
        properties = value[physical]["mappings"]["properties"]
        container = properties[filters.VECTOR_PATH]
        vector = container["properties"]["vector"]
        valid = (
            container.get("type") == "nested"
            and vector.get("type") == "knn_vector"
            and type(vector.get("dimension")) is int
            and vector["dimension"] == VECTOR_DIMENSION
            and vector.get("method", {}).get("engine") in {"faiss", "lucene"}
        )
    except (KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ProjectionError("catalog_projection_vector_mapping_invalid")
    url = properties.get("url")
    if type(url) is not dict:
        raise ProjectionError("catalog_projection_mapping_changed")
    ignore_above = url.get("ignore_above", 2_147_483_647)
    if (
        url.get("type") != "keyword"
        or url.get("doc_values", True) is not True
        or url.get("normalizer") is not None
        or type(ignore_above) is not int
        or not max_url_length <= ignore_above <= 2_147_483_647
    ):
        raise ProjectionError("catalog_projection_mapping_changed")
    has_projection = filters.FIELD in properties
    if (has_projection and not _condition_mapping(properties[filters.FIELD])) or (
        projected and not has_projection
    ):
        raise ProjectionError("catalog_projection_mapping_changed")
    return physical, has_projection


def require_complete_shards(value):
    shards = value.get("_shards", {})
    if (
        type(shards) is not dict
        or type(shards.get("total")) is not int
        or shards["total"] < 1
        or type(shards.get("successful")) is not int
        or shards["successful"] != shards["total"]
        or type(shards.get("failed")) is not int
        or shards["failed"] != 0
    ):
        raise ProjectionError("catalog_projection_partial_shards")


def _finite(number):
    if type(number) not in {int, float}:
        return False
    try:
        return math.isfinite(number)
    except OverflowError:
        return False


def _vectors(source):
    chunks = source.get(filters.VECTOR_PATH)
    if (
        source.get("content_chunk_status") != "done"
        or type(chunks) is not list
        or not 1 <= len(chunks) <= MAX_CHUNKS_PER_DOCUMENT
    ):
        raise ProjectionError("catalog_projection_vectors_incomplete")
    for chunk in chunks:
        vector = chunk.get("vector") if type(chunk) is dict else None
        if (
            type(vector) is not list
            or len(vector) != VECTOR_DIMENSION
            or any(not _finite(number) for number in vector)
            or not any(vector)
        ):
            raise ProjectionError("catalog_projection_vector_invalid")
    return chunks


def _inventory(client, bundle, index, *, projected):
    expected = {row["dataset_id"]: row for row in bundle.documents}
    expected_hashes = {
        url: digest for url, _bundle, digest in bundle.expected_inventory_entries
    }
    count = len(expected)
    found, native_ids = {}, set()
    vector_hashes, chunk_count = {}, 0
    previous_url = None
    for start in range(0, count, PAGE_SIZE):
        response = _ok(
            client,
            "POST",
            "/" + index + "/_search",
            {
                "from": start,
                "size": PAGE_SIZE,
                # Canonical, unique URLs retain their order across segment merges.
                "sort": [{"url": "asc"}],
                "track_total_hits": True,
                "seq_no_primary_term": True,
                "_source": list(SOURCE_FIELDS),
                "query": {"match_all": {}},
            },
        )
        require_complete_shards(response)
        if (
            response.get("timed_out") is not False
            or response.get("terminated_early", False) is not False
        ):
            raise ProjectionError("catalog_projection_partial_search")
        hits = response.get("hits", {})
        total = hits.get("total") if type(hits) is dict else None
        rows = hits.get("hits") if type(hits) is dict else None
        if (
            not same_json(total, {"value": count, "relation": "eq"})
            or type(rows) is not list
            or len(rows) != min(PAGE_SIZE, count - start)
        ):
            raise ProjectionError("catalog_projection_inventory_incomplete")
        for row in rows:
            if type(row) is not dict or type(row.get("_source")) is not dict:
                raise ProjectionError("catalog_projection_document_invalid")
            source = row["_source"]
            identifier, native_id = source.get("dataset_id"), row.get("_id")
            document = expected.get(identifier) if type(identifier) is str else None
            if (
                document is None
                or identifier in found
                or row.get("_index") != index
                or type(native_id) is not str
                or not 1 <= len(native_id) <= 1000
                or any(character in native_id for character in "\x00\r\n")
                or native_id in native_ids
                or type(row.get("_seq_no")) is not int
                or row["_seq_no"] < 0
                or type(row.get("_primary_term")) is not int
                or row["_primary_term"] < 1
                or source.get("url") != document["url"]
                or row.get("sort") != [document["url"]]
                or previous_url is not None
                and document["url"] <= previous_url
                or source.get("depositar_bundle_id") != bundle.bundle_id
                or source.get("depositar_document_sha256")
                != expected_hashes[document["url"]]
                or any(
                    not same_json(source.get(key), list(document[key]))
                    for key in ("role", "label", "virtual_host")
                )
            ):
                raise ProjectionError("catalog_projection_inventory_changed")
            projection = filters.metadata(
                bundle.condition_metadata[identifier], bundle.bundle_id
            )
            actual = source.get(filters.FIELD)
            if (actual is not None and not same_json(actual, projection)) or (
                projected and actual is None
            ):
                raise ProjectionError("catalog_projection_document_changed")
            context = _json_wire_copy(document["resource_context"])
            has_context = "resource_context" in source
            if (has_context and not same_json(source["resource_context"], context)) or (
                not has_context and (projected or context)
            ):
                raise ProjectionError("catalog_projection_resource_context_changed")
            chunks = _vectors(source)
            chunk_count += len(chunks)
            vector_hashes[identifier] = hashlib.sha256(
                private_io.canonical(chunks)
            ).hexdigest()
            found[identifier] = {
                "id": native_id,
                "seq_no": row["_seq_no"],
                "primary_term": row["_primary_term"],
                "projected": actual is not None and has_context,
                "projection": projection,
                "resource_context": context,
            }
            native_ids.add(native_id)
            previous_url = document["url"]
    if set(found) != set(expected):
        raise ProjectionError("catalog_projection_inventory_incomplete")
    vector_digest = hashlib.sha256(
        private_io.canonical(sorted(vector_hashes.items()))
    ).hexdigest()
    return found, chunk_count, vector_digest


def _update(client, index, rows):
    lines = []
    for row in rows:
        lines.extend(
            (
                json.dumps(
                    {
                        "update": {
                            "_index": index,
                            "_id": row["id"],
                            "if_seq_no": row["seq_no"],
                            "if_primary_term": row["primary_term"],
                        }
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "doc": {
                            filters.FIELD: row["projection"],
                            "resource_context": row["resource_context"],
                        }
                    },
                    ensure_ascii=False,
                ),
            )
        )
    response = _ok(
        client, "POST", "/" + index + "/_bulk", "\n".join(lines) + "\n", ndjson=True
    )
    items = response.get("items")
    if (
        response.get("errors") is not False
        or type(items) is not list
        or len(items) != len(rows)
    ):
        raise ProjectionError("catalog_projection_bulk_incomplete")
    for item, row in zip(items, rows):
        update = item.get("update") if type(item) is dict else None
        if (
            type(update) is not dict
            or set(item) != {"update"}
            or update.get("_index") != index
            or update.get("_id") != row["id"]
            or type(update.get("status")) is not int
            or update["status"] != 200
            or update.get("result") not in {"updated", "noop"}
            or "error" in update
        ):
            raise ProjectionError("catalog_projection_bulk_item_failed")
        shards = update.get("_shards", {})
        if (
            type(shards) is not dict
            or type(shards.get("total")) is not int
            or type(shards.get("successful")) is not int
            or not 1 <= shards["successful"] <= shards["total"]
            or type(shards.get("failed")) is not int
            or shards["failed"] != 0
        ):
            raise ProjectionError("catalog_projection_bulk_partial_shards")


def _vector_report(bundle, index, count, vector_digest):
    return {
        "schema_version": 1,
        "status": "ready",
        "index": index,
        "bundle_id": bundle.bundle_id,
        "manifest_sha256": bundle.manifest_sha256,
        "document_count": len(bundle.documents),
        "inventory_verified": True,
        "vector_state_verified": True,
        "vector_dimension": VECTOR_DIMENSION,
        "vector_chunk_count": count,
        "vectors_sha256": vector_digest,
        "inventory_sha256": hashlib.sha256(
            private_io.canonical(bundle.expected_inventory_entries)
        ).hexdigest(),
    }


def _report(bundle, index, count, vector_digest, updated):
    return {
        **_vector_report(bundle, index, count, vector_digest),
        "projection_schema": filters.VERSION,
        "projection_updated_count": updated,
        "projection_verified": True,
    }


def verify_vectors(client, bundle, index=None):
    """Read all actual vectors and catalog identities, without changing the index.

    The caller independently binds this bounded client to the sealed catalog
    runtime and holds the operation lock. This proves the observed index, not
    scheduler/token/helper cleanup or deployment authorization. A default alias
    is resolved before and after paging; a supplied physical index must match
    exactly. Existing projection fields, if present, remain strictly checked.
    The complete partition digest uses the vector-pass algorithm, not a new
    count-only encoding, so a host facade can compare the durable checkpoint.
    """
    _bundle(bundle)
    if index is not None:
        require_index(index)
    max_url_length = max(len(row["url"]) for row in bundle.documents)
    physical, _projected = _mapping(client, index, max_url_length=max_url_length)
    _rows, chunks, digest = _inventory(client, bundle, physical, projected=False)
    repeated, _projected = _mapping(client, index, max_url_length=max_url_length)
    if repeated != physical:
        raise ProjectionError("catalog_projection_index_changed")
    entries = bundle.expected_inventory_entries
    state = VectorProgressSnapshot(
        InventorySnapshot(entries), done_entries=entries, pending_entries=()
    )
    return {
        **_vector_report(bundle, physical, chunks, digest),
        "acl_verified": True,
        "done_count": state.inventory.count,
        "pending_count": 0,
        "after_state_sha256": state.sha256,
    }


def verify_projection(client, bundle, index):
    """Read every document and its vectors/projection; never writes the index."""
    _bundle(bundle)
    require_index(index)
    max_url_length = max(len(row["url"]) for row in bundle.documents)
    _mapping(client, index, projected=True, max_url_length=max_url_length)
    _rows, chunks, digest = _inventory(client, bundle, index, projected=True)
    _mapping(client, index, projected=True, max_url_length=max_url_length)
    return _report(bundle, index, chunks, digest, 0)


def provision_projection(client, bundle):
    """Project only a fully verified catalog; partial deterministic reruns are safe.

    The fixed alias is resolved once; all subsequent writes use that physical
    index. Optimistic sequence/term checks reject concurrent content changes.
    No returned readiness proof is issued until the full post-write scan passes.
    """
    _bundle(bundle)
    max_url_length = max(len(row["url"]) for row in bundle.documents)
    index, already_mapped = _mapping(client, max_url_length=max_url_length)
    rows, chunks, digest = _inventory(client, bundle, index, projected=False)
    if not already_mapped:
        response = _ok(client, "PUT", "/" + index + "/_mapping", filters.mapping())
        if response.get("acknowledged") is not True:
            raise ProjectionError("catalog_projection_mapping_unconfirmed")
    pending = [rows[key] for key in sorted(rows) if not rows[key]["projected"]]
    for start in range(0, len(pending), BULK_SIZE):
        _update(client, index, pending[start : start + BULK_SIZE])
    if pending or not already_mapped:
        response = _ok(client, "POST", "/" + index + "/_refresh")
        require_complete_shards(response)
    proof = verify_projection(client, bundle, index)
    if proof["vector_chunk_count"] != chunks or proof["vectors_sha256"] != digest:
        raise ProjectionError("catalog_projection_vectors_changed")
    return {**proof, "projection_updated_count": len(pending)}
