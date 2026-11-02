# icloud-calendar-mcp

Give Claude full access to your **iCloud Calendar**, the one behind Apple's Calendar app on iPhone, iPad and Mac.

This is an MCP server and a REST API built on one shared core. It talks to iCloud over CalDAV, the same protocol Calendar.app uses, so everything Claude changes shows up on all your Apple devices.

```
claude.ai / Claude app ──OAuth──▶  /mcp       ┐
Claude Code / Desktop ──API key─▶  /mcp       ├─▶ CalendarService ──CalDAV──▶ iCloud
scripts / Shortcuts   ──API key─▶  /api/v1/*  ┘
```

## What Claude can do

| Area | Tools |
|---|---|
| Calendars | `list_calendars`, `create_calendar`, `update_calendar` (rename/recolor), `delete_calendar` |
| Events | `list_events`, `search_events`, `get_event`, `create_event`, `update_event`, `delete_event`, `move_event` |
| Invitations | attendees on create/update (iCloud emails the invites), `respond_to_invite` (accept/decline/maybe) |
| Availability | `get_busy_times`, `find_free_time` (working hours, minimum duration, weekdays only) |
| Context | `get_current_time`, so "tomorrow at 3" resolves in your timezone |

Event fields cover what Calendar.app supports:

- title and notes
- start and end, or a duration
- all-day events
- per-event timezone
- **location**: plain text, or a map pin with coordinates (written as Apple's `X-APPLE-STRUCTURED-LOCATION`)
- URL
- **alerts**: any number of them, relative ("15 min before") or at an absolute time
- **repeat rules**: daily, weekly, monthly or yearly, with interval, weekdays, "2nd Friday", count or until; or a raw RRULE; plus skipped dates
- **travel time**
- show as busy or free
- status: confirmed, tentative or cancelled
- attendees
- categories

For **repeating events**, edits and deletes work like the Calendar app's prompt: *this event*, *this and future events*, or *all events*.

> **Note on "Reminders":** event alerts are fully supported. The separate *Reminders app* (to-do lists) has used a private Apple backend since iOS 13 that CalDAV can't reach, so it is out of scope.

## 1. Get an app-specific password

iCloud doesn't accept your normal Apple ID password from third-party apps. Create a dedicated one:

1. Go to [account.apple.com](https://account.apple.com) → **Sign-In and Security** → **App-Specific Passwords**.
2. Click **+** and name it "Claude calendar".
3. Copy the password (`abcd-efgh-ijkl-mnop`).

You can revoke it there at any time, and access stops immediately.

## 2. Deploy (remote, recommended)

claude.ai on web, desktop and mobile connects to **remote** MCP servers over HTTPS, so the server needs a public URL. Any host that runs a Docker container with a small persistent disk works.

### Fly.io

```bash
fly launch --no-deploy --copy-config --name my-icloud-cal
```

```bash
fly volumes create data --size 1
```

```bash
fly secrets set \
  ICLOUD_USERNAME=you@icloud.com \
  ICLOUD_APP_PASSWORD=abcd-efgh-ijkl-mnop \
  DEFAULT_TIMEZONE=America/New_York \
  PUBLIC_URL=https://my-icloud-cal.fly.dev \
  OWNER_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" \
  API_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

```bash
fly deploy
```

`fly secrets set` doesn't echo the generated values back. Either generate `OWNER_PASSWORD` and `API_KEY` yourself first and keep a copy in your password manager, or set them again later.

### Docker anywhere (VPS, Railway, Render, home server behind a tunnel)

```bash
cp .env.example .env
```

Edit `.env`, then:

```bash
docker build -t icloud-calendar-mcp .
```

```bash
docker run -d --name icloud-cal --env-file .env -p 8000:8000 -v icloud-cal-data:/data icloud-calendar-mcp
```

Put it behind HTTPS (Caddy, Cloudflare Tunnel, or your platform's TLS) and set `PUBLIC_URL` to that HTTPS address.

Before deploying, you can check that your credentials work:

```bash
uv run icloud-calendar check
```

## 3. Connect Claude

### claude.ai (web, desktop app, mobile)

1. Open **Settings → Connectors → Add custom connector**.
2. Enter the URL `https://<your-host>/mcp`.
3. Click **Connect**. A login page from your server opens; enter your `OWNER_PASSWORD`.
4. Done. Ask Claude: *"What's on my calendar this week?"*

Behind the scenes this is standard MCP OAuth: dynamic client registration, PKCE, and refresh-token rotation. Tokens are stored hashed in SQLite on the data volume, so they survive restarts.

### Claude Code

```bash
claude mcp add --transport http icloud-calendar https://<your-host>/mcp --header "Authorization: Bearer <API_KEY>"
```

### Local only, no server (optional)

The same code also runs as a local stdio MCP server, with no hosting and no auth. That's handy for trying it out. Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "icloud-calendar": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/aadev151/icloud-calendar-mcp", "icloud-calendar", "stdio"],
      "env": {
        "ICLOUD_USERNAME": "you@icloud.com",
        "ICLOUD_APP_PASSWORD": "abcd-efgh-ijkl-mnop",
        "DEFAULT_TIMEZONE": "America/New_York"
      }
    }
  }
}
```

**Do you need both local and remote?** No. Remote alone is enough: one deployment serves claude.ai, the mobile apps, Claude Desktop and Claude Code. Local stdio is an optional extra for development, or if you'd rather not host anything.

## REST API

Every MCP tool has a REST equivalent under `/api/v1`. Interactive docs are at `https://<your-host>/docs`. Authenticate with `Authorization: Bearer <API_KEY>`. An OAuth access token works too.

