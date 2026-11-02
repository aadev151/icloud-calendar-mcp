"""High-level calendar operations used by both the MCP server and the REST API."""

from __future__ import annotations

import asyncio
import copy
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Literal

import icalendar
import recurring_ical_events
from dateutil.rrule import rrulestr

from . import ical
from .caldav_client import NS, CalDAVClient, CalendarObject, _clark
from .config import Settings
from .errors import Forbidden, InvalidInput, NotFound
from .models import (
    BusyBlock,
    CalendarInfo,
    EventCreate,
    EventOut,
    EventPatch,
    Scope,
    TimeSlot,
)
from .timeutil import as_aware, fmt, get_tz, match_form, parse_datetime, parse_when, same_instant

APPLE_COLORS = {
    "red": "#FF2968",
    "orange": "#FF9500",
    "yellow": "#FFCC00",
    "green": "#63DA38",
    "blue": "#1BADF8",
    "purple": "#CC73E1",
    "brown": "#A2845E",
}
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$")
_CAL_CACHE_TTL = 300.0
_TIMING_FIELDS = {"start", "end", "duration_minutes", "all_day", "timezone"}
_SERIES_FIELDS = {"recurrence", "rrule", "excluded_dates", "remove_recurrence"}


def normalize_color(color: str) -> str:
    named = APPLE_COLORS.get(color.strip().lower())
    if named:
        return named + "FF"
    m = _HEX.match(color.strip())
    if not m:
        raise InvalidInput(f"Unknown color {color!r}: use #RRGGBB or one of {', '.join(APPLE_COLORS)}.")
    return f"#{m.group(1).upper()}{(m.group(2) or 'FF').upper()}"


def _display_color(raw: str | None) -> str | None:
    if not raw:
        return None
    m = _HEX.match(raw)
    if not m:
        return raw
    return f"#{m.group(1).upper()}"


@dataclass
class _Cal:
    info: CalendarInfo
    href: str


