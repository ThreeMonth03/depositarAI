"""Search-session state is typed data, never a transcript or a model summary."""

import copy
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from search.planning.contract import checked_query
from search.planning.predicates import validate_conditions
from ..retrieval.schemas import RetrievalResponse, QueryContext
from ..shared.schemas import StrictModel
from .time import RelativeTime


class Interval(StrictModel):
    start: str | None = Field(max_length=10)
    end: str | None = Field(max_length=10)

    @model_validator(mode="after")
    def dates(self):
        validate_conditions({"time": self.model_dump()})
        return self


class LunarTime(StrictModel):
    """Literal search text, never an executable date interval."""

    calendar: Literal["lunar"]
    text: str = Field(min_length=1, max_length=128)

    @field_validator("text")
    @classmethod
    def literal_text(cls, value):
        return checked_query(value)


class SearchState(StrictModel):
    topic: str = Field(default="", max_length=256)
    time: Interval | None = None
    formats: list[str] = Field(default_factory=list, max_length=4)
    unapplied_time: LunarTime | None = None

    @model_validator(mode="after")
    def exclusive_time(self):
        if self.time is not None and self.unapplied_time is not None:
            raise ValueError("cannot apply civil and unresolved lunar time together")
        return self

    @field_validator("topic")
    @classmethod
    def topic_text(cls, value):
        return checked_query(value) if value else value

    @field_validator("formats")
    @classmethod
    def allowed_formats(cls, value):
        if value:
            validate_conditions({"formats": value})
        return value


class TurnRequest(StrictModel):
    query: str = Field(min_length=1, max_length=512)
    state: SearchState = Field(default_factory=SearchState)
    pending: str | None = Field(default=None, max_length=512)
    context: QueryContext | None = None
    top_k: int = Field(default=5, ge=1, le=20)

    @field_validator("query", "pending")
    @classmethod
    def query_text(cls, value):
        return checked_query(value) if value is not None else None


class TurnPlan(StrictModel):
    action: Literal["new_search", "refine", "clarify", "reject"]
    topic: str | None = Field(max_length=256)
    time_op: Literal["keep", "set", "clear", "lunar", "relative"]
    time: Interval | None
    lunar_time: LunarTime | None = None
    relative_time: RelativeTime | None = None
    formats_op: Literal["keep", "set", "clear"]
    formats: list[str] = Field(max_length=4)
    reason: Literal[
        "none",
        "ambiguous_time",
        "missing_topic",
        "unsupported_condition",
        "location_not_supported",
        "off_topic",
        "unsafe",
    ]

    @model_validator(mode="after")
    def coherent(self):
        if self.topic is not None:
            self.topic = checked_query(self.topic)
        if (self.time_op == "set") != (self.time is not None):
            raise ValueError("time operation mismatch")
        if (self.time_op == "lunar") != (self.lunar_time is not None):
            raise ValueError("lunar operation mismatch")
        if (self.time_op == "relative") != (self.relative_time is not None):
            raise ValueError("relative operation mismatch")
        if (self.formats_op == "set") != bool(self.formats):
            raise ValueError("format operation mismatch")
        if self.formats:
            validate_conditions({"formats": self.formats})
        allowed = {
            "new_search": {"none"},
            "refine": {"none"},
            "clarify": {
                "ambiguous_time",
                "missing_topic",
                "unsupported_condition",
                "location_not_supported",
            },
            "reject": {"off_topic", "unsafe"},
        }
        if self.reason not in allowed[self.action]:
            raise ValueError("action reason mismatch")
        if self.action in {"clarify", "reject"} and (
            self.topic is not None
            or self.time_op != "keep"
            or self.formats_op != "keep"
        ):
            raise ValueError("non-search cannot change state")
        if self.action == "new_search" and not self.topic:
            raise ValueError("new search needs topic")
        return self

    def apply(self, previous):
        if self.relative_time is not None:
            raise ValueError("relative time must be resolved before state application")
        if self.action not in {"new_search", "refine"}:
            raise ValueError("not an executable plan")
        state = (
            SearchState()
            if self.action == "new_search"
            else previous.model_copy(deep=True)
        )
        if self.topic is not None:
            state.topic = self.topic
        if self.time_op != "keep":
            state.time = self.time if self.time_op == "set" else None
            state.unapplied_time = self.lunar_time if self.time_op == "lunar" else None
        if self.formats_op != "keep":
            state.formats = list(self.formats) if self.formats_op == "set" else []
        if not state.topic:
            raise ValueError("refinement needs an existing topic")
        return SearchState.model_validate(state.model_dump())


class TurnResponse(StrictModel):
    status: Literal["ok", "rejected", "clarification_required", "unavailable"]
    message: str
    state: SearchState
    search: RetrievalResponse | None = None
    reason: str = "none"
    timing_ms: dict[str, float] = Field(default_factory=dict)
    revision: str
    notices: list[str] = Field(default_factory=list, max_length=4)


