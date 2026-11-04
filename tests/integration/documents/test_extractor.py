"""Opt-in actual Fess Java output; no original resources or GPU work."""

import csv
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.operations.documents import extractor
from search.serialization import strict_json


ROOT = Path(__file__).resolve().parents[3]


def _stream(payload):
    return (
        f"<< /Length {len(payload)} >>\nstream\n".encode("ascii")
        + payload
        + b"\nendstream"
    )


def _document(objects):
    document = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, payload in enumerate(objects, 1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode("ascii"))
        document.extend(payload)
        document.extend(b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    document.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(document)


def pdf_fixture(text):
    """Return deterministic bytes whose printable glyph 0x42 maps to NUL.

    Other Unicode characters use distinct printable glyph codes and UTF-16BE
    ToUnicode values. At most 93 distinct non-NUL characters are supported.
    This encodes a syntactic mapping; PDFBox/Fess may still transform output.
    """
    if type(text) is not str or not text:
        raise ValueError("fixture text must be a non-empty exact str")
    characters = tuple(dict.fromkeys(text.replace("\x00", "")))
    available = [code for code in range(0x21, 0x7F) if code != 0x42]
    if len(characters) > len(available):
        raise ValueError("fixture supports 93 distinct non-NUL characters")
    glyphs = dict(zip(characters, available))
    glyphs["\x00"] = 0x42
    mappings = "\n".join(
        f"<{code:02X}> <{character.encode('utf-16-be').hex().upper()}>"
        for character, code in glyphs.items()
    )
    cmap = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) "
        "/Supplement 0 >> def\n"
        "/CMapName /FixtureUnicode def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        f"{len(glyphs)} beginbfchar\n{mappings}\nendbfchar\n"
        "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
    ).encode("ascii")
    encoded = bytes(glyphs[character] for character in text).hex().upper()
    content = f"BT\n/F1 12 Tf\n20 70 Td\n<{encoded}> Tj\nET".encode("ascii")
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 100] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding /ToUnicode 6 0 R >>",
        _stream(content),
        _stream(cmap),
    )
    return _document(objects)


def csv_fixture(columns, values, encoding="utf-8"):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(columns)
    writer.writerow(values)
    text = stream.getvalue()
    if encoding == "utf-16":
        return b"\xff\xfe" + text.encode("utf-16-le")
    return text.encode(encoding)


@unittest.skipUnless(
    os.environ.get("DEPOSITAR_TEST_DOCUMENT_EXTRACTOR_RUNTIME") == "1",
    "explicit sole-owner CPU Docker window required",
)
class ExtractorRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = extractor.build_image(ROOT)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="extractor-fixture-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.commands = []
        self.native_run = extractor._run

    def record_run(self, argv, **kwargs):
        result = self.native_run(argv, **kwargs)
        self.commands.append((list(argv), dict(kwargs), result))
        return result

    def extract_report(self, raw, format, encoding="utf-8"):
        path = self.directory / ("resource." + format.lower())
        path.write_bytes(raw)
        path.chmod(0o600)
        start = len(self.commands)
        with patch.object(extractor, "_run", side_effect=self.record_run):
            report = extractor.extract(
                path, format, encoding, "first_record", image=self.image
            )
        runs = [
            result
            for argv, _, result in self.commands[start:]
            if argv[5:6] == ["run"]
        ]
        self.assertEqual(1, len(runs))
        self.assertEqual(0, runs[0].returncode)
        self.assertEqual(report, strict_json(runs[0].stdout))
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(), report["input_sha256"]
        )
        self.assertEqual(raw, path.read_bytes())
        return report

    def test_pdf_internal_and_repeated_nul_preserve_word_boundaries(self):
        for text, expected in (("A\x00B", "A B"), ("A\x00\x00B", "A  B")):
            with self.subTest(text=text):
                report = self.extract_report(pdf_fixture(text), "PDF")
                self.assertEqual("ok", report["status"])
                self.assertIn(expected, report["text"])
                self.assertNotIn("\x00", report["text"])
                self.assertFalse(report["may_require_ocr"])

    def test_nul_only_pdf_is_empty_with_an_ocr_hint(self):
        report = self.extract_report(pdf_fixture("\x00\x00\x00"), "PDF")
        self.assertEqual("empty_text", report["status"])
        self.assertEqual("", report["text"].strip())
        self.assertEqual([], report["columns"])
        self.assertTrue(report["may_require_ocr"])

    def test_csv_display_labels_and_text_replace_only_nul(self):
        raw = csv_fixture(
            ["sta\x00tion", "value"], ["A\x00\x00B", "7.2"], "utf-16"
        )
        report = self.extract_report(raw, "CSV", "utf-16")
        self.assertEqual("ok", report["status"])
        self.assertEqual(["sta tion", "value"], report["columns"])
        self.assertIn("A  B", report["text"])
        self.assertNotIn("\x00", report["text"])

    def test_non_nul_controls_keep_useful_pdf_and_utf8_csv_text(self):
        report = self.extract_report(pdf_fixture("ALPHA BETA"), "PDF")
        self.assertEqual("ok", report["status"])
        self.assertIn("ALPHA BETA", report["text"])
        raw = csv_fixture(["station", "value"], ["測站😀 e\u0301", "7.2"])
        report = self.extract_report(raw, "CSV")
        self.assertEqual(["station", "value"], report["columns"])
        self.assertIn("測站", report["text"])
        self.assertIn("😀", report["text"])
        self.assertIn("e\u0301", report["text"])

    def test_csv_column_limits_apply_before_display_normalization(self):
        for size, status in ((512, "ok"), (513, "parse_error")):
            with self.subTest(size=size):
                name = (
                    "A" * (size // 2) + "\x00" + "B" * (size - size // 2 - 1)
                )
                raw = csv_fixture([name], ["value"], "utf-16")
                report = self.extract_report(raw, "CSV", "utf-16")
                self.assertEqual(status, report["status"])
                expected = [name.replace("\x00", " ")] if size == 512 else []
                self.assertEqual(expected, report["columns"])

    def test_text_truncation_keeps_surrogates_and_existing_limit(self):
        value = "A" * 65_520 + "\x00" + "😀" * 16 + "B" * 128
        raw = csv_fixture(["column"], [value], "utf-16")
        report = self.extract_report(raw, "CSV", "utf-16")
        self.assertEqual("ok", report["status"])
        self.assertTrue(report["text_truncated"])
        self.assertLessEqual(
            len(report["text"].encode("utf-16-le")) // 2,
            extractor.MAX_TEXT_CHARS,
        )
        self.assertNotIn("\x00", report["text"])
