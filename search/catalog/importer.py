"""Atomic bounded import/reconciliation of one sealed catalog."""

from __future__ import annotations

from dataclasses import dataclass
import math
import secrets
from typing import (
    Any,
    Callable,
    Dict,
    Mapping,
    Optional,
    Tuple,
)

from .admin import AdminAccess, AdminSession
from .bundle import CatalogBundle, MAX_BULK_DOCUMENTS
from .codec import _exact_int
from .errors import (
    CatalogError,
    ConfigurationError,
    ImportFailed,
    InventoryConflict,
    InventoryUnstable,
    _diagnosed,
    _failure_cause,
)
from .inventory import (
    InventoryAdapter,
    InventorySnapshot,
    SearchListInventory,
    validate_inventory,
)
from .policy import ID_RE, NONCE_RE
from .transport import Deadline

MAX_TOKEN_OPERATION_DEADLINE_SECONDS = 600.0


def _token_operation_deadline(seconds: float, clock: Callable[[], float]) -> Deadline:
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(float(seconds))
        or not 0 < float(seconds) <= MAX_TOKEN_OPERATION_DEADLINE_SECONDS
    ):
        raise ConfigurationError("operation deadline is outside its token-safe cap")
    return Deadline.after(float(seconds), clock)


@dataclass(frozen=True)
class ImportReport:
    status: str
    bundle_id: str
    manifest_sha256: str
    document_count: int
    batch_count: int
    elapsed_seconds: float
    inventory_verified: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "bundle_id": self.bundle_id,
            "manifest_sha256": self.manifest_sha256,
            "document_count": self.document_count,
            "batch_count": self.batch_count,
            "elapsed_seconds": self.elapsed_seconds,
            "inventory_verified": self.inventory_verified,
        }