| Method | Path | |
|---|---|---|
| GET | `/api/v1/now` | current time in your timezone |
| GET / POST | `/api/v1/calendars` | list / create |
| PATCH / DELETE | `/api/v1/calendars/{calendar}` | rename, recolor / delete |
| GET | `/api/v1/events?start=&end=&calendar=&q=` | list (repeating events are expanded) |
| POST | `/api/v1/events` | create |
| GET / PATCH / DELETE | `/api/v1/events/{id}?occurrence=&scope=` | get / update / delete |
| POST | `/api/v1/events/{id}/move` | move to another calendar |
| POST | `/api/v1/events/{id}/rsvp` | accept / decline / tentative |
| GET | `/api/v1/busy`, `/api/v1/free-slots` | availability |

```bash
curl -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  https://<your-host>/api/v1/events -d '{
    "title": "Dentist", "start": "2026-11-10T14:00", "duration_minutes": 45,
    "location": "123 Main St", "alerts": [{"minutes_before": 60}]
  }'
```

### Conventions

- Times are ISO-8601. A time without an offset is read in `DEFAULT_TIMEZONE`, or in the event's own `timezone`.
- All-day events use plain dates, and `end` is the **last day, inclusive**: a one-day event has `start == end`.
- `calendar` accepts a calendar's name or its id.
- Recurring events: `list_events` returns one item per occurrence. Pass its `occurrence` value with `scope` = `this`, `future` or `all` to edit or delete part of the series.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `ICLOUD_USERNAME` | ✓ | Apple ID email |
| `ICLOUD_APP_PASSWORD` | ✓ | app-specific password |
| `DEFAULT_TIMEZONE` | | IANA timezone, default `UTC` |
| `DEFAULT_CALENDAR` | | calendar used when none is given; default is your first writable calendar |
| `PUBLIC_URL` | remote | public HTTPS base URL (the OAuth issuer) |
| `OWNER_PASSWORD` | remote* | password for the OAuth login page (claude.ai) |
| `API_KEY` | remote* | static bearer token (REST, Claude Code) |
| `DATA_DIR` | | OAuth state storage, default `./data` (`/data` in Docker) |
| `PORT`, `HOST` | | default `8000`, `0.0.0.0` |

\* At least one of them must be set, or the HTTP server refuses to start.

## Security notes

- Your iCloud credentials stay on your server. Claude only ever receives calendar data.
- The app-specific password grants access to calendars and contacts, not to your Apple ID or iCloud Drive. Revoke it at account.apple.com whenever you like.
- Use long random values for `OWNER_PASSWORD` and `API_KEY`. The login page locks out after 5 wrong attempts in 15 minutes.
- Delete tools are marked destructive, so Claude asks before deleting.

## Limitations

- **Reminders app**: not reachable over CalDAV (see the note above).
- **Subscribed calendars** (holidays, birthdays, webcal subscriptions) are synced to your devices separately from CalDAV, so they don't appear here.
- **Invitations**: iCloud sends invitation emails using server-side scheduling. Delivery is iCloud's job and can't be confirmed from here.
- Some iCloud-only behaviour can't be covered by automated tests (see Development). Run the smoke test to check it against your own account.

## Development

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uvx ruff check src tests
```

The tests start a throwaway [Radicale](https://radicale.org) CalDAV server. They cover:

- calendar CRUD
- rich events: location, alerts, travel time, attendees
- all-day events
- recurring-event edits and deletes for this, future and all scopes, including series splitting
- moving events
- free and busy time
- the REST API
- MCP over HTTP and over stdio
- the full claude.ai OAuth flow

To check against your real iCloud account, run the smoke test. It creates one temporary event a year from now, edits it, then deletes it:

```bash
uv run python scripts/smoke_icloud.py
```

### Layout

```
src/icloud_calendar/
  caldav_client.py   async CalDAV client (iCloud discovery + quirks)
  ical.py            iCalendar <-> models, incl. Apple X- properties
  service.py         calendar operations, recurrence scopes, availability
  mcp_server.py      MCP tools
  api.py             REST API
  auth.py            single-user OAuth server + API key
  app.py             ASGI app wiring
```
