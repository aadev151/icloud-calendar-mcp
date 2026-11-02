"""Single-user OAuth 2.1 authorization server + static API key support.

claude.ai custom connectors authenticate with OAuth (dynamic client registration + PKCE).
This module implements the MCP SDK's ``OAuthAuthorizationServerProvider`` for exactly one
user - you. The /authorize step shows a small password page (``OWNER_PASSWORD``); on success
an authorization code is issued and the usual code -> token exchange follows.

Scripts and Claude Code can skip OAuth and send ``Authorization: Bearer $API_KEY``.

State (registered clients, refresh/access tokens) lives in SQLite under ``DATA_DIR`` so
connections survive restarts. Tokens are stored as SHA-256 hashes, never in plain text.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import Settings

SCOPE = "calendar"
_CODE_TTL = 300
_PENDING_TTL = 900
_MAX_FAILURES = 5
_FAILURE_WINDOW = 900


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class _Store:
    """Tiny thread-safe key/value store on SQLite."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS kv (kind TEXT, key TEXT, value TEXT, expires REAL, PRIMARY KEY (kind, key))"
        )
        self._lock = threading.Lock()

    def put(self, kind: str, key: str, value: dict[str, Any], expires: float | None = None) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?, ?)", (kind, key, json.dumps(value), expires))

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT value, expires FROM kv WHERE kind=? AND key=?", (kind, key)).fetchone()
        if row is None:
            return None
        if row[1] is not None and row[1] < time.time():
            self.delete(kind, key)
            return None
        return json.loads(row[0])

    def delete(self, kind: str, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE kind=? AND key=?", (kind, key))

    def delete_where(self, kind: str, field: str, value: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE kind=? AND json_extract(value, ?) = ?", (kind, f"$.{field}", value))

    def purge(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE expires IS NOT NULL AND expires < ?", (time.time(),))


class OwnerOAuthProvider:
    """OAuthAuthorizationServerProvider for a single owner, gated by a password page."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = _Store(settings.data_dir / "oauth.sqlite3")
        self._pending: dict[str, tuple[float, str, AuthorizationParams]] = {}
        self._failures: dict[str, list[float]] = {}
        self.store.purge()

    # --- clients ---------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.put("client", client_info.client_id or "", client_info.model_dump(mode="json"))

    # --- authorization ---------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if self.settings.owner_password is None:
            raise AuthorizeError("access_denied", "OAuth login is disabled: set OWNER_PASSWORD on the server.")
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v[0] > now}
        txn = secrets.token_urlsafe(24)
        self._pending[txn] = (now + _PENDING_TTL, client.client_id or "", params)
        return f"{self.settings.public_url}/oauth/login?txn={txn}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self.store.get("code", _h(authorization_code))
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate({**data, "code": authorization_code})

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self.store.delete("code", _h(authorization_code.code))  # single use
        return self._issue(client.client_id or "", authorization_code.scopes, authorization_code.resource)

    # --- tokens ----------------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = int(time.time())
        a_exp = now + self.settings.access_token_ttl_seconds
        r_exp = now + self.settings.refresh_token_ttl_seconds
        family = secrets.token_hex(8)
        self.store.put(
            "access",
            _h(access),
            {
                "client_id": client_id,
                "scopes": scopes,
                "expires_at": a_exp,
                "resource": resource,
                "subject": "owner",
                "family": family,
            },
            expires=a_exp,
        )
        self.store.put(
            "refresh",
            _h(refresh),
            {
                "client_id": client_id,
                "scopes": scopes,
                "expires_at": r_exp,
                "resource": resource,
                "subject": "owner",
                "family": family,
            },
            expires=r_exp,
        )
        return OAuthToken(
            access_token=access,
            expires_in=self.settings.access_token_ttl_seconds,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        data = self.store.get("refresh", _h(refresh_token))
        if not data or data["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, **{k: v for k, v in data.items() if k != "family"})

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        data = self.store.get("refresh", _h(refresh_token.token))
        if data is None:
            raise TokenError("invalid_grant", "refresh token expired or revoked")
        # Rotate: revoke the whole token family, then issue a fresh pair.
        self.store.delete_where("access", "family", data["family"])
        self.store.delete_where("refresh", "family", data["family"])
        if scopes and not set(scopes) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope", "cannot widen scopes on refresh")
        return self._issue(client.client_id or "", scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        api_key = self.settings.api_key
        if (
            api_key is not None
            and api_key.get_secret_value()
            and hmac.compare_digest(token.encode(), api_key.get_secret_value().encode())
        ):
            return AccessToken(
                token=token, client_id="api-key", scopes=[SCOPE], subject="owner", resource=self.resource_url
            )
        data = self.store.get("access", _h(token))
        if data is None:
            return None
        return AccessToken(token=token, **{k: v for k, v in data.items() if k != "family"})

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "refresh" if isinstance(token, RefreshToken) else "access"
        data = self.store.get(kind, _h(token.token))
        if data:
            self.store.delete_where("access", "family", data["family"])
            self.store.delete_where("refresh", "family", data["family"])

    @property
    def resource_url(self) -> str:
        return f"{self.settings.public_url}/mcp"

    # --- login page ------------------------------------------------------

    def _blocked(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(ip, []) if now - t < _FAILURE_WINDOW]
        self._failures[ip] = recent
        return len(recent) >= _MAX_FAILURES

    async def login_page(self, request: Request) -> Response:
        if request.method == "GET":
            txn = request.query_params.get("txn", "")
            pending = self._pending.get(txn)
            if not pending or pending[0] < time.time():
                return HTMLResponse(_page("This sign-in link has expired. Start the connection again."), 400)
            client = await self.get_client(pending[1])
            name = (client.client_name if client else None) or "An MCP client"
            return HTMLResponse(_login_form(txn, name))

        form = await request.form()
        txn = str(form.get("txn", ""))
        password = str(form.get("password", ""))
        ip = request.client.host if request.client else "?"
        pending = self._pending.get(txn)
        if not pending or pending[0] < time.time():
            return HTMLResponse(_page("This sign-in link has expired. Start the connection again."), 400)
        if self._blocked(ip):
            return HTMLResponse(_page("Too many failed attempts. Try again in 15 minutes."), 429)
        expected = self.settings.owner_password.get_secret_value() if self.settings.owner_password else ""
        if not expected or not hmac.compare_digest(password.encode(), expected.encode()):
            self._failures.setdefault(ip, []).append(time.time())
            client = await self.get_client(pending[1])
            name = (client.client_name if client else None) or "An MCP client"
            return HTMLResponse(_login_form(txn, name, error="Wrong password."), 401)

        self._pending.pop(txn, None)
        _, client_id, params = pending
        code = secrets.token_urlsafe(32)
        self.store.put(
            "code",
            _h(code),
            {
                "client_id": client_id,
                "scopes": params.scopes or [SCOPE],
                "expires_at": time.time() + _CODE_TTL,
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
                "subject": "owner",
            },
            expires=time.time() + _CODE_TTL,
        )
        target = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
        return RedirectResponse(target, status_code=302)


def _page(body: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>iCloud Calendar MCP</title>
<style>
:root{{color-scheme:light dark;--bg:#f5f5f7;--card:#fff;--fg:#1d1d1f;--muted:#6e6e73;--accent:#0071e3;--err:#d70015}}
@media (prefers-color-scheme:dark){{:root{{--bg:#000;--card:#1c1c1e;--fg:#f5f5f7;
--muted:#a1a1a6;--accent:#2997ff;--err:#ff453a}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:16px;box-sizing:border-box}}
.card{{background:var(--card);border-radius:16px;padding:28px;max-width:360px;width:100%;box-shadow:0 4px 24px #0002}}
h1{{font-size:20px;margin:0 0 8px}} p{{color:var(--muted);margin:0 0 20px}}
input{{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #8884;font:inherit;
background:transparent;color:inherit;margin-bottom:12px}}
button{{width:100%;padding:12px;border:0;border-radius:10px;background:var(--accent);color:#fff;font:inherit;
font-weight:600;cursor:pointer}} .err{{color:var(--err);margin-bottom:12px}}
</style></head><body><div class="card">{body}</div></body></html>"""


def _login_form(txn: str, client_name: str, error: str | None = None) -> str:
    err = f'<div class="err">{html.escape(error)}</div>' if error else ""
    return _page(
        f"""<h1>Connect your iCloud Calendar</h1>
<p><b>{html.escape(client_name)}</b> wants full access to your calendars.
Enter the owner password configured on this server to allow it.</p>
<form method="post" action="/oauth/login">{err}
<input type="hidden" name="txn" value="{html.escape(txn)}">
<input type="password" name="password" placeholder="Owner password" autofocus required autocomplete="current-password">
<button type="submit">Allow access</button></form>"""
    )
