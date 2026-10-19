"""Execute the actual pinned Haystack graph with injected live-client boundary."""

import unittest
from search.api.retrieval.pipeline import retrieve


class PipelineTests(unittest.TestCase):
    def test_both_branches_receive_exact_same_conditions_and_fuse(self):
        calls = []

        class Client:
            def retrieve(self, query, conditions, branch, vector):
                calls.append((query, conditions, branch, vector))
                return [("shared", 1), (branch, 0.5)]

        conditions = {"time": {"start": "2025", "end": "2025"}}
        rows, branches = retrieve(Client(), "水質", conditions, [1.0] * 768, 20)
        self.assertEqual(rows[0].id, "shared")
        self.assertEqual(len(rows), 3)
        self.assertEqual(set(branches), {"lexical", "vector"})
        self.assertEqual({r[2] for r in calls}, {"lexical", "vector"})
        self.assertTrue(all(r[1] == conditions for r in calls))

    def test_failed_branch_does_not_return_one_sided_results(self):
        class Client:
            def retrieve(self, query, conditions, branch, vector):
                if branch == "vector":
                    raise ValueError("private error")
                return [("found", 1)]

        with self.assertRaises(Exception):
            retrieve(Client(), "水質", {}, [1.0] * 768, 5)

    def test_both_empty_produce_empty_results(self):
        class Client:
            def retrieve(self, *args):
                return []

        rows, _ = retrieve(Client(), "水質", {}, [1.0] * 768, 5)
        self.assertEqual(rows, [])
