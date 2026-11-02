"""HTTP-level tests: REST API, MCP over streamable HTTP, and the OAuth flow claude.ai uses."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from icloud_calendar.app import create_app
from icloud_calendar.config import Settings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(settings: Settings) -> Iterator[str]:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    settings.public_url = base
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    # Radicale has no calendars by default; create one like iCloud's "Home".
    httpx.post(f"{base}/api/v1/calendars", json={"name": "Home"}, headers=_auth()).raise_for_status()
    yield base
    server.should_exit = True
    thread.join(timeout=5)


def _auth(token: str = "test-api-key") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_rest_api(server: str) -> None:
    c = httpx.Client(base_url=server, headers=_auth())
    assert httpx.get(f"{server}/api/v1/calendars").status_code == 401
    assert httpx.get(f"{server}/api/v1/calendars", headers=_auth("nope")).status_code == 401

    cals = c.get("/api/v1/calendars").json()
    assert [x["name"] for x in cals] == ["Home"]

    ev = c.post(
        "/api/v1/events",
        json={
            "title": "Review",
            "start": "2026-11-09T14:00",
            "duration_minutes": 30,
            "alerts": [{"minutes_before": 10}],
            "location": "Room 4",
        },
    )
    assert ev.status_code == 201, ev.text
    ev = ev.json()
    assert ev["end"] == "2026-11-09T14:30-05:00"

    got = c.get(f"/api/v1/events/{ev['id']}").json()
    assert got["location"]["title"] == "Room 4"

    patched = c.patch(f"/api/v1/events/{ev['id']}", json={"title": "Design review"}).json()
    assert patched["title"] == "Design review"

    listed = c.get("/api/v1/events", params={"start": "2026-11-09", "end": "2026-11-09"}).json()
    assert [e["title"] for e in listed] == ["Design review"]

    c.post("/api/v1/calendars", json={"name": "Work", "color": "green"}).raise_for_status()
    moved = c.post(f"/api/v1/events/{ev['id']}/move", json={"calendar": "Work"}).json()
    assert moved["calendar_name"] == "Work"

    slots = c.get(
        "/api/v1/free-slots", params={"start": "2026-11-09", "end": "2026-11-09", "duration_minutes": 60}
    ).json()
    assert [(s["start"][11:16], s["end"][11:16]) for s in slots] == [("09:00", "14:00"), ("14:30", "17:00")]

    assert c.delete(f"/api/v1/events/{moved['id']}").status_code == 200
    missing = c.get(f"/api/v1/events/{moved['id']}")
    assert missing.status_code == 404 and "Not found" in missing.json()["detail"]
    bad = c.post("/api/v1/events", json={"title": "x", "start": "tomorrow-ish"})
    assert bad.status_code == 400


async def _mcp_session(url: str, token: str) -> Client:
    http = httpx2.AsyncClient(headers=_auth(token), timeout=30)
    return Client(streamable_http_client(url, http_client=http))


async def test_mcp_tools_with_api_key(server: str) -> None:
    async with await _mcp_session(f"{server}/mcp", "test-api-key") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert {
            "get_current_time",
            "list_calendars",
            "create_calendar",
            "update_calendar",
            "delete_calendar",
            "list_events",
            "search_events",
            "get_event",
            "create_event",
            "update_event",
            "delete_event",
            "move_event",
            "respond_to_invite",
            "get_busy_times",
            "find_free_time",
        } <= set(tools)
        assert tools["delete_event"].annotations.destructive_hint is True
        assert tools["list_events"].annotations.read_only_hint is True

        res = await client.call_tool(
            "create_event",
            {
                "title": "Team dinner",
                "start": "2026-11-13T19:00",
                "duration_minutes": 120,
                "location": {
                    "title": "Nopa",
                    "address": "560 Divisadero St",
                    "latitude": 37.7749,
                    "longitude": -122.4376,
                },
                "alerts": [{"minutes_before": 60}],
                "recurrence": {"frequency": "monthly", "by_weekday": ["2FR"], "count": 3},
            },
        )
        assert not res.is_error, res.content
        created = res.structured_content
        assert created["title"] == "Team dinner" and created["is_recurring"]

        res = await client.call_tool("list_events", {"start": "2026-11-01", "end": "2027-02-28"})
        events = res.structured_content["events"]
        assert [e["start"][:10] for e in events] == ["2026-11-13", "2026-12-11", "2027-01-08"]

        res = await client.call_tool(
            "update_event",
            {
                "event_id": created["id"],
                "occurrence": events[1]["occurrence"],
                "scope": "this",
                "title": "Holiday dinner",
            },
        )
        assert not res.is_error, res.content

        res = await client.call_tool("search_events", {"query": "holiday", "start": "2026-11-01", "end": "2027-03-01"})
        assert [e["title"] for e in res.structured_content["events"]] == ["Holiday dinner"]

        res = await client.call_tool("get_event", {"event_id": "Home/nope.ics"})
        assert res.is_error and "Not found" in res.content[0].text

        res = await client.call_tool(
            "create_event", {"title": "x", "start": "2026-11-13T10:00", "end": "2026-11-13T09:00"}
        )
        assert res.is_error and "after" in res.content[0].text

        res = await client.call_tool("delete_event", {"event_id": created["id"]})
        assert not res.is_error

        res = await client.call_tool("get_current_time", {})
        assert res.structured_content["timezone"] == "America/New_York"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def test_oauth_flow_like_claude_ai(server: str) -> None:
    c = httpx.Client(base_url=server, follow_redirects=False)

    # 1. Unauthenticated MCP request -> 401 pointing at the protected-resource metadata.
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]

    prm = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == f"{server}/mcp"
    asm = c.get("/.well-known/oauth-authorization-server").json()
    assert asm["registration_endpoint"].endswith("/register")

    # 2. Dynamic client registration.
    redirect_uri = "https://claude.ai/api/mcp/auth_callback"
    reg = c.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "client_name": "Claude",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]

    # 3. Authorize -> our password page.
    verifier, challenge = _pkce()
    r = c.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": f"{server}/mcp",
        },
    )
    assert r.status_code == 302, r.text
    login_url = r.headers["location"]
    assert "/oauth/login?txn=" in login_url
    page = c.get(login_url)
    assert page.status_code == 200 and "Claude" in page.text
    txn = parse_qs(urlsplit(login_url).query)["txn"][0]

    assert c.post("/oauth/login", data={"txn": txn, "password": "wrong"}).status_code == 401
    r = c.post("/oauth/login", data={"txn": txn, "password": "owner-pass"})
    assert r.status_code == 302
    cb = urlsplit(r.headers["location"])
    assert f"{cb.scheme}://{cb.netloc}{cb.path}" == redirect_uri
    q = parse_qs(cb.query)
    assert q["state"] == ["xyz"]

    # 4. Token exchange (PKCE).
    tok = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": f"{server}/mcp",
        },
    )
    assert tok.status_code == 200, tok.text
    tokens = tok.json()
    # Code is single-use.
    again = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert again.status_code == 400

    # 5. Use the token with MCP and REST.
    async with await _mcp_session(f"{server}/mcp", tokens["access_token"]) as client:
        res = await client.call_tool("list_calendars", {})
        assert [x["name"] for x in res.structured_content["calendars"]] == ["Home"]
    assert c.get("/api/v1/calendars", headers=_auth(tokens["access_token"])).status_code == 200

    # 6. Refresh rotates tokens and revokes the old access token.
    ref = c.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": client_id,
        },
    )
    assert ref.status_code == 200, ref.text
    new = ref.json()
    assert new["access_token"] != tokens["access_token"]
    assert c.get("/api/v1/calendars", headers=_auth(tokens["access_token"])).status_code == 401
    assert c.get("/api/v1/calendars", headers=_auth(new["access_token"])).status_code == 200
    json.dumps(new)


async def test_stdio_mode(settings: Settings) -> None:
    import os
    import sys

    from mcp.client.stdio import StdioServerParameters

    env = {
        **os.environ,
        "ICLOUD_USERNAME": settings.icloud_username,
        "ICLOUD_APP_PASSWORD": "secret",
        "CALDAV_URL": settings.caldav_url,
        "DEFAULT_TIMEZONE": "America/New_York",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "icloud_calendar", "stdio"], env=env)
    async with Client(params) as client:
        res = await client.call_tool("create_calendar", {"name": "Home"})
        assert not res.is_error, res.content
        res = await client.call_tool("create_event", {"title": "From stdio", "start": "2026-11-20"})
        assert not res.is_error, res.content
        assert res.structured_content["all_day"] is True
