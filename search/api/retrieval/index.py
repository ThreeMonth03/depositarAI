"""Query one sealed OpenSearch catalog with shared lexical/vector filters."""

from search.catalog.bundle import CatalogBundle
from search.catalog.embedding import MODEL, MODEL_DIGEST
from search.catalog.query import normalize_query

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import ssl
import stat
import urllib.request

from .results import METADATA_FIELDS, RANKING, ValidatedSearch
from .query import retrieval_query
from search.transport.http import HTTPRequest, UrllibTransport, _NoRedirect

from search.planning import index_filters as filters
from search.planning.predicates import classify, project, validate_conditions
from search.secrets import read_secret
from search.serialization import strict_json
from ..shared.peers import service_address

INDEX_RE = re.compile(r"fess\.[0-9]{14,20}\Z")


def load_binding(path, bundle):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o777 not in (0o400, 0o600)
            or info.st_nlink != 1
        ):
            raise ValueError("untrusted index binding")
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise ValueError("binding size cap")
        value = strict_json(raw)
    finally:
        os.close(fd)
    if (
        type(value) is not dict
        or set(value) != {"index", "manifest_sha256", "bundle_id", "projection_schema"}
        or type(value["index"]) is not str
        or not INDEX_RE.fullmatch(value["index"])
        or value["manifest_sha256"] != bundle.manifest_sha256
        or value["bundle_id"] != bundle.bundle_id
        or value["projection_schema"] != filters.VERSION
    ):
        raise ValueError("index binding mismatch")
    return value


class IndexClient:
    def __init__(self, bundle):
        self.bundle = bundle
        self.binding = load_binding(Path("/run/index/binding.json"), bundle)
        # DNS must resolve to one private peer; the fixed CA AND TLS hostname
        # authenticate OpenSearch. No proxy, redirect or caller-chosen URL.
        service_address("opensearch-search", 9200)
        self.origin = "https://opensearch-search:9200"
        self.transport = UrllibTransport()
        context = ssl.create_default_context(cafile="/run/index/root-ca.pem")
        self.transport._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=context),
        )
        self.embedding_origin = service_address("filtered-embedding", 11434)
        self.embedding_transport = UrllibTransport()
        self.by_id = {r["dataset_id"]: r for r in bundle.documents}

    @staticmethod
    def json_request(transport, url, payload=None, *, authorization=None):
        headers = [("Content-Type", "application/json")]
        if authorization:
            headers.append(("Authorization", authorization))
        response = transport.send(
            HTTPRequest(
                "POST" if payload is not None else "GET",
                url,
                tuple(headers),
                json.dumps(payload, allow_nan=False).encode()
                if payload is not None
                else None,
            ),
            timeout=11,
            max_bytes=2 * 1024**2,
        )
        if response.status != 200 or response.url != url:
            raise ValueError("index dependency unavailable")
        return strict_json(response.body)

    def embed(self, query):
        tags = self.json_request(
            self.embedding_transport, self.embedding_origin + "/api/tags"
        )
        if [
            m.get("digest") for m in tags.get("models", []) if m.get("name") == MODEL
        ] != [MODEL_DIGEST]:
            raise ValueError("embedding identity mismatch")
        data = self.json_request(
            self.embedding_transport,
            self.embedding_origin + "/api/embed",
            {
                "model": MODEL,
                "input": normalize_query(query),
                "truncate": False,
                "keep_alive": "5m",
                "options": {"num_gpu": 0, "num_thread": 8},
            },
        )
        if data.get("model") != MODEL or len(data.get("embeddings", [])) != 1:
            raise ValueError("embedding response mismatch")
        return data["embeddings"][0]

    def retrieve(self, query, conditions, branch, vector):
        body = retrieval_query(
            query, conditions, self.bundle.bundle_id, branch=branch, vector=vector
        )
        body["_source"] = [
            "dataset_id",
            "url",
            "role",
            "label",
            "virtual_host",
            "depositar_document_sha256",
            filters.FIELD,
        ]
        password = read_secret(Path("/run/secrets/index_query_password"))
        auth = (
            "Basic "
            + base64.b64encode(("depositar_filtered:" + password).encode()).decode()
        )
        response = self.json_request(
            self.transport,
            self.origin + "/" + self.binding["index"] + "/_search",
            body,
            authorization=auth,
        )
        return validate_hits(response, self.bundle, self.binding["index"], conditions)


def validate_hits(response, bundle, index, conditions):
    shards = response.get("_shards", {})
    if (
        response.get("timed_out") is not False
        or response.get("terminated_early", False)
        or type(shards.get("total")) is not int
        or shards["total"] < 1
        or shards.get("failed") != 0
        or shards.get("successful") != shards["total"]
    ):
        raise ValueError("partial native retrieval")
    hits = response.get("hits", {}).get("hits")
    if type(hits) is not list or len(hits) > 20:
        raise ValueError("invalid index hits")
    by_id = {r["dataset_id"]: r for r in bundle.documents}
    hashes = {url: digest for url, _, digest in bundle.expected_inventory_entries}
    seen, result = set(), []
    for hit in hits:
        source = hit["_source"]
        identifier = source["dataset_id"]
        document = by_id[identifier]
        expected = filters.metadata(
            bundle.condition_metadata[identifier], bundle.bundle_id
        )
        score = hit["_score"]
        if (
            hit["_index"] != index
            or identifier in seen
            or source.get("url") != document["url"]
            or source.get("depositar_document_sha256") != hashes[document["url"]]
            or source.get(filters.FIELD) != expected
            or any(
                source.get(k) != list(document[k])
                for k in ("role", "label", "virtual_host")
            )
            or classify(project(bundle.condition_metadata[identifier]), conditions)
            == "outside"
            or type(score) not in (int, float)
            or not math.isfinite(score)
        ):
            raise ValueError("index hit violates sealed catalog")
        seen.add(identifier)
        result.append((identifier, score))
    return result


def search(payload, settings):
    if type(payload) is not dict or set(payload) != {"query", "top_k", "conditions"}:
        raise ValueError("index request shape")
    query = normalize_query(payload["query"])
    if type(payload["top_k"]) is not int or not 1 <= payload["top_k"] <= 20:
        raise ValueError("index top_k bound")
    conditions = payload["conditions"]
    validate_conditions(conditions)
    if "bbox" in conditions:
        raise ValueError("geographic retrieval is not enabled")
    bundle = CatalogBundle.load(
        settings.bundle_path, expected_manifest_sha256=settings.manifest_sha256
    )
    client = IndexClient(bundle)
    from .pipeline import retrieve

    fused, branches = retrieve(
        client, query, conditions, client.embed(query), payload["top_k"]
    )
    by_id = client.by_id
    results = []
    for hit in fused:
        row = {k: json.loads(json.dumps(by_id[hit.id][k])) for k in METADATA_FIELDS}
        row.update(
            raw_score=hit.score,
            rank_fusion_score=hit.score,
            searchers=[
                label
                for branch, label in (
                    ("lexical", "opensearch-bm25"),
                    ("vector", "opensearch-knn"),
                )
                if hit.id in branches[branch]
            ],
        )
        results.append(row)
    envelope = {
        "manifest_sha256": bundle.manifest_sha256,
        "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
        "conditions": conditions,
        "ranking": RANKING,
        "results": results,
    }
    ValidatedSearch.from_report(
        query,
        envelope,
        conditions=conditions,
        manifest_sha256=settings.manifest_sha256,
        maximum=payload["top_k"],
    )
    return envelope
