"""Small helpers for parsing and formatting dates/times."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import InvalidInput

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def get_tz(name: str | None, default: ZoneInfo) -> ZoneInfo:
    if not name:
        return default
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidInput(f"Unknown timezone {name!r}; use an IANA name like 'America/New_York'.") from exc


def parse_when(value: str, tz: tzinfo) -> date | datetime:
    """Parse an ISO date or datetime. Naive datetimes are placed in ``tz``."""
    value = value.strip()
    if _DATE_RE.match(value):
        return date.fromisoformat(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError as exc:
        raise InvalidInput(f"Could not parse {value!r}; use ISO-8601 like '2026-11-03T15:00' or '2026-11-03'.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def parse_datetime(value: str, tz: tzinfo) -> datetime:
    """Parse to an aware datetime (a bare date means midnight in ``tz``)."""
    parsed = parse_when(value, tz)
    if isinstance(parsed, datetime):
        return parsed
    return datetime.combine(parsed, time.min, tzinfo=tz)


def as_aware(value: date | datetime, tz: tzinfo) -> datetime:
    """Turn a date / naive (floating) datetime / aware datetime into an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=tz)
    return datetime.combine(value, time.min, tzinfo=tz)


def fmt(value: date | datetime | None, display_tz: tzinfo) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(display_tz)
        return value.isoformat(timespec="minutes" if value.second == 0 else "seconds")
    return value.isoformat()


def tz_name(value: date | datetime | None) -> str | None:
    if isinstance(value, datetime) and value.tzinfo is not None:
        key = getattr(value.tzinfo, "key", None)
        if key:
            return key
        if value.utcoffset() == timedelta(0):
            return "UTC"
        return str(value.tzinfo)
    return None


def same_instant(a: date | datetime, b: date | datetime, tz: tzinfo) -> bool:
    """Compare two RECURRENCE-ID-like values, tolerant of date/datetime/timezone forms."""
    if not isinstance(a, datetime) or not isinstance(b, datetime):
        da = a.date() if isinstance(a, datetime) else a
        db = b.date() if isinstance(b, datetime) else b
        return da == db
    return as_aware(a, tz) == as_aware(b, tz)


def utcnow() -> datetime:
    return datetime.now(UTC)


def match_form(value: date | datetime, template: date | datetime) -> date | datetime:
    """Coerce ``value`` into the same form (date vs datetime, timezone) as ``template``.

    Used for RECURRENCE-ID / EXDATE values, which must match DTSTART's form.
    """
    if not isinstance(template, datetime):
        return value.date() if isinstance(value, datetime) else value
    if not isinstance(value, datetime):
        value = datetime.combine(value, template.time(), tzinfo=template.tzinfo)
    if template.tzinfo is None:
        return value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone().replace(tzinfo=None)
    if value.tzinfo is None:
        return value.replace(tzinfo=template.tzinfo)
    return value.astimezone(template.tzinfo)
