"""End-to-end tests of CalendarService against a real CalDAV server (Radicale)."""

from __future__ import annotations

import pytest

from icloud_calendar.errors import InvalidInput, NotFound
from icloud_calendar.models import Alert, EventCreate, EventPatch, Location, Recurrence, Scope


async def test_calendar_crud(service):
    cals = await service.list_calendars()
    assert [c.name for c in cals] == ["Home"]
    assert cals[0].is_default and cals[0].color == "#1BADF8"

    work = await service.create_calendar("Work", color="#FF0000", description="Job stuff")
    assert work.name == "Work" and work.color == "#FF0000" and work.description == "Job stuff"

    updated = await service.update_calendar("Work", name="Day Job", color="purple")
    assert updated.name == "Day Job" and updated.color == "#CC73E1"

    await service.delete_calendar("Day Job")
    assert [c.name for c in await service.list_calendars()] == ["Home"]
    with pytest.raises(NotFound):
        await service.delete_calendar("Day Job")


async def test_create_rich_event_roundtrip(service):
    ev = await service.create_event(
        EventCreate(
            title="Coffee with Sam",
            start="2026-11-05T10:00",
            duration_minutes=45,
            location=Location(
                title="Blue Bottle", address="1 Ferry Building, SF", latitude=37.7955, longitude=-122.3937
            ),
            notes="Bring the deck",
            url="https://example.com",
            alerts=[Alert(minutes_before=15), Alert(minutes_before=60 * 24)],
            availability="free",
            status="tentative",
            travel_time_minutes=20,
            categories=["social"],
        )
    )
    assert ev.start == "2026-11-05T10:00-05:00" and ev.end == "2026-11-05T10:45-05:00"
    got = await service.get_event(ev.id)
    assert got.title == "Coffee with Sam"
    assert got.timezone == "America/New_York"
    assert got.location.title == "Blue Bottle" and got.location.address == "1 Ferry Building, SF"
    assert got.location.latitude == pytest.approx(37.7955)
    assert sorted(a.minutes_before for a in got.alerts) == [15, 1440]
    assert got.availability == "free" and got.status == "tentative"
    assert got.travel_time_minutes == 20
    assert got.notes == "Bring the deck" and got.url == "https://example.com"
    assert got.categories == ["social"]


async def test_all_day_event_inclusive_end(service):
    ev = await service.create_event(EventCreate(title="Trip", start="2026-12-20", end="2026-12-22"))
    assert ev.all_day and ev.start == "2026-12-20" and ev.end == "2026-12-22"
    single = await service.create_event(EventCreate(title="Holiday", start="2026-12-25"))
    assert single.start == single.end == "2026-12-25"
    events = await service.list_events("2026-12-22", "2026-12-22")
    assert [e.title for e in events] == ["Trip"]


async def test_update_and_clear_fields(service):
    ev = await service.create_event(
        EventCreate(title="Dentist", start="2026-11-10T14:00", location="Main St", alerts=[Alert(minutes_before=30)])
    )
    upd = await service.update_event(ev.id, EventPatch(start="2026-11-10T16:00", location="", alerts=[]))
    assert upd.start == "2026-11-10T16:00-05:00" and upd.end == "2026-11-10T17:00-05:00"  # duration kept
    assert upd.location is None and upd.alerts == []
    upd = await service.update_event(ev.id, EventPatch(timezone="Europe/London"))
    assert upd.timezone == "Europe/London" and upd.start == "2026-11-10T11:00-05:00"  # 16:00 London
    with pytest.raises(InvalidInput):
        await service.update_event(ev.id, EventPatch(end="2026-11-10T09:00"))


async def test_list_and_search(service):
    await service.create_event(EventCreate(title="Standup", start="2026-11-03T09:00", duration_minutes=15))
    await service.create_event(EventCreate(title="Lunch", start="2026-11-03T12:00", notes="tacos"))
    await service.create_event(EventCreate(title="Outside range", start="2026-11-20T12:00"))
    events = await service.list_events("2026-11-03", "2026-11-03")
    assert [e.title for e in events] == ["Standup", "Lunch"]
    found = await service.list_events("2026-11-01", "2026-11-30", query="TACOS")
    assert [e.title for e in found] == ["Lunch"]


