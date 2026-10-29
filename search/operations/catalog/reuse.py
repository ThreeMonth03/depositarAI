"""Reuse verified native document vectors through bounded private exports.

The caller owns the product and catalog operation locks. Cached native content
and vectors never carry metadata, permissions or deployment authority into a new
catalog. A durable intent permits deterministic recovery before its first pass.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from search.catalog import embedding
from search.catalog.bundle import CatalogBundle
from search.catalog.codec import _json_wire_copy
from search.operations.catalog import backend, chunks, connection, execution, projection
from search.operations.catalog.policy import RuntimeError
from search.operations.catalog.transport import MAX_REQUEST_BYTES, OpenSearchClient
from search.operations.shared import private_io
from search.planning import index_filters as filters
from search.serialization import strict_json


MAX_ENTRY_BYTES = 2 * 1024**2
MAX_MANIFEST_BYTES = 8 * 1024**2
MAX_EXPORT_BYTES = 256 * 1024**2
MAX_RECORDS = 10_000
SLOT = re.compile(r"[0-9a-f]{12}-[0-9a-f]{12}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
NATIVE_FIELDS = {"content", "content_chunk_vector", "content_chunk_status"}
PROFILE_FILES = (
    "deploy/docker/fess/Dockerfile",
    "deploy/docker/fess/Dockerfile.dockerignore",
    "deploy/docker/fess/entrypoint.sh",
    "deploy/docker/fess/fess_healthcheck.sh",
    "deploy/config/fess/scheduled_job.bulk",
)


def _require(condition):
    if not condition:
        raise RuntimeError("catalog_vector_reuse_evidence_invalid")


def _digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def _content_sha256(value):
    _require(type(value) is str and len(value) <= chunks.MAX_CONTENT_CHARACTERS)
    return hashlib.sha256(value.encode("utf-8", "strict")).hexdigest()


def _read(path, *, mode=0o600, limit=MAX_MANIFEST_BYTES):
    path = Path(path)
    descriptor = private_io.open_directory(path.parent)
    try:
        raw = private_io.read_file(descriptor, path.name, mode=mode, max_bytes=limit)
        private_io.verify_directory(descriptor, path.parent)
    finally:
        os.close(descriptor)
    value = strict_json(raw)
    _require(private_io.canonical(value) == raw)
    return value, hashlib.sha256(raw).hexdigest()


def _write(path, value, *, immutable=False, limit=MAX_MANIFEST_BYTES):
    path = Path(path)
    descriptor = private_io.open_directory(path.parent)
    try:
        raw = private_io.canonical(value)
        try:
            previous = private_io.read_file(
                descriptor,
                path.name,
                mode=0o400 if immutable else 0o600,
                max_bytes=limit,
            )
        except FileNotFoundError:
            if immutable:
                temporary = ".immutable-" + uuid.uuid4().hex
                private_io.write_file(descriptor, temporary, raw, max_bytes=limit)
                leaf = os.open(
                    temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                try:
                    os.fchmod(leaf, 0o400)
                    os.fsync(leaf)
                finally:
                    os.close(leaf)
                try:
                    os.link(
                        temporary,
                        path.name,
                        src_dir_fd=descriptor,
                        dst_dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                finally:
                    os.unlink(temporary, dir_fd=descriptor)
                os.fsync(descriptor)
            else:
                private_io.write_file(descriptor, path.name, raw, max_bytes=limit)
        else:
            _require(previous == raw)
        private_io.verify_directory(descriptor, path.parent)
    finally:
        os.close(descriptor)
    return hashlib.sha256(raw).hexdigest()


def _directory(path):
    os.close(private_io.open_directory(path, create=True))
    return path


def _discard_temporaries(root):
    descriptor = private_io.open_directory(root)
    try:
        names = os.listdir(descriptor)
        _require(len(names) <= MAX_RECORDS + 5)
        for name in names:
            if re.fullmatch(r"\.(?:record|immutable)-[0-9a-f]{32}", name) is None:
                continue
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            _require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid()
                and info.st_nlink in {1, 2}
                and stat.S_IMODE(info.st_mode) in {0o400, 0o600}
                and 1 <= info.st_size <= MAX_MANIFEST_BYTES
            )
            os.unlink(name, dir_fd=descriptor)
        os.fsync(descriptor)
        private_io.verify_directory(descriptor, root)
    finally:
        os.close(descriptor)


def _profile(context):
    services = context["compose"]["services"]
    fess, ollama = services["fess-search"], services["ollama-embedding"]
    return {
        "schema_version": 1,
        "model": embedding.MODEL,
        "model_manifest_sha256": embedding.MODEL_DIGEST,
        "dimensions": embedding.DIMENSIONS,
        "native_chunk_profile": chunks.PROFILE,
        "fess_build_arguments": fess["build"]["args"],
        "fess_environment": fess["environment"],
        "fess_sources": {
            name: hashlib.sha256(execution.sealed_source(context, name)).hexdigest()
            for name in PROFILE_FILES
        },
        "embedding_image": ollama["image"],
        "embedding_environment": ollama["environment"],
    }


def _query_proof(root, source, ready, vector_digest):
    _require(
        type(ready) is dict
        and set(ready) == {"identity", "run_id", "complete_sha256"}
        and type(ready["run_id"]) is str
        and RUN_ID.fullmatch(ready["run_id"]) is not None
        and type(ready["identity"]) is dict
        and projection.same_json(
            {key: ready["identity"].get(key) for key in source}, source
        )
        and ready["identity"].get("vectors_sha256") == vector_digest
    )
    history = root / "query-operations"
    identity, _ = _read(history / "identity.json")
    _require(projection.same_json(identity, ready["identity"]))
    run = history / ready["run_id"]
    started, started_sha = _read(run / "started.json")
    complete, complete_sha = _read(run / "complete.json")
    _require(
        projection.same_json(started, {"identity": identity, "run_id": ready["run_id"]})
        and complete_sha == ready["complete_sha256"]
        and complete.get("started_sha256") == started_sha
        and complete.get("reports", {}).get("projection", {}).get("vectors_sha256")
        == vector_digest
    )


def _record(original, native_content, vectors):
    native = chunks.validate(native_content, vectors, original)
    return {"schema_version": 1, "content_sha256": _content_sha256(original), **native}


def _source_profile(root, plan, manifest):
    """Bind exported logical inputs to the original completed catalog recipe."""
    recipe, recipe_sha = _read(root / "vector-operations/recipe.json")
    compose, compose_sha = _read(root / "compose.json")
    _require(
        recipe_sha == manifest["query_ready"]["identity"].get("recipe_sha256")
        and compose_sha == plan["compose_sha256"]
    )
    fess = compose["services"]["fess-search"]
    ollama = compose["services"]["ollama-embedding"]
    options = fess["environment"]["FESS_JAVA_OPTS"].split()
    native = {}
    for key in chunks.PROFILE:
        prefix = "-Dfess.system.content_chunker." + key + "="
        values = [value[len(prefix) :] for value in options if value.startswith(prefix)]
        _require(len(values) == 1)
        native[key] = values[0]
    sources = {name: plan["source_sha256"][name] for name in PROFILE_FILES}
    _require(
        all(
            type(value) is str and SHA256.fullmatch(value) is not None
            for value in sources.values()
        )
    )
    expected = {
        "schema_version": 1,
        "model": recipe["embedding_model"],
        "model_manifest_sha256": recipe["embedding_manifest_sha256"],
        "dimensions": recipe["dimensions"],
        "native_chunk_profile": native,
        "fess_build_arguments": fess["build"]["args"],
        "fess_environment": fess["environment"],
        "fess_sources": sources,
        "embedding_image": ollama["image"],
        "embedding_environment": ollama["environment"],
    }
    _require(
        type(manifest["profile"]) is dict
        and type(manifest["profile"].get("schema_version")) is int
        and projection.same_json(manifest["profile"], expected)
    )


def _load_record(exported, entry, original):
    digest = entry["record_sha256"]
    _require(type(digest) is str and SHA256.fullmatch(digest) is not None)
    value, actual = _read(
        exported / (digest + ".json"), mode=0o400, limit=MAX_ENTRY_BYTES
    )
    _require(
        actual == digest
        and type(value) is dict
        and set(value)
        == {"schema_version", "content_sha256", "content", "content_chunk_vector"}
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["content_sha256"]
        == entry["content_sha256"]
        == _content_sha256(original)
    )
    native = chunks.validate(value["content"], value["content_chunk_vector"], original)
    _require(chunks.digest(native) == entry["vectors_sha256"])
    return {**native, "content_chunk_status": "done"}


def _published(root, profile):
    exported = root / "vector-cache"
    try:
        manifest, manifest_sha = _read(exported / "manifest.json", mode=0o400)
    except FileNotFoundError:
        return None
    _require(
        type(manifest) is dict
        and set(manifest)
        == {
            "schema_version",
            "source",
            "profile",
            "profile_sha256",
            "query_ready",
            "vectors_sha256",
            "entries",
            "cached_count",
            "skipped_size_limit_count",
            "record_bytes",
        }
        and type(manifest["schema_version"]) is int
        and manifest["schema_version"] == 1
        and manifest["profile_sha256"] == _digest(manifest["profile"])
        and type(manifest["entries"]) is list
        and 1 <= len(manifest["entries"]) <= MAX_RECORDS
    )
    plan, _ = _read(root / "plan.json")
    bundle = CatalogBundle.load(
        Path(plan["bundle_path"]), expected_manifest_sha256=plan["manifest_sha256"]
    )
    _require(
        root.name == bundle.bundle_id[:12] + "-" + plan["revision"][:12]
        and projection.same_json(
            manifest["source"],
            execution.catalog_identity({"plan": plan, "bundle": bundle}),
        )
        and plan["document_count"] == len(bundle.documents)
    )
    _query_proof(
        root, manifest["source"], manifest["query_ready"], manifest["vectors_sha256"]
    )
    _source_profile(root, plan, manifest)
    documents = {row["dataset_id"]: row for row in bundle.documents}
    identifiers = []
    files = {"manifest.json", "export-started.json"}
    hashes, cached = [], 0
    for entry in manifest["entries"]:
        _require(
            type(entry) is dict
            and set(entry)
            == {"dataset_id", "content_sha256", "vectors_sha256", "record_sha256"}
            and entry["dataset_id"] in documents
            and entry["content_sha256"]
            == _content_sha256(documents[entry["dataset_id"]]["content"])
            and type(entry["vectors_sha256"]) is str
            and SHA256.fullmatch(entry["vectors_sha256"]) is not None
        )
        identifiers.append(entry["dataset_id"])
        hashes.append((entry["dataset_id"], entry["vectors_sha256"]))
        digest = entry["record_sha256"]
        if digest is not None:
            _require(type(digest) is str and SHA256.fullmatch(digest) is not None)
            files.add(digest + ".json")
            cached += 1
    _require(
        identifiers == sorted(documents)
        and len(set(identifiers)) == len(documents)
        and _digest(hashes) == manifest["vectors_sha256"]
        and type(manifest["cached_count"]) is int
        and manifest["cached_count"] == cached
        and type(manifest["skipped_size_limit_count"]) is int
        and manifest["skipped_size_limit_count"] == len(documents) - cached
        and type(manifest["record_bytes"]) is int
        and 0 <= manifest["record_bytes"] <= MAX_EXPORT_BYTES
        and set(os.listdir(exported)) == files
    )
    sizes = []
    for name in files - {"manifest.json", "export-started.json"}:
        info = (exported / name).lstat()
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o400
            and 1 <= info.st_size <= MAX_ENTRY_BYTES
        )
        sizes.append(info.st_size)
    _require(sum(sizes) == manifest["record_bytes"])
    intent, _ = _read(exported / "export-started.json")
    _require(
        projection.same_json(
            intent,
            {
                key: manifest[key]
                for key in (
                    "source",
                    "profile",
                    "profile_sha256",
                    "query_ready",
                    "vectors_sha256",
                )
            },
        )
    )
    if manifest["profile"] != profile:
        return None
    return {"root": root, "manifest": manifest, "manifest_sha256": manifest_sha}


def _export(context, client, scan, query_ready_record):
    _require(1 <= len(context["bundle"].documents) <= MAX_RECORDS)
    profile = _profile(context)
    source = execution.catalog_identity(context)
    _query_proof(context["root"], source, query_ready_record, scan["vectors_sha256"])
    exported = context["root"] / "vector-cache"
    if exported.exists():
        _discard_temporaries(exported)
    previous = _published(context["root"], profile)
    if previous is not None:
        _require(previous["manifest"]["vectors_sha256"] == scan["vectors_sha256"])
        return {
            "status": "already-exported",
            "cached_documents": previous["manifest"]["cached_count"],
            "skipped_size_limit_documents": previous["manifest"][
                "skipped_size_limit_count"
            ],
        }
    exported = _directory(context["root"] / "vector-cache")
    intent = {
        "source": source,
        "profile": profile,
        "profile_sha256": _digest(profile),
        "query_ready": query_ready_record,
        "vectors_sha256": scan["vectors_sha256"],
    }
    try:
        saved, _ = _read(exported / "export-started.json")
    except FileNotFoundError:
        _write(exported / "export-started.json", intent)
    else:
        _require(
            set(saved) == set(intent)
            and all(
                projection.same_json(saved[key], intent[key])
                for key in intent
                if key != "query_ready"
            )
        )
        _query_proof(
            context["root"], source, saved["query_ready"], scan["vectors_sha256"]
        )
        intent = saved
    entries, stored, stored_bytes = [], {}, 0

    def capture(identifier, original, native_content, vectors):
        nonlocal stored_bytes
        value = _record(original, native_content, vectors)
        raw = private_io.canonical(value)
        native = {key: value[key] for key in ("content", "content_chunk_vector")}
        digest = None
        candidate = hashlib.sha256(raw).hexdigest()
        if len(raw) <= MAX_ENTRY_BYTES and (
            candidate in stored or stored_bytes + len(raw) <= MAX_EXPORT_BYTES
        ):
            digest = candidate
            _write(
                exported / (digest + ".json"),
                value,
                immutable=True,
                limit=MAX_ENTRY_BYTES,
            )
            if digest not in stored:
                stored_bytes += len(raw)
            stored[digest] = len(raw)
        entries.append(
            {
                "dataset_id": identifier,
                "content_sha256": value["content_sha256"],
                "vectors_sha256": chunks.digest(native),
                "record_sha256": digest,
            }
        )

    observed = projection.verify_vectors(
        client, context["bundle"], scan["index"], capture=capture
    )
    _require(projection.same_json(observed, scan))
    entries.sort(key=lambda row: row["dataset_id"])
    _require(len(entries) == len(context["bundle"].documents))
    cached = sum(row["record_sha256"] is not None for row in entries)
    manifest = {
        "schema_version": 1,
        **intent,
        "entries": entries,
        "cached_count": cached,
        "skipped_size_limit_count": len(entries) - cached,
        "record_bytes": stored_bytes,
    }
    _write(exported / "manifest.json", manifest, immutable=True)
    _require(_published(context["root"], profile) is not None)
    return {
        "status": "exported",
        "cached_documents": cached,
        "skipped_size_limit_documents": len(entries) - cached,
    }


def export(context, client, scan, query_ready_record):
    """Publish one complete verified catalog's immutable native-vector export."""
    try:
        return _export(context, client, scan, query_ready_record)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise RuntimeError("catalog_vector_reuse_export_invalid") from None