class ModelTurnPlan(StrictModel):
    """One value per field prevents contradictory operation/value pairs in JSON."""

    action: Literal["new_search", "refine", "clarify", "reject"]
    topic: str | None = Field(max_length=256)
    time: Literal["keep", "clear"] | Interval | LunarTime | RelativeTime
    formats: (
        Literal["keep", "clear"]
        | Annotated[
            list[
                Literal["CSV", "PDF", "XLSX", "ZIP", "JSON", "GEOJSON", "SHP", "JPEG"]
            ],
            Field(min_length=1, max_length=4, json_schema_extra={"uniqueItems": True}),
        ]
    )
    reason: Literal[
        "none",
        "ambiguous_time",
        "missing_topic",
        "unsupported_condition",
        "location_not_supported",
        "off_topic",
        "unsafe",
    ]

    @field_validator("formats")
    @classmethod
    def unique_formats(cls, value):
        if isinstance(value, list) and len(value) != len(set(value)):
            raise ValueError("duplicate formats")
        return value

    def compile(self):
        return TurnPlan.model_validate(
            {
                "action": self.action,
                "topic": self.topic,
                "reason": self.reason,
                "time_op": self.time
                if isinstance(self.time, str)
                else "lunar"
                if isinstance(self.time, LunarTime)
                else "relative"
                if isinstance(self.time, RelativeTime)
                else "set",
                "time": self.time.model_dump()
                if isinstance(self.time, Interval)
                else None,
                "lunar_time": self.time.model_dump()
                if isinstance(self.time, LunarTime)
                else None,
                "relative_time": self.time.model_dump()
                if isinstance(self.time, RelativeTime)
                else None,
                "formats_op": self.formats if isinstance(self.formats, str) else "set",
                "formats": [] if isinstance(self.formats, str) else self.formats,
            }
        )


def model_plan_schema(*, allow_refine=True, relative_only=False, relative_texts=()):
    """Constrain action/reason pairs in generation, not just after generation.

    Ollama's JSON grammar supports anyOf. Separate branches also prevent a
    clarification/rejection from carrying partially executable filters.
    Python validation remains the final authority for dates and state changes.
    """
    base = ModelTurnPlan.model_json_schema()
    definitions = base.pop("$defs", {})
    # Match runtime mode/value constraints in generation too, so arithmetic
    # intent mistakes become grounded clarifications, not malformed-plan 503s.
    relative = definitions["RelativeTime"]
    if relative_texts:
        # Choose an exact detected span, not a free-form quotation that can
        # accidentally swallow a suffix (e.g. 最近一年 + 的). The engine still
        # verifies its meaning and rejects incomplete/compound constraints.
        relative["properties"]["text"] = {
            "type": "string",
            "enum": list(relative_texts),
        }
    alternatives = []
    for mode, low, high in (
        ("rolling", 1, 1000),
        ("calendar", -1000, 0),
        ("to_date", 0, 0),
    ):
        variant = copy.deepcopy(relative)
        variant["properties"]["mode"] = {"type": "string", "enum": [mode]}
        variant["properties"]["value"] = {
            "type": "integer",
            "minimum": low,
            "maximum": high,
        }
        alternatives.append(variant)
    definitions["RelativeTime"] = {"anyOf": alternatives}
    branches = []
    for action, reasons in {
        "new_search": ["none"],
        "refine": ["none"],
        "clarify": [
            "ambiguous_time",
            "missing_topic",
            "unsupported_condition",
            "location_not_supported",
        ],
        "reject": ["off_topic", "unsafe"],
    }.items():
        if action == "refine" and not allow_refine:
            continue
        branch = copy.deepcopy(base)
        fields = branch["properties"]
        fields["action"] = {"type": "string", "enum": [action]}
        fields["reason"] = {"type": "string", "enum": reasons}
        if action == "new_search":
            fields["topic"] = {"type": "string", "minLength": 1, "maxLength": 256}
        if relative_only and action in {"new_search", "refine"}:
            fields["time"] = {
                "anyOf": [
                    {"$ref": "#/$defs/RelativeTime"},
                    {"type": "string", "enum": ["clear"]},
                ]
            }
        if action in {"clarify", "reject"}:
            fields["topic"] = {"type": "null"}
            for key in ("time", "formats"):
                fields[key] = {"type": "string", "enum": ["keep"]}
        branches.append(branch)
    return {"$defs": definitions, "anyOf": branches}


MESSAGES = {
    "lunar_time_unapplied": "已保留農曆文字搜尋相關資料；未換算國曆，也未套用日期篩選，結果不保證符合該農曆期間。若需精確篩選，請補充國曆日期或起訖範圍。",
    "ambiguous_time": "請提供明確年份或日期範圍，例如「2025 年」或「最近三年」。",
    "missing_topic": "你想找哪一類資料集？例如河川水質、氣候觀測或土地利用。",
    "unsupported_condition": "這個條件目前不支援。可搜尋主題、資料涵蓋時間與檔案格式；不支援更新日、排除條件或直接解讀檔案內容。",
    "location_not_supported": "這版未啟用座標、附近或距離篩選。可以將地名當作文字搜尋，但不能保證地理範圍。",
    "off_topic": "這裡只協助尋找 depositar 的公開資料集，不提供一般問答或程式解題。",
    "unsafe": "無法處理這個要求。請描述你想尋找的公開資料集。",
}
