"""Exact catalog inventory identity and bounded SearchList pagination."""

from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
    Protocol,
    Tuple,
)

from .codec import _exact_int
from .errors import InventoryUnstable, ProtocolError
from .policy import MAX_DOCUMENTS, MAX_INVENTORY_PAGE, SHA256_RE
from .session import FessSession
from .transport import Deadline


@dataclass(frozen=True)
class InventorySnapshot:
    entries: Tuple[Tuple[str, str, str], ...]

    def __post_init__(self) -> None:
        previous = None
        seen = set()
        for entry in self.entries:
            if not isinstance(entry, tuple) or len(entry) != 3:
                raise ProtocolError("inventory adapter returned an invalid entry")
            url, bundle_id, document_sha256 = entry
            if (
                not isinstance(url, str)
                or not isinstance(bundle_id, str)
                or SHA256_RE.fullmatch(bundle_id) is None
                or not isinstance(document_sha256, str)
                or SHA256_RE.fullmatch(document_sha256) is None
            ):
                raise ProtocolError("inventory adapter returned invalid values")
            if url in seen or (previous is not None and url < previous):
                raise ProtocolError(
                    "inventory adapter entries must be unique and sorted"
                )
            seen.add(url)
            previous = url

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def urls(self) -> Tuple[str, ...]:
        return tuple(url for url, _bundle_id, _document_sha256 in self.entries)

    @property
    def bundle_ids(self) -> frozenset:
        return frozenset(
            bundle_id for _url, bundle_id, _document_sha256 in self.entries
        )


class InventoryAdapter(Protocol):
    def snapshot(
        self, session: FessSession, token: str, deadline: Deadline
    ) -> InventorySnapshot: ...


def _search_list_rows(
    session: FessSession, token: str, deadline: Deadline
) -> Tuple[Tuple[str, str, str, Optional[str]], ...]:
    """Read one stable, globally scoped SearchList generation."""

    start = 0
    expected_total: Optional[int] = None
    entries: Dict[str, Tuple[str, str, Optional[str]]] = {}
    while expected_total is None or start < expected_total:
        response = session.search_list(token, start, MAX_INVENTORY_PAGE, deadline)
        docs = response.get("docs")
        total = response.get("record_count")
        relation = response.get("record_count_relation")
        partial = response.get("partial")
        page_size = response.get("page_size")
        page_number = response.get("page_number")
        start_record = response.get("start_record_number")
        end_record = response.get("end_record_number")
        if (
            not isinstance(docs, list)
            or not _exact_int(total, minimum=0, maximum=MAX_DOCUMENTS)
            or relation != "EQUAL_TO"
            or partial is not False
            or page_size != MAX_INVENTORY_PAGE
            or page_number != (start // MAX_INVENTORY_PAGE) + 1
            or start_record != (start + 1 if docs else 0)
            or end_record != start + len(docs)
        ):
            raise ProtocolError("SearchList inventory response is incomplete")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise InventoryUnstable("SearchList inventory changed while paging")
        if len(docs) > MAX_INVENTORY_PAGE or (start < total and not docs):
            raise ProtocolError("SearchList inventory page is invalid")
        for document in docs:
            if not isinstance(document, Mapping):
                raise ProtocolError("SearchList returned a non-document")
            url = document.get("url")
            bundle_id = document.get("depositar_bundle_id")
            document_sha256 = document.get("depositar_document_sha256")
            status_value = document.get("content_chunk_status")
            if (
                not isinstance(url, str)
                or not isinstance(bundle_id, str)
                or SHA256_RE.fullmatch(bundle_id) is None
                or not isinstance(document_sha256, str)
                or SHA256_RE.fullmatch(document_sha256) is None
                or (status_value is not None and not isinstance(status_value, str))
            ):
                raise ProtocolError("SearchList document lacks catalog identity")
            # Inventory is deliberately global. Any non-catalog document blocks
            # activation instead of being hidden by the product query scope.
            if url in entries:
                raise InventoryUnstable("SearchList inventory changed while paging")
            entries[url] = (bundle_id, document_sha256, status_value)
        start += len(docs)
        if expected_total == 0:
            break
    if expected_total is None or len(entries) != expected_total:
        raise InventoryUnstable("SearchList inventory changed while paging")
    return tuple(
        (url, values[0], values[1], values[2])
        for url, values in sorted(entries.items())
    )


class SearchListInventory:
    """Strict Fess 15.8 SearchList adapter for the entire content index."""

    def snapshot(
        self, session: FessSession, token: str, deadline: Deadline
    ) -> InventorySnapshot:
        return InventorySnapshot(
            tuple(
                (url, bundle_id, document_sha256)
                for url, bundle_id, document_sha256, _status in _search_list_rows(
                    session, token, deadline
                )
            )
        )


def validate_inventory(snapshot: Any) -> InventorySnapshot:
    if not isinstance(snapshot, InventorySnapshot):
        raise ProtocolError("inventory adapter returned an unsupported shape")
    return snapshot