def _sources(context, profile):
    catalogs = context["root"].parent
    descriptor = private_io.open_directory(catalogs)
    try:
        names = os.listdir(descriptor)
        _require(len(names) <= backend.MAX_SLOTS)
        found = []
        for name in sorted(names):
            _require(SLOT.fullmatch(name) is not None)
            if name == context["root"].name:
                continue
            root = catalogs / name
            try:
                (root / "failure.json").lstat()
            except FileNotFoundError:
                pass
            else:
                continue
            exported = _published(root, profile)
            if exported is not None:
                found.append(exported)
        private_io.verify_directory(descriptor, catalogs)
        return found
    finally:
        os.close(descriptor)


def _target_inventory(client, bundle):
    index, _ = projection._mapping(
        client, max_url_length=max(len(row["url"]) for row in bundle.documents)
    )
    documents = {row["dataset_id"]: row for row in bundle.documents}
    hashes = {url: digest for url, _, digest in bundle.expected_inventory_entries}
    found, native_ids, previous = {}, set(), None
    for start in range(0, len(documents), projection.PAGE_SIZE):
        response = projection._ok(
            client,
            "POST",
            "/" + index + "/_search",
            {
                "from": start,
                "size": projection.PAGE_SIZE,
                "sort": [{"url": "asc"}],
                "track_total_hits": True,
                "seq_no_primary_term": True,
                "_source": list(projection.SOURCE_FIELDS),
                "query": {"match_all": {}},
            },
        )
        projection.require_complete_shards(response)
        hits = response.get("hits", {})
        rows = hits.get("hits") if type(hits) is dict else None
        _require(
            response.get("timed_out") is False
            and response.get("terminated_early", False) is False
        )
        _require(
            type(rows) is list
            and len(rows) == min(projection.PAGE_SIZE, len(documents) - start)
            and projection.same_json(
                hits.get("total"), {"value": len(documents), "relation": "eq"}
            )
        )
        for row in rows:
            _require(type(row) is dict and type(row.get("_source")) is dict)
            data = row["_source"]
            identifier = data.get("dataset_id")
            document = documents.get(identifier) if type(identifier) is str else None
            native_id = row.get("_id")
            _require(
                document is not None
                and identifier not in found
                and row.get("_index") == index
                and type(native_id) is str
                and 1 <= len(native_id) <= 1000
                and not any(c in native_id for c in "\x00\r\n")
                and native_id not in native_ids
                and type(row.get("_seq_no")) is int
                and row["_seq_no"] >= 0
                and type(row.get("_primary_term")) is int
                and row["_primary_term"] >= 1
                and data.get("url") == document["url"]
                and row.get("sort") == [document["url"]]
                and (previous is None or document["url"] > previous)
                and data.get("depositar_bundle_id") == bundle.bundle_id
                and data.get("depositar_document_sha256") == hashes[document["url"]]
                and all(
                    projection.same_json(data.get(key), list(document[key]))
                    for key in ("role", "label", "virtual_host")
                )
            )
            projected = data.get(filters.FIELD)
            _require(
                projected is None
                or projection.same_json(
                    projected,
                    filters.metadata(
                        bundle.condition_metadata[identifier], bundle.bundle_id
                    ),
                )
            )
            context = _json_wire_copy(document["resource_context"])
            _require(projection.same_json(data.get("resource_context", []), context))
            status = data.get("content_chunk_status")
            if status is None:
                _require(
                    data.get("content") == document["content"]
                    and "content_chunk_vector" not in data
                )
                native_sha = None
            else:
                _require(status == "done")
                native_sha = chunks.digest(
                    chunks.validate(
                        data.get("content"),
                        data.get("content_chunk_vector"),
                        document["content"],
                    )
                )
            found[identifier] = {
                "id": native_id,
                "seq_no": row["_seq_no"],
                "primary_term": row["_primary_term"],
                "native_sha256": native_sha,
                "metadata_sha256": _digest(
                    {
                        key: value
                        for key, value in data.items()
                        if key not in NATIVE_FIELDS
                    }
                ),
            }
            native_ids.add(native_id)
            previous = document["url"]
    _require(set(found) == set(documents))
    repeated, _ = projection._mapping(
        client, max_url_length=max(len(row["url"]) for row in bundle.documents)
    )
    _require(repeated == index)
    return index, found


