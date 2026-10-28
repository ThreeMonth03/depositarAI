"""Internal retrieval inputs and the result embedded in a conversation turn."""

from search.catalog.errors import UnsafeQuery
from search.catalog.query import normalize_query
from search.catalog.resources import (
    MAX_COLUMNS,
    MAX_EXCERPT_CHARS,
    MAX_RESOURCE_NAME_CHARS,
    MAX_RESOURCES,
    MAX_URL_CHARS,
    validate_resource_context,
)

from typing import Literal

from pydantic import Field, field_validator, model_validator


from search.planning.clock import normalize_context
from search.planning.predicates import validate_conditions
from .metadata import validate_temporal_coverage
from ..shared.schemas import StrictModel


class QueryContext(StrictModel):
    reference_time: str = Field(
        max_length=40, description="Offset-aware RFC3339 reference instant."
    )
    timezone: str = Field(max_length=64, description="IANA timezone, e.g. Asia/Taipei.")

    @model_validator(mode="after")
    def valid_clock(self):
        normalize_context(self.model_dump())
        return self


class ContextUsed(QueryContext):
    local_datetime: str
    reference_date: str

    @model_validator(mode="after")
    def valid_clock(self):
        expected = normalize_context(
            {"reference_time": self.reference_time, "timezone": self.timezone}
        )
        if (
            self.local_datetime != expected["local_datetime"]
            or self.reference_date != expected["reference_date"]
        ):
            raise ValueError("clock echo mismatch")
        return self


class TimeRange(StrictModel):
    start: str | None
    end: str | None
    field: Literal["data_coverage"] = "data_coverage"
    relation: Literal["overlaps"] = "overlaps"
    endpoints: Literal["inclusive"] = "inclusive"


class Conditions(StrictModel):
    time: TimeRange | None = None
    place: None = None
    formats: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def supported_conditions(self):
        value = {}
        if self.time is not None:
            value["time"] = self.time.model_dump(include={"start", "end"})
        if self.formats:
            value["formats"] = self.formats
        validate_conditions(value)
        return self


class RetrievalRequest(StrictModel):
    query: str = Field(min_length=1, max_length=512)
    top_k: int = Field(default=5, ge=1, le=20)
    conditions: Conditions = Field(default_factory=Conditions)

    @field_validator("query")
    @classmethod
    def valid_query(cls, value):
        try:
            normalize_query(value)
        except UnsafeQuery:
            raise ValueError("invalid search query") from None
        return value


class ConditionCounts(StrictModel):
    match: int = Field(default=0, ge=0, le=20)
    unknown: int = Field(default=0, ge=0, le=20)
    outside: int = Field(default=0, ge=0, le=20)
    not_checked: int = Field(default=0, ge=0, le=20)


class TemporalCoverage(StrictModel):
    start: str | None = Field(max_length=128)
    end: str | None = Field(max_length=128)
    status: Literal["complete", "partial", "absent", "invalid"]

    @model_validator(mode="after")
    def valid_coverage(self):
        validate_temporal_coverage(self.model_dump())
        return self


class ResourceContext(StrictModel):
    resource_id: str = Field(min_length=36, max_length=36)
    name: str = Field(max_length=MAX_RESOURCE_NAME_CHARS)
    format: Literal["CSV", "PDF"]
    url: str = Field(max_length=MAX_URL_CHARS)
    columns: list[str] = Field(max_length=MAX_COLUMNS)
    excerpt: str = Field(max_length=MAX_EXCERPT_CHARS)


class Card(StrictModel):
    rank: int
    dataset_id: str
    title: str
    url: str
    snippet: str
    organization_title: str
    resource_formats: list[str]
    metadata_modified: str
    temporal_coverage: TemporalCoverage
    resource_context: list[ResourceContext] = Field(max_length=MAX_RESOURCES)
    condition_status: Literal["match", "unknown", "not_checked"]

    @field_validator("resource_context", mode="before")
    @classmethod
    def valid_resource_context(cls, value):
        return validate_resource_context(value)

    @model_validator(mode="after")
    def resource_context_belongs_to_dataset(self):
        if any(
            entry.url != self.url + "/resource/" + entry.resource_id
            for entry in self.resource_context
        ):
            raise ValueError("resource context dataset mismatch")
        return self


class RetrievalResponse(StrictModel):
    api_version: Literal["1-draft"] = "1-draft"
    request_id: str
    revision: str
    catalog_manifest_sha256: str
    mode: Literal["ai"] = "ai"
    status: Literal["ok", "unavailable"]
    decision: Literal["search"] = "search"
    cards: list[Card]
    ranking: Literal["opensearch-haystack-rrf", "nvidia-rerank"]
    rerank_status: Literal["disabled", "not_run", "applied", "fallback"]
    candidate_count: int = Field(ge=0, le=20)
    warnings: list[str]
    timing_ms: dict[str, float]
    context_used: ContextUsed | None
    parsed_conditions: Conditions
    applied_conditions: Conditions | None
    filter_scope: Literal["none", "index_prefilter"]
    condition_counts: ConditionCounts
