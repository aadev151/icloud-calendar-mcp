"""Command line entry point.

icloud-calendar serve    # HTTP: MCP at /mcp + REST at /api/v1 (for claude.ai, remote use)
icloud-calendar stdio    # local MCP over stdio (Claude Desktop / Claude Code, no auth needed)
icloud-calendar check    # verify iCloud credentials and list calendars
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import get_settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="icloud-calendar", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="Run the HTTP server (default).")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    sub.add_parser("stdio", help="Run as a local stdio MCP server.")
    sub.add_parser("check", help="Test the iCloud connection.")
    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "stdio":
        from .mcp_server import build_mcp
        from .service import CalendarService

        build_mcp(CalendarService(settings), settings).run("stdio")
        return

    if args.command == "check":
        asyncio.run(_check(settings))
        return

    import uvicorn

    from .app import create_app

    uvicorn.run(
        create_app(settings),
        host=getattr(args, "host", None) or settings.host,
        port=getattr(args, "port", None) or settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


async def _check(settings) -> None:
    from .errors import CalendarError
    from .service import CalendarService

    svc = CalendarService(settings)
    try:
        cals = await svc.list_calendars()
    except CalendarError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        await svc.aclose()
    print(f"Connected as {settings.icloud_username}. {len(cals)} calendars:")
    for c in cals:
        flags = " (default)" if c.is_default else ""
        flags += "" if c.writable else " (read-only)"
        print(f"  - {c.name} [{c.id}] {c.color or ''}{flags}")


if __name__ == "__main__":
    main()