def _donor_record(sources, hit, original):
    donor = next(
        (row for row in sources if row["root"].name == hit["source_slot"]), None
    )
    _require(
        donor is not None and donor["manifest_sha256"] == hit["source_manifest_sha256"]
    )
    entry = next(
        (
            row
            for row in donor["manifest"]["entries"]
            if row["dataset_id"] == hit["source_dataset_id"]
        ),
        None,
    )
    _require(entry is not None and entry["record_sha256"] == hit["record_sha256"])
    return _load_record(donor["root"] / "vector-cache", entry, original)


def _select_hits(documents, sources):
    by_content = {}
    for donor in sources:
        for entry in donor["manifest"]["entries"]:
            if entry["record_sha256"] is not None:
                by_content.setdefault(entry["content_sha256"], (donor, entry))
    selected = []
    for identifier, document in sorted(documents.items()):
        candidate = by_content.get(_content_sha256(document["content"]))
        if candidate is None:
            continue
        donor, entry = candidate
        hit = {
            "dataset_id": identifier,
            "source_slot": donor["root"].name,
            "source_manifest_sha256": donor["manifest_sha256"],
            "source_dataset_id": entry["dataset_id"],
            "record_sha256": entry["record_sha256"],
        }
        _donor_record(sources, hit, document["content"])
        selected.append(hit)
    return selected