class CalendarService:
    def __init__(self, settings: Settings, client: CalDAVClient | None = None) -> None:
        self.settings = settings
        self.tz = settings.tz
        self._client = client
        self._home: str | None = None  # path of the calendar home, e.g. /123/calendars/
        self._addresses: list[str] = []
        self._cals: dict[str, _Cal] = {}
        self._cals_at = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- plumbing

    @property
    def client(self) -> CalDAVClient:
        if self._client is None:
            self._client = CalDAVClient(
                self.settings.caldav_url,
                self.settings.icloud_username,
                self.settings.icloud_app_password.get_secret_value(),
                timeout=self.settings.http_timeout,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _ensure_home(self) -> str:
        if self._home is None:
            async with self._lock:
                if self._home is None:
                    _, home_url, addresses = await self.client.discover()
                    self._addresses = addresses
                    self._home = self.client.path_of(home_url)
        return self._home

    @property
    def organizer_email(self) -> str | None:
        mails = [a[7:] for a in self._addresses if a.lower().startswith("mailto:")]
        user = self.settings.icloud_username.lower()
        for m in mails:
            if m.lower() == user:
                return m
        if mails:
            return mails[0]
        return self.settings.icloud_username if "@" in self.settings.icloud_username else None

    def _my_addresses(self) -> set[str]:
        out = {a[7:].lower() for a in self._addresses if a.lower().startswith("mailto:")}
        if self.settings.icloud_username:
            out.add(self.settings.icloud_username.lower())
        return out

    def _rel(self, href: str) -> str:
        """Server path -> compact id relative to the calendar home."""
        home = self._home or ""
        return href[len(home) :] if home and href.startswith(home) else href

    def _abs(self, ident: str) -> str:
        if ident.startswith("/") or ident.startswith("http"):
            return self.client.path_of(ident)
        return (self._home or "/") + ident.lstrip("/")

    # ------------------------------------------------------------ calendars

    async def _load_calendars(self, refresh: bool = False) -> dict[str, _Cal]:
        if not refresh and self._cals and time.monotonic() - self._cals_at < _CAL_CACHE_TTL:
            return self._cals
        home = await self._ensure_home()
        resources = await self.client.list_collections(home)
        cals: dict[str, _Cal] = {}
        for r in resources:
            rtype = r.props.get(_clark("d:resourcetype"))
            if rtype is None or rtype.find("c:calendar", NS) is None:
                continue
            href = r.href if r.href.endswith("/") else r.href + "/"
            comps_el = r.props.get(_clark("c:supported-calendar-component-set"))
            comps = [c.get("name") for c in comps_el.iterfind("c:comp", NS)] if comps_el is not None else []
            priv_el = r.props.get(_clark("d:current-user-privilege-set"))
            writable = True
            if priv_el is not None:
                names = {p[0].tag.split("}")[-1] for p in priv_el.iterfind("d:privilege", NS) if len(p)}
                writable = bool(names & {"all", "write", "write-content"})
            order = r.text(_clark("a:calendar-order"))
            cid = self._rel(href).rstrip("/")
            cals[cid] = _Cal(
                info=CalendarInfo(
                    id=cid,
                    name=r.text(_clark("d:displayname")) or cid,
                    color=_display_color(r.text(_clark("a:calendar-color"))),
                    description=r.text(_clark("c:calendar-description")),
                    order=int(order) if order and order.lstrip("-").isdigit() else None,
                    writable=writable,
                    components=[c for c in comps if c],
                ),
                href=href,
            )
        default = self._pick_default(cals)
        if default:
            default.info.is_default = True
        self._cals, self._cals_at = cals, time.monotonic()
        return cals

    def _pick_default(self, cals: dict[str, _Cal]) -> _Cal | None:
        want = self.settings.default_calendar
        if want:
            for c in cals.values():
                if want in (c.info.id, c.info.name) or c.info.name.lower() == want.lower():
                    return c
        candidates = [c for c in cals.values() if c.info.writable and self._supports_events(c)]
        candidates.sort(key=lambda c: (c.info.order if c.info.order is not None else 1 << 30, c.info.name))
        return candidates[0] if candidates else None

    @staticmethod
    def _supports_events(c: _Cal) -> bool:
        return not c.info.components or "VEVENT" in c.info.components

    async def list_calendars(self) -> list[CalendarInfo]:
        cals = await self._load_calendars(refresh=True)
        return sorted(
            (c.info for c in cals.values()),
            key=lambda i: (i.order if i.order is not None else 1 << 30, i.name.lower()),
        )

    async def _calendar(self, ref: str | None) -> _Cal:
        cals = await self._load_calendars()
        if ref is None:
            default = next((c for c in cals.values() if c.info.is_default), None)
            if default is None:
                raise NotFound("No writable calendar found.")
            return default
        found = self._match_calendar(cals, ref)
        if found is None:
            cals = await self._load_calendars(refresh=True)
            found = self._match_calendar(cals, ref)
        if found is None:
            names = ", ".join(f"{c.info.name!r}" for c in cals.values())
            raise NotFound(f"No calendar {ref!r}. Available: {names}")
        return found

    @staticmethod
    def _match_calendar(cals: dict[str, _Cal], ref: str) -> _Cal | None:
        key = ref.strip().strip("/")
        if key in cals:
            return cals[key]
        for c in cals.values():
            if c.href.rstrip("/") == "/" + key or c.href.rstrip("/").endswith("/" + key):
                return c
        matches = [c for c in cals.values() if c.info.name.lower() == key.lower()]
        return matches[0] if len(matches) == 1 else None

    async def _calendar_for_href(self, href: str) -> _Cal:
        parent = href.rsplit("/", 1)[0] + "/"
        cals = await self._load_calendars()
        for c in cals.values():
            if c.href == parent:
                return c
        cals = await self._load_calendars(refresh=True)
        for c in cals.values():
            if c.href == parent:
                return c
        raise NotFound(f"Calendar for event {self._rel(href)!r} not found.")

    async def create_calendar(
        self, name: str, color: str | None = None, description: str | None = None
    ) -> CalendarInfo:
        home = await self._ensure_home()
        href = f"{home}{uuid.uuid4().hex.upper()}/"
        await self.client.mkcalendar(
            href, name, color=normalize_color(color) if color else None, description=description
        )
        cals = await self._load_calendars(refresh=True)
        cid = self._rel(href).rstrip("/")
        if cid not in cals:
            raise NotFound("Calendar was created but could not be read back.")
        return cals[cid].info

    async def update_calendar(
        self,
        calendar: str,
        name: str | None = None,
        color: str | None = None,
        description: str | None = None,
        order: int | None = None,
    ) -> CalendarInfo:
        cal = await self._calendar(calendar)
        props: dict[str, str] = {}
        if name is not None:
            props["d:displayname"] = name
        if color is not None:
            props["a:calendar-color"] = normalize_color(color)
        if description is not None:
            props["c:calendar-description"] = description
        if order is not None:
            props["a:calendar-order"] = str(order)
        if not props:
            raise InvalidInput("Nothing to update.")
        await self.client.proppatch(cal.href, props)
        cals = await self._load_calendars(refresh=True)
        return cals[cal.info.id].info

    async def delete_calendar(self, calendar: str) -> CalendarInfo:
        cal = await self._calendar(calendar)
        await self.client.delete(cal.href)
        await self._load_calendars(refresh=True)
        return cal.info

    # --------------------------------------------------------------- events

    def _to_out(self, comp: icalendar.Event, href: str, cal: _Cal, master: icalendar.Event | None = None) -> EventOut:
        return ical.event_to_out(
            comp,
            event_id=self._rel(href),
            calendar_id=cal.info.id,
            calendar_name=cal.info.name,
            display_tz=self.tz,
            master=master,
        )

    def _expand(
        self, obj: CalendarObject, cal: _Cal, start: datetime, end: datetime
    ) -> list[tuple[datetime, EventOut]]:
        try:
            vcal = ical.parse_calendar(obj.data)
        except ValueError:
            return []
        master = ical.master_of(vcal)
        out = []
        try:
            occurrences = recurring_ical_events.of(vcal).between(start, end)
        except Exception:  # malformed recurrence: fall back to raw components
            occurrences = ical.vevents(vcal)
        recurring = master is not None and ("RRULE" in master or "RDATE" in master)
        for occ in occurrences:
            if recurring or master is None:
                ev = self._to_out(occ, obj.href, cal, master=master)
            else:
                # recurring_ical_events stamps a RECURRENCE-ID on plain events too; use the original.
                ev = self._to_out(master, obj.href, cal)
            out.append((as_aware(ical.get_start(occ), self.tz), ev))
        return out

    async def list_events(
        self,
        start: str,
        end: str,
        calendars: list[str] | None = None,
        query: str | None = None,
        limit: int = 500,
    ) -> list[EventOut]:
        s = parse_datetime(start, self.tz)
        e = parse_datetime(end, self.tz)
        if isinstance(parse_when(end, self.tz), date) and not isinstance(parse_when(end, self.tz), datetime):
            e += timedelta(days=1)  # a bare end date is inclusive
        if e <= s:
            raise InvalidInput("`end` must be after `start`.")
        if calendars:
            targets = [await self._calendar(c) for c in calendars]
        else:
            targets = [c for c in (await self._load_calendars()).values() if self._supports_events(c)]

        async def one(cal: _Cal) -> list[tuple[datetime, EventOut]]:
            objs = await self.client.query_events(cal.href, s, e)
            rows: list[tuple[datetime, EventOut]] = []
            for obj in objs:
                rows.extend(self._expand(obj, cal, s, e))
            return rows

        results = await asyncio.gather(*(one(c) for c in targets))
        rows = [r for chunk in results for r in chunk]
        if query:
            q = query.lower()
            rows = [
                r
                for r in rows
                if q
                in " ".join(
                    filter(
                        None,
                        [
                            r[1].title,
                            r[1].notes,
                            r[1].location.title if r[1].location else None,
                            r[1].location.address if r[1].location else None,
                        ],
                    )
                ).lower()
            ]
        rows.sort(key=lambda r: (r[0], r[1].title or ""))
        return [r[1] for r in rows[:limit]]

    async def _load(self, event_id: str) -> tuple[CalendarObject, icalendar.Calendar, _Cal]:
        href = self._abs(event_id)
        if not href.endswith(".ics"):
            raise InvalidInput(f"{event_id!r} is not an event id (expected something like 'home/ABC.ics').")
        obj = await self.client.get(href)
        cal = await self._calendar_for_href(obj.href)
        return obj, ical.parse_calendar(obj.data), cal

    def _parse_occurrence(self, occurrence: str, template: date | datetime) -> date | datetime:
        tz = template.tzinfo if isinstance(template, datetime) and template.tzinfo else self.tz
        return match_form(parse_when(occurrence, tz), template)

    async def get_event(self, event_id: str, occurrence: str | None = None) -> EventOut:
        obj, vcal, cal = await self._load(event_id)
        master = ical.master_of(vcal)
        if occurrence is None:
            comp = master or ical.vevents(vcal)[0]
            return self._to_out(comp, obj.href, cal)
        template = ical.get_start(master or ical.vevents(vcal)[0])
        rid = self._parse_occurrence(occurrence, template)
        around = as_aware(rid, self.tz)
        for occ in recurring_ical_events.of(vcal).between(around - timedelta(days=1), around + timedelta(days=2)):
            occ_rid = occ.get("RECURRENCE-ID")
            if occ_rid is not None and same_instant(occ_rid.dt, rid, self.tz):
                return self._to_out(occ, obj.href, cal, master=master)
        raise NotFound(f"Event {event_id!r} has no occurrence at {occurrence}.")

    async def create_event(self, data: EventCreate) -> EventOut:
        cal = await self._calendar(data.calendar)
        if not cal.info.writable:
            raise Forbidden(f"Calendar {cal.info.name!r} is read-only.")
        default_tz = get_tz(data.timezone, self.tz)
        await self._ensure_home()
        comp = ical.new_event(data, default_tz=default_tz, organizer_email=self.organizer_email)
        vcal = ical.new_calendar()
        vcal.add_component(comp)
        href = f"{cal.href}{comp['UID']}.ics"
        await self.client.put(href, ical.serialize(vcal), create=True)
        return self._to_out(comp, href, cal)

    async def _save(self, obj: CalendarObject, vcal: icalendar.Calendar) -> None:
        await self.client.put(obj.href, ical.serialize(vcal), etag=obj.etag)

    async def update_event(
        self,
        event_id: str,
        patch: EventPatch,
        occurrence: str | None = None,
        scope: Scope = Scope.all,
    ) -> EventOut:
        obj, vcal, cal = await self._load(event_id)
        master = ical.master_of(vcal)
        fields_set = patch.model_fields_set
        if master is None:
            # Only detached occurrences are stored (e.g. you were invited to one instance).
            comp = ical.vevents(vcal)[0]
            ical.apply_fields(comp, patch, default_tz=self.tz, organizer_email=self.organizer_email, touch_series=False)
            await self._save(obj, vcal)
            return self._to_out(comp, obj.href, cal)

        recurring = "RRULE" in master or "RDATE" in master
        if not recurring or occurrence is None:
            scope = Scope.all
        if scope == Scope.this and fields_set & _SERIES_FIELDS:
            raise InvalidInput("Recurrence can only be changed with scope='all' or 'future'.")

        if scope == Scope.all:
            self._apply_to_series(vcal, master, patch, occurrence)
            await self._save(obj, vcal)
            return self._to_out(master, obj.href, cal)

        rid = self._parse_occurrence(occurrence, ical.get_start(master))  # type: ignore[arg-type]

        if scope == Scope.this:
            override = self._find_override(vcal, rid)
            if override is None:
                override = ical.clone_for_override(master, rid)
                vcal.add_component(override)
            ical.apply_fields(
                override, patch, default_tz=self.tz, organizer_email=self.organizer_email, touch_series=False
            )
            await self._save(obj, vcal)
            return self._to_out(override, obj.href, cal, master=master)

        # scope == future
        if same_instant(rid, ical.get_start(master), self.tz):
            self._apply_to_series(vcal, master, patch, None)
            await self._save(obj, vcal)
            return self._to_out(master, obj.href, cal)
        new_vcal = self._split_series(vcal, master, rid)
        new_master = ical.master_of(new_vcal)
        assert new_master is not None
        self._apply_to_series(new_vcal, new_master, patch, None)
        new_href = f"{cal.href}{new_master['UID']}.ics"
        await self._save(obj, vcal)
        await self.client.put(new_href, ical.serialize(new_vcal), create=True)
        return self._to_out(new_master, new_href, cal)

    def _apply_to_series(
        self,
        vcal: icalendar.Calendar,
        master: icalendar.Event,
        patch: EventPatch,
        occurrence: str | None,
    ) -> None:
        old_start = ical.get_start(master)
        fields_set = patch.model_fields_set
        timing = fields_set & _TIMING_FIELDS
        if patch.remove_recurrence:
            for name in ical.SERIES_ONLY_PROPS:
                while name in master:
                    master.pop(name)
            vcal.subcomponents = [c for c in vcal.subcomponents if not (c.name == "VEVENT" and "RECURRENCE-ID" in c)]

        if timing and occurrence is not None and ("RRULE" in master or "RDATE" in master):
            # The user edited an occurrence's time "for all events": shift the series by the same delta.
            rid = self._parse_occurrence(occurrence, old_start)
            occ_end = rid + (ical.get_end(master) - old_start)
            times = ical.resolve_times(patch, self._tz_of(master), current_start=rid, current_end=occ_end)
            assert times is not None
            new_s, new_e = times
            if isinstance(new_s, datetime) != isinstance(old_start, datetime):
                raise InvalidInput("To switch a whole series between all-day and timed, omit `occurrence`.")
            delta = new_s - rid
            ical.apply_fields(
                master,
                patch.model_copy(update={k: None for k in _TIMING_FIELDS}),
                default_tz=self.tz,
                organizer_email=self.organizer_email,
            )
            master.pop("DTSTART")
            master.pop("DTEND", None)
            master.pop("DURATION", None)
            master.add("DTSTART", old_start + delta)
            master.add("DTEND", old_start + delta + (new_e - new_s))
        else:
            ical.apply_fields(master, patch, default_tz=self.tz, organizer_email=self.organizer_email)

        new_start = ical.get_start(master)
        overrides = ical.overrides_of(vcal)
        if overrides and type(new_start) is type(old_start) and new_start != old_start:
            delta = new_start - old_start
            self._shift_series_dates(vcal, master, delta)
        elif overrides and type(new_start) is not type(old_start):
            vcal.subcomponents = [c for c in vcal.subcomponents if not (c.name == "VEVENT" and "RECURRENCE-ID" in c)]

        # Propagate non-timing edits to detached occurrences as well.
        other = patch.model_copy(update={k: None for k in _TIMING_FIELDS | {"recurrence", "rrule", "excluded_dates"}})
        if fields_set - _TIMING_FIELDS - _SERIES_FIELDS:
            for ov in ical.overrides_of(vcal):
                ical.apply_fields(
                    ov, other, default_tz=self.tz, organizer_email=self.organizer_email, touch_series=False
                )

    def _shift_series_dates(self, vcal: icalendar.Calendar, master: icalendar.Event, delta: timedelta) -> None:
        start = ical.get_start(master)
        ex = master.get("EXDATE")
        if ex is not None:
            items = ex if isinstance(ex, list) else [ex]
            vals = [match_form(d.dt + delta, start) for it in items for d in it.dts]
            while "EXDATE" in master:
                master.pop("EXDATE")
            master.add("EXDATE", vals)
        for ov in ical.overrides_of(vcal):
            rid = ov["RECURRENCE-ID"].dt
            ov.pop("RECURRENCE-ID")
            ov.add("RECURRENCE-ID", match_form(rid + delta, start))

    def _tz_of(self, comp: icalendar.Event):
        s = ical.get_start(comp)
        return s.tzinfo if isinstance(s, datetime) and s.tzinfo else self.tz

    def _find_override(self, vcal: icalendar.Calendar, rid: date | datetime) -> icalendar.Event | None:
        for ov in ical.overrides_of(vcal):
            if same_instant(ov["RECURRENCE-ID"].dt, rid, self.tz):
                return ov
        return None

    def _truncate_series(self, vcal: icalendar.Calendar, master: icalendar.Event, rid: date | datetime) -> int | None:
        """End the series right before ``rid``. Returns the remaining COUNT, if the rule had one."""
        start = ical.get_start(master)
        rule = master.get("RRULE")
        remaining = None
        if rule is not None:
            if "COUNT" in rule:
                total = int(rule["COUNT"][0])
                # ``rid`` has the same form as DTSTART (see _parse_occurrence), so they compare.
                dstart = start if isinstance(start, datetime) else datetime.combine(start, dtime.min)
                rid_dt = rid if isinstance(rid, datetime) else datetime.combine(rid, dtime.min)
                before = 0
                for d in rrulestr(rule.to_ical().decode(), dtstart=dstart):
                    if d >= rid_dt:
                        break
                    before += 1
                remaining = max(1, total - before)
                del rule["COUNT"]
            if isinstance(start, datetime):
                until = rid - timedelta(seconds=1)
            else:
                until = rid - timedelta(days=1)
            rule["UNTIL"] = [ical.until_value(until, start, self._tz_of(master))]
        # Drop exceptions at/after the split from the old series.
        ex = master.get("EXDATE")
        if ex is not None:
            items = ex if isinstance(ex, list) else [ex]
            keep = [d.dt for it in items for d in it.dts if as_aware(d.dt, self.tz) < as_aware(rid, self.tz)]
            while "EXDATE" in master:
                master.pop("EXDATE")
            if keep:
                master.add("EXDATE", keep)
        if "RDATE" in master:
            items = master.get("RDATE")
            items = items if isinstance(items, list) else [items]
            keep = [d.dt for it in items for d in it.dts if as_aware(d.dt, self.tz) < as_aware(rid, self.tz)]
            while "RDATE" in master:
                master.pop("RDATE")
            if keep:
                master.add("RDATE", keep)
        vcal.subcomponents = [
            c
            for c in vcal.subcomponents
            if not (
                c.name == "VEVENT"
                and "RECURRENCE-ID" in c
                and as_aware(c["RECURRENCE-ID"].dt, self.tz) >= as_aware(rid, self.tz)
            )
        ]
        ical.touch(master)
        return remaining

    def _split_series(
        self, vcal: icalendar.Calendar, master: icalendar.Event, rid: date | datetime
    ) -> icalendar.Calendar:
        """Split a series at ``rid``: ``vcal`` keeps the past, the returned calendar holds the future."""
        start = ical.get_start(master)
        duration = ical.get_end(master) - start
        future_overrides = [
            copy.deepcopy(c)
            for c in ical.overrides_of(vcal)
            if as_aware(c["RECURRENCE-ID"].dt, self.tz) >= as_aware(rid, self.tz)
        ]
        new_master = copy.deepcopy(master)
        remaining = self._truncate_series(vcal, master, rid)

        uid = ical.new_uid()
        new_master.pop("UID")
        new_master.add("UID", uid)
        for name in ("DTSTART", "DTEND", "DURATION"):
            new_master.pop(name, None)
        new_master.add("DTSTART", rid)
        new_master.add("DTEND", rid + duration)
        rule = new_master.get("RRULE")
        if rule is not None and remaining is not None:
            rule.pop("UNTIL", None)
            rule["COUNT"] = [remaining]
        ex = new_master.get("EXDATE")
        if ex is not None:
            items = ex if isinstance(ex, list) else [ex]
            keep = [d.dt for it in items for d in it.dts if as_aware(d.dt, self.tz) >= as_aware(rid, self.tz)]
            while "EXDATE" in new_master:
                new_master.pop("EXDATE")
            if keep:
                new_master.add("EXDATE", keep)
        new_master.pop("CREATED", None)
        new_master.pop("SEQUENCE", None)
        ical.touch(new_master)

        new_vcal = ical.new_calendar()
        new_vcal.add_component(new_master)
        for ov in future_overrides:
            ov.pop("UID")
            ov.add("UID", uid)
            new_vcal.add_component(ov)
        return new_vcal

    async def delete_event(self, event_id: str, occurrence: str | None = None, scope: Scope = Scope.all) -> str:
        obj, vcal, cal = await self._load(event_id)
        master = ical.master_of(vcal)
        recurring = master is not None and ("RRULE" in master or "RDATE" in master)
        if not recurring or occurrence is None or scope == Scope.all:
            await self.client.delete(obj.href, etag=obj.etag)
            return "Deleted the event" + (" (all occurrences)." if recurring else ".")
        assert master is not None
        start = ical.get_start(master)
        rid = self._parse_occurrence(occurrence, start)
        if scope == Scope.this:
            existing = self._find_override(vcal, rid)
            if existing is not None:
                vcal.subcomponents.remove(existing)
            ical.touch(master)
            master.add("EXDATE", [rid])
            await self._save(obj, vcal)
            return f"Deleted the occurrence on {fmt(rid, self.tz)}."
        # future
        if as_aware(rid, self.tz) <= as_aware(start, self.tz):
            await self.client.delete(obj.href, etag=obj.etag)
            return "Deleted the whole series (the occurrence was the first one)."
        self._truncate_series(vcal, master, rid)
        await self._save(obj, vcal)
        return f"Deleted all occurrences from {fmt(rid, self.tz)} onward."

    async def move_event(self, event_id: str, calendar: str) -> EventOut:
        obj, vcal, src = await self._load(event_id)
        dst = await self._calendar(calendar)
        if dst.href == src.href:
            raise InvalidInput("Event is already in that calendar.")
        if not dst.info.writable:
            raise Forbidden(f"Calendar {dst.info.name!r} is read-only.")
        filename = obj.href.rsplit("/", 1)[1]
        new_href = f"{dst.href}{filename}"
        # iCloud refuses the same UID in two calendars, so delete first and restore on failure.
        await self.client.delete(obj.href, etag=obj.etag)
        try:
            await self.client.put(new_href, obj.data, create=True)
        except Exception:
            await self.client.put(obj.href, obj.data, create=True)
            raise
        comp = ical.master_of(vcal) or ical.vevents(vcal)[0]
        return self._to_out(comp, new_href, dst)

    async def respond_to_invite(
        self,
        event_id: str,
        response: Literal["accepted", "declined", "tentative"],
        occurrence: str | None = None,
    ) -> EventOut:
        obj, vcal, cal = await self._load(event_id)
        await self._ensure_home()
        mine = self._my_addresses()
        master = ical.master_of(vcal)
        if occurrence is not None and master is not None:
            rid = self._parse_occurrence(occurrence, ical.get_start(master))
            target = self._find_override(vcal, rid)
            if target is None:
                target = ical.clone_for_override(master, rid)
                vcal.add_component(target)
            targets = [target]
        else:
            targets = ical.vevents(vcal)
        found = False
        for comp in targets:
            attendees = comp.get("ATTENDEE")
            attendees = attendees if isinstance(attendees, list) else ([attendees] if attendees else [])
            for a in attendees:
                if str(a).lower().removeprefix("mailto:") in mine:
                    a.params["PARTSTAT"] = response.upper()
                    a.params.pop("RSVP", None)
                    found = True
            ical.touch(comp, bump_sequence=False)
        if not found:
            raise NotFound("You are not listed as an attendee of this event.")
        await self._save(obj, vcal)
        out_comp = targets[0] if occurrence else (master or targets[0])
        return self._to_out(out_comp, obj.href, cal, master=master if occurrence else None)

    # --------------------------------------------------------- availability

    async def busy_blocks(self, start: str, end: str, calendars: list[str] | None = None) -> list[BusyBlock]:
        events = await self.list_events(start, end, calendars, limit=100_000)
        blocks = []
        for ev in events:
            if ev.availability == "free" or ev.status == "cancelled":
                continue
            s, e = self._event_span(ev)
            if ev.travel_time_minutes:
                s -= timedelta(minutes=ev.travel_time_minutes)
            blocks.append(
                BusyBlock(
                    start=fmt(s, self.tz) or "",
                    end=fmt(e, self.tz) or "",
                    minutes=int((e - s).total_seconds() // 60),
                    title=ev.title,
                    calendar_name=ev.calendar_name,
                    event_id=ev.id,
                )
            )
        return blocks

    def _event_span(self, ev: EventOut) -> tuple[datetime, datetime]:
        s = parse_datetime(ev.start, self.tz)
        e = parse_datetime(ev.end, self.tz)
        if ev.all_day:
            e += timedelta(days=1)
        return s, e

    async def find_free_slots(
        self,
        start: str,
        end: str,
        duration_minutes: int = 30,
        day_start: str = "09:00",
        day_end: str = "17:00",
        weekdays_only: bool = False,
        calendars: list[str] | None = None,
        timezone: str | None = None,
    ) -> list[TimeSlot]:
        tz = get_tz(timezone, self.tz)
        s = parse_datetime(start, tz)
        e = parse_datetime(end, tz)
        if isinstance(parse_when(end, tz), date) and not isinstance(parse_when(end, tz), datetime):
            e += timedelta(days=1)
        busy = [self._event_span_from_block(b) for b in await self.busy_blocks(start, end, calendars)]
        busy.sort()
        try:
            ds = datetime.strptime(day_start, "%H:%M").time()
            de = datetime.strptime(day_end, "%H:%M").time()
        except ValueError as exc:
            raise InvalidInput("day_start/day_end must be HH:MM.") from exc
        need = timedelta(minutes=duration_minutes)
        slots: list[TimeSlot] = []
        day = s.astimezone(tz).date()
        while day <= e.astimezone(tz).date():
            if not (weekdays_only and day.weekday() >= 5):
                win_s = max(datetime.combine(day, ds, tzinfo=tz), s)
                win_e = min(datetime.combine(day, de, tzinfo=tz), e)
                cursor = win_s
                for bs, be in busy:
                    if be <= cursor or bs >= win_e:
                        continue
                    if bs - cursor >= need:
                        slots.append(self._slot(cursor, bs, tz))
                    cursor = max(cursor, be)
                if win_e - cursor >= need:
                    slots.append(self._slot(cursor, win_e, tz))
            day += timedelta(days=1)
        return slots

    def _event_span_from_block(self, b: BusyBlock) -> tuple[datetime, datetime]:
        return parse_datetime(b.start, self.tz), parse_datetime(b.end, self.tz)

    @staticmethod
    def _slot(s: datetime, e: datetime, tz) -> TimeSlot:
        return TimeSlot(start=fmt(s, tz) or "", end=fmt(e, tz) or "", minutes=int((e - s).total_seconds() // 60))

    def now(self) -> dict[str, str]:
        n = datetime.now(self.tz)
        return {
            "now": n.isoformat(timespec="seconds"),
            "timezone": self.settings.default_timezone,
            "weekday": n.strftime("%A"),
            "utc_offset": n.strftime("%z"),
        }
