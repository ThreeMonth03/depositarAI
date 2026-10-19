"""Conservative metadata predicates; no model or live-index access.

Coarse dates describe uncertain endpoints, not fabricated observation days.
Filters distinguish supported overlap, provable exclusion, and unknown.
Geographic predicates compare declared bboxes, not administrative boundaries.
"""

from __future__ import annotations

import calendar
from datetime import date
import re

FORMATS = ("CSV", "PDF", "XLSX", "ZIP", "JSON", "GEOJSON", "SHP", "JPEG")
MIN_DAY, MAX_DAY = date.min.toordinal(), date.max.toordinal()


def date_bounds(value):
    """Return ordinal bounds and precision; accept unpadded month/day fields."""
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}(?:-[0-9]{1,2}(?:-[0-9]{1,2})?)?", value
    ):
        raise ValueError("unsupported date shape")
    parts = list(map(int, value.split("-")))
    year = parts[0]
    month = parts[1] if len(parts) > 1 else 1
    day = parts[2] if len(parts) > 2 else 1
    low = date(year, month, day)
    if len(parts) == 1:
        high, precision = date(year, 12, 31), "year"
    elif len(parts) == 2:
        high = date(year, month, calendar.monthrange(year, month)[1])
        precision = "month"
    else:
        high, precision = low, "day"
    return low.toordinal(), high.toordinal(), precision


def checked_bbox(value):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("bbox must have four coordinates")
    if any(type(v) not in (int, float) or not -180 <= v <= 180 for v in value):
        raise ValueError("bbox requires finite numbers, not booleans")
    west, south, east, north = value
    if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
        raise ValueError("bbox bounds or unsupported antimeridian crossing")
    return tuple(value)


def project(document):
    """Flat research metadata for Haystack; never writes back to the corpus."""
    temporal = document["temporal"]
    result = {"record": True}
    invalid = False
    for side in ("start", "end"):
        value = temporal[side]
        known = value not in (None, "")
        try:
            low, high, precision = date_bounds(value) if known else (0, 0, "absent")
        except ValueError:
            low, high, precision, invalid = 0, 0, "invalid", True
            known = False
        result.update(
            {
                side + "_known": known,
                side + "_lo": low,
                side + "_hi": high,
                side + "_precision": precision,
            }
        )
    start, end = result["start_known"], result["end_known"]
    if start and end and result["start_lo"] > result["end_hi"]:
        invalid = True
    result["time_status"] = (
        "invalid"
        if invalid
        else "complete"
        if start and end
        else "partial"
        if start or end
        else "absent"
    )
    bbox = document["bbox"]
    coordinates = [bbox.get(k) for k in ("x_min", "y_min", "x_max", "y_max")]
    try:
        if bbox["status"] != "complete":
            raise ValueError("bbox incomplete")
        coordinates = checked_bbox(coordinates)
        result["bbox_known"] = True
    except ValueError:
        coordinates = (0, 0, 0, 0)
        result["bbox_known"] = False
    result.update(zip(("west", "south", "east", "north"), coordinates))
    formats = {f.strip().upper() for f in document["resource_formats"] if f.strip()}
    result["formats_known"] = bool(formats)
    for name in FORMATS:
        result["format_" + name] = name in formats
    return result


def validate_conditions(conditions):
    """Only a bounded contract, never user/model-supplied filter AST or DSL."""
    if not isinstance(conditions, dict) or set(conditions) - {
        "time",
        "bbox",
        "formats",
    }:
        raise ValueError("unsupported condition fields")
    result = {}
    if "time" in conditions:
        value = conditions["time"]
        if not isinstance(value, dict) or set(value) != {"start", "end"}:
            raise ValueError("time endpoints required")
        if value["start"] is None and value["end"] is None:
            raise ValueError("empty time condition")
        low = MIN_DAY if value["start"] is None else date_bounds(value["start"])[0]
        high = MAX_DAY if value["end"] is None else date_bounds(value["end"])[1]
        if low > high:
            raise ValueError("reversed query interval")
        result["time"] = (low, high)
    if "bbox" in conditions:
        result["bbox"] = checked_bbox(conditions["bbox"])
    if "formats" in conditions:
        formats = conditions["formats"]
        if (
            not isinstance(formats, list)
            or not 1 <= len(formats) <= 4
            or any(not isinstance(f, str) or f not in FORMATS for f in formats)
            or len(set(formats)) != len(formats)
        ):
            raise ValueError("unsupported or unbounded format alternatives")
        result["formats"] = tuple(formats)
    return result


