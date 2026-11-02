"""REST API (FastAPI) over the same CalendarService. Interactive docs at /docs."""

from __future__ import annotations

import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .auth import OwnerOAuthProvider
from .models import BusyBlock, CalendarInfo, EventCreate, EventOut, EventPatch, Scope, TimeSlot
from .service import CalendarService

_bearer = HTTPBearer(auto_error=False)


class CalendarCreate(BaseModel):
    name: str
    color: str | None = None
    description: str | None = None


class CalendarUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    description: str | None = None
    order: int | None = None


class MoveRequest(BaseModel):
    calendar: str


class RsvpRequest(BaseModel):
    response: Literal["accepted", "declined", "tentative"]
    occurrence: str | None = None


class Message(BaseModel):
    message: str


def build_router(service: CalendarService, provider: OwnerOAuthProvider) -> APIRouter:
    async def require_auth(
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        if creds is None or creds.scheme.lower() != "bearer":
            raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        token = await provider.load_access_token(creds.credentials)
        if token is None or (token.expires_at is not None and token.expires_at < time.time()):
            raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})

    r = APIRouter(prefix="/api/v1", dependencies=[Depends(require_auth)])
    Cals = Annotated[list[str] | None, Query(description="Calendar names/ids (repeatable).")]

    @r.get("/now", tags=["context"])
    async def now() -> dict[str, str]:
        return service.now()

    # --- calendars -------------------------------------------------------

    @r.get("/calendars", tags=["calendars"])
    async def list_calendars() -> list[CalendarInfo]:
        return await service.list_calendars()

    @r.post("/calendars", status_code=201, tags=["calendars"])
    async def create_calendar(body: CalendarCreate) -> CalendarInfo:
        return await service.create_calendar(body.name, body.color, body.description)

    @r.patch("/calendars/{calendar}", tags=["calendars"])
    async def update_calendar(calendar: str, body: CalendarUpdate) -> CalendarInfo:
        return await service.update_calendar(calendar, **body.model_dump())

    @r.delete("/calendars/{calendar}", tags=["calendars"])
    async def delete_calendar(calendar: str) -> Message:
        info = await service.delete_calendar(calendar)
        return Message(message=f"Deleted calendar {info.name!r}.")

    # --- events ----------------------------------------------------------

    @r.get("/events", tags=["events"])
    async def list_events(
        start: str,
        end: str,
        calendar: Cals = None,
        q: Annotated[str | None, Query(description="Text filter on title/notes/location.")] = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    ) -> list[EventOut]:
        return await service.list_events(start, end, calendar, q, limit)

    @r.post("/events", status_code=201, tags=["events"])
    async def create_event(body: EventCreate) -> EventOut:
        return await service.create_event(body)

    @r.post("/events/{event_id:path}/move", tags=["events"])
    async def move_event(event_id: str, body: MoveRequest) -> EventOut:
        return await service.move_event(event_id, body.calendar)

    @r.post("/events/{event_id:path}/rsvp", tags=["events"])
    async def rsvp(event_id: str, body: RsvpRequest) -> EventOut:
        return await service.respond_to_invite(event_id, body.response, body.occurrence)

    @r.get("/events/{event_id:path}", tags=["events"])
    async def get_event(event_id: str, occurrence: str | None = None) -> EventOut:
        return await service.get_event(event_id, occurrence)

    @r.patch("/events/{event_id:path}", tags=["events"])
    async def update_event(
        event_id: str, body: EventPatch, occurrence: str | None = None, scope: Scope = Scope.all
    ) -> EventOut:
        return await service.update_event(event_id, body, occurrence, scope)

    @r.delete("/events/{event_id:path}", tags=["events"])
    async def delete_event(event_id: str, occurrence: str | None = None, scope: Scope = Scope.all) -> Message:
        return Message(message=await service.delete_event(event_id, occurrence, scope))

    # --- availability ----------------------------------------------------

    @r.get("/busy", tags=["availability"])
    async def busy(start: str, end: str, calendar: Cals = None) -> list[BusyBlock]:
        return await service.busy_blocks(start, end, calendar)

    @r.get("/free-slots", tags=["availability"])
    async def free_slots(
        start: str,
        end: str,
        duration_minutes: Annotated[int, Query(ge=5)] = 30,
        day_start: str = "09:00",
        day_end: str = "17:00",
        weekdays_only: bool = False,
        calendar: Cals = None,
        timezone: str | None = None,
    ) -> list[TimeSlot]:
        return await service.find_free_slots(
            start, end, duration_minutes, day_start, day_end, weekdays_only, calendar, timezone
        )

    return r
