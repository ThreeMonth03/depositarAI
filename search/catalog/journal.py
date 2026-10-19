"""Bounded durable-event protocol shared by token and vector operations."""

from __future__ import annotations

from typing import (
    Any,
    Mapping,
    Optional,
    Protocol,
)

from .errors import VectorBuildError


class VectorJournal(Protocol):
    identity: Mapping[str, Any]

    def append(self, event: str, payload: Mapping[str, Any]) -> None: ...


def _vector_event(
    journal: Optional[VectorJournal], event: str, payload: Mapping[str, Any]
) -> None:
    if journal is not None:
        try:
            journal.append(event, payload)
        except Exception:
            # Local file errors can contain paths or supplied content. Neither
            # belongs in the bounded catalog error exposed by the CLI.
            raise VectorBuildError("vector evidence could not be committed") from None
