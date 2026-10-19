"""Wire decoding has one strict implementation, independent of API settings."""

import unittest

from search.serialization import strict_json


class StrictJSONTests(unittest.TestCase):
    def test_duplicate_keys_are_rejected_at_every_object_depth(self):
        for raw in (b'{"x":1,"x":2}', b'{"outer":{"x":1,"x":2}}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "duplicate"):
                strict_json(raw)

    def test_nonfinite_constants_are_rejected(self):
        for raw in (b"NaN", b"Infinity", b"-Infinity", b'{"x":NaN}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "number"):
                strict_json(raw)

    def test_invalid_utf8_and_invalid_json_are_rejected(self):
        for raw in (b'"\xff"', b"{", b"{} trailing"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                strict_json(raw)
