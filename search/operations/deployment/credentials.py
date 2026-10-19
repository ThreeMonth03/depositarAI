"""Prepare one product-owned API key, without starting or exposing services.

The caller retains the product lock and its operation receipt. A key survives
source upgrades, but not changes to the product configuration identity. Existing
keys are never rotated or adopted from an incomplete/unknown receipt. Key bytes
and their private verification digest are not included in the returned report.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets

from search.serialization import strict_json
from search.operations.config import ProductConfig
from search.operations.shared import bounded_json, private_io
from search.operations.shared.source_identity import require_clean_revision
from search.operations.state import StateStore, _marker, operation_receipts


STARTED = "api-key-started.json"
COMPLETED = "api-key-complete.json"
KEY = "api_key"
ANCHOR = "api-key-identity.json"
FILES = {STARTED, COMPLETED, KEY}
MAX_RECORD_BYTES = 4096
REVISION = re.compile(r"[0-9a-f]{40}\Z")


class CredentialError(ValueError):
    """Fixed operator errors, without key bytes, digests or environment values."""


def _digest(value):
    return hashlib.sha256(private_io.canonical(value)).hexdigest()


def _record(descriptor, name):
    raw = private_io.read_file(descriptor, name, mode=0o600, max_bytes=MAX_RECORD_BYTES)
    bounded_json.predecode(raw)
    return strict_json(raw)


def _same(actual, expected, code):
    if private_io.canonical(actual) != private_io.canonical(expected):
        raise CredentialError(code)


def _key_digest(descriptor):
    raw = private_io.read_file(descriptor, KEY, mode=0o600, max_bytes=65)
    if re.fullmatch(rb"[0-9a-f]{64}\n", raw) is None:
        raise CredentialError("product_api_key_invalid")
    return hashlib.sha256(raw).hexdigest()


def read_api_key_authority(config, store):
    """Verify an existing complete API key without creating or adopting state.

    The caller holds the product lock. The creation source may predate the
    current release; the key remains bound to the product configuration. Only
    a digest of the authenticated completion receipt leaves this function.
    """
    if (
        not isinstance(config, ProductConfig)
        or not isinstance(store, StateStore)
        or store.root != config.state_root
    ):
        raise CredentialError("product_credentials_state_invalid")
    descriptor = None
    try:
        store._check_directory()
        marker = _marker(config)
        _same(
            store.read("product.json"),
            marker,
            "product_credentials_configuration_changed",
        )
        started = store.read(ANCHOR)
        if (
            type(started) is not dict
            or type(started.get("creation_revision")) is not str
            or REVISION.fullmatch(started["creation_revision"]) is None
        ):
            raise CredentialError("product_credentials_receipt_changed")
        _same(
            started,
            {
                "schema_version": 1,
                "kind": "product-api-key",
                "configuration_sha256": marker["configuration_sha256"],
                "creation_revision": started["creation_revision"],
            },
            "product_credentials_receipt_changed",
        )
        descriptor = private_io.open_child_directory(store.fd, "secrets")
        root = config.state_root / "secrets"
        private_io.verify_directory(descriptor, root)
        if set(os.listdir(descriptor)) != FILES:
            raise CredentialError("product_credentials_recovery_required")
        _same(
            _record(descriptor, STARTED), started, "product_credentials_receipt_changed"
        )
        completed = {
            "schema_version": 1,
            "status": "verified",
            "started_sha256": _digest(started),
            "key_sha256": _key_digest(descriptor),
        }
        _same(
            _record(descriptor, COMPLETED),
            completed,
            "product_credentials_receipt_changed",
        )
        # Re-read both receipts and bytes before returning authority, including
        # the retained directory's binding to its configured pathname.
        _same(
            _record(descriptor, STARTED), started, "product_credentials_receipt_changed"
        )
        if _key_digest(descriptor) != completed["key_sha256"]:
            raise CredentialError("product_credentials_receipt_changed")
        _same(
            _record(descriptor, COMPLETED),
            completed,
            "product_credentials_receipt_changed",
        )
        if set(os.listdir(descriptor)) != FILES:
            raise CredentialError("product_credentials_inventory_changed")
        private_io.verify_directory(descriptor, root)
        store._check_directory()
        _same(store.read(ANCHOR), started, "product_credentials_receipt_changed")
        _same(
            store.read("product.json"),
            marker,
            "product_credentials_configuration_changed",
        )
        return {"credential_sha256": _digest(completed)}
    except CredentialError:
        raise
    except FileNotFoundError:
        raise CredentialError("product_credentials_complete_evidence_missing") from None
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise CredentialError("product_credentials_verification_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def prepare_api_key(config, store, source_revision, *, repo_root, runner=None):
    """Create once or reverify the private key under an already-held product lock.

    This does not provision Fess/OpenSearch identities, emit the API key, modify
    existing keys or bind any port. Fresh state may contain no secret files;
    existing state must have the exact complete inventory and matching digests.
    A crash between durable intent and completion requires explicit recovery.
    """
    if (
        not isinstance(config, ProductConfig)
        or not isinstance(store, StateStore)
        or store.root != config.state_root
        or type(source_revision) is not str
        or REVISION.fullmatch(source_revision) is None
    ):
        raise CredentialError("product_credentials_state_invalid")
    descriptor = None
    try:
        store._check_directory()
        _same(
            store.read("product.json"),
            _marker(config),
            "product_credentials_configuration_changed",
        )
        if require_clean_revision(repo_root, runner=runner) != source_revision:
            raise CredentialError("product_credentials_source_changed")
        configuration = _marker(config)["configuration_sha256"]
        try:
            started = store.read(ANCHOR)
        except FileNotFoundError:
            reused = False
            if any(
                row["operation"] == "prepare-secrets"
                and row["status"] in {"staged", "failed"}
                for row in operation_receipts(store)
            ):
                raise CredentialError("product_credentials_recovery_required")
            # Check an existing directory before writing intent; never adopt
            # orphan material. Keep its FD retained through any later writes.
            if "secrets" in os.listdir(store.fd):
                descriptor = private_io.open_child_directory(store.fd, "secrets")
                if os.listdir(descriptor):
                    raise CredentialError("product_credentials_recovery_required")
            started = {
                "schema_version": 1,
                "kind": "product-api-key",
                "configuration_sha256": configuration,
                "creation_revision": source_revision,
            }
            # This product-root anchor survives loss of the entire key folder.
            # Never mistake a missing/empty prior folder for first installation.
            store.write(ANCHOR, started)
        else:
            reused = True
            if (
                type(started) is not dict
                or type(started.get("creation_revision")) is not str
                or REVISION.fullmatch(started["creation_revision"]) is None
            ):
                raise CredentialError("product_credentials_receipt_changed")
            _same(
                started,
                {
                    "schema_version": 1,
                    "kind": "product-api-key",
                    "configuration_sha256": configuration,
                    "creation_revision": started["creation_revision"],
                },
                "product_credentials_receipt_changed",
            )
            if "secrets" not in os.listdir(store.fd):
                raise CredentialError("product_credentials_recovery_required")
        if descriptor is None:
            descriptor = private_io.open_child_directory(
                store.fd, "secrets", create="secrets" not in os.listdir(store.fd)
            )
        root = config.state_root / "secrets"
        names = set(os.listdir(descriptor))
        if reused:
            if names != FILES:
                raise CredentialError("product_credentials_recovery_required")
            _same(
                _record(descriptor, STARTED),
                started,
                "product_credentials_receipt_changed",
            )
        else:
            if names:
                raise CredentialError("product_credentials_recovery_required")
            private_io.verify_directory(descriptor, root)
            private_io.write_json(
                descriptor, STARTED, started, max_bytes=MAX_RECORD_BYTES
            )
            key = secrets.token_hex(32)
            if type(key) is not str or re.fullmatch(r"[0-9a-f]{64}", key) is None:
                raise CredentialError("product_api_key_generation_invalid")
            private_io.verify_directory(descriptor, root)
            private_io.write_file(
                descriptor, KEY, (key + "\n").encode("ascii"), max_bytes=65
            )
        completed = {
            "schema_version": 1,
            "status": "verified",
            "started_sha256": _digest(started),
            "key_sha256": _key_digest(descriptor),
        }
        if reused:
            _same(
                _record(descriptor, COMPLETED),
                completed,
                "product_credentials_receipt_changed",
            )
        else:
            private_io.verify_directory(descriptor, root)
            private_io.write_json(
                descriptor, COMPLETED, completed, max_bytes=MAX_RECORD_BYTES
            )
        # Re-read the authority and material, not just the in-memory write result.
        _same(
            _record(descriptor, STARTED),
            started,
            "product_credentials_receipt_changed",
        )
        if _key_digest(descriptor) != completed["key_sha256"]:
            raise CredentialError("product_credentials_receipt_changed")
        _same(
            _record(descriptor, COMPLETED),
            completed,
            "product_credentials_receipt_changed",
        )
        if set(os.listdir(descriptor)) != FILES:
            raise CredentialError("product_credentials_inventory_changed")
        private_io.verify_directory(descriptor, root)
        store._check_directory()
        _same(store.read(ANCHOR), started, "product_credentials_receipt_changed")
        _same(
            store.read("product.json"),
            _marker(config),
            "product_credentials_configuration_changed",
        )
        if require_clean_revision(repo_root, runner=runner) != source_revision:
            raise CredentialError("product_credentials_source_changed")
        return {
            "schema_version": 1,
            "status": "secrets-ready",
            "source_revision": source_revision,
            "api_key_reused": reused,
            "secret_material_verified": True,
            "services_started": False,
            "active_changed": False,
        }
    except CredentialError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise CredentialError("product_credentials_verification_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
