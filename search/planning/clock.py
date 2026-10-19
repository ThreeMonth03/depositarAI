"""Validate request clocks without inferring the host clock or location."""

from datetime import datetime
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def normalize_context(value):
    """Validate an RFC3339 instant and named zone before placing them in a prompt."""
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"reference_time", "timezone"}:
        raise ValueError("reference_time and timezone are both required")
    stamp, zone = value["reference_time"], value["timezone"]
    if not isinstance(stamp, str) or not _TIMESTAMP.fullmatch(stamp):
        raise ValueError("reference_time must be an offset-aware RFC3339 timestamp")
    if not isinstance(zone, str) or not 1 <= len(zone) <= 64:
        raise ValueError("invalid timezone")
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        local = moment.astimezone(ZoneInfo(zone))
    except (ValueError, OverflowError, ZoneInfoNotFoundError) as error:
        raise ValueError("invalid clock context") from error
    if not 1900 <= local.year <= 2100:
        raise ValueError("reference year outside research bounds")
    return {
        "reference_time": moment.isoformat(),
        "timezone": zone,
        "local_datetime": local.isoformat(),
        "reference_date": local.date().isoformat(),
    }
