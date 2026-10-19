"""Bounded relative-time DSL: model selects intent, Python computes dates.

The literal-span check validates a model selection; it never chooses a topic,
executes a query, or substitutes a date into arbitrary user text. Unsupported
wording must be clarified rather than treated as a model-computed interval.
"""

import calendar
from datetime import date, timedelta
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from search.planning.clock import normalize_context
from search.planning.contract import checked_query
from .calendar import number
from ..shared.schemas import StrictModel


class RelativeTime(StrictModel):
    kind: Literal["relative"]
    mode: Literal["rolling", "calendar", "to_date"]
    unit: Literal["year", "month", "week", "day"]
    value: int = Field(ge=-1000, le=1000)
    text: str = Field(min_length=1, max_length=128)

    @field_validator("text")
    @classmethod
    def literal(cls, value):
        return checked_query(value)

    @model_validator(mode="after")
    def limits(self):
        if self.mode == "rolling" and self.value < 1:
            raise ValueError("rolling duration must be positive")
        if self.mode == "calendar" and self.value > 0:
            raise ValueError("future periods are not supported")
        if self.mode == "to_date" and self.value != 0:
            raise ValueError("to-date starts in the current period")
        return self


_N = r"(?:[0-9]{1,4}|[零〇一二三四五六七八九十百千兩两]{1,7})"
_EN_NUMBERS = dict(
    zip("one two three four five six seven eight nine ten".split(), range(1, 11))
)
_E = r"(?:[0-9]{1,4}|one|two|three|four|five|six|seven|eight|nine|ten)"
_ROLLING = re.compile(
    rf"(?:最近|近期|過去|过去|近)\s*(?P<zh>{_N})\s*(?:個|个)?\s*(?P<zu>年|月|週|周|天|日)"
    rf"|(?<![A-Za-z0-9_])(?:past|last|recent)\s+(?P<en>{_E})\s+(?P<eu>years?|months?|weeks?|days?)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_NAMED = {
    "今年": ("year", 0),
    "去年": ("year", -1),
    "前年": ("year", -2),
    "本月": ("month", 0),
    "這個月": ("month", 0),
    "这个月": ("month", 0),
    "上個月": ("month", -1),
    "上个月": ("month", -1),
    "上月": ("month", -1),
    "本週": ("week", 0),
    "本周": ("week", 0),
    "上週": ("week", -1),
    "上周": ("week", -1),
    "今天": ("day", 0),
    "昨天": ("day", -1),
    "前天": ("day", -2),
    "this year": ("year", 0),
    "last year": ("year", -1),
    "this month": ("month", 0),
    "last month": ("month", -1),
    "this week": ("week", 0),
    "last week": ("week", -1),
    "today": ("day", 0),
    "yesterday": ("day", -1),
}
_NAMED_RE = re.compile(
    "|".join(
        r"(?<![A-Za-z0-9_])" + re.escape(k) + r"(?![A-Za-z0-9_])"
        if k.isascii()
        else re.escape(k)
        for k in sorted(_NAMED, key=len, reverse=True)
    ),
    re.IGNORECASE,
)
_TO_DATE = re.compile(
    r"(?:今年|本月|本週|本周)(?:截至今天|至今)|(?<![A-Za-z0-9_])(?:year|month|week)[ -]to[ -]date(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_UNITS = {
    "年": "year",
    "月": "month",
    "週": "week",
    "周": "week",
    "天": "day",
    "日": "day",
}


def literal_spec(text):
    """Closed supported phrases, used only to check the model's typed claim."""
    text = checked_query(text)
    match = _ROLLING.fullmatch(text)
    if match:
        if match["zh"]:
            return "rolling", _UNITS[match["zu"]], number(match["zh"])
        n = match["en"].lower()
        return (
            "rolling",
            match["eu"].lower().rstrip("s"),
            _EN_NUMBERS[n] if n in _EN_NUMBERS else int(n),
        )
    if _TO_DATE.fullmatch(text):
        unit = (
            "year"
            if text.startswith("今年") or text.lower().startswith("year")
            else "month"
            if text.startswith("本月") or text.lower().startswith("month")
            else "week"
        )
        return "to_date", unit, 0
    pair = _NAMED.get(text.lower())
    if pair:
        return "calendar", *pair
    raise ValueError("unsupported relative phrase")


