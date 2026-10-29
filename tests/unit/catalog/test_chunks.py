"""Native content/vector alignment without reproducing Fess's chunker."""

from __future__ import annotations

import copy
import unittest

from search.operations.catalog import chunks


def vector():
    return {"vector": [1.0] + [0.0] * 767}


class NativeChunksTests(unittest.TestCase):
    def test_ordered_unicode_chunks_preserve_whitespace_and_are_copied(self):
        content = [" 臺灣 🌧️\n", "e\u0301 data "]
        vectors = [vector(), vector()]
        payload = chunks.validate(content, vectors, "".join(content))
        self.assertEqual({"content": content, "content_chunk_vector": vectors}, payload)
        content[0] = "changed"
        vectors[0]["vector"][0] = -1.0
        self.assertEqual(" 臺灣 🌧️\n", payload["content"][0])
        self.assertEqual(1.0, payload["content_chunk_vector"][0]["vector"][0])

    def test_repeated_text_is_valid_when_order_and_vectors_align(self):
        payload = chunks.validate(["same", "same"], [vector(), vector()], "samesame")
        self.assertEqual(["same", "same"], payload["content"])

    def test_digest_binds_text_boundaries_even_when_full_text_and_vectors_match(self):
        first = chunks.validate(["ab", "cdef"], [vector(), vector()], "abcdef")
        shifted = chunks.validate(["abc", "def"], [vector(), vector()], "abcdef")
        self.assertNotEqual(chunks.digest(first), chunks.digest(shifted))

    def test_wrong_order_overlap_missing_text_and_malformed_chunks_are_rejected(self):
        cases = (
            (["def", "abc"], "abcdef"),
            (["abc", "cdef"], "abcdef"),
            (["abc"], "abcdef"),
            (["abc", "def", "extra"], "abcdef"),
            ([" abcdef "], "abcdef"),
            (["abc", None], "abcdef"),
            (["abc", 123], "abcdef"),
            (["", "abcdef"], "abcdef"),
            (("abcdef",), "abcdef"),
            ("abcdef", "abcdef"),
            (None, "abcdef"),
            ([], "abcdef"),
        )
        for content, original in cases:
            with self.subTest(content=content):
                with self.assertRaisesRegex(
                    ValueError, "^catalog_chunk_content_invalid$"
                ):
                    chunks.validate(content, [vector()], original)

    def test_scalar_input_and_native_chunk_limits_are_enforced(self):
        for original in (None, True, b"abc", "", "x" * 50_001, "\ud800"):
            with self.subTest(type=type(original), length=len(str(original))):
                with self.assertRaisesRegex(
                    ValueError, "^catalog_chunk_content_invalid$"
                ):
                    chunks.validate([original], [vector()], original)
        with self.assertRaisesRegex(ValueError, "^catalog_chunk_content_invalid$"):
            chunks.validate(["x"] * 1001, [vector()] * 1001, "x" * 1001)

    def test_native_chunk_and_vector_counts_must_match(self):
        for vectors in (None, (), [], [vector()], [vector()] * 3):
            with self.subTest(count=len(vectors) if vectors is not None else None):
                with self.assertRaisesRegex(
                    ValueError, "^catalog_chunk_vectors_invalid$"
                ):
                    chunks.validate(["abc", "def"], vectors, "abcdef")

    def test_only_exact_finite_nonzero_768_dimensional_vectors_are_accepted(self):
        invalid = (
            None,
            [],
            {},
            {"other": vector()["vector"]},
            {"vector": vector()["vector"], "unbound": "metadata"},
            {"vector": tuple(vector()["vector"])},
            {"vector": [1.0] * 767},
            {"vector": [1.0] * 769},
            {"vector": [0.0] * 768},
            {"vector": [True] + [0.0] * 767},
            {"vector": ["1"] + [0.0] * 767},
            {"vector": [None] + [0.0] * 767},
            {"vector": [float("nan")] + [0.0] * 767},
            {"vector": [float("inf")] + [0.0] * 767},
            {"vector": [-float("inf")] + [0.0] * 767},
            {"vector": [10**1000] + [0.0] * 767},
        )
        for index, item in enumerate(invalid):
            with self.subTest(index=index):
                with self.assertRaisesRegex(
                    ValueError, "^catalog_chunk_vectors_invalid$"
                ):
                    chunks.validate(["private content"], [item], "private content")

    def test_validation_does_not_mutate_inputs_on_failure(self):
        content = ["abc"]
        vectors = [vector()]
        before = copy.deepcopy((content, vectors))
        with self.assertRaises(ValueError):
            chunks.validate(content, vectors, "different")
        self.assertEqual(before, (content, vectors))


if __name__ == "__main__":
    unittest.main()
