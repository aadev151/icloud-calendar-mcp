"""Conversion between iCalendar VEVENTs and the public models.

Covers the properties Apple Calendar uses, including Apple's private ones:

* ``X-APPLE-STRUCTURED-LOCATION`` (location with coordinates -> map + travel time)
* ``X-APPLE-TRAVEL-DURATION`` (travel time block before an event)
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

import icalendar
from icalendar import vCalAddress, vRecur

from .errors import InvalidInput
from .models import Alert, Attendee, EventOut, Location, Recurrence, _EventFields
from .timeutil import fmt, get_tz, match_form, parse_when, tz_name, utcnow

PRODID = "-//icloud-calendar-mcp//EN"

_ROLE_TO_ICAL = {
    "required": "REQ-PARTICIPANT",
    "optional": "OPT-PARTICIPANT",
    "chair": "CHAIR",
    "non-participant": "NON-PARTICIPANT",
}
_ROLE_FROM_ICAL = {v: k for k, v in _ROLE_TO_ICAL.items()}
_WEEKDAYS = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}

# Properties that belong to a series master only; stripped when cloning into overrides.
SERIES_ONLY_PROPS = ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID")


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def new_calendar() -> icalendar.Calendar:
    cal = icalendar.Calendar()
    cal.add("PRODID", PRODID)
    cal.add("VERSION", "2.0")
    cal.add("CALSCALE", "GREGORIAN")
    return cal


def parse_calendar(data: str | bytes) -> icalendar.Calendar:
    return icalendar.Calendar.from_ical(data)


def serialize(cal: icalendar.Calendar) -> str:
    # Make sure every TZID used has a VTIMEZONE definition.
    try:
        cal.add_missing_timezones()
    except Exception:  # pragma: no cover - best effort, servers accept Olson TZIDs anyway
        pass
    return cal.to_ical().decode("utf-8")


def vevents(cal: icalendar.Calendar) -> list[icalendar.Event]:
    return [c for c in cal.subcomponents if c.name == "VEVENT"]


def master_of(cal: icalendar.Calendar) -> icalendar.Event | None:
    for ev in vevents(cal):
        if "RECURRENCE-ID" not in ev:
            return ev
    return None


def overrides_of(cal: icalendar.Calendar) -> list[icalendar.Event]:
    return [ev for ev in vevents(cal) if "RECURRENCE-ID" in ev]


def new_uid() -> str:
    return str(uuid.uuid4()).upper()


def _text(comp: icalendar.Component, name: str) -> str | None:
    value = comp.get(name)
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0]
    return str(value)


def _dt(comp: icalendar.Component, name: str) -> date | datetime | None:
    prop = comp.get(name)
    if prop is None:
        return None
    return prop.dt


def get_start(comp: icalendar.Component) -> date | datetime:
    start = _dt(comp, "DTSTART")
    if start is None:
        raise InvalidInput("Event has no DTSTART")
    return start


def get_end(comp: icalendar.Component) -> date | datetime:
    """Exclusive end, computed from DTEND / DURATION / defaults (RFC 5545 3.6.1)."""
    start = get_start(comp)
    end = _dt(comp, "DTEND")
    if end is not None:
        return end
    duration = comp.get("DURATION")
    if duration is not None:
        return start + duration.dt
    if isinstance(start, datetime):
        return start
    return start + timedelta(days=1)


def is_all_day(comp: icalendar.Component) -> bool:
    return not isinstance(get_start(comp), datetime)


def _set(comp: icalendar.Component, name: str, value: Any, **params: Any) -> None:
    comp.pop(name, None)
    if value is None:
        return
    comp.add(name, value, parameters=params or None)


def _pop_all(comp: icalendar.Component, *names: str) -> None:
    for name in names:
        while name in comp:
            comp.pop(name)


def _duration_minutes(value: Any) -> int | None:
    if isinstance(value, timedelta):
        return int(value.total_seconds() // 60)
    return None


def _strip_mailto(addr: str) -> str:
    return addr[7:] if addr.lower().startswith("mailto:") else addr


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read_location(comp: icalendar.Component) -> Location | None:
    text = _text(comp, "LOCATION")
    structured = comp.get("X-APPLE-STRUCTURED-LOCATION")
    if isinstance(structured, list):
        structured = structured[0]
    lat = lon = radius = None
    address = None
    title = None
    if structured is not None:
        raw = str(structured).replace("\\,", ",")
        if raw.lower().startswith("geo:"):
            try:
                lat_s, lon_s = raw[4:].split(";")[0].split(",")[:2]
                lat, lon = float(lat_s), float(lon_s)
            except ValueError:
                pass
        params = getattr(structured, "params", {})
        title = params.get("X-TITLE")
        address = params.get("X-ADDRESS")
        radius_s = params.get("X-APPLE-RADIUS")
        try:
            radius = float(radius_s) if radius_s else None
        except ValueError:
            radius = None
    if lat is None and "GEO" in comp:
        geo = comp["GEO"]
        lat, lon = geo.latitude, geo.longitude
    if not text and not title:
        return None
    if text and not title:
        # Apple stores "Title\nAddress" in LOCATION.
        first, _, rest = text.partition("\n")
        title = first
        address = address or (rest or None)
    return Location(title=title or text or "", address=address, latitude=lat, longitude=lon, radius_m=radius)


def read_alerts(comp: icalendar.Component, display_tz: tzinfo, duration: timedelta = timedelta(0)) -> list[Alert]:
    """Read VALARMs. ``duration`` (event length) converts end-relative triggers to start-relative."""
    alerts: list[Alert] = []
    for alarm in comp.subcomponents:
        if alarm.name != "VALARM":
            continue
        action = str(alarm.get("ACTION", "DISPLAY")).upper()
        if action == "NONE" or "X-APPLE-PROXIMITY" in alarm:
            continue
        trigger = alarm.get("TRIGGER")
        if trigger is None:
            continue
        value = trigger.dt
        if isinstance(value, timedelta):
            if str(trigger.params.get("RELATED", "START")).upper() == "END":
                value = duration + value
            alerts.append(Alert(minutes_before=int(round(-value.total_seconds() / 60))))
        elif isinstance(value, datetime):
            alerts.append(Alert(at=fmt(value, display_tz)))
    return alerts


def recurrence_from_vrecur(rule: vRecur) -> Recurrence | None:
    try:
        freq = str(rule["FREQ"][0]).lower()
        if freq not in {"daily", "weekly", "monthly", "yearly"}:
            return None

        def ints(key: str) -> list[int] | None:
            return [int(x) for x in rule[key]] if key in rule else None

        until = None
        if "UNTIL" in rule:
            u = rule["UNTIL"][0]
            until = u.isoformat() if not isinstance(u, datetime) else u.isoformat()
        return Recurrence(
            frequency=freq,  # type: ignore[arg-type]
            interval=int(rule.get("INTERVAL", [1])[0]),
            by_weekday=[str(d) for d in rule["BYDAY"]] if "BYDAY" in rule else None,
            by_month_day=ints("BYMONTHDAY"),
            by_month=ints("BYMONTH"),
            by_set_pos=ints("BYSETPOS"),
            count=int(rule["COUNT"][0]) if "COUNT" in rule else None,
            until=until,
            week_start=str(rule["WKST"][0]) if "WKST" in rule else None,
        )
    except (KeyError, ValueError, TypeError):
        return None


def read_attendee(addr: Any) -> Attendee:
    params = getattr(addr, "params", {})
    status = params.get("PARTSTAT")
    return Attendee(
        email=_strip_mailto(str(addr)),
        name=params.get("CN"),
        role=_ROLE_FROM_ICAL.get(str(params.get("ROLE", "REQ-PARTICIPANT")).upper(), "required"),  # type: ignore[arg-type]
        status=str(status).lower()
        if status and str(status).lower() in {"needs-action", "accepted", "declined", "tentative", "delegated"}
        else None,  # type: ignore[arg-type]
    )


def event_to_out(
    comp: icalendar.Event,
    *,
    event_id: str,
    calendar_id: str,
    calendar_name: str | None,
    display_tz: tzinfo,
    master: icalendar.Event | None = None,
) -> EventOut:
    """Convert a VEVENT (a master, a plain event, or an expanded occurrence) to ``EventOut``.

    ``master`` is the series master when ``comp`` is an occurrence; series-level data
    (RRULE, EXDATE) is read from it.
    """
    series = master if master is not None else comp
    start = get_start(comp)
    end = get_end(comp)
    all_day = not isinstance(start, datetime)
    if all_day:
        end_display = end - timedelta(days=1) if end > start else start  # inclusive last day
    else:
        end_display = end

    rrule = series.get("RRULE")
    if isinstance(rrule, list):
        rrule = rrule[0]
    exdates: list[str] = []
    ex = series.get("EXDATE")
    for item in ex if isinstance(ex, list) else ([ex] if ex is not None else []):
        exdates.extend(fmt(d.dt, display_tz) or "" for d in item.dts)

    rid = _dt(comp, "RECURRENCE-ID")
    is_recurring = rrule is not None or "RDATE" in series or rid is not None
    alerts = read_alerts(comp, display_tz, end - start)

    organizer = comp.get("ORGANIZER")
    attendees = comp.get("ATTENDEE")
    attendees = attendees if isinstance(attendees, list) else ([attendees] if attendees else [])

    cats: list[str] = []
    raw_cats = comp.get("CATEGORIES")
    for c in raw_cats if isinstance(raw_cats, list) else ([raw_cats] if raw_cats else []):
        cats.extend(str(x) for x in getattr(c, "cats", [c]))

    status = _text(comp, "STATUS")
    transp = (_text(comp, "TRANSP") or "OPAQUE").upper()
    created = _dt(comp, "CREATED")
    modified = _dt(comp, "LAST-MODIFIED")
    travel = comp.get("X-APPLE-TRAVEL-DURATION")
    travel_minutes = None
    if travel is not None:
        value = getattr(travel, "dt", None)
        if not isinstance(value, timedelta):
            raw = travel.to_ical() if hasattr(travel, "to_ical") else travel
            raw = raw.decode() if isinstance(raw, bytes) else str(raw)
            try:
                value = icalendar.vDuration.from_ical(raw)
            except (ValueError, TypeError):
                value = None
        travel_minutes = _duration_minutes(value)

    return EventOut(
        id=event_id,
        calendar_id=calendar_id,
        calendar_name=calendar_name,
        uid=str(comp.get("UID", "")),
        title=_text(comp, "SUMMARY"),
        start=fmt(start, display_tz) or "",
        end=fmt(end_display, display_tz) or "",
        all_day=all_day,
        timezone=tz_name(start),
        location=read_location(comp),
        notes=_text(comp, "DESCRIPTION"),
        url=_text(comp, "URL"),
        alerts=alerts,
        is_recurring=is_recurring,
        recurrence=recurrence_from_vrecur(rrule) if rrule is not None else None,
        rrule=rrule.to_ical().decode() if rrule is not None else None,
        excluded_dates=exdates,
        occurrence=fmt(rid, display_tz) if rid is not None else None,
        is_modified_occurrence=rid is not None and master is not None and _is_override(comp, master),
        availability="free" if transp == "TRANSPARENT" else "busy",
        status=status.lower() if status and status.lower() in {"confirmed", "tentative", "cancelled"} else None,  # type: ignore[arg-type]
        travel_time_minutes=travel_minutes,
        organizer=read_attendee(organizer) if organizer is not None else None,
        attendees=[read_attendee(a) for a in attendees],
        categories=cats,
        created=fmt(created, display_tz) if created else None,
        last_modified=fmt(modified, display_tz) if modified else None,
    )


def _is_override(occurrence: icalendar.Event, master: icalendar.Event) -> bool:
    """Whether an expanded occurrence came from an explicit RECURRENCE-ID component."""
    rid = _dt(occurrence, "RECURRENCE-ID")
    start = get_start(occurrence)
    if rid != start:
        return True
    return _text(occurrence, "SUMMARY") != _text(master, "SUMMARY") or (
        get_end(occurrence) - start != get_end(master) - get_start(master)
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def build_vrecur(rec: Recurrence, dtstart: date | datetime, tz: tzinfo) -> vRecur:
    rule: dict[str, Any] = {"FREQ": rec.frequency.value.upper()}
    if rec.interval and rec.interval != 1:
        rule["INTERVAL"] = rec.interval
    if rec.by_weekday:
        days = []
        for d in rec.by_weekday:
            d = d.strip().upper()
            if d[-2:] not in _WEEKDAYS:
                raise InvalidInput(f"Bad weekday {d!r}; use MO..SU, optionally with ordinal like '1MO' or '-1FR'.")
            days.append(d)
        rule["BYDAY"] = days
    if rec.by_month_day:
        rule["BYMONTHDAY"] = rec.by_month_day
    if rec.by_month:
        rule["BYMONTH"] = rec.by_month
    if rec.by_set_pos:
        rule["BYSETPOS"] = rec.by_set_pos
    if rec.count and rec.until:
        raise InvalidInput("Recurrence can have `count` or `until`, not both.")
    if rec.count:
        rule["COUNT"] = rec.count
    if rec.until:
        rule["UNTIL"] = until_value(parse_when(rec.until, tz), dtstart, tz)
    if rec.week_start:
        rule["WKST"] = rec.week_start.upper()
    return vRecur(rule)


def until_value(until: date | datetime, dtstart: date | datetime, tz: tzinfo) -> date | datetime:
    """RFC 5545: UNTIL must be a DATE if DTSTART is, and UTC if DTSTART is zoned."""
    if not isinstance(dtstart, datetime):
        return until.date() if isinstance(until, datetime) else until
    if not isinstance(until, datetime):
        # Inclusive date -> end of that day.
        until = datetime.combine(until, datetime.max.time().replace(microsecond=0), tzinfo=tz)
    if dtstart.tzinfo is None:
        return until.replace(tzinfo=None) if until.tzinfo is None else until.astimezone(tz).replace(tzinfo=None)
    from datetime import UTC

    return until.astimezone(UTC)


def parse_rrule(text: str, dtstart: date | datetime, tz: tzinfo) -> vRecur:
    text = text.strip()
    if text.upper().startswith("RRULE:"):
        text = text[6:]
    try:
        rule = vRecur.from_ical(text)
    except (ValueError, TypeError) as exc:
        raise InvalidInput(f"Invalid RRULE {text!r}: {exc}") from exc
    if "FREQ" not in rule:
        raise InvalidInput("RRULE must include FREQ")
    if "UNTIL" in rule:
        rule["UNTIL"] = [until_value(rule["UNTIL"][0], dtstart, tz)]
    return rule


def set_location(comp: icalendar.Component, loc: Location | str) -> None:
    _pop_all(comp, "LOCATION", "X-APPLE-STRUCTURED-LOCATION", "GEO")
    if isinstance(loc, str):
        if loc:
            comp.add("LOCATION", loc)
        return
    text = loc.title if not loc.address or loc.address == loc.title else f"{loc.title}\n{loc.address}"
    comp.add("LOCATION", text)
    if loc.latitude is not None and loc.longitude is not None:
        params: dict[str, str] = {"VALUE": "URI", "X-TITLE": loc.title}
        if loc.address:
            params["X-ADDRESS"] = loc.address
        params["X-APPLE-RADIUS"] = str(loc.radius_m if loc.radius_m is not None else 70)
        params["X-APPLE-REFERENCEFRAME"] = "1"
        prop = icalendar.vUri(f"geo:{loc.latitude},{loc.longitude}")  # URI: commas must not be escaped
        comp.add("X-APPLE-STRUCTURED-LOCATION", prop, parameters=params)
        comp.add("GEO", (loc.latitude, loc.longitude))


def set_alerts(comp: icalendar.Component, alerts: list[Alert], tz: tzinfo) -> None:
    comp.subcomponents = [c for c in comp.subcomponents if c.name != "VALARM"]
    for a in alerts:
        alarm = icalendar.Alarm()
        alarm.add("ACTION", "DISPLAY")
        alarm.add("DESCRIPTION", "Reminder")
        uid = new_uid()
        alarm.add("UID", uid)
        alarm.add("X-WR-ALARMUID", uid)
        if a.at:
            from datetime import UTC

            when = parse_when(a.at, tz)
            if not isinstance(when, datetime):
                raise InvalidInput("Alert `at` must be a date-time.")
            alarm.add("TRIGGER", when.astimezone(UTC))
        elif a.minutes_before is not None:
            alarm.add("TRIGGER", timedelta(minutes=-a.minutes_before))
        else:
            raise InvalidInput("Each alert needs `minutes_before` or `at`.")
        comp.add_component(alarm)


def set_attendees(comp: icalendar.Component, attendees: list[Attendee], organizer_email: str | None) -> None:
    _pop_all(comp, "ATTENDEE", "ORGANIZER")
    if not attendees:
        return
    if organizer_email:
        org = vCalAddress(f"mailto:{organizer_email}")
        comp.add("ORGANIZER", org)
        me = vCalAddress(f"mailto:{organizer_email}")
        me.params["PARTSTAT"] = "ACCEPTED"
        me.params["ROLE"] = "CHAIR"
        me.params["CUTYPE"] = "INDIVIDUAL"
        comp.add("ATTENDEE", me)
    for a in attendees:
        if organizer_email and a.email.lower() == organizer_email.lower():
            continue
        addr = vCalAddress(f"mailto:{a.email}")
        if a.name:
            addr.params["CN"] = a.name
        addr.params["ROLE"] = _ROLE_TO_ICAL[a.role]
        addr.params["PARTSTAT"] = (a.status or "needs-action").upper()
        addr.params["RSVP"] = "TRUE"
        addr.params["CUTYPE"] = "INDIVIDUAL"
        comp.add("ATTENDEE", addr)


def resolve_times(
    fields: _EventFields,
    tz: tzinfo,
    *,
    current_start: date | datetime | None = None,
    current_end: date | datetime | None = None,
) -> tuple[date | datetime, date | datetime] | None:
    """Work out the new (start, exclusive end) from the user's partial input.

    Returns None when no timing field was supplied.
    """
    touched = any(
        v is not None for v in (fields.start, fields.end, fields.duration_minutes, fields.all_day, fields.timezone)
    )
    if not touched:
        return None

    parsed_start = parse_when(fields.start, tz) if fields.start else None
    if fields.all_day is not None:
        all_day = fields.all_day
    elif parsed_start is not None:
        all_day = not isinstance(parsed_start, datetime)
    else:
        all_day = current_start is not None and not isinstance(current_start, datetime)

    # --- start ---
    if parsed_start is not None:
        start = parsed_start
    elif current_start is not None:
        start = current_start
        if fields.timezone and isinstance(start, datetime):
            # Keep the wall-clock time, move it to the new zone (as Calendar.app does).
            start = start.astimezone(start.tzinfo).replace(tzinfo=tz) if start.tzinfo else start.replace(tzinfo=tz)
    else:
        raise InvalidInput("`start` is required.")

    if all_day and isinstance(start, datetime):
        start = start.date()
    elif not all_day and not isinstance(start, datetime):
        raise InvalidInput("Timed events need a start time like '2026-11-03T15:00' (or set all_day=true).")

    # --- end ---
    if fields.end:
        end = parse_when(fields.end, tz)
        if all_day:
            end = (end.date() if isinstance(end, datetime) else end) + timedelta(days=1)
        elif not isinstance(end, datetime):
            raise InvalidInput("`end` must include a time for timed events.")
    elif fields.duration_minutes is not None:
        if all_day:
            end = start + timedelta(days=max(1, -(-fields.duration_minutes // 1440)))
        else:
            end = start + timedelta(minutes=fields.duration_minutes)
    elif (
        current_start is not None
        and current_end is not None
        and (isinstance(current_start, datetime) == isinstance(start, datetime))
    ):
        end = start + (current_end - current_start)  # keep duration
    else:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))

    if all_day:
        if end <= start:
            raise InvalidInput("All-day `end` (last day, inclusive) must not be before `start`.")
    elif end < start:
        raise InvalidInput("`end` must be after `start`.")
    return start, end


def apply_fields(
    comp: icalendar.Event,
    fields: _EventFields,
    *,
    default_tz: tzinfo,
    organizer_email: str | None = None,
    touch_series: bool = True,
) -> None:
    """Apply user-supplied fields to a VEVENT in place.

    ``touch_series`` is False for single-occurrence overrides, which must not carry RRULE/EXDATE.
    """
    current_start = _dt(comp, "DTSTART")
    tz = (
        get_tz(fields.timezone, default_tz)
        if fields.timezone
        else (current_start.tzinfo if isinstance(current_start, datetime) and current_start.tzinfo else default_tz)
    )
    if fields.title is not None:
        _set(comp, "SUMMARY", fields.title)

    times = resolve_times(
        fields,
        tz,
        current_start=current_start,
        current_end=get_end(comp) if current_start is not None else None,
    )
    if times is not None:
        start, end = times
        _pop_all(comp, "DTSTART", "DTEND", "DURATION")
        comp.add("DTSTART", start)
        comp.add("DTEND", end)  # always write DTEND: iCloud rejects events without one
        # Keep EXDATE / RDATE / UNTIL in the same form as the new DTSTART.
        if touch_series:
            _reform_series_dates(comp, start, tz)

    if fields.location is not None:
        set_location(comp, fields.location)
    if fields.notes is not None:
        _set(comp, "DESCRIPTION", fields.notes or None)
    if fields.url is not None:
        _set(comp, "URL", fields.url or None)
    if fields.alerts is not None:
        set_alerts(comp, fields.alerts, tz)
    if fields.availability is not None:
        _set(comp, "TRANSP", "TRANSPARENT" if fields.availability == "free" else "OPAQUE")
    if fields.status is not None:
        _set(comp, "STATUS", fields.status.upper())
    if fields.travel_time_minutes is not None:
        _pop_all(comp, "X-APPLE-TRAVEL-DURATION", "X-APPLE-TRAVEL-START", "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR")
        if fields.travel_time_minutes > 0:
            comp.add(
                "X-APPLE-TRAVEL-DURATION",
                icalendar.vDuration(timedelta(minutes=fields.travel_time_minutes)),
                parameters={"VALUE": "DURATION"},
            )
    if fields.attendees is not None:
        set_attendees(comp, fields.attendees, organizer_email)
    if fields.categories is not None:
        _pop_all(comp, "CATEGORIES")
        if fields.categories:
            comp.add("CATEGORIES", fields.categories)

    if touch_series:
        dtstart = get_start(comp)
        if fields.recurrence is not None and fields.rrule:
            raise InvalidInput("Give `recurrence` or `rrule`, not both.")
        if fields.recurrence is not None:
            _set(comp, "RRULE", build_vrecur(fields.recurrence, dtstart, tz))
        elif fields.rrule:
            _set(comp, "RRULE", parse_rrule(fields.rrule, dtstart, tz))
        if fields.excluded_dates is not None:
            _pop_all(comp, "EXDATE")
            if fields.excluded_dates:
                comp.add("EXDATE", [match_form(parse_when(d, tz), dtstart) for d in fields.excluded_dates])

    touch(comp)


def _reform_series_dates(comp: icalendar.Event, dtstart: date | datetime, tz: tzinfo) -> None:
    ex = comp.get("EXDATE")
    if ex is not None:
        items = ex if isinstance(ex, list) else [ex]
        values = [match_form(d.dt, dtstart) for item in items for d in item.dts]
        _pop_all(comp, "EXDATE")
        if values:
            comp.add("EXDATE", values)
    rule = comp.get("RRULE")
    if rule is not None and "UNTIL" in rule:
        rule["UNTIL"] = [until_value(rule["UNTIL"][0], dtstart, tz)]


def touch(comp: icalendar.Event, *, bump_sequence: bool = True) -> None:
    now = utcnow().replace(microsecond=0)
    _set(comp, "DTSTAMP", now)
    _set(comp, "LAST-MODIFIED", now)
    if "CREATED" not in comp:
        comp.add("CREATED", now)
    if bump_sequence:
        seq = int(comp.get("SEQUENCE", -1))
        _set(comp, "SEQUENCE", seq + 1)


def new_event(fields: _EventFields, *, default_tz: tzinfo, organizer_email: str | None = None) -> icalendar.Event:
    ev = icalendar.Event()
    ev.add("UID", new_uid())
    if fields.availability is None:
        fields = fields.model_copy(update={"availability": "busy"})
    apply_fields(ev, fields, default_tz=default_tz, organizer_email=organizer_email)
    return ev


def clone_for_override(master: icalendar.Event, recurrence_id: date | datetime) -> icalendar.Event:
    """Create a RECURRENCE-ID override that starts as a copy of the master's occurrence."""
    ev = icalendar.Event()
    master_start = get_start(master)
    duration = get_end(master) - master_start
    for name, value in master.property_items(recursive=False):
        if name in ("BEGIN", "END") or name in SERIES_ONLY_PROPS or name in ("DTSTART", "DTEND", "DURATION"):
            continue
        ev.add(name, value, encode=False)
    rid = match_form(recurrence_id, master_start)
    ev.add("RECURRENCE-ID", rid)
    ev.add("DTSTART", rid)
    ev.add("DTEND", rid + duration)
    for sub in master.subcomponents:
        if sub.name == "VALARM":
            ev.add_component(icalendar.Alarm.from_ical(sub.to_ical()))
    return ev
