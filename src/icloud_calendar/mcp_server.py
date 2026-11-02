"""MCP tool definitions. Thin wrappers around :class:`CalendarService`."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, ParamSpec, TypeVar

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .auth import SCOPE, OwnerOAuthProvider
from .config import Settings
from .errors import CalendarError
from .models import Alert, Attendee, EventCreate, EventPatch, Location, Recurrence, Scope
from .service import CalendarService

INSTRUCTIONS = """\
Full access to the user's iCloud (Apple) Calendar: calendars, events, alerts, locations,
recurrence, invitations, travel time and availability.

Conventions:
- Call get_current_time first when the user says "today", "tomorrow", "next week", etc.
- Times are ISO-8601. Times without an offset are in the user's timezone
  (or the event's `timezone`). All-day events use plain dates (YYYY-MM-DD) and their
  `end` is the LAST day, inclusive.
- `calendar` arguments accept a calendar's name or id. Omit to use the default calendar.
- Recurring events: list_events returns each occurrence with an `occurrence` value. Pass it
  with `scope` = "this" | "future" | "all" to update_event / delete_event.
- Outputs omit fields that are empty or at their defaults (e.g. no alerts, availability=busy).
- Confirm with the user before deleting calendars or events.
"""

P = ParamSpec("P")
R = TypeVar("R")

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def _tool_errors(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Surface CalendarError messages to the model (the SDK hides unexpected exceptions)."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except CalendarError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _compact(model: BaseModel) -> dict[str, Any]:
    """Drop empty/default fields to keep responses small (the instructions tell the model)."""
    return model.model_dump(mode="json", exclude_none=True, exclude_defaults=True)


def _fields(**kwargs: Any) -> dict[str, Any]:
    """Keep only arguments that were actually supplied (None = leave unchanged)."""
    return {k: v for k, v in kwargs.items() if v is not None}


# Reusable parameter annotations --------------------------------------------------------
Start = Annotated[str, Field(description="ISO datetime ('2026-11-03T15:00') or date ('2026-11-03') for all-day.")]
OptStart = Annotated[str | None, Field(description="New start: ISO datetime, or date for all-day.")]
End = Annotated[str | None, Field(description="ISO datetime; for all-day events the last day (inclusive).")]
Duration = Annotated[int | None, Field(description="Length in minutes (alternative to `end`).", ge=0)]
AllDay = Annotated[bool | None, Field(description="All-day event.")]
TZ = Annotated[str | None, Field(description="IANA timezone, e.g. 'Europe/Paris'. Defaults to the user's.")]
CalendarRef = Annotated[str | None, Field(description="Calendar name or id. Omit for the default calendar.")]
LocationArg = Annotated[
    Location | str | None,
    Field(description="Plain text, or {title, address, latitude, longitude} for a map pin. '' clears."),
]
Notes = Annotated[str | None, Field(description="Notes / description. '' clears.")]
Url = Annotated[str | None, Field(description="URL attached to the event. '' clears.")]
Alerts = Annotated[
    list[Alert] | None,
    Field(
        description="Alerts, e.g. [{minutes_before: 15}, {minutes_before: 1440}]. Replaces existing; [] removes all."
    ),
]
RecurrenceArg = Annotated[
    Recurrence | None,
    Field(description="Repeat rule, e.g. {frequency:'weekly', by_weekday:['MO','WE'], until:'2027-06-30'}."),
]
RRule = Annotated[str | None, Field(description="Raw RFC 5545 RRULE, alternative to `recurrence`.")]
ExDates = Annotated[list[str] | None, Field(description="Occurrence starts to skip. Replaces the existing list.")]
Availability = Annotated[Literal["busy", "free"] | None, Field(description="Show as busy or free.")]
Status = Annotated[Literal["confirmed", "tentative", "cancelled"] | None, Field(description="Event status.")]
Travel = Annotated[int | None, Field(description="Travel time before the event in minutes (0 removes).", ge=0)]
Attendees = Annotated[
    list[Attendee] | None,
    Field(description="Invitees [{email, name?, role?}]. iCloud emails them invitations. Replaces the list."),
]
Categories = Annotated[list[str] | None, Field(description="Category labels.")]
EventId = Annotated[str, Field(description="Event `id` from list_events / search_events / create_event.")]
Occurrence = Annotated[
    str | None,
    Field(description="For recurring events: the occurrence's `occurrence` value (its original start)."),
]
ScopeArg = Annotated[
    Scope,
    Field(
        description="For recurring events with `occurrence`: 'this' occurrence, "
        "'future' (this and following ones), or 'all'."
    ),
]
Calendars = Annotated[list[str] | None, Field(description="Calendar names/ids to include. Default: all.")]


def build_mcp(
    service: CalendarService,
    settings: Settings,
    provider: OwnerOAuthProvider | None = None,
) -> MCPServer:
    auth = None
    if provider is not None:
        auth = AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=f"{settings.public_url}/mcp",
            validate_token_resource=False,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
    mcp = MCPServer(
        name="icloud-calendar",
        title="iCloud Calendar",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        auth_server_provider=provider,
        auth=auth,
    )

    def tool(annotations: ToolAnnotations, title: str):
        def deco(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
            mcp.add_tool(_tool_errors(fn), title=title, annotations=annotations)
            return fn

        return deco

    # ------------------------------------------------------------------ context

    @tool(READ, "Current date and time")
    async def get_current_time() -> dict[str, str]:
        """Current date, time, weekday and timezone of the user. Use it to resolve relative dates."""
        return service.now()

    # ---------------------------------------------------------------- calendars

    @tool(READ, "List calendars")
    async def list_calendars() -> dict[str, Any]:
        """List all calendars with id, name, color, whether they are writable and which is the default."""
        cals = await service.list_calendars()
        return {"calendars": [c.model_dump(mode="json", exclude_none=True) for c in cals]}

    @tool(WRITE, "Create calendar")
    async def create_calendar(
        name: Annotated[str, Field(description="Calendar name.")],
        color: Annotated[str | None, Field(description="#RRGGBB or red/orange/yellow/green/blue/purple/brown.")] = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a new calendar for events."""
        return (await service.create_calendar(name, color, description)).model_dump(mode="json", exclude_none=True)

    @tool(WRITE, "Rename / recolor calendar")
    async def update_calendar(
        calendar: Annotated[str, Field(description="Calendar name or id.")],
        name: Annotated[str | None, Field(description="New name.")] = None,
        color: Annotated[str | None, Field(description="New color: #RRGGBB or a color name.")] = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Rename a calendar, change its color or description."""
        info = await service.update_calendar(calendar, name=name, color=color, description=description)
        return info.model_dump(mode="json", exclude_none=True)

    @tool(DESTRUCTIVE, "Delete calendar")
    async def delete_calendar(calendar: Annotated[str, Field(description="Calendar name or id.")]) -> str:
        """Permanently delete a calendar AND all of its events. Always confirm with the user first."""
        info = await service.delete_calendar(calendar)
        return f"Deleted calendar {info.name!r}."

    # ------------------------------------------------------------------- events

    @tool(READ, "List events")
    async def list_events(
        start: Annotated[str, Field(description="Range start (ISO date or datetime).")],
        end: Annotated[str, Field(description="Range end (ISO date = inclusive day, or datetime).")],
        calendars: Calendars = None,
        query: Annotated[str | None, Field(description="Only events whose title/notes/location contain this.")] = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 250,
    ) -> dict[str, Any]:
        """List events between two dates across calendars. Repeating events are expanded into
        individual occurrences, sorted by start time."""
        events = await service.list_events(start, end, calendars, query, limit)
        return {"count": len(events), "events": [_compact(e) for e in events]}

    @tool(READ, "Search events")
    async def search_events(
        query: Annotated[str, Field(description="Text to look for in title, notes or location.")],
        start: Annotated[str | None, Field(description="Range start. Default: 90 days ago.")] = None,
        end: Annotated[str | None, Field(description="Range end. Default: 1 year from now.")] = None,
        calendars: Calendars = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 100,
    ) -> dict[str, Any]:
        """Find events by text (case-insensitive) within a date range."""
        now = datetime.now(service.tz)
        start = start or (now - timedelta(days=90)).date().isoformat()
        end = end or (now + timedelta(days=365)).date().isoformat()
        events = await service.list_events(start, end, calendars, query, limit)
        return {"count": len(events), "events": [_compact(e) for e in events]}

    @tool(READ, "Get event")
    async def get_event(event_id: EventId, occurrence: Occurrence = None) -> dict[str, Any]:
        """Get all details of one event (or of one occurrence of a repeating event)."""
        return _compact(await service.get_event(event_id, occurrence))

    @tool(WRITE, "Create event")
    async def create_event(
        title: Annotated[str, Field(description="Event title.")],
        start: Start,
        end: End = None,
        duration_minutes: Duration = None,
        all_day: AllDay = None,
        timezone: TZ = None,
        calendar: CalendarRef = None,
        location: LocationArg = None,
        notes: Notes = None,
        url: Url = None,
        alerts: Alerts = None,
        recurrence: RecurrenceArg = None,
        rrule: RRule = None,
        availability: Availability = None,
        status: Status = None,
        travel_time_minutes: Travel = None,
        attendees: Attendees = None,
        categories: Categories = None,
    ) -> dict[str, Any]:
        """Create an event. Defaults: 1 hour long (timed) or a single day (all-day; also when `start`
        is a date). Supports alerts, location with map coordinates, repeat rules, travel time,
        free/busy, and inviting attendees."""
        data = EventCreate(
            **_fields(
                title=title,
                start=start,
                end=end,
                duration_minutes=duration_minutes,
                all_day=all_day,
                timezone=timezone,
                calendar=calendar,
                location=location,
                notes=notes,
                url=url,
                alerts=alerts,
                recurrence=recurrence,
                rrule=rrule,
                availability=availability,
                status=status,
                travel_time_minutes=travel_time_minutes,
                attendees=attendees,
                categories=categories,
            )
        )
        return _compact(await service.create_event(data))

    @tool(WRITE, "Update event")
    async def update_event(
        event_id: EventId,
        occurrence: Occurrence = None,
        scope: ScopeArg = Scope.all,
        title: str | None = None,
        start: OptStart = None,
        end: End = None,
        duration_minutes: Duration = None,
        all_day: AllDay = None,
        timezone: TZ = None,
        location: LocationArg = None,
        notes: Notes = None,
        url: Url = None,
        alerts: Alerts = None,
        recurrence: RecurrenceArg = None,
        rrule: RRule = None,
        remove_recurrence: Annotated[bool, Field(description="Make a repeating event a one-off.")] = False,
        excluded_dates: ExDates = None,
        availability: Availability = None,
        status: Status = None,
        travel_time_minutes: Travel = None,
        attendees: Attendees = None,
        categories: Categories = None,
    ) -> dict[str, Any]:
        """Change an event. Only the fields you pass are changed. Changing `start` alone keeps the
        duration. For repeating events pass `occurrence` + `scope` to edit one occurrence, it and all
        following ones, or the whole series. To move an event to another calendar use move_event."""
        patch = EventPatch(
            **_fields(
                title=title,
                start=start,
                end=end,
                duration_minutes=duration_minutes,
                all_day=all_day,
                timezone=timezone,
                location=location,
                notes=notes,
                url=url,
                alerts=alerts,
                recurrence=recurrence,
                rrule=rrule,
                excluded_dates=excluded_dates,
                availability=availability,
                status=status,
                travel_time_minutes=travel_time_minutes,
                attendees=attendees,
                categories=categories,
                remove_recurrence=remove_recurrence or None,
            )
        )
        if not patch.model_fields_set:
            raise ToolError("Nothing to change: pass at least one field.")
        return _compact(await service.update_event(event_id, patch, occurrence, scope))

    @tool(DESTRUCTIVE, "Delete event")
    async def delete_event(event_id: EventId, occurrence: Occurrence = None, scope: ScopeArg = Scope.all) -> str:
        """Delete an event. For repeating events pass `occurrence` and `scope` ('this' / 'future') to
        delete only part of the series; without `occurrence` the entire series is deleted."""
        return await service.delete_event(event_id, occurrence, scope)

    @tool(WRITE, "Move event to another calendar")
    async def move_event(
        event_id: EventId, calendar: Annotated[str, Field(description="Target calendar name or id.")]
    ) -> dict[str, Any]:
        """Move an event (the whole series, if repeating) to another calendar. Returns the new id."""
        return _compact(await service.move_event(event_id, calendar))

    @tool(WRITE, "Respond to invitation")
    async def respond_to_invite(
        event_id: EventId,
        response: Literal["accepted", "declined", "tentative"],
        occurrence: Occurrence = None,
    ) -> dict[str, Any]:
        """Accept, decline or tentatively accept an invitation you received."""
        return _compact(await service.respond_to_invite(event_id, response, occurrence))

    # ------------------------------------------------------------- availability

    @tool(READ, "Busy times")
    async def get_busy_times(
        start: Start, end: Annotated[str, Field(description="Range end.")], calendars: Calendars = None
    ) -> dict[str, Any]:
        """List busy blocks (events marked busy, including travel time) in a range."""
        blocks = await service.busy_blocks(start, end, calendars)
        return {"busy": [b.model_dump(mode="json", exclude_none=True) for b in blocks]}

    @tool(READ, "Find free time")
    async def find_free_time(
        start: Annotated[str, Field(description="Range start (date or datetime).")],
        end: Annotated[str, Field(description="Range end (date = inclusive day, or datetime).")],
        duration_minutes: Annotated[int, Field(ge=5, description="Minimum slot length.")] = 30,
        day_start: Annotated[str, Field(description="Earliest time of day, HH:MM.")] = "09:00",
        day_end: Annotated[str, Field(description="Latest time of day, HH:MM.")] = "17:00",
        weekdays_only: bool = False,
        calendars: Calendars = None,
        timezone: TZ = None,
    ) -> dict[str, Any]:
        """Find free time slots of at least `duration_minutes` within working hours."""
        slots = await service.find_free_slots(
            start, end, duration_minutes, day_start, day_end, weekdays_only, calendars, timezone
        )
        return {"slots": [s.model_dump(mode="json") for s in slots]}

    return mcp
