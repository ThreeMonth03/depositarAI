"""Compiler contracts, independent predicate oracle and safe query boundaries."""

from copy import deepcopy
import itertools
import unittest

from search.planning import index_filters as index
from search.api.retrieval import query as compiler
from search.planning.predicates import classify, project
from search.tests.fixtures import condition_document


def evaluate(query, flat):
    if "bool" in query:
        value = query["bool"]
        return (
            all(evaluate(q, flat) for q in value.get("filter", []))
            and not any(evaluate(q, flat) for q in value.get("must_not", []))
            and sum(evaluate(q, flat) for q in value.get("should", []))
            >= value.get("minimum_should_match", 0)
        )
    kind, values = next(iter(query.items()))
    field, expected = next(iter(values.items()))
    actual = flat.get(field)
    if kind == "term":
        return actual == expected
    if kind == "terms":
        return actual in expected
    if kind == "range":
        op, limit = next(iter(expected.items()))
        return (
            actual is not None
            and {
                "lt": lambda: actual < limit,
                "lte": lambda: actual <= limit,
                "gt": lambda: actual > limit,
                "gte": lambda: actual >= limit,
            }[op]()
        )
    raise AssertionError(kind)


class IndexFilterTests(unittest.TestCase):
    def test_mapping_and_projection_are_explicit_and_do_not_mutate(self):
        source = condition_document("2020", "2024-02", formats=["csv", "PDF"])
        before = deepcopy(source)
        projected = index.metadata(source, "a" * 64)
        fields = index.mapping()["properties"][index.FIELD]
        self.assertEqual(fields["dynamic"], "strict")
        self.assertEqual(set(projected), set(fields["properties"]))
        self.assertEqual(source, before)
        self.assertEqual(projected["schema"], index.VERSION)

    def test_generated_filters_equal_independent_reference(self):
        dates = [None, "2019", "2020", "2020-06", "2020-06-15", "2025", "invalid"]
        conditions = [
            {},
            {"time": {"start": "2020", "end": "2020"}},
            {"time": {"start": "2020-06", "end": "2020-06"}},
            {"time": {"start": None, "end": "2020"}},
            {"time": {"start": "2020", "end": None}},
            {"formats": ["CSV"]},
            {"formats": ["CSV", "PDF"]},
            {"time": {"start": "2020", "end": "2020"}, "formats": ["CSV"]},
        ]
        for start, end, formats, cond in itertools.product(
            dates, dates, ([], ["CSV"], ["pdf"], ["XML"]), conditions
        ):
            doc = condition_document(start, end, formats=formats)
            state = classify(project(doc), cond)
            flat = {
                index.FIELD + "." + k: v
                for k, v in index.metadata(doc, "a" * 64).items()
            }
            for policy, expected in (
                ("strict", state == "match"),
                ("outside", state == "outside"),
                ("unknown", state == "unknown"),
                ("retain_unknown", state != "outside"),
                ("all", True),
            ):
                self.assertEqual(
                    evaluate(compiler.condition_filter(cond, policy), flat), expected
                )

    def test_missing_schema_is_not_treated_as_unknown(self):
        for policy in ("strict", "retain_unknown", "unknown", "outside", "all"):
            self.assertFalse(evaluate(compiler.condition_filter({}, policy), {}))

    def test_invalid_conditions_and_unimplemented_geo_fail_closed(self):
        for value in (
            None,
            {"query": {"match_all": {}}},
            {"formats": ["CSV OR *"]},
            {"bbox": [0, 0, 1, 1]},
        ):
            with self.assertRaises(ValueError):
                compiler.condition_filter(value)
        with self.assertRaises(ValueError):
            compiler.condition_filter({}, "user_defined")

    def test_same_filter_in_lexical_vector_and_outer_boundary(self):
        args = ("去年水質 CSV", {"formats": ["CSV"]}, "a" * 64)
        lexical = compiler.retrieval_query(*args, branch="lexical")
        vector = compiler.retrieval_query(*args, branch="vector", vector=[0.1] * 768)
        filt = lexical["query"]["bool"]["filter"][0]
        self.assertEqual(vector["query"]["bool"]["filter"], [filt])
        nested = vector["query"]["bool"]["must"][0]["nested"]
        self.assertEqual(nested["query"]["knn"][index.VECTOR_FIELD]["filter"], filt)

    def test_unsafe_limits_and_vectors_rejected(self):
        for size in (0, 21, True, "5"):
            with self.assertRaises(ValueError):
                compiler.retrieval_query(
                    "水質", {}, "a" * 64, branch="lexical", top_k=size
                )
        for vec in (None, [1], [0.0] * 768, [float("nan")] * 768, [True] * 768):
            with self.assertRaises(ValueError):
                compiler.retrieval_query(
                    "水質", {}, "a" * 64, branch="vector", vector=vec
                )
        with self.assertRaises(ValueError):
            compiler.catalog_filter("* OR private")

    def test_user_text_is_not_query_dsl(self):
        query = compiler.retrieval_query(
            "水質 OR role:admin", {}, "a" * 64, branch="lexical"
        )
        self.assertNotIn("query_string", str(query))
        self.assertEqual(query["_source"], ["dataset_id", "url", "title"])
        self.assertNotIn("script", str(query))

    def test_denied_role_matches_pinned_fess_prefix(self):
        self.assertEqual(
            compiler.catalog_filter("a" * 64)["bool"]["must_not"],
            [{"term": {"role": "DRdepositar_catalog"}}],
        )

    def test_bundle_identity_uses_explicit_keyword_not_fess_language_template(self):
        self.assertEqual(
            index.mapping()["properties"][index.FIELD]["properties"]["catalog"],
            {"type": "keyword"},
        )
        self.assertEqual(
            compiler.catalog_filter("a" * 64)["bool"]["filter"][0],
            {"term": {index.FIELD + ".catalog": "a" * 64}},
        )
        with self.assertRaises(ValueError):
            index.metadata(condition_document(), "* OR private")
