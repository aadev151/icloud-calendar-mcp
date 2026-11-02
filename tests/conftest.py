"""Shared fixtures: a throwaway Radicale CalDAV server standing in for iCloud."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from icloud_calendar.config import Settings
from icloud_calendar.service import CalendarService


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def radicale_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    root = tmp_path_factory.mktemp("radicale")
    port = _free_port()
    config = root / "config"
    config.write_text(
        "[server]\n"
        f"hosts = 127.0.0.1:{port}\n"
        "[auth]\n"
        "type = none\n"
        "[storage]\n"
        f"filesystem_folder = {root / 'collections'}\n"
        "[logging]\n"
        "level = warning\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "radicale", "--config", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ},
    )
    url = f"http://127.0.0.1:{port}/"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:  # pragma: no cover
        proc.kill()
        raise RuntimeError("Radicale did not start: " + (proc.stderr.read().decode() if proc.stderr else ""))
    yield url
    proc.terminate()
    proc.wait(timeout=5)


_counter = 0


@pytest.fixture
def settings(radicale_url: str, tmp_path: Path) -> Settings:
    global _counter
    _counter += 1
    return Settings(
        icloud_username=f"user{_counter}@example.com",
        icloud_app_password="secret",
        caldav_url=radicale_url,
        default_timezone="America/New_York",
        public_url="http://testserver",
        api_key="test-api-key",
        owner_password="owner-pass",
        data_dir=tmp_path / "data",
        _env_file=None,
    )


@pytest.fixture
async def service(settings: Settings) -> AsyncIterator[CalendarService]:
    svc = CalendarService(settings)
    # Radicale starts with no calendars; create a default one like iCloud's "Home".
    await svc.create_calendar("Home", color="blue")
    yield svc
    await svc.aclose()
