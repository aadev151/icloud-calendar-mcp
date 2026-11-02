"""Smoke test against your real iCloud account.

Creates a temporary event one year from now in your default calendar, reads it back, edits
it, and deletes it. Nothing else is touched.

    ICLOUD_USERNAME=... ICLOUD_APP_PASSWORD=... uv run python scripts/smoke_icloud.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from icloud_calendar.config import get_settings
from icloud_calendar.models import Alert, EventCreate, EventPatch, Location, Recurrence
from icloud_calendar.service import CalendarService


async def main() -> None:
    svc = CalendarService(get_settings())
    try:
        cals = await svc.list_calendars()
        print("Calendars:", ", ".join(f"{c.name}{' *' if c.is_default else ''}" for c in cals))
        day = (datetime.now(svc.tz) + timedelta(days=365)).date().isoformat()
        ev = await svc.create_event(
            EventCreate(
                title="[icloud-calendar-mcp smoke test]",
                start=f"{day}T10:00",
                duration_minutes=30,
                location=Location(title="Apple Park", latitude=37.3349, longitude=-122.0090),
                alerts=[Alert(minutes_before=10)],
                travel_time_minutes=15,
                recurrence=Recurrence(frequency="daily", count=3),
            )
        )
        print("Created:", ev.id)
        occ = await svc.list_events(day, day, query="smoke test")
        assert occ, "event not found after creation"
        await svc.update_event(ev.id, EventPatch(title="[smoke test - edited]"))
        got = await svc.get_event(ev.id)
        assert got.title == "[smoke test - edited]" and got.travel_time_minutes == 15
        print("Round-trip OK:", got.start, got.location.title if got.location else None)
    finally:
        if "ev" in locals():
            print(await svc.delete_event(ev.id))
        await svc.aclose()


if __name__ == "__main__":
    asyncio.run(main())
