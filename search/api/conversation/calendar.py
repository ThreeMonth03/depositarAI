"""Bounded calendar normalization, not a general natural-language date parser.

Era conversion is arithmetic; lunar conversion is deliberately unsupported.
Never edits the original text used by the scope classifier. A single, ordinary
date can supply an exact interval; ranges/relative/excluded dates stay with the
planner. Ambiguity is a normal clarification, never a backend exception.
"""

from dataclasses import dataclass
import re

from search.planning.contract import checked_query
from search.planning.predicates import date_bounds

_DIGITS = {c: i for i, c in enumerate("零一二三四五六七八九")}
_DIGITS.update({"〇": 0, "○": 0, "兩": 2, "两": 2})
_NUMBER = r"(?:[0-9]{1,4}|[零〇○一二三四五六七八九十百千兩两元]{1,7})"
_ROC = r"(?:中華民國|中华民国|民國|民国|R\.?O\.?C\.?)"
_CE = r"(?:西元|公元|西曆|西历|國曆|国历|公曆|公历)"
_DATE = re.compile(
    rf"(?:(?P<era>{_ROC}|{_CE})\s*)?(?P<year>{_NUMBER})\s*年"
    rf"(?:\s*(?P<month>{_NUMBER})\s*月(?:\s*(?P<day>{_NUMBER})\s*[日號号])?)?"
    r"|(?<![0-9])(?P<iso>[0-9]{4}(?:-[0-9]{1,2}(?:-[0-9]{1,2})?)?)(?![0-9])",
    re.IGNORECASE,
)
_SHORT_YEAR = re.compile(rf"(?P<number>{_NUMBER})\s*年")
_LUNAR = r"(?:農曆|农历|陰曆|阴历|舊曆|旧历|lunar(?:\s+calendar)?)"
_NUMERIC_DATE = r"[0-9]{4}(?:[-/][0-9]{1,2}){1,2}"
_LUNAR_DATE = re.compile(
    rf"{_LUNAR}\s*(?:的\s*)?(?:{_ROC}\s*)?"
    rf"(?:{_NUMBER}\s*(?:年|月|日)|{_NUMERIC_DATE}|正月|冬月|臘月|腊月|初[一二三四五六七八九十]|閏|闰)"
    rf"|(?:年|月|日|號|号)\s*[（(]?\s*{_LUNAR}(?=[）)\s]|$)"
    rf"|{_NUMERIC_DATE}\s*[（(]?\s*{_LUNAR}(?=[）)\s]|$)"
    rf"|(?:{_NUMBER})\s*年\s*{_LUNAR}\s*(?:全年|整年|資料|资料|期間|期间|$)",
    re.IGNORECASE,
)
_RELATIVE_PREFIX = re.compile(
    r"(?:最近|近期|過去|过去|未來|未来|近|前|後|后|每|連續|连续|第)\s*$"
)
_COMPLEX_TIME = re.compile(
    r"以前|以後|以后|之前|之後|之后|截至|最近|近期|過去|过去|未來|未来|"
    r"起|至|到|從|从|不|排除|取消|移除|清除|去掉|以外|前|後|后|"
    r"半年|季|初|底|末|春|夏|秋|冬|更新|發布|发布|上架|建立|出版|學年|学年|"
    r"[~～—]|\b(?:before|after|since|until|from|between|except|not|last|past|next)\b",
    re.IGNORECASE,
)
_YEAR_TEXT = rf"(?:(?:{_ROC}|{_CE})\s*)?{_NUMBER}\s*年"
_LUNAR_MONTH = rf"(?:(?:閏|闰)\s*)?(?:正|冬|臘|腊|{_NUMBER})\s*月"
_LUNAR_DAY = (
    rf"(?:初[一二三四五六七八九十]|(?:二十|三十|十|廿)[一二三四五六七八九]?|{_NUMBER})"
    r"(?:[日號号])?"
)
_LUNAR_SPAN = re.compile(
    rf"(?:{_YEAR_TEXT}\s*)?{_LUNAR}\s*(?:{_YEAR_TEXT}\s*)?"
    rf"{_LUNAR_MONTH}(?:\s*{_LUNAR_DAY})?"
    rf"|(?:{_YEAR_TEXT}\s*{_LUNAR}|{_LUNAR}\s*{_YEAR_TEXT})(?:全年|整年)?"
    rf"|{_YEAR_TEXT}(?:\s*{_NUMBER}\s*月(?:\s*{_NUMBER}\s*[日號号])?)?"
    rf"\s*[（(]\s*{_LUNAR}\s*[）)]",
    re.IGNORECASE,
)


def number(value):
    """Arabic, digit-by-digit Chinese, and ordinary Chinese numbers up to 9999."""
    if value == "元":
        return 1
    if value.isascii() and value.isdecimal():
        return int(value)
    if all(c in _DIGITS for c in value):
        return int("".join(str(_DIGITS[c]) for c in value))
    total, digit, previous_unit = 0, None, 10000
    for char in value:
        if char in _DIGITS:
            if digit not in (None, 0):
                raise ValueError("ambiguous numeral")
            digit = _DIGITS[char]
        elif char in "十百千":
            unit = {"十": 10, "百": 100, "千": 1000}[char]
            if unit >= previous_unit:
                raise ValueError("invalid numeral order")
            total += (1 if digit is None else digit) * unit
            digit, previous_unit = None, unit
        else:
            raise ValueError("unsupported numeral")
    return total + (digit or 0)