class CatalogImporter:
    """Import or reconcile one exact sealed catalog."""

    def __init__(
        self,
        bundle: CatalogBundle,
        access: AdminAccess,
        *,
        inventory: Optional[InventoryAdapter] = None,
    ) -> None:
        self.bundle = bundle
        self.access = access
        self.inventory = inventory or SearchListInventory()

    def _wait_for_import_visibility(
        self,
        session: AdminSession,
        token: str,
        deadline: Deadline,
        expected_entries: Tuple[Tuple[str, str, str], ...],
    ) -> InventorySnapshot:
        """Wait for OpenSearch refresh without accepting foreign documents.

        Fess's Documents bulk endpoint does not promise that a successful write
        is immediately visible to SearchList.  During that short refresh window
        only an empty or proper subset of this exact sealed bundle is safe.  A
        foreign URL, a different bundle identity, a different document digest,
        or an over-count is evidence of pollution and fails immediately instead
        of being retried away.
        """

        expected = frozenset(expected_entries)
        delay = 0.25
        while True:
            try:
                snapshot = validate_inventory(
                    self.inventory.snapshot(session, token, deadline)
                )
            except InventoryUnstable:
                self.access.sleep(deadline.remaining(delay))
                delay = min(1.0, delay * 2.0)
                continue
            actual = frozenset(snapshot.entries)
            if snapshot.entries == expected_entries:
                return snapshot
            if snapshot.count > len(expected_entries) or not actual.issubset(expected):
                raise _diagnosed(
                    ImportFailed(
                        "post-import inventory contains data outside the sealed bundle"
                    ),
                    stage="post_import_inventory",
                    cause="inventory_mismatch",
                )
            # Deadline.remaining checks the shared operation budget before the
            # bounded backoff.  It deliberately raises rather than starting an
            # unbounded secondary refresh deadline.
            self.access.sleep(deadline.remaining(delay))
            delay = min(1.0, delay * 2.0)

    def import_bundle(
        self,
        admin_username: str,
        admin_password: str,
        *,
        batch_size: int = 100,
        nonce: Optional[str] = None,
        deadline_seconds: float = 300.0,
    ) -> ImportReport:
        if not _exact_int(batch_size, minimum=1, maximum=MAX_BULK_DOCUMENTS):
            raise ConfigurationError("bulk batch size is outside its cap")
        nonce = nonce or secrets.token_hex(6)
        if NONCE_RE.fullmatch(nonce) is None:
            raise ConfigurationError(
                "import token nonce must be 12 lowercase hex characters"
            )
        deadline = _token_operation_deadline(deadline_seconds, self.access.clock)
        started = self.access.clock()
        batch_count = 0
        with self.access.token(
            admin_username, admin_password, nonce, deadline
        ) as values:
            session, token = values
            before = validate_inventory(
                self.inventory.snapshot(session, token, deadline)
            )
            expected_entries = self.bundle.expected_inventory_entries
            if before.count:
                if before.entries == expected_entries:
                    return ImportReport(
                        status="already-active-no-op",
                        bundle_id=self.bundle.bundle_id,
                        manifest_sha256=self.bundle.manifest_sha256,
                        document_count=before.count,
                        batch_count=0,
                        elapsed_seconds=self.access.clock() - started,
                    )
                raise InventoryConflict(
                    "Fess inventory is not a complete copy of this bundle"
                )
            for offset in range(0, len(self.bundle.documents), batch_size):
                batch = self.bundle.documents[offset : offset + batch_size]
                try:
                    response = session.documents_bulk(token, batch, deadline)
                except ImportFailed:
                    raise
                except CatalogError as error:
                    if batch_count == 0:
                        raise
                    raise _diagnosed(
                        ImportFailed(
                            "document bulk request failed; catalog may be partially imported"
                        ),
                        stage="bulk_after_prior_batch",
                        cause=_failure_cause(error),
                    ) from None
                items = response.get("items")
                if (
                    response.get("status") != 0
                    or not isinstance(items, list)
                    or len(items) != len(batch)
                ):
                    raise _diagnosed(
                        ImportFailed(
                            "document bulk response was partial; catalog may be polluted"
                        ),
                        stage="bulk_response",
                        cause=(
                            "api_rejected"
                            if response.get("status") != 0
                            else "bulk_response_partial"
                        ),
                    )
                for item in items:
                    if (
                        not isinstance(item, Mapping)
                        or item.get("result") not in ("CREATED", "OK")
                        or not isinstance(item.get("id"), str)
                        or ID_RE.fullmatch(item["id"]) is None
                    ):
                        raise _diagnosed(
                            ImportFailed(
                                "document bulk response was partial; catalog may be polluted"
                            ),
                            stage="bulk_response",
                            cause="bulk_response_partial",
                        )
                batch_count += 1
            try:
                after = self._wait_for_import_visibility(
                    session, token, deadline, expected_entries
                )
            except ImportFailed:
                raise
            except CatalogError as error:
                raise _diagnosed(
                    ImportFailed("post-import inventory could not be verified"),
                    stage="post_import_inventory",
                    cause=_failure_cause(error),
                ) from None
            return ImportReport(
                status="imported",
                bundle_id=self.bundle.bundle_id,
                manifest_sha256=self.bundle.manifest_sha256,
                document_count=after.count,
                batch_count=batch_count,
                elapsed_seconds=self.access.clock() - started,
            )

    def reconcile_bundle(
        self,
        admin_username: str,
        admin_password: str,
        *,
        nonce: Optional[str] = None,
        deadline_seconds: float = 180.0,
    ) -> ImportReport:
        """Prove an uncertain import without writing catalog documents.

        The wrapper exposes this only while one matching quarantine record is
        present.  A complete exact inventory resolves that evidence; a subset,
        foreign URL, or different bundle remains quarantined.
        """

        nonce = nonce or secrets.token_hex(6)
        if NONCE_RE.fullmatch(nonce) is None:
            raise ConfigurationError(
                "import token nonce must be 12 lowercase hex characters"
            )
        deadline = _token_operation_deadline(deadline_seconds, self.access.clock)
        started = self.access.clock()
        expected_entries = self.bundle.expected_inventory_entries
        with self.access.token(
            admin_username, admin_password, nonce, deadline
        ) as values:
            session, token = values
            try:
                snapshot = self._wait_for_import_visibility(
                    session, token, deadline, expected_entries
                )
            except CatalogError:
                raise InventoryConflict(
                    "quarantined catalog does not exactly match the sealed bundle"
                ) from None
            return ImportReport(
                status="reconciled-active",
                bundle_id=self.bundle.bundle_id,
                manifest_sha256=self.bundle.manifest_sha256,
                document_count=snapshot.count,
                batch_count=0,
                elapsed_seconds=self.access.clock() - started,
            )
