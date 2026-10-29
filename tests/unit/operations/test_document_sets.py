"""Frozen resource sets authenticate references without scanning private caches."""

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.operations.documents import sets
from search.operations.shared import private_io


class DocumentSetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fd = private_io.open_directory(self.root)
        self.addCleanup(os.close, self.fd)
        self.rows = [
            {
                "dataset_id": "00000000-0000-4000-8000-%012x" % index,
                "resource_id": "10000000-0000-4000-8000-%012x" % index,
                "profile_sha256": "%064x" % index,
            }
            for index in (1, 2)
        ]

    def publish(self, rows=None, failures=None):
        result = sets.publish(
            self.fd,
            "a" * 64,
            "b" * 64,
            self.rows if rows is None else rows,
            failures or [],
        )
        return self.root / result["profile_set_file"]

    def test_sorted_content_addressed_publication_is_idempotent_and_copied(self):
        path = self.publish(list(reversed(self.rows)))
        record, digest = sets.read(path)
        self.assertEqual(self.rows, record["profiles"])
        self.assertEqual(self.publish(), path)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertEqual("profile-set-" + digest + ".json", path.name)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        validated = sets.validate(record)
        validated["profiles"][0]["dataset_id"] = "changed"
        self.assertEqual(self.rows, record["profiles"])

    def test_failure_only_set_is_explicit_and_duplicate_ids_are_rejected(self):
        failure = {key: self.rows[0][key] for key in ("dataset_id", "resource_id")}
        failure["code"] = "document_source_failed"
        record, _ = sets.read(self.publish([], [failure]))
        self.assertEqual([failure], record["failures"])
        with self.assertRaises(sets.ProfileSetError):
            self.publish(self.rows, [failure])
        changed = deepcopy(self.rows[0])
        changed["profile_sha256"] = "c" * 64
        with self.assertRaises(sets.ProfileSetError):
            self.publish([self.rows[0], changed])

    def test_closed_schema_and_canonical_order_are_required(self):
        record, _ = sets.read(self.publish())
        variants = []
        for key, value in (
            ("schema_version", True),
            ("source_sha256", "B" * 64),
            ("path", "../profile.json"),
        ):
            changed = deepcopy(record)
            changed[key] = value
            variants.append(changed)
        changed = deepcopy(record)
        changed["profiles"].reverse()
        variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed):
                with self.assertRaises(sets.ProfileSetError):
                    sets.validate(changed)

    def test_private_file_canonical_bytes_hash_and_links_are_checked(self):
        path = self.publish()
        original = path.read_bytes()
        for raw in (
            original + b" ",
            original.replace(b'"schema_version":1', b'"schema_version":true'),
        ):
            path.write_bytes(raw)
            with self.assertRaises(sets.ProfileSetError):
                sets.read(path)
        path.write_bytes(original)
        path.chmod(0o644)
        with self.assertRaises(sets.ProfileSetError):
            sets.read(path)
        path.chmod(0o600)
        link = self.root / ("profile-set-" + "f" * 64 + ".json")
        link.symlink_to(path)
        with self.assertRaises(sets.ProfileSetError):
            sets.read(link)
        link.unlink()
        os.link(path, link)
        with self.assertRaises(sets.ProfileSetError):
            sets.read(path)

    def test_outside_git_and_private_parent_are_required(self):
        path = self.publish()
        (self.root / ".git").mkdir()
        with self.assertRaises(sets.ProfileSetError):
            sets.read(path)
        (self.root / ".git").rmdir()
        self.root.chmod(0o755)
        with self.assertRaises(sets.ProfileSetError):
            sets.read(path)
        self.root.chmod(0o700)

    def test_count_and_byte_limits_fail_before_publication(self):
        with patch.object(sets, "MAX_SELECTED", 1):
            with self.assertRaisesRegex(sets.ProfileSetError, "limit_exceeded"):
                self.publish()
        with patch.object(sets, "MAX_SET_BYTES", 64):
            with self.assertRaisesRegex(sets.ProfileSetError, "limit_exceeded"):
                self.publish()
        self.assertEqual([], list(self.root.iterdir()))

    def test_existing_content_addressed_file_cannot_be_overwritten(self):
        path = self.publish()
        path.write_bytes(b"{}\n")
        with self.assertRaisesRegex(sets.ProfileSetError, "conflict"):
            self.publish()
        self.assertEqual(b"{}\n", path.read_bytes())
