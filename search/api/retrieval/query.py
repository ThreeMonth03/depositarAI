"""Compile literal searches and validated conditions into one OpenSearch query."""

from copy import deepcopy
import math

from search.catalog.embedding import DIMENSIONS
from search.catalog.policy import (
    CATALOG_LABEL,
    CATALOG_SEARCH_ROLE,
    CATALOG_VIRTUAL_HOST,
)
from search.catalog.query import normalize_query
from search.planning.index_filters import (
    FIELD,
    FIELDS,
    VERSION,
    VECTOR_PATH,
    VECTOR_FIELD,
    validate_bundle_id,
)
from search.planning.predicates import compile_filters, validate_conditions


def _translate(node):
    """Translate only the AST produced locally by compile_filters()."""
    if "conditions" in node:
        children = [_translate(c) for c in node["conditions"]]
        if node["operator"] == "AND":
            return {"bool": {"filter": children}}
        if node["operator"] == "OR":
            return {"bool": {"should": children, "minimum_should_match": 1}}
        raise ValueError("unsupported internal boolean operation")
    name = node["field"].removeprefix("meta.")
    if name not in FIELDS:
        raise ValueError("unsupported internal field")
    field, op, value = FIELD + "." + name, node["operator"], node["value"]
    if op == "==":
        return {"term": {field: value}}
    if op == "in":
        return {"terms": {field: value}}
    if op in ("<", "<=", ">", ">="):
        return {
            "range": {
                field: {{"<": "lt", "<=": "lte", ">": "gt", ">=": "gte"}[op]: value}
            }
        }
    raise ValueError("unsupported internal comparison")


def condition_filter(conditions, policy="retain_unknown"):
    validate_conditions(conditions)
    if "bbox" in conditions:
        raise ValueError("geographic index filtering not enabled")
    if policy not in ("strict", "retain_unknown", "outside", "unknown", "all"):
        raise ValueError("unknown filter policy")
    filters = compile_filters(conditions)
    match, outside = (_translate(filters[k]) for k in ("match", "outside"))
    # A missing/unmigrated projection is an error in the index, NOT an unknown
    # observation date. All modes must fail closed for those documents.
    query = {"filter": [{"term": {FIELD + ".schema": VERSION}}]}
    if policy == "strict":
        query["filter"].append(match)
    elif policy == "outside":
        query["filter"].append(outside)
    elif policy == "retain_unknown":
        query["must_not"] = [outside]
    elif policy == "unknown":
        query["must_not"] = [match, outside]
    return {"bool": query}


def catalog_filter(bundle_id):
    validate_bundle_id(bundle_id)
    return {
        "bool": {
            "filter": [
                # Fess's *_id dynamic template treats 'id' as Indonesian;
                # the legacy source identifier is NOT an exact keyword field.
                {"term": {FIELD + ".catalog": bundle_id}},
                {"term": {"label": CATALOG_LABEL}},
                {"term": {"virtual_host": CATALOG_VIRTUAL_HOST}},
                {"term": {"role": CATALOG_SEARCH_ROLE}},
            ],
            # Fess 15.8's configured/default denied-role prefix is 'D'. This adapter
            # is limited to the single verified public catalog role, not users'
            # general Fess role expressions.
            "must_not": [{"term": {"role": "D" + CATALOG_SEARCH_ROLE}}],
        }
    }


def retrieval_query(
    query,
    conditions,
    bundle_id,
    *,
    branch,
    policy="retain_unknown",
    top_k=20,
    vector=None,
):
    """Both lexical and nested ANN paths enforce the SAME bounded filter.

    The kNN copy is an efficient filter during candidate selection; the outer
    copy remains an independent enforcement boundary. BM25 weights here are
    fixed settings shared by every product query.
    """
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError("top_k outside 1..20")
    literal = normalize_query(query)
    filt = {
        "bool": {
            "filter": [catalog_filter(bundle_id), condition_filter(conditions, policy)]
        }
    }
    if branch == "lexical":
        ranked = {
            "multi_match": {
                "query": literal,
                "fields": ["title^2", "content", "important_content^2"],
                "type": "best_fields",
                "operator": "or",
            }
        }
    elif branch == "vector":
        if (
            not isinstance(vector, list)
            or len(vector) != DIMENSIONS
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
            or not any(vector)
        ):
            raise ValueError("expected finite nonzero Granite 768D query vector")
        ranked = {
            "nested": {
                "path": VECTOR_PATH,
                "score_mode": "max",
                "query": {
                    "knn": {
                        VECTOR_FIELD: {
                            "vector": list(vector),
                            "k": 100,
                            "filter": deepcopy(filt),
                        }
                    }
                },
            }
        }
    else:
        raise ValueError("unsupported retrieval branch")
    return {
        "size": top_k,
        "timeout": "10s",
        "track_total_hits": True,
        "_source": ["dataset_id", "url", "title"],
        "query": {"bool": {"filter": [filt], "must": [ranked]}},
    }
