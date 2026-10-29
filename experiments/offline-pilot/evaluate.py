"""Trace one private catalog through real CPU embeddings, BM25, kNN and RRF.

Inputs and outputs stay outside the checkout. The caller supplies the isolated
catalog network and the fixed query binding, CA and password mounts used by
IndexClient; this script neither provisions services nor starts an API worker.
"""

import argparse
from contextlib import contextmanager
import gzip
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
MAX_CASES = 64
CASE_KINDS = {
    "csv_header",
    "pdf_phrase",
    "metadata_control",
    "time_control",
    "format_control",
}
TEXT_FIELDS = ("title", "content", "important_content")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def private_path(value):
    path = Path(value).resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("pilot inputs and outputs must stay outside the checkout")
    return path


def read_json(path, maximum):
    from search.serialization import strict_json

    if path.stat().st_mode & 0o077:
        raise ValueError("pilot JSON must be private to its owner")
    with path.open("rb") as source:
        raw = source.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("pilot JSON exceeds its byte limit")
    return strict_json(raw)


def write_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".pilot-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def report_lifecycle(path, report):
    """Replace prior success before dependencies run and preserve failure status."""
    write_json(path, report)
    try:
        yield
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
        write_json(path, report)
        raise
    else:
        report["status"] = "complete"
        write_json(path, report)


def condition_status(bundle, identifier, conditions):
    from search.planning.predicates import classify, project

    return (
        classify(project(bundle.condition_metadata[identifier]), conditions)
        if conditions
        else "not_checked"
    )


def load_cases(path, dataset_ids):
    from search.catalog.query import normalize_query
    from search.planning.predicates import validate_conditions

    value = read_json(path, 1024 * 1024)
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "cases"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["cases"]) is not list
        or not 1 <= len(value["cases"]) <= MAX_CASES
    ):
        raise ValueError("invalid pilot case file")
    seen = set()
    for case in value["cases"]:
        if type(case) is not dict or set(case) != {
            "id",
            "kind",
            "query",
            "conditions",
            "targets",
            "anchor",
        }:
            raise ValueError("invalid pilot case fields")
        identifier = case["id"]
        if (
            type(identifier) is not str
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", identifier)
            or identifier in seen
            or type(case["kind"]) is not str
            or case["kind"] not in CASE_KINDS
            or type(case["query"]) is not str
            or not 1 <= len(case["query"]) <= 512
            or normalize_query(case["query"]) != case["query"]
            or type(case["conditions"]) is not dict
            or set(case["conditions"]) - {"time", "formats"}
            or type(case["targets"]) is not list
            or not 1 <= len(case["targets"]) <= 20
            or any(type(target) is not str for target in case["targets"])
            or len(set(case["targets"])) != len(case["targets"])
            or not set(case["targets"]) <= dataset_ids
        ):
            raise ValueError("invalid pilot case values")
        validate_conditions(case["conditions"])
        anchor = case["anchor"]
        if case["kind"] in {"csv_header", "pdf_phrase"}:
            if (
                type(anchor) is not dict
                or set(anchor) != {"resource_id", "text"}
                or type(anchor["resource_id"]) is not str
                or type(anchor["text"]) is not str
                or not 1 <= len(anchor["text"]) <= 320
                or not anchor["text"].strip()
            ):
                raise ValueError("resource case needs a literal source anchor")
        elif anchor is not None:
            raise ValueError("metadata and condition controls have no resource anchor")
        seen.add(identifier)
    return value["cases"]


def bundle_evidence(directory, bundle):
    """Reuse the sealed corpus to derive a metadata projection without an index."""
    from search.catalog.build.normalize import fess_document
    from search.serialization import strict_json

    with gzip.open(directory / "corpus.v2.jsonl.gz", "rb") as source:
        corpus = [strict_json(line) for line in source]
    with gzip.open(directory / "resource-context.v1.jsonl.gz", "rb") as source:
        resources = {
            row["dataset_id"]: row["resources"]
            for row in (strict_json(line) for line in source)
        }
    baseline = {row["dataset_id"]: fess_document(row, {}) for row in corpus}
    identifiers = {row["dataset_id"] for row in bundle.documents}
    if set(baseline) != identifiers or set(resources) != identifiers:
        raise ValueError("pilot evidence differs from the sealed inventory")
    return baseline, resources


