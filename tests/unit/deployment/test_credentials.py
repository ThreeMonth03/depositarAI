"""Real private key files and receipts; no Docker or network mutations."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search.secrets import read_secret
from search.operations.config import GPUConfig, ProductConfig
from search.operations.deployment import credentials
from search.operations.shared import private_io
from search.operations.state import locked_state


REVISION = "a" * 40


class ProductCredentialsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="depositar-credentials-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = ProductConfig(
            2,
            self.root / "state",
            self.root / "api",
            self.root / "embedding",
            self.root / "reranker",
            GPUConfig("GPU-a", ("GPU-b", "GPU-c"), "GPU-d"),
        )
        self.key_root = self.config.state_root / "secrets"
        self.key = self.key_root / credentials.KEY

    def prepare(self, revision=REVISION):
        with (
            locked_state(self.config, create=True) as store,
            patch.object(credentials, "require_clean_revision", return_value=revision),
        ):
            return credentials.prepare_api_key(
                self.config, store, revision, repo_root=self.root
            )

    def overwrite(self, name, value):
        descriptor = private_io.open_directory(self.key_root)
        try:
            private_io.write_json(
                descriptor,
                name,
                value,
                replace=True,
                max_bytes=credentials.MAX_RECORD_BYTES,
            )
        finally:
            os.close(descriptor)

    def test_create_and_repeat_verify_same_api_compatible_key(self):
        first = self.prepare()
        original = self.key.read_bytes()
        info = self.key.stat()
        self.assertEqual(65, len(original))
        self.assertEqual(original.decode().strip(), read_secret(self.key))
        self.assertEqual(0o600, info.st_mode & 0o777)
        self.assertEqual(0o700, self.key_root.stat().st_mode & 0o777)
        self.assertEqual(1, info.st_nlink)
        second = self.prepare()
        self.assertFalse(first["api_key_reused"])
        self.assertTrue(second["api_key_reused"])
        self.assertEqual(info.st_ino, self.key.stat().st_ino)
        self.assertEqual(original, self.key.read_bytes())
        self.assertEqual(credentials.FILES, set(os.listdir(self.key_root)))
        for report in (first, second):
            self.assertEqual("secrets-ready", report["status"])
            self.assertTrue(report["secret_material_verified"])
            self.assertFalse(report["active_changed"])
            self.assertFalse(report["services_started"])
            text = json.dumps(report)
            self.assertNotIn(original.decode().strip(), text)
            self.assertNotIn(hashlib.sha256(original).hexdigest(), text)

    def test_key_survives_source_upgrade_and_cache_ttl_change(self):
        self.prepare()
        before = self.key.read_bytes()
        self.config = replace(self.config, cache_ttl_seconds=60)
        self.assertTrue(self.prepare("b" * 40)["api_key_reused"])
        self.assertEqual(before, self.key.read_bytes())
        started = json.loads((self.key_root / credentials.STARTED).read_bytes())
        self.assertEqual(REVISION, started["creation_revision"])

    def test_incomplete_state_is_not_adopted_or_rotated(self):
        self.prepare()
        original = self.key.read_bytes()
        (self.key_root / credentials.COMPLETED).unlink()
        with self.assertRaisesRegex(credentials.CredentialError, "recovery_required"):
            self.prepare()
        self.assertEqual(original, self.key.read_bytes())
        self.assertFalse((self.key_root / credentials.COMPLETED).exists())

    def test_missing_entire_key_directory_never_generates_a_new_key(self):
        self.prepare()
        saved = self.root / "saved-secrets"
        self.key_root.rename(saved)
        with (
            patch.object(credentials.secrets, "token_hex") as generate,
            self.assertRaisesRegex(credentials.CredentialError, "recovery_required"),
        ):
            self.prepare()
        generate.assert_not_called()
        self.assertFalse(self.key_root.exists())
        self.assertTrue((saved / credentials.KEY).exists())

    def test_existing_empty_key_directory_never_generates_a_new_key(self):
        self.prepare()
        self.key_root.rename(self.root / "saved-secrets")
        self.key_root.mkdir(mode=0o700)
        with (
            patch.object(credentials.secrets, "token_hex") as generate,
            self.assertRaisesRegex(credentials.CredentialError, "recovery_required"),
        ):
            self.prepare()
        generate.assert_not_called()
        self.assertEqual([], os.listdir(self.key_root))

    def test_deleted_anchor_cannot_adopt_existing_key(self):
        self.prepare()
        original = self.key.read_bytes()
        (self.config.state_root / credentials.ANCHOR).unlink()
        with self.assertRaisesRegex(credentials.CredentialError, "recovery_required"):
            self.prepare()
        self.assertEqual(original, self.key.read_bytes())

    def test_unknown_file_is_preserved_and_blocks_reuse(self):
        self.prepare()
        extra = self.key_root / "unexpected-secret"
        extra.touch(mode=0o600)
        with self.assertRaisesRegex(credentials.CredentialError, "recovery_required"):
            self.prepare()
        self.assertTrue(extra.exists())

    def test_original_configuration_binding_cannot_be_changed(self):
        self.prepare()
        started = json.loads((self.key_root / credentials.STARTED).read_bytes())
        started["configuration_sha256"] = "0" * 64
        self.overwrite(credentials.STARTED, started)
        with self.assertRaisesRegex(credentials.CredentialError, "receipt_changed"):
            self.prepare()

    def test_boolean_schema_is_not_integer_identity(self):
        self.prepare()
        started = json.loads((self.key_root / credentials.STARTED).read_bytes())
        started["schema_version"] = True
        self.overwrite(credentials.STARTED, started)
        with self.assertRaisesRegex(credentials.CredentialError, "receipt_changed"):
            self.prepare()

    def test_modified_key_does_not_update_its_receipt(self):
        self.prepare()
        original = (self.key_root / credentials.COMPLETED).read_bytes()
        self.key.write_bytes(b"f" * 64 + b"\n")
        with self.assertRaisesRegex(credentials.CredentialError, "receipt_changed"):
            self.prepare()
        self.assertEqual(original, (self.key_root / credentials.COMPLETED).read_bytes())

    def test_duplicate_json_fields_are_rejected(self):
        self.prepare()
        (self.key_root / credentials.STARTED).write_bytes(
            b'{"schema_version":1,"schema_version":1}\n'
        )
        with self.assertRaises(credentials.CredentialError):
            self.prepare()

    def test_symlink_key_is_rejected_without_touching_target(self):
        self.prepare()
        target = self.root / "external-key"
        original = self.key.read_bytes()
        target.write_bytes(original)
        target.chmod(0o600)
        self.key.unlink()
        self.key.symlink_to(target)
        with self.assertRaises(credentials.CredentialError):
            self.prepare()
        self.assertEqual(original, target.read_bytes())
        self.assertTrue(self.key.is_symlink())

    def test_hardlink_key_is_rejected(self):
        self.prepare()
        os.link(self.key, self.root / "other-key")
        with self.assertRaises(credentials.CredentialError):
            self.prepare()

    def test_group_readable_key_is_rejected(self):
        self.prepare()
        self.key.chmod(0o640)
        with self.assertRaises(credentials.CredentialError):
            self.prepare()

    def test_fifo_key_fails_without_blocking(self):
        self.prepare()
        self.key.unlink()
        os.mkfifo(self.key, 0o600)
        with self.assertRaises(credentials.CredentialError):
            self.prepare()

    def test_invalid_randomness_leaves_intent_for_explicit_recovery(self):
        with patch.object(credentials.secrets, "token_hex", return_value="bad"):
            with self.assertRaisesRegex(
                credentials.CredentialError, "generation_invalid"
            ):
                self.prepare()
        self.assertEqual({credentials.STARTED}, set(os.listdir(self.key_root)))
        with self.assertRaisesRegex(credentials.CredentialError, "recovery_required"):
            self.prepare()

    def test_source_drift_before_creation_leaves_no_secret_root(self):
        with locked_state(self.config, create=True) as store:
            with patch.object(
                credentials, "require_clean_revision", return_value="b" * 40
            ):
                with self.assertRaisesRegex(
                    credentials.CredentialError, "source_changed"
                ):
                    credentials.prepare_api_key(
                        self.config, store, REVISION, repo_root=self.root
                    )
        self.assertFalse(self.key_root.exists())

    def test_product_directory_replacement_is_rejected(self):
        with locked_state(self.config, create=True) as store:
            saved = self.root / "saved-state"
            self.config.state_root.rename(saved)
            self.config.state_root.mkdir(mode=0o700)
            with self.assertRaises(credentials.CredentialError):
                credentials.prepare_api_key(
                    self.config, store, REVISION, repo_root=self.root
                )
        self.assertFalse((self.config.state_root / "secrets").exists())

    def read_authority(self):
        with locked_state(self.config) as store:
            return credentials.read_api_key_authority(self.config, store)

    def file_snapshot(self):
        return {
            str(path.relative_to(self.config.state_root)): (
                path.stat().st_ino,
                path.read_bytes(),
            )
            for path in self.config.state_root.rglob("*")
            if path.is_file()
        }

    def test_readonly_authority_verifies_complete_key_without_writes(self):
        self.prepare()
        before = self.file_snapshot()
        with (
            patch.object(private_io, "write_file", side_effect=AssertionError("write")),
            patch.object(private_io, "write_json", side_effect=AssertionError("write")),
            patch.object(
                credentials.secrets, "token_hex", side_effect=AssertionError("generate")
            ),
        ):
            report = self.read_authority()
        completed = json.loads((self.key_root / credentials.COMPLETED).read_bytes())
        self.assertEqual({"credential_sha256": credentials._digest(completed)}, report)
        self.assertEqual(before, self.file_snapshot())
        encoded = json.dumps(report)
        self.assertNotIn(self.key.read_text().strip(), encoded)
        self.assertNotIn(completed["key_sha256"], encoded)

    def test_readonly_missing_material_never_creates_or_adopts(self):
        with locked_state(self.config, create=True) as store:
            before = self.file_snapshot()
            with self.assertRaisesRegex(
                credentials.CredentialError, "complete_evidence_missing"
            ):
                credentials.read_api_key_authority(self.config, store)
            self.assertEqual(before, self.file_snapshot())
        self.assertFalse(self.key_root.exists())
        self.prepare()
        (self.config.state_root / credentials.ANCHOR).unlink()
        before = self.file_snapshot()
        with self.assertRaisesRegex(
            credentials.CredentialError, "complete_evidence_missing"
        ):
            self.read_authority()
        self.assertEqual(before, self.file_snapshot())

    def test_readonly_partial_or_unknown_secret_inventory_blocks(self):
        self.prepare()
        complete = self.key_root / credentials.COMPLETED
        original = complete.read_bytes()
        complete.unlink()
        with self.assertRaisesRegex(credentials.CredentialError, "recovery_required"):
            self.read_authority()
        complete.write_bytes(original)
        complete.chmod(0o600)
        (self.key_root / "unknown").touch(mode=0o600)
        with self.assertRaisesRegex(credentials.CredentialError, "recovery_required"):
            self.read_authority()

    def test_readonly_changed_key_or_closed_receipt_is_rejected(self):
        self.prepare()
        original = self.key.read_bytes()
        self.key.write_bytes(b"f" * 64 + b"\n")
        with self.assertRaisesRegex(credentials.CredentialError, "receipt_changed"):
            self.read_authority()
        self.key.write_bytes(original)
        complete = json.loads((self.key_root / credentials.COMPLETED).read_bytes())
        for field, value in (
            ("schema_version", True),
            ("key_sha256", "0" * 64),
            ("started_sha256", "0" * 64),
            ("extra", True),
        ):
            self.overwrite(credentials.COMPLETED, {**complete, field: value})
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(credentials.CredentialError, "receipt_changed"),
            ):
                self.read_authority()

    def test_readonly_anchor_started_and_configuration_must_agree(self):
        self.prepare()
        started = json.loads((self.key_root / credentials.STARTED).read_bytes())
        for field, value in (
            ("schema_version", True),
            ("creation_revision", "bad"),
            ("configuration_sha256", "0" * 64),
        ):
            self.overwrite(credentials.STARTED, {**started, field: value})
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(credentials.CredentialError, "receipt_changed"),
            ):
                self.read_authority()
        self.overwrite(credentials.STARTED, started)
        with locked_state(self.config) as store:
            store.write(
                credentials.ANCHOR,
                {**started, "creation_revision": "bad"},
                replace=True,
            )
            with self.assertRaisesRegex(credentials.CredentialError, "receipt_changed"):
                credentials.read_api_key_authority(self.config, store)
        with locked_state(self.config) as store:
            store.write("product.json", {}, replace=True)
            with self.assertRaisesRegex(
                credentials.CredentialError, "configuration_changed"
            ):
                credentials.read_api_key_authority(self.config, store)

    def test_readonly_authority_rejects_linked_key_and_replaced_secret_directory(self):
        self.prepare()
        target = self.root / "external-key"
        original = self.key.read_bytes()
        target.write_bytes(original)
        target.chmod(0o600)
        self.key.unlink()
        self.key.symlink_to(target)
        with self.assertRaises(credentials.CredentialError):
            self.read_authority()
        self.assertEqual(original, target.read_bytes())
        self.key.unlink()
        self.key.write_bytes(original)
        self.key.chmod(0o600)
        original_record = credentials._record
        calls = []

        def replace_directory(fd, name):
            result = original_record(fd, name)
            if not calls:
                self.key_root.rename(self.root / "saved-secrets")
                self.key_root.mkdir(mode=0o700)
            calls.append(name)
            return result

        with patch.object(credentials, "_record", side_effect=replace_directory):
            with self.assertRaises(credentials.CredentialError):
                self.read_authority()
        self.assertEqual([], list(self.key_root.iterdir()))

    def test_readonly_authority_rejects_product_directory_replacement(self):
        self.prepare()
        with locked_state(self.config) as store:
            self.config.state_root.rename(self.root / "saved-state")
            self.config.state_root.mkdir(mode=0o700)
            with self.assertRaises(credentials.CredentialError):
                credentials.read_api_key_authority(self.config, store)
        self.assertEqual([], list(self.config.state_root.iterdir()))

    def test_changed_key_and_completion_during_verification_cannot_publish_stale_digest(
        self,
    ):
        self.prepare()
        original_key = self.key.read_bytes()
        original_record = credentials._record
        original_complete = json.loads(
            (self.key_root / credentials.COMPLETED).read_bytes()
        )

        def changed_material(fd, name):
            result = original_record(fd, name)
            if name == credentials.COMPLETED and result == original_complete:
                replacement = b"f" * 64 + b"\n"
                self.key.write_bytes(replacement)
                self.overwrite(
                    credentials.COMPLETED,
                    {
                        **original_complete,
                        "key_sha256": hashlib.sha256(replacement).hexdigest(),
                    },
                )
            return result

        for reader in (self.read_authority, self.prepare):
            self.key.write_bytes(original_key)
            self.overwrite(credentials.COMPLETED, original_complete)
            with (
                self.subTest(reader=reader.__name__),
                patch.object(credentials, "_record", side_effect=changed_material),
                self.assertRaisesRegex(credentials.CredentialError, "receipt_changed"),
            ):
                reader()