def relative_literals(query):
    """Detect supported temporal cues, never infer a search topic or interval."""
    # Longest spans own their substrings: 今年截至今天 is not independent 今年 + 今天.
    spans = []
    for pattern in (_TO_DATE, _ROLLING, _NAMED_RE):
        for match in pattern.finditer(query):
            if not any(
                start < match.end() and match.start() < end for start, end, _ in spans
            ):
                spans.append((match.start(), match.end(), match[0]))
    return [text for _, _, text in sorted(spans)]


def month_shift(value, offset):
    month_index = value.year * 12 + value.month - 1 + offset
    year, month = divmod(month_index, 12)
    month += 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def period(value, unit):
    if unit == "year":
        return date(value.year, 1, 1), date(value.year, 12, 31)
    if unit == "month":
        return value.replace(day=1), value.replace(
            day=calendar.monthrange(value.year, value.month)[1]
        )
    if unit == "week":
        start = value - timedelta(days=value.weekday())
        return start, start + timedelta(days=6)
    return value, value


def resolve_relative(spec, context, query):
    """Anchor exclusively to the validated caller clock; never wall-clock fallback."""
    if spec.text not in query or literal_spec(spec.text) != (
        spec.mode,
        spec.unit,
        spec.value,
    ):
        raise ValueError("relative expression differs from user's literal")
    literals = relative_literals(query)
    if literals != [spec.text]:
        # Do not select one date out of a comparison/range/compound request.
        raise ValueError("relative selection incomplete or ambiguous")
    tail = query[query.index(spec.text) + len(spec.text) :]
    if re.match(
        r"\s*(?:半|多|左右|以前|以後|以后|之前|之後|之后|以上|以下|起|至|到)", tail
    ):
        raise ValueError("relative duration has an unsupported qualifier")
    clock = normalize_context(context)
    if clock is None:
        raise ValueError("relative date requires reference clock")
    today = date.fromisoformat(clock["reference_date"])
    value, unit = spec.value, spec.unit
    if spec.mode == "rolling":
        if unit in {"year", "month"}:
            start = month_shift(today, -value * (12 if unit == "year" else 1))
        else:
            start = today - timedelta(days=value * (7 if unit == "week" else 1) - 1)
        end = today
    elif spec.mode == "calendar":
        anchor = (
            month_shift(today, value * (12 if unit == "year" else 1))
            if unit in {"year", "month"}
            else today + timedelta(days=value * (7 if unit == "week" else 1))
        )
        start, end = period(anchor, unit)
    else:
        start, _ = period(today, unit)
        end = today
    return {"start": start.isoformat(), "end": end.isoformat()}


RELATIVE_POLICY = """
RELATIVE DATES: Never calculate or output absolute start/end dates for them.
Select a literal phrase from the LATEST query and output this typed object:
{"kind":"relative","mode":"rolling|calendar|to_date","unit":"year|month|week|day",
"value":integer,"text":"exact complete relative phrase"}.
The SERVER computes dates using only the verified reference clock, not user claims.
rolling: 最近N年/過去N年/past N years -> unit=year,value=N, likewise months/weeks/days.
NEVER subtract 1 from the duration. 最近三年 -> value=3, NOT 2. Preserve its unit.
calendar: 去年/last year -> unit=year,value=-1; 今年/this year -> value=0.
前年 -> year,-2. 上個月/last month -> month,-1; 本月 -> month,0.
上週/last week -> week,-1; 本週 -> week,0. 今天/today -> day,0; 昨天 -> day,-1.
to_date: 今年截至今天/今年至今/year-to-date -> unit=year,value=0;
本月至今/month-to-date -> unit=month,value=0; 本週至今/week-to-date -> week,0.
For calendar periods return the FULL period, not a rolling duration. Weeks start Monday.
Rolling years/months end on local today; the server clamps invalid month-end days.
Rolling N days/weeks include today: N days or 7*N days total. Both endpoints inclusive.
Without reference clock, vague recency, future dates, compound relative constraints,
or wording not supported above: clarify/ambiguous_time; do not guess dates.
This DSL applies to Gregorian relative time, never convert explicit lunar periods.
An ordinary follow-up (e.g. CSV) uses time=keep; do not recalculate old relative dates.
Only absolute civil dates use {start,end}. Never fabricate a relative phrase not supplied.
"""
