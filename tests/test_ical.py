"""Unit tests for iCalendar parsing of Apple-generated data."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from icloud_calendar import ical
from icloud_calendar.models import EventCreate, Location, Recurrence

TZ = ZoneInfo("America/Los_Angeles")

# Shaped like what Calendar.app writes to iCloud.
APPLE_EVENT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Apple Inc.//macOS 15.0//EN
CALSCALE:GREGORIAN
BEGIN:VEVENT
CREATED:20261020T170000Z
UID:5F1C2A0E-9E7B-4F63-8D0F-2A2B0C1D3E4F
DTEND;TZID=America/Los_Angeles:20261105T110000
TRANSP:OPAQUE
X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC
SUMMARY:Coffee
LAST-MODIFIED:20261020T170100Z
DTSTAMP:20261020T170100Z
DTSTART;TZID=America/Los_Angeles:20261105T100000
LOCATION:Blue Bottle Coffee\\n1 Ferry Building\\, San Francisco
X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-ADDRESS=1 Ferry Building\\, San Fra
 ncisco;X-APPLE-RADIUS=70;X-TITLE=Blue Bottle Coffee:geo:37.795500,-122.393700
X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT25M
SEQUENCE:1
BEGIN:VALARM
X-WR-ALARMUID:A1
UID:A1
TRIGGER:-PT15M
ATTACH;VALUE=URI:Chord
ACTION:AUDIO
END:VALARM
BEGIN:VALARM
TRIGGER;VALUE=DATE-TIME:19760401T005545Z
ACTION:NONE
END:VALARM
BEGIN:VALARM
TRIGGER;RELATED=END:PT0S
ACTION:DISPLAY
DESCRIPTION:Reminder
END:VALARM
END:VEVENT
END:VCALENDAR
"""


def test_reads_apple_properties() -> None:
    cal = ical.parse_calendar(APPLE_EVENT)
    ev = ical.event_to_out(
        ical.master_of(cal), event_id="home/x.ics", calendar_id="home", calendar_name="Home", display_tz=TZ
    )
    assert ev.title == "Coffee"
    assert ev.start == "2026-11-05T10:00-08:00" and ev.end == "2026-11-05T11:00-08:00"
    assert ev.location.title == "Blue Bottle Coffee"
    assert ev.location.address == "1 Ferry Building, San Francisco"
    assert ev.location.latitude == 37.7955 and ev.location.longitude == -122.3937
    assert ev.travel_time_minutes == 25
    # ACTION:NONE placeholder skipped; end-relative alarm converted to minutes before start.
    assert [a.minutes_before for a in ev.alerts] == [15, -60]


def test_new_event_serializes_for_icloud() -> None:
    comp = ical.new_event(
        EventCreate(
            title="Flight",
            start="2026-12-01T08:00",
            end="2026-12-01T11:30",
            timezone="Europe/Paris",
            location=Location(title="CDG", latitude=49.0097, longitude=2.5479),
            recurrence=Recurrence(frequency="weekly", until="2026-12-31"),
        ),
        default_tz=TZ,
    )
    cal = ical.new_calendar()
    cal.add_component(comp)
    text = ical.serialize(cal)
    assert "DTSTART;TZID=Europe/Paris:20261201T080000" in text
    assert "DTEND;TZID=Europe/Paris:20261201T113000" in text  # iCloud requires DTEND
    assert "BEGIN:VTIMEZONE" in text and "TZID:Europe/Paris" in text
    assert "UNTIL=20261231T225959Z" in text  # inclusive day end, converted to UTC
    assert "X-APPLE-STRUCTURED-LOCATION" in text and "geo:49.0097,2.5479" in text
    assert "TRANSP:OPAQUE" in text
