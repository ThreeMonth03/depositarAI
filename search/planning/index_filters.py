"""Versioned time/format metadata projected into sealed catalog documents.

Integer calendar-day ordinals preserve uncertain year/month endpoints exactly.
Query compilation belongs to the retrieval layer.
"""

import re

from .predicates import FORMATS, project


FIELD = "depositar_conditions"
VERSION = "time-format-v2"
VECTOR_PATH = "content_chunk_vector"
VECTOR_FIELD = VECTOR_PATH + ".vector"
BOOLEAN_FIELDS = ("record", "start_known", "end_known", "formats_known") + tuple(
    "format_" + f for f in FORMATS
)
INTEGER_FIELDS = ("start_lo", "start_hi", "end_lo", "end_hi")
KEYWORD_FIELDS = ("time_status", "start_precision", "end_precision")
FIELDS = BOOLEAN_FIELDS + INTEGER_FIELDS + KEYWORD_FIELDS


def mapping():
    properties = {
        **{name: {"type": "boolean"} for name in BOOLEAN_FIELDS},
        **{name: {"type": "integer"} for name in INTEGER_FIELDS},
        **{name: {"type": "keyword"} for name in KEYWORD_FIELDS},
        "schema": {"type": "keyword"},
        "catalog": {"type": "keyword"},
    }
    return {
        "properties": {
            FIELD: {"type": "object", "dynamic": "strict", "properties": properties}
        }
    }


def metadata(document, bundle_id):
    validate_bundle_id(bundle_id)
    normalized = project(document)
    return {
        **{k: normalized[k] for k in FIELDS},
        "schema": VERSION,
        "catalog": bundle_id,
    }


def validate_bundle_id(bundle_id):
    if not isinstance(bundle_id, str) or not re.fullmatch(r"[0-9a-f]{64}", bundle_id):
        raise ValueError("verified bundle identity required")