def _write_hit(client, index, row, native):
    action = {
        "update": {
            "_index": index,
            "_id": row["id"],
            "if_seq_no": row["seq_no"],
            "if_primary_term": row["primary_term"],
        }
    }
    payload = (
        private_io.canonical(action) + private_io.canonical({"doc": native})
    ).decode("utf-8")
    _require(len(payload.encode("utf-8")) <= MAX_REQUEST_BYTES)
    response = projection._ok(
        client, "POST", "/" + index + "/_bulk", payload, ndjson=True
    )
    items = response.get("items")
    _require(
        response.get("errors") is False and type(items) is list and len(items) == 1
    )
    item = items[0]
    update = item.get("update") if type(item) is dict else None
    _require(
        type(update) is dict
        and set(item) == {"update"}
        and update.get("_index") == index
        and update.get("_id") == row["id"]
        and update.get("status") == 200
        and update.get("result") in {"updated", "noop"}
        and "error" not in update
    )
    shards = update.get("_shards", {})
    _require(
        type(shards) is dict
        and type(shards.get("total")) is int
        and type(shards.get("successful")) is int
        and 1 <= shards["successful"] <= shards["total"]
        and type(shards.get("failed")) is int
        and shards["failed"] == 0
    )


def _seed(context, runtime, images, opensearch_factory):
    history = context["root"] / "vector-operations"
    if history.exists():
        descriptor = private_io.open_directory(history)
        try:
            _require(set(os.listdir(descriptor)) <= {"recipe.json"})
        finally:
            os.close(descriptor)
    profile = _profile(context)
    sources = _sources(context, profile)
    peers = connection.peers(context, runtime, images)
    admin, ca = connection.materials(context)
    client = opensearch_factory(peers["opensearch-search"], ca, admin)
    documents = {row["dataset_id"]: row for row in context["bundle"].documents}
    index, rows = _target_inventory(client, context["bundle"])
    root = _directory(context["root"] / "vector-reuse")
    base = {
        "schema_version": 1,
        "target": execution.catalog_identity(context),
        "profile_sha256": _digest(profile),
        "index": index,
    }
    try:
        intent, intent_sha = _read(root / "started.json")
    except FileNotFoundError:
        _require(all(row["native_sha256"] is None for row in rows.values()))
        hits = _select_hits(documents, sources)
        intent = {
            **base,
            "hits": hits,
            "metadata_sha256": {
                key: row["metadata_sha256"] for key, row in sorted(rows.items())
            },
        }
        intent_sha = _write(root / "started.json", intent)
    else:
        _require(
            type(intent) is dict
            and set(intent) == {*base, "hits", "metadata_sha256"}
            and all(
                projection.same_json(intent[key], value) for key, value in base.items()
            )
        )
        hits = intent["hits"]
        _require(type(hits) is list and len(hits) <= len(documents))
        for hit in hits:
            _require(
                type(hit) is dict
                and set(hit)
                == {
                    "dataset_id",
                    "source_slot",
                    "source_manifest_sha256",
                    "source_dataset_id",
                    "record_sha256",
                }
            )
            _require(
                type(hit["dataset_id"]) is str
                and hit["dataset_id"] in documents
                and type(hit["source_dataset_id"]) is str
                and type(hit["source_slot"]) is str
                and SLOT.fullmatch(hit["source_slot"]) is not None
            )
            _require(
                all(
                    type(hit[key]) is str and SHA256.fullmatch(hit[key]) is not None
                    for key in ("source_manifest_sha256", "record_sha256")
                )
            )
        _require(
            [row["dataset_id"] for row in hits]
            == sorted({row["dataset_id"] for row in hits})
        )
    _require(
        intent["metadata_sha256"]
        == {key: row["metadata_sha256"] for key, row in sorted(rows.items())}
    )
    by_id = {row["dataset_id"]: row for row in hits}
    try:
        saved, _ = _read(root / "complete.json")
    except FileNotFoundError:
        saved = None
    changed = False
    for identifier, row in rows.items():
        hit = by_id.get(identifier)
        if hit is None:
            _require(row["native_sha256"] is None)
            continue
        native = _donor_record(sources, hit, documents[identifier]["content"])
        if row["native_sha256"] is not None:
            _require(
                row["native_sha256"]
                == chunks.digest(
                    {key: native[key] for key in ("content", "content_chunk_vector")}
                )
            )
        else:
            _require(saved is None)
            _write_hit(client, index, row, native)
            changed = True
    if changed:
        projection.require_complete_shards(
            projection._ok(client, "POST", "/" + index + "/_refresh")
        )
    repeated_index, repeated = _target_inventory(client, context["bundle"])
    _require(
        repeated_index == index
        and {key: row["metadata_sha256"] for key, row in sorted(repeated.items())}
        == intent["metadata_sha256"]
    )
    for identifier, row in repeated.items():
        expected = (
            _donor_record(sources, by_id[identifier], documents[identifier]["content"])
            if identifier in by_id
            else None
        )
        expected_sha = (
            chunks.digest(
                {key: expected[key] for key in ("content", "content_chunk_vector")}
            )
            if expected
            else None
        )
        _require(row["native_sha256"] == expected_sha)
    _require(connection.peers(context, runtime, images) == peers)
    result = {
        **base,
        "status": "seeded",
        "started_sha256": intent_sha,
        "reused_documents": len(hits),
        "new_documents": len(documents) - len(hits),
    }
    if saved is not None:
        _require(projection.same_json(saved, result))
    else:
        _write(root / "complete.json", result)
    return {
        "status": "already-seeded" if saved is not None else "seeded",
        "reused_documents": len(hits),
        "new_documents": len(documents) - len(hits),
    }


def seed(context, runtime, images, *, opensearch_factory=OpenSearchClient):
    """Idempotently seed only three native fields before a target's first pass."""
    try:
        return _seed(context, runtime, images, opensearch_factory)
    except (OSError, ValueError, KeyError, TypeError, RecursionError, StopIteration):
        raise RuntimeError("catalog_vector_reuse_seed_invalid") from None
