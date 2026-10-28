"""Verify acquisition reuse, invalidation and corrupt-cache failure behavior."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.operations.documents import cache, source
from search.operations.shared.private_io import canonical


class DocumentCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-document-cache-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "cache"
        self.item = {
            "id": "resource",
            "url": "https://data.depositar.io/data.csv",
            "format": "CSV",
            "size": 17,
            "last_modified": "2026-10-28",
        }
        self.body = b"station,pH\nA,7.2\n"
        self.headers = {
            "content_type": "text/csv",
            "etag": '"one"',
            "last_modified": "Wed, 28 Oct 2026 00:00:00 GMT",
        }

    def acquire(self, **options):
        with patch.object(
            source, "fetch", return_value=(self.body, self.headers)
        ) as fetch:
            result = source.acquire(self.root, self.item, **options)
        return result, fetch.call_count

    def record_path(self):
        key = cache.key_for(source.source_identity(self.item))
        return self.root / ("resource-" + key + ".json")

    def test_first_fetch_and_hit_preserve_body_and_original_acquisition_time(self):
        first, calls = self.acquire(now=100)
        self.assertEqual(1, calls)
        second, calls = self.acquire(now=101)
        self.assertEqual(0, calls)
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(first["local_path"], second["local_path"])
        self.assertEqual(100, second["acquired_at"])
        self.assertEqual(hashlib.sha256(self.body).hexdigest(), second["sha256"])
        self.assertEqual(self.body, second["local_path"].read_bytes())
        self.assertEqual(0o700, self.root.stat().st_mode & 0o777)
        for path in self.root.iterdir():
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertNotIn(self.item["url"], self.record_path().read_text())

    def test_expiry_and_explicit_refresh_fetch_again(self):
        self.acquire(now=100)
        _, calls = self.acquire(now=100 + source.CACHE_TTL_SECONDS - 1)
        self.assertEqual(0, calls)
        result, calls = self.acquire(now=100 + source.CACHE_TTL_SECONDS)
        self.assertEqual(1, calls)
        self.assertEqual(100 + source.CACHE_TTL_SECONDS, result["acquired_at"])
        _, calls = self.acquire(now=100 + source.CACHE_TTL_SECONDS + 1, refresh=True)
        self.assertEqual(1, calls)

    def test_resource_changes_invalidate_but_unrelated_metadata_reuses(self):
        self.acquire(now=100)
        for name, value in (
            ("description", "new description"),
            ("dataset_id", "other dataset"),
        ):
            with self.subTest(name=name):
                self.item[name] = value
                _, calls = self.acquire(now=101)
                self.assertEqual(0, calls)
        for name, value in (
            ("last_modified", "2026-10-29"),
            ("size", 18),
            ("format", "PDF"),
            ("url", "https://data.depositar.io/changed.csv"),
            ("id", "changed"),
        ):
            with self.subTest(name=name):
                self.item[name] = value
                _, calls = self.acquire(now=102)
                self.assertEqual(1, calls)

    def test_refresh_keeps_previous_content_objects(self):
        first, _ = self.acquire(now=100)
        self.body += b"B,7.3\n"
        second, _ = self.acquire(now=101, refresh=True)
        self.assertNotEqual(first["local_path"], second["local_path"])
        self.assertTrue(first["local_path"].is_file())
        self.assertTrue(second["local_path"].is_file())

    def test_source_failure_does_not_promote_stale_cache_or_replace_receipt(self):
        self.acquire(now=100)
        before = self.record_path().read_bytes()
        with patch.object(
            source,
            "fetch",
            side_effect=source.SourceError("document_source_http_failed"),
        ):
            with self.assertRaisesRegex(
                source.SourceError, "^document_source_http_failed$"
            ):
                source.acquire(self.root, self.item, now=100 + source.CACHE_TTL_SECONDS)
        self.assertEqual(before, self.record_path().read_bytes())

    def test_receipt_identity_comparison_preserves_json_types(self):
        self.item["size"] = 1
        self.acquire(now=100)
        record = json.loads(self.record_path().read_text())
        record["source"]["declared_size"] = True
        self.record_path().write_bytes(canonical(record))
        with patch.object(source, "fetch") as fetch:
            with self.assertRaisesRegex(
                source.SourceError, "document_cache_record_invalid"
            ):
                source.acquire(self.root, self.item, now=101)
        fetch.assert_not_called()

    def test_corrupt_or_missing_body_never_causes_implicit_refetch(self):
        for corruption in ("changed", "missing", "mode", "symlink"):
            with self.subTest(corruption=corruption):
                self.root = self.root.parent / ("cache-" + corruption)
                result, _ = self.acquire(now=100)
                body = result["local_path"]
                if corruption == "changed":
                    body.write_bytes(b"different")
                elif corruption == "missing":
                    body.unlink()
                elif corruption == "mode":
                    body.chmod(0o644)
                else:
                    body.unlink()
                    body.symlink_to("/etc/hosts")
                with patch.object(source, "fetch") as fetch:
                    with self.assertRaisesRegex(
                        source.SourceError, "^document_cache_body_invalid$"
                    ):
                        source.acquire(self.root, self.item, now=101, refresh=True)
                    fetch.assert_not_called()

    def test_receipt_validation_rejects_future_clock_and_invalid_shapes(self):
        for corruption in ("future", "extra", "duplicate", "digest", "bytes"):
            with self.subTest(corruption=corruption):
                self.root = self.root.parent / ("receipt-" + corruption)
                self.acquire(now=100)
                path = self.record_path()
                record = json.loads(path.read_text())
                if corruption == "future":
                    record["acquired_at"] = 102
                elif corruption == "extra":
                    record["private"] = "must not accept extra fields"
                elif corruption == "digest":
                    record["sha256"] = "../../outside"
                elif corruption == "bytes":
                    record["bytes"] = True
                if corruption == "duplicate":
                    path.write_bytes(b'{"schema_version":1,"schema_version":1}\n')
                else:
                    path.write_bytes(canonical(record))
                with patch.object(source, "fetch") as fetch:
                    with self.assertRaises(source.SourceError):
                        source.acquire(self.root, self.item, now=101)
                    fetch.assert_not_called()

    def test_existing_unsafe_cache_and_lock_are_rejected(self):
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        with self.assertRaisesRegex(source.SourceError, "^document_cache_io_failed$"):
            source.acquire(self.root, self.item, now=100)
        self.root.chmod(0o700)
        (self.root / ".lock").symlink_to("/etc/hosts")
        with self.assertRaisesRegex(source.SourceError, "^document_cache_io_failed$"):
            source.acquire(self.root, self.item, now=100)

    def test_root_lock_prevents_concurrent_acquisition(self):
        with cache.locked(self.root):
            with patch.object(source, "fetch") as fetch:
                with self.assertRaisesRegex(
                    source.SourceError, "^document_cache_busy$"
                ):
                    source.acquire(self.root, self.item, now=100)
                fetch.assert_not_called()

    def test_clock_and_options_require_exact_types(self):
        for options in ({"now": True}, {"now": -1}, {"now": 1.2}, {"refresh": 1}):
            with self.subTest(options=options), self.assertRaises(source.SourceError):
                source.acquire(self.root, self.item, **options)
