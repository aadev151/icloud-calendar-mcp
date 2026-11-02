"""ASGI application: MCP (streamable HTTP) at /mcp, REST at /api/v1, OAuth endpoints, /healthz."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.routing import Mount

from .api import build_router
from .auth import OwnerOAuthProvider
from .config import Settings, get_settings
from .errors import CalendarError
from .mcp_server import build_mcp
from .service import CalendarService


def create_app(settings: Settings | None = None, service: CalendarService | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.api_key is None and settings.owner_password is None:
        raise RuntimeError(
            "Refusing to expose your calendar without authentication: set OWNER_PASSWORD "
            "(for claude.ai OAuth) and/or API_KEY (for scripts / Claude Code)."
        )
    service = service or CalendarService(settings)
    provider = OwnerOAuthProvider(settings)
    mcp = build_mcp(service, settings, provider)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,  # no server-side sessions: survives restarts/redeploys cleanly
        host=settings.host,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield
        await service.aclose()

    app = FastAPI(
        title="iCloud Calendar API",
        version="0.1.0",
        description="REST access to your iCloud Calendar. Authenticate with `Authorization: Bearer <API_KEY>`.",
        lifespan=lifespan,
    )

    @app.exception_handler(CalendarError)
    async def _calendar_error(_: Request, exc: CalendarError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.add_api_route("/oauth/login", provider.login_page, methods=["GET", "POST"], include_in_schema=False)
    app.include_router(build_router(service, provider))
    # Everything else (/mcp, /.well-known/*, /authorize, /token, /register, /revoke) -> MCP SDK app.
    app.router.routes.append(Mount("/", app=mcp_app))
    app.state.service = service
    app.state.provider = provider
    return app
