"""Validate the one trace contract shared by producers and private tools."""

from datetime import datetime, timezone
import math
from typing import Annotated, Literal, get_args

from pydantic import Field, field_validator, model_validator

from search.api.conversation.contract import SearchState
from search.api.retrieval.schemas import (
    ConditionCounts,
    Conditions,
    QueryContext,
    TemporalCoverage,
)
from search.api.shared.schemas import StrictModel


TRACE_VERSION = 1
NOTICE_VERSION = "search-trace-v1"
CaptureStatus = Literal[
    "disabled",
    "ready",
    "queued",
    "captured",
    "oversize",
    "queue_full",
    "session_limit",
    "total_limit",
    "unavailable",
    "revoked",
    "incomplete",
    "shutdown",
    "projection_invalid",
]
DeletionStatus = Literal["none", "pending", "complete", "unavailable"]
CAPTURE_STATUSES = frozenset(get_args(CaptureStatus))
DELETION_STATUSES = frozenset(get_args(DeletionStatus))
TraceGap = Literal["upstream_unavailable", "result_unavailable", "cancelled"]
UnavailableField = Literal[
    "branch_candidates_and_scores",
    "rrf_candidate_order_and_scores",
    "reranker_scores_and_complete_order",
    "model_identity",
    "runtime_config_identity",
    "catalog_identity",
    "backend_identity",
    "bff_identity",
    "backend_timing",
    "legacy_conditions",
]
UNAVAILABLE_TRACE = [
    "branch_candidates_and_scores",
    "rrf_candidate_order_and_scores",
    "reranker_scores_and_complete_order",
    "model_identity",
    "runtime_config_identity",
]


class CaptureChoice(StrictModel):
    capture: bool = False
    notice_version: Literal["search-trace-v1"] | None = None

    @model_validator(mode="after")
    def notice_required(self):
        if self.capture and self.notice_version != NOTICE_VERSION:
            raise ValueError("trace_notice_required")
        return self


class WithdrawChoice(StrictModel):
    capture: bool

    @field_validator("capture")
    @staticmethod
    def only_withdraw(value):
        if value:
            raise ValueError("trace_withdraw_only")
        return value


def utc_timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(
        parsed
    ):
        raise ValueError("trace_timestamp_invalid")
    return value


class Consent(StrictModel):
    notice_version: Literal["search-trace-v1"]
    consented_at: str = Field(min_length=20, max_length=40)

    @field_validator("consented_at")
    @staticmethod
    def valid_stamp(value):
        return utc_timestamp(value)


class TraceInput(StrictModel):
    query: str = Field(min_length=1, max_length=512)
    top_k: int = Field(ge=1, le=20)


class LegacyExecution(StrictModel):
    kind: Literal["legacy_conditions"] = "legacy_conditions"
    parsed_conditions: Conditions | None = None
    applied_conditions: Conditions | None = None


class SQLExecution(StrictModel):
    """Reserve the SQL union for handmade fixtures."""

    kind: Literal["validated_sql"]
    sql: str = Field(min_length=1, max_length=8192)
    parameters: dict[str, str | int | float | bool | None] = Field(
        max_length=32
    )
    fixture_only: Literal[True]

    @field_validator("fixture_only", mode="before")
    @staticmethod
    def explicit_fixture(value):
        if value is not True:
            raise ValueError("trace_fixture_required")
        return value


class TraceCard(StrictModel):
    rank: int = Field(ge=1, le=20)
    dataset_id: str = Field(min_length=1, max_length=128)
    title: str = Field(max_length=512)
    resource_formats: list[str] = Field(max_length=256)
    temporal_coverage: TemporalCoverage
    condition_status: Literal["match", "unknown", "not_checked"]


class TraceSearch(StrictModel):
    request_id: str = Field(max_length=128)
    revision: str = Field(max_length=128)
    catalog_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ok", "unavailable"]
    cards: list[TraceCard] = Field(max_length=20)
    ranking: Literal["opensearch-haystack-rrf", "nvidia-rerank"]
    rerank_status: Literal["disabled", "not_run", "applied", "fallback"]
    candidate_count: int = Field(ge=0, le=20)
    condition_counts: ConditionCounts
    warnings: list[str] = Field(max_length=32)
    filter_scope: Literal["none", "index_prefilter"]
    parsed_conditions: Conditions
    applied_conditions: Conditions | None


class TraceOutput(StrictModel):
    version: int = Field(ge=0, le=20)
    status: Literal["ok", "rejected", "clarification_required", "unavailable"]
    reason: str = Field(max_length=128)
    message: str = Field(max_length=4096)
    state: SearchState
    notices: list[str] = Field(max_length=4)
    search: TraceSearch | None


class TraceIdentities(StrictModel):
    bff_revision: str | None = Field(pattern=r"^[0-9a-f]{40}$")
    backend_revision: str | None = Field(pattern=r"^[0-9a-f]{40}$")
    catalog_manifest_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    capture_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_identity: None = None


class CaptureCompleteness(StrictModel):
    complete: bool
    gaps: list[TraceGap] = Field(max_length=32)

    @model_validator(mode="after")
    def coherent(self):
        if self.complete and self.gaps:
            raise ValueError("trace_completeness_invalid")
        return self


class TraceEvent(StrictModel):
    trace_version: Literal[1]
    test_session_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    epoch: int = Field(ge=0)
    turn: int = Field(ge=1, le=100)
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    upstream_request_id: str | None = Field(max_length=128)
    timestamp: str = Field(min_length=20, max_length=40)
    consent: Consent
    context: QueryContext
    input: TraceInput
    state_before: SearchState
    pending_before: str | None = Field(max_length=512)
    execution: Annotated[
        LegacyExecution | SQLExecution, Field(discriminator="kind")
    ]
    state_after: SearchState
    pending_after: str | None = Field(max_length=512)
    output: TraceOutput
    identities: TraceIdentities
    timing_ms: dict[Literal["bff_total", "backend_total"], float]
    capture: CaptureCompleteness
    unavailable_trace: list[UnavailableField] = Field(max_length=32)

    @field_validator("trace_version", mode="before")
    @staticmethod
    def integer_version(value):
        if type(value) is not int:
            raise ValueError("trace_version_invalid")
        return value

    @field_validator("timestamp")
    @staticmethod
    def valid_stamp(value):
        return utc_timestamp(value)

    @field_validator("timing_ms")
    @staticmethod
    def finite_timings(value):
        if "bff_total" not in value or any(
            not math.isfinite(item) or item < 0 for item in value.values()
        ):
            raise ValueError("trace_timing_invalid")
        return value


def validate_event(value):
    """Return a detached whitelist, rejecting additional fields."""
    return TraceEvent.model_validate(value).model_dump()