@dataclass(frozen=True)
class CalendarText:
    query: str
    reason: str = ""
    message: str = ""
    notices: tuple[str, ...] = ()
    exact: str | None = None
    literal: bool = False
    lunar_date: bool = False


def calendar_text(query):
    query = checked_query(query)
    # A calendar name may be the subject. Never block it or apply Gregorian
    # arithmetic to its numbers; the planner separates topic from literal time.
    lunar_mentioned = bool(re.search(_LUNAR, query, re.IGNORECASE))
    if lunar_mentioned and _LUNAR_DATE.search(query):
        return CalendarText(query, lunar_date=True)
    matches = []
    for match in _DATE.finditer(query):
        if match["year"] and not match["era"]:
            if _RELATIVE_PREFIX.search(query[: match.start()]) or re.match(
                r"\s*(?:前|後|后|來|来|內|内)", query[match.end() :]
            ):
                continue
            try:
                if number(match["year"]) < 1000:
                    continue
            except ValueError:
                continue
        if match["year"] or "-" in match["iso"] or match[0] == query:
            matches.append(match)
    for match in _SHORT_YEAR.finditer(query):
        if any(start.start() <= match.start() < start.end() for start in matches):
            continue
        before, after = query[: match.start()], query[match.end() :]
        if match["number"] in {"百", "千", "一百", "一千"} and re.match(
            r"(?:古蹟|古迹|歷史|历史|老|樹|树|傳統|传统)", after
        ):
            continue
        if _RELATIVE_PREFIX.search(before) or re.match(
            r"\s*(?:前|後|后|來|来|內|内)", after
        ):
            continue
        try:
            year = number(match["number"])
        except ValueError:
            return CalendarText(
                query,
                "invalid_date",
                "無法確認這個年份，請改用阿拉伯數字並註明民國或西元。",
            )
        if year < 1:
            return CalendarText(
                query,
                "invalid_date",
                "年份必須有效且明確標示民國或西元，不能使用零年。",
            )
        if 0 < year < 1000:
            return CalendarText(
                query,
                "ambiguous_era",
                f"「{match['number']}年」尚未註明紀年。若指民國{year}年，請回覆「民國{year}年」（西元{year + 1911}年）；若指西元，請明寫「西元{year}年」。",
            )
    replacements, dates, notices = [], [], []
    try:
        for match in matches:
            if match["iso"]:
                value = match["iso"]
            else:
                year = number(match["year"])
                if year < 1:
                    raise ValueError("nonpositive year")
                roc = re.fullmatch(_ROC, match["era"] or "", re.IGNORECASE)
                if roc:
                    year += 1911
                value = f"{year:04d}"
                for part in ("month", "day"):
                    if match[part]:
                        value += f"-{number(match[part]):02d}"
                if roc:
                    notices.append(
                        f"{match[0]}已換算為西元 {value}；月份與日期按國曆處理。"
                    )
                text = value + ("年" if not match["month"] else "")
                replacements.append((match.start(), match.end(), text))
            date_bounds(value)
            dates.append(value)
        normalized = query
        for start, end, value in reversed(replacements):
            normalized = normalized[:start] + value + normalized[end:]
        normalized = checked_query(normalized)
    except ValueError:
        return CalendarText(
            query,
            "invalid_date",
            "日期無效或超出支援範圍，請檢查年份、月份與日期；民國與西元請明確標示。",
        )
    # Never widen an unconsumed month/day or an incomplete slash date to a year.
    suffix = query[matches[0].end() :] if len(matches) == 1 else ""
    exact = (
        dates[0]
        if len(dates) == 1
        and not _COMPLEX_TIME.search(query)
        and not re.match(rf"\s*{_NUMBER}\s*(?:月|日)|\s*[-/]", suffix)
        else None
    )
    literal = len(matches) == 1 and matches[0].span() == (0, len(query))
    return CalendarText(
        # Keep source text for semantic calendar classification. An independent
        # civil year may still apply when lunar words name the research subject.
        query if lunar_mentioned else normalized,
        notices=tuple(notices[:4]),
        exact=exact,
        literal=literal,
    )


def corrected_pending(pending, latest):
    """Replace one superseded date in a planner-only copy of pending text.

    Only an entire date reply can resolve this narrow adapter. Keep all subject,
    format and instruction text; do not erase other constraints or convert lunar
    dates ourselves. Complex/unrecognized corrections stay with the planner.
    The session's pending/state are not mutated before a successful search.
    """
    if not pending or not latest.literal or not latest.exact:
        return pending
    old = calendar_text(pending)
    if not old.reason and not old.lunar_date:
        return pending
    pattern = _LUNAR_SPAN if old.lunar_date else _DATE
    matches = list(pattern.finditer(old.query))
    if len(matches) != 1:
        return pending
    match = matches[0]
    remainder = old.query[: match.start()] + old.query[match.end() :]
    # Never collapse a range/exclusion or consume an incomplete lunar date.
    if (
        _COMPLEX_TIME.search(remainder)
        or _DATE.search(remainder)
        or re.match(rf"\s*(?:{_NUMBER}|初|廿|閏|闰)", old.query[match.end() :])
    ):
        return pending
    replacement = latest.exact + ("年" if len(latest.exact) == 4 else "")
    revised = old.query[: match.start()] + replacement + old.query[match.end() :]
    try:
        parsed = calendar_text(revised)
    except ValueError:
        return pending
    return revised if not parsed.reason and parsed.exact == latest.exact else pending
