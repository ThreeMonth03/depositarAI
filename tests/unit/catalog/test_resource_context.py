"""Validate public resource cards and private provenance without runtime deps."""

from copy import deepcopy
import unittest

from search.catalog import resources


IDENTIFIER = "11111111-1111-4111-8111-111111111111"


class ResourceContextTests(unittest.TestCase):
    def context(self):
        return {
            "resource_id": IDENTIFIER,
            "name": "Water observations",
            "format": "CSV",
            "url": resources.resource_url(
                "https://data.depositar.io/dataset/water", IDENTIFIER
            ),
            "columns": ["pH", "eventDate"],
            "excerpt": "",
        }

    def entry(self):
        context = self.context()
        return {
            "context": context,
            "index_text": " ".join(context["columns"]),
            "profile_sha256": "a" * 64,
            "source_manifest_sha256": "b" * 64,
            "content_sha256": "c" * 64,
            "extractor_image_id": "sha256:" + "d" * 64,
            "extractor_source_sha256": "e" * 64,
            "acquired_at": 1,
            "truncated": False,
        }

    def test_empty_metadata_only_and_independent_mutable_copies(self):
        self.assertEqual([], resources.validate_resource_context([]))
        original = [self.entry()]
        copied = resources.validate_enrichment_resources(original)
        copied[0]["context"]["columns"].append("extra")
        self.assertEqual(["pH", "eventDate"], original[0]["context"]["columns"])

    def test_source_links_have_one_canonical_local_landing_shape(self):
        good = "https://data.depositar.io/dataset/%E6%B0%B4%E8%B3%AA"
        self.assertEqual(
            good + "/resource/" + IDENTIFIER, resources.resource_url(good, IDENTIFIER)
        )
        for url in (
            "http://data.depositar.io/dataset/water",
            "https://evil.test/dataset/water",
            "https://data.depositar.io:443/dataset/water",
            "https://data.depositar.io/dataset/water?token=x",
            "https://data.depositar.io/dataset/water#x",
            "https://data.depositar.io/dataset/..",
            "https://data.depositar.io/dataset/water/resource/" + IDENTIFIER,
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                resources.resource_url(url, IDENTIFIER)

    def test_contexts_are_closed_sorted_unique_json_objects(self):
        item = self.context()
        for value in (
            (item,),
            [item, item],
            [{**item, "content_sha256": "a" * 64}],
            [{**item, "resource_id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"}],
            [{**item, "columns": ("pH",)}],
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resources.validate_resource_context(value)
        another = {**item, "resource_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
        another["url"] = resources.resource_url(
            "https://data.depositar.io/dataset/water", another["resource_id"]
        )
        with self.assertRaises(ValueError):
            resources.validate_resource_context([another, item])

    def test_type_unicode_and_total_column_budgets_fail_closed(self):
        item = self.context()
        for changes in (
            {"name": "someone@example.test"},
            {"name": "fullwidthＡ"},
            {"name": "bad\x00text"},
            {"name": "bad\ud800text"},
            {"columns": ["x" * 129]},
            {"columns": ["x" * 128] * 17},
            {"columns": []},
            {"format": "csv"},
            {"excerpt": "row values"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                resources.validate_resource_context([{**item, **changes}])

    def test_private_provenance_types_and_public_text_agreement(self):
        original = self.entry()
        for changes in (
            {"acquired_at": True},
            {"truncated": 1},
            {"profile_sha256": "a" * 63},
            {"extractor_image_id": "tag:latest"},
            {"index_text": "unrelated values"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                resources.validate_enrichment_resources([{**original, **changes}])
        pdf = deepcopy(original)
        pdf["context"].update(format="PDF", columns=[], excerpt="wrong")
        with self.assertRaises(ValueError):
            resources.validate_enrichment_resources([pdf])