def anchor_evidence(case, documents, baseline, resources):
    """Check literal grounding without interpreting a retrieval hit as causation."""
    anchor = case["anchor"]
    if anchor is None:
        return None
    needle = anchor["text"].casefold()
    baseline_matches = [
        identifier
        for identifier, document in baseline.items()
        if any(needle in document[field].casefold() for field in TEXT_FIELDS)
    ]
    if baseline_matches:
        raise ValueError("source anchor already occurs in baseline metadata")
    found = []
    for identifier in case["targets"]:
        for resource in resources[identifier]:
            context = resource["context"]
            if context["resource_id"] != anchor["resource_id"]:
                continue
            expected_format = "CSV" if case["kind"] == "csv_header" else "PDF"
            if (
                context["format"] != expected_format
                or needle not in resource["index_text"].casefold()
                or needle not in documents[identifier]["content"].casefold()
                or (
                    case["kind"] == "csv_header"
                    and needle
                    not in {column.casefold() for column in context["columns"]}
                )
            ):
                raise ValueError("source anchor is not in the selected indexed content")
            found.append(
                {
                    "dataset_id": identifier,
                    "resource_id": context["resource_id"],
                    "anchor_sha256": digest(anchor["text"]),
                    "profile_sha256": resource["profile_sha256"],
                    "content_sha256": resource["content_sha256"],
                    "index_text_sha256": digest(resource["index_text"]),
                    "metadata_projection_occurrences": 0,
                    "enriched_dataset_occurrences": [
                        target
                        for target, document in documents.items()
                        if needle in document["content"].casefold()
                    ],
                    "literal_in_query": needle in case["query"].casefold(),
                }
            )
    if len(found) != 1:
        raise ValueError("source anchor must identify one selected target resource")
    return found[0]


def checked_vector(value):
    from search.catalog.embedding import DIMENSIONS

    if (
        type(value) is not list
        or len(value) != DIMENSIONS
        or any(
            type(item) not in (int, float) or not math.isfinite(item) for item in value
        )
        or not any(value)
    ):
        raise ValueError("expected an actual finite nonzero Granite 768D vector")
    return value


def load_vector_cache(path):
    from search.catalog.embedding import DIMENSIONS, MODEL, MODEL_DIGEST

    identity = {
        "schema_version": 1,
        "model": MODEL,
        "model_digest": MODEL_DIGEST,
        "dimensions": DIMENSIONS,
    }
    if not path.exists():
        return {**identity, "queries": {}}
    value = read_json(path, 2 * 1024 * 1024)
    if (
        type(value) is not dict
        or set(value) != {*identity, "queries"}
        or any(
            type(value[key]) is not type(expected) or value[key] != expected
            for key, expected in identity.items()
        )
        or type(value["queries"]) is not dict
        or len(value["queries"]) > MAX_CASES
    ):
        raise ValueError("query embedding cache identity mismatch")
    for key, entry in value["queries"].items():
        if (
            type(entry) is not dict
            or set(entry) != {"query", "vector"}
            or type(entry["query"]) is not str
            or digest(entry["query"]) != key
        ):
            raise ValueError("invalid query embedding cache entry")
        checked_vector(entry["vector"])
    return value


class TracedClient:
    def __init__(self, client):
        self.client = client
        self.branches = {}
        self.timing_ms = {}

    def retrieve(self, query, conditions, branch, vector):
        started = time.monotonic()
        hits = self.client.retrieve(query, conditions, branch, vector)
        self.branches[branch] = hits
        self.timing_ms[branch] = (time.monotonic() - started) * 1000
        return hits