def _comparison(field, operator, value):
    return {"field": "meta." + field, "operator": operator, "value": value}


def _logic(operator, *conditions):
    return {"operator": operator, "conditions": list(conditions)}


def compile_filters(conditions):
    """Produce disjoint match/outside filters; unknown is their complement."""
    query = validate_conditions(conditions)
    matches, exclusions = [], []
    c, logic = _comparison, _logic
    if "time" in query:
        low, high = query["time"]
        matches.append(
            logic(
                "AND",
                c("time_status", "==", "complete"),
                c("start_hi", "<=", high),
                c("end_lo", ">=", low),
            )
        )
        exclusions.append(
            logic(
                "AND",
                c("time_status", "in", ["complete", "partial"]),
                logic(
                    "OR",
                    logic(
                        "AND", c("start_known", "==", True), c("start_lo", ">", high)
                    ),
                    logic("AND", c("end_known", "==", True), c("end_hi", "<", low)),
                ),
            )
        )
    if "bbox" in query:
        west, south, east, north = query["bbox"]
        matches.append(
            logic(
                "AND",
                c("bbox_known", "==", True),
                c("west", "<=", east),
                c("east", ">=", west),
                c("south", "<=", north),
                c("north", ">=", south),
            )
        )
        exclusions.append(
            logic(
                "AND",
                c("bbox_known", "==", True),
                logic(
                    "OR",
                    c("west", ">", east),
                    c("east", "<", west),
                    c("south", ">", north),
                    c("north", "<", south),
                ),
            )
        )
    if "formats" in query:
        matches.append(
            logic("OR", *(c("format_" + f, "==", True) for f in query["formats"]))
        )
        exclusions.append(
            logic(
                "AND",
                c("formats_known", "==", True),
                *(c("format_" + f, "==", False) for f in query["formats"]),
            )
        )
    return {
        "match": logic("AND", *matches) if matches else c("record", "==", True),
        "outside": logic("OR", *exclusions) if exclusions else c("record", "==", False),
    }


def classify(meta, conditions):
    """Imperative reference independent of Haystack's AST evaluation."""
    query = validate_conditions(conditions)
    states = []
    if "time" in query:
        low, high = query["time"]
        if meta["time_status"] not in ("complete", "partial"):
            state = "unknown"
        elif (meta["start_known"] and meta["start_lo"] > high) or (
            meta["end_known"] and meta["end_hi"] < low
        ):
            state = "outside"
        elif (
            meta["time_status"] == "complete"
            and meta["start_hi"] <= high
            and meta["end_lo"] >= low
        ):
            state = "match"
        else:
            state = "unknown"
        states.append(state)
    if "bbox" in query:
        west, south, east, north = query["bbox"]
        if not meta["bbox_known"]:
            state = "unknown"
        elif (
            meta["west"] > east
            or meta["east"] < west
            or meta["south"] > north
            or meta["north"] < south
        ):
            state = "outside"
        else:
            state = "match"
        states.append(state)
    if "formats" in query:
        states.append(
            "unknown"
            if not meta["formats_known"]
            else "match"
            if any(meta["format_" + f] for f in query["formats"])
            else "outside"
        )
    return (
        "outside"
        if "outside" in states
        else "unknown"
        if "unknown" in states
        else "match"
    )
