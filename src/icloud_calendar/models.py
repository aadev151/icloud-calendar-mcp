"""Public data models shared by the MCP tools and the REST API.

Date/time conventions (also stated in tool descriptions so the model gets them right):

* Timed values are ISO-8601 strings. A value without an offset (``2026-11-03T15:00``)
  is interpreted in the event's ``timezone`` (or the server default timezone).
* All-day values are plain dates (``2026-11-03``). For all-day events ``end`` is the
  **last day of the event, inclusive** (a one-day event has start == end).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Scope(StrEnum):
    """Which occurrences of a recurring event an edit/delete applies to."""

    this = "this"
    future = "future"
    all = "all"


class Frequency(StrEnum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"
    yearly = "yearly"


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


class Location(BaseModel):
    """An event location. Only ``title`` is required.

    Supplying ``latitude``/``longitude`` makes Apple Calendar show a map and enables
    travel-time features.
    """

    title: str = Field(description="Display name, e.g. 'Blue Bottle Coffee' or a full address.")
    address: str | None = Field(default=None, description="Street address, if different from title.")
    latitude: float | None = None
    longitude: float | None = None
    radius_m: float | None = Field(default=None, description="Geofence radius in metres.")


class Alert(BaseModel):
    """An alert (notification). Give exactly one of ``minutes_before`` or ``at``."""

    minutes_before: int | None = Field(
        default=None,
        description="Minutes before the event start (0 = at start time, negative = after start). "
        "For all-day events this is relative to midnight at the start of the day, "
        "e.g. 900 = 9:00 the day before, -540 = 9:00 on the day.",
    )
    at: str | None = Field(default=None, description="Absolute ISO-8601 time for the alert.")
    sound: bool = Field(default=True, description="Play a sound (AUDIO) vs silent (DISPLAY).")


class Recurrence(BaseModel):
    """A structured recurrence rule (RFC 5545 RRULE)."""

    frequency: Frequency
    interval: int = Field(default=1, ge=1, description="Every N days/weeks/months/years.")
    by_weekday: list[str] | None = Field(
        default=None,
        description="Weekdays as MO,TU,WE,TH,FR,SA,SU, optionally with ordinal for monthly/yearly: "
        "'1MO' = first Monday, '-1FR' = last Friday.",
    )
    by_month_day: list[int] | None = Field(default=None, description="Days of month, e.g. [1, 15, -1].")
    by_month: list[int] | None = Field(default=None, description="Months 1-12 (for yearly rules).")
    by_set_pos: list[int] | None = Field(
        default=None, description="Select the Nth match within the period, e.g. [-1] for 'last weekday'."
    )
    count: int | None = Field(default=None, ge=1, description="Stop after this many occurrences.")
    until: str | None = Field(default=None, description="Last possible date/time (inclusive).")
    week_start: str | None = Field(default=None, description="WKST, e.g. 'MO' or 'SU'.")


class Attendee(BaseModel):
    email: str
    name: str | None = None
    role: Literal["required", "optional", "chair", "non-participant"] = "required"
    status: Literal["needs-action", "accepted", "declined", "tentative", "delegated"] | None = None


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class CalendarInfo(BaseModel):
    id: str
    name: str
    color: str | None = None
    description: str | None = None
    order: int | None = None
    writable: bool = True
    components: list[str] = Field(default_factory=list, description="e.g. ['VEVENT'] or ['VTODO'].")
    is_default: bool = False


class EventOut(BaseModel):
    id: str = Field(description="Stable id of the event resource (use with get/update/delete).")
    calendar_id: str
    calendar_name: str | None = None
    uid: str
    title: str | None = None
    start: str
    end: str
    all_day: bool = False
    timezone: str | None = None
    location: Location | None = None
    notes: str | None = None
    url: str | None = None
    alerts: list[Alert] = Field(default_factory=list)
    is_recurring: bool = False
    recurrence: Recurrence | None = None
    rrule: str | None = Field(default=None, description="Raw RRULE string.")
    excluded_dates: list[str] = Field(default_factory=list)
    occurrence: str | None = Field(
        default=None,
        description="For an occurrence of a recurring event: its original start "
        "(pass as `occurrence` to edit/delete just this or future occurrences).",
    )
    is_modified_occurrence: bool = False
    availability: Literal["busy", "free"] = "busy"
    status: Literal["confirmed", "tentative", "cancelled"] | None = None
    travel_time_minutes: int | None = None
    organizer: Attendee | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    created: str | None = None
    last_modified: str | None = None


class TimeSlot(BaseModel):
    start: str
    end: str
    minutes: int


class BusyBlock(TimeSlot):
    title: str | None = None
    calendar_name: str | None = None
    event_id: str | None = None


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class _EventFields(BaseModel):
    title: str | None = None
    start: str | None = Field(default=None, description="ISO datetime, or date for all-day events.")
    end: str | None = Field(default=None, description="ISO datetime, or (all-day) the last day inclusive.")
    duration_minutes: int | None = Field(default=None, ge=0, description="Alternative to `end`.")
    all_day: bool | None = None
    timezone: str | None = Field(default=None, description="IANA name, e.g. 'Europe/London'.")
    location: Location | str | None = Field(
        default=None, description="A string, or a structured location. Empty string clears it."
    )
    notes: str | None = Field(default=None, description="Event notes/description. Empty string clears.")
    url: str | None = Field(default=None, description="Empty string clears.")
    alerts: list[Alert] | None = Field(default=None, description="Replaces all alerts. [] removes all.")
    recurrence: Recurrence | None = None
    rrule: str | None = Field(
        default=None, description="Raw RRULE (e.g. 'FREQ=WEEKLY;BYDAY=MO,WE') - alternative to `recurrence`."
    )
    excluded_dates: list[str] | None = Field(
        default=None, description="Occurrence starts to skip (EXDATE). Replaces existing list."
    )
    availability: Literal["busy", "free"] | None = None
    status: Literal["confirmed", "tentative", "cancelled"] | None = None
    travel_time_minutes: int | None = Field(default=None, ge=0, description="0 removes travel time.")
    attendees: list[Attendee] | None = Field(
        default=None, description="Invitees. iCloud emails invitations. Replaces the existing list."
    )
    categories: list[str] | None = None


class EventCreate(_EventFields):
    calendar: str | None = Field(default=None, description="Calendar id or name (default calendar if omitted).")
    title: str
    start: str


class EventPatch(_EventFields):
    remove_recurrence: bool = Field(default=False, description="Turn a repeating event into a single event.")