async def test_recurring_this_future_all(service):
    ev = await service.create_event(
        EventCreate(
            title="Gym",
            start="2026-11-02T07:00",
            duration_minutes=60,
            recurrence=Recurrence(frequency="daily", count=6),
        )
    )
    occ = await service.list_events("2026-11-01", "2026-11-30")
    assert [o.start[:10] for o in occ] == [f"2026-11-0{d}" for d in range(2, 8)]
    assert all(o.is_recurring and o.occurrence for o in occ)

    # Move just the 3rd occurrence.
    third = occ[2]
    moved = await service.update_event(
        ev.id, EventPatch(start="2026-11-04T18:00", title="Evening gym"), occurrence=third.occurrence, scope=Scope.this
    )
    assert moved.start == "2026-11-04T18:00-05:00" and moved.title == "Evening gym"
    occ = await service.list_events("2026-11-01", "2026-11-30")
    assert [o.title for o in occ] == ["Gym", "Gym", "Evening gym", "Gym", "Gym", "Gym"]
    assert occ[2].is_modified_occurrence

    # Delete only the 2nd.
    await service.delete_event(ev.id, occurrence=occ[1].occurrence, scope=Scope.this)
    occ = await service.list_events("2026-11-01", "2026-11-30")
    assert [o.start[:10] for o in occ] == ["2026-11-02", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07"]

    # Change time for this and future from the 5th: splits the series, COUNT carried over.
    new_series = await service.update_event(
        ev.id, EventPatch(start="2026-11-05T08:00"), occurrence=occ[2].occurrence, scope=Scope.future
    )
    assert new_series.id != ev.id
    occ = await service.list_events("2026-11-01", "2026-11-30")
    assert [o.start for o in occ] == [
        "2026-11-02T07:00-05:00",
        "2026-11-04T18:00-05:00",
        "2026-11-05T08:00-05:00",
        "2026-11-06T08:00-05:00",
        "2026-11-07T08:00-05:00",
    ]

    # Rename the whole (new) series.
    await service.update_event(
        new_series.id, EventPatch(title="Morning gym"), occurrence=occ[3].occurrence, scope=Scope.all
    )
    occ = await service.list_events("2026-11-01", "2026-11-30")
    assert [o.title for o in occ] == ["Gym", "Evening gym", "Morning gym", "Morning gym", "Morning gym"]

    # Delete future from the 6th.
    await service.delete_event(new_series.id, occurrence=occ[3].occurrence, scope=Scope.future)
    occ = await service.list_events("2026-11-01", "2026-11-30")
    assert [o.start[:10] for o in occ] == ["2026-11-02", "2026-11-04", "2026-11-05"]

    # Delete all of the original series.
    await service.delete_event(ev.id)
    occ = await service.list_events("2026-11-01", "2026-11-30")
    assert [o.title for o in occ] == ["Morning gym"]


async def test_recurring_shift_all_from_occurrence(service):
    ev = await service.create_event(
        EventCreate(
            title="Weekly sync",
            start="2026-11-02T10:00",
            recurrence=Recurrence(frequency="weekly", by_weekday=["MO"], until="2026-11-30"),
        )
    )
    occ = await service.list_events("2026-11-01", "2026-12-31")
    assert len(occ) == 5
    await service.update_event(
        ev.id, EventPatch(start="2026-11-16T11:30"), occurrence=occ[2].occurrence, scope=Scope.all
    )
    occ = await service.list_events("2026-11-01", "2026-12-31")
    assert [o.start for o in occ][:2] == ["2026-11-02T11:30-05:00", "2026-11-09T11:30-05:00"]
    assert all(o.end.endswith("12:30-05:00") for o in occ)

    got = await service.get_event(ev.id, occurrence=occ[1].occurrence)
    assert got.start == "2026-11-09T11:30-05:00"
    full = await service.get_event(ev.id)
    assert full.recurrence.frequency == "weekly" and full.recurrence.by_weekday == ["MO"]

    await service.update_event(ev.id, EventPatch(remove_recurrence=True))
    occ = await service.list_events("2026-11-01", "2026-12-31")
    assert len(occ) == 1 and not occ[0].is_recurring


async def test_move_event(service):
    await service.create_calendar("Personal")
    ev = await service.create_event(EventCreate(title="Haircut", start="2026-11-12T13:00"))
    moved = await service.move_event(ev.id, "Personal")
    assert moved.calendar_name == "Personal"
    assert (await service.get_event(moved.id)).title == "Haircut"
    with pytest.raises(NotFound):
        await service.get_event(ev.id)


async def test_free_slots(service):
    await service.create_event(EventCreate(title="A", start="2026-11-04T10:00", duration_minutes=60))
    await service.create_event(
        EventCreate(title="B", start="2026-11-04T13:00", duration_minutes=30, travel_time_minutes=30)
    )
    await service.create_event(EventCreate(title="Free", start="2026-11-04T15:00", availability="free"))
    slots = await service.find_free_slots("2026-11-04", "2026-11-04", duration_minutes=60)
    assert [(s.start[11:16], s.end[11:16]) for s in slots] == [
        ("09:00", "10:00"),
        ("11:00", "12:30"),
        ("13:30", "17:00"),
    ]
    busy = await service.busy_blocks("2026-11-04", "2026-11-04")
    assert [b.title for b in busy] == ["A", "B"]


async def test_attendees_and_rsvp(service):
    me = service.settings.icloud_username
    ev = await service.create_event(
        EventCreate(
            title="Planning", start="2026-11-06T15:00", attendees=[{"email": "friend@example.com", "name": "Friend"}]
        )
    )
    assert ev.organizer.email == me
    emails = {a.email: a for a in ev.attendees}
    assert emails["friend@example.com"].status == "needs-action"
    assert emails[me].status == "accepted"
    upd = await service.respond_to_invite(ev.id, "tentative")
    assert {a.email: a.status for a in upd.attendees}[me] == "tentative"
