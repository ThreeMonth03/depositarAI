"""Read one bounded private JSONL snapshot without network access."""

import gzip
import json
import os
from typing import (
    Any,
    BinaryIO,
    Dict,
    List,
)

from . import codec
from . import policy
from . import storage


def load_source(path: str) -> List[Dict[str, Any]]:
    descriptor, initial = storage._safe_source_fd(path)
    raw = os.fdopen(descriptor, "rb", closefd=True)
    stream: BinaryIO
    if path.casefold().endswith(".gz"):
        stream = gzip.GzipFile(fileobj=raw, mode="rb")
    else:
        stream = raw

    records: List[Dict[str, Any]] = []
    expanded = 0
    line_number = 0
    try:
        while True:
            line = stream.readline(policy.MAX_LINE_BYTES + 1)
            if not line:
                break
            line_number += 1
            expanded += len(line)
            if len(line) > policy.MAX_LINE_BYTES:
                raise policy.BundleError(
                    "source line %d exceeds the line-size limit" % line_number
                )
            if expanded > policy.MAX_EXPANDED_BYTES:
                raise policy.BundleError("source exceeds the expanded-size limit")
            if not line.strip():
                raise policy.BundleError("blank JSON Lines records are not allowed")
            codec.check_json_nesting(line)
            try:
                text = line.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise policy.BundleError(
                    "source line %d is not strict UTF-8" % line_number
                ) from exc
            try:
                value = json.loads(
                    text,
                    parse_constant=codec.reject_json_constant,
                    parse_int=codec.parse_json_int,
                    parse_float=codec.parse_json_float,
                    object_pairs_hook=codec.unique_json_object,
                )
            except policy.BundleError:
                raise
            except (json.JSONDecodeError, RecursionError) as exc:
                raise policy.BundleError(
                    "source line %d is not strict JSON" % line_number
                ) from exc
            if not isinstance(value, dict):
                raise policy.BundleError(
                    "source line %d must contain a JSON object" % line_number
                )
            codec.reject_surrogates(value)
            records.append(value)
            if len(records) > policy.MAX_ROWS:
                raise policy.BundleError("source exceeds the row limit")
        if stream is not raw:
            stream.close()
        final = os.fstat(raw.fileno())
        if (
            initial.st_dev,
            initial.st_ino,
            initial.st_size,
            initial.st_mtime_ns,
            initial.st_ctime_ns,
        ) != (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ):
            raise policy.BundleError("source changed while it was read")
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise policy.BundleError("source gzip stream is invalid") from exc
    finally:
        if not raw.closed:
            raw.close()
    if not records:
        raise policy.BundleError("source must contain at least one dataset")
    return records