def run(args):
    from search.api.retrieval.index import IndexClient
    from search.api.retrieval.pipeline import retrieve
    from search.catalog.bundle import CatalogBundle
    from search.catalog.embedding import DIMENSIONS, MODEL, MODEL_DIGEST
    from search.planning.index_filters import VERSION

    directory = private_path(args.bundle)
    casefile = private_path(args.casefile)
    output = private_path(args.output)
    cache_path = private_path(args.vector_cache)
    if len({casefile, output, cache_path}) != 3:
        raise ValueError("case file, output and vector cache must be distinct")
    bundle = CatalogBundle.load(
        directory, expected_manifest_sha256=args.manifest_sha256
    )
    documents = {row["dataset_id"]: row for row in bundle.documents}
    cases = load_cases(casefile, set(documents))
    baseline, resources = bundle_evidence(directory, bundle)
    evidence = {
        case["id"]: anchor_evidence(case, documents, baseline, resources)
        for case in cases
    }
    cache = load_vector_cache(cache_path)
    report = {
        "schema_version": 1,
        "status": "running",
        "scope": "curated offline pilot; no comparative quality claim",
        "bundle_id": bundle.bundle_id,
        "manifest_sha256": bundle.manifest_sha256,
        "casefile_sha256": hashlib.sha256(casefile.read_bytes()).hexdigest(),
        "index": None,
        "projection_schema": VERSION,
        "model": MODEL,
        "model_digest": MODEL_DIGEST,
        "dimensions": DIMENSIONS,
        "haystack_version": version("haystack-ai"),
        "evaluation_source_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "dataset_count": len(documents),
        "catalog_metadata_resource_count": sum(
            row["resource_count"] for row in documents.values()
        ),
        "indexed_resource_context_count": sum(
            len(row["resource_context"]) for row in documents.values()
        ),
        "metadata_only_dataset_count": sum(
            not row["resource_context"] for row in documents.values()
        ),
        "retrieval_top_k": 20,
        "ranking": "opensearch-haystack-rrf",
        "reranker_used": False,
        "generation_used": False,
        "conditions_are_dataset_metadata": True,
        "cases": [],
    }
    with report_lifecycle(output, report):
        client = IndexClient(bundle)
        report["index"] = client.binding["index"]
        for case in cases:
            key = digest(case["query"])
            cached = key in cache["queries"]
            started = time.monotonic()
            if not cached:
                if len(cache["queries"]) >= MAX_CASES:
                    raise ValueError("query embedding cache exceeds its pilot limit")
                cache["queries"][key] = {
                    "query": case["query"],
                    "vector": checked_vector(client.embed(case["query"])),
                }
                write_json(cache_path, cache)
            vector = cache["queries"][key]["vector"]
            embedding_ms = (time.monotonic() - started) * 1000
            traced = TracedClient(client)
            fused, _ = retrieve(traced, case["query"], case["conditions"], vector, 20)
            stages = {**traced.branches, "rrf": [(hit.id, hit.score) for hit in fused]}
            rows = {}
            for stage, hits in stages.items():
                rows[stage] = [
                    {
                        "dataset_id": identifier,
                        "rank": rank,
                        "score": score,
                        "condition_status": condition_status(
                            bundle, identifier, case["conditions"]
                        ),
                    }
                    for rank, (identifier, score) in enumerate(hits, 1)
                ]
            if any(
                row["condition_status"] == "outside"
                for stage in rows.values()
                for row in stage
            ):
                raise ValueError(
                    "retrieval returned a dataset outside the metadata conditions"
                )
            targets = [
                {
                    "dataset_id": identifier,
                    "condition_status": condition_status(
                        bundle, identifier, case["conditions"]
                    ),
                    "has_resource_context": bool(
                        documents[identifier]["resource_context"]
                    ),
                    "ranks": {
                        stage: next(
                            (
                                row["rank"]
                                for row in stage_rows
                                if row["dataset_id"] == identifier
                            ),
                            None,
                        )
                        for stage, stage_rows in rows.items()
                    },
                }
                for identifier in case["targets"]
            ]
            report["cases"].append(
                {
                    **case,
                    "query_sha256": key,
                    "anchor_evidence": evidence[case["id"]],
                    "embedding_cache_hit": cached,
                    "timing_ms": {"embedding": embedding_ms, **traced.timing_ms},
                    "candidate_count": len(fused),
                    "stages": rows,
                    "target_results": targets,
                }
            )
            write_json(output, report)
    return {"status": "complete", "cases": len(cases), "output": str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--casefile", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--vector-cache", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    try:
        print(json.dumps(run(args), sort_keys=True))
    except Exception as error:
        print(
            json.dumps({"status": "failed", "error_type": type(error).__name__}),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
