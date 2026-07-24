"""Headless-browser fallback for JS-rendered pages.

Uses a **system Chrome/Chromium** binary in headless mode via ``subprocess``
(``--dump-dom``), so it adds no Python dependency and keeps the library
stdlib-only. If no browser is found (or rendering is disabled), the caller
transparently falls back to the static fetch.

    html = render.render_html("https://example.com")   # -> str | None
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

from .config import settings
from . import net, robots

log = logging.getLogger("studyweb.render")

_CHROME_NAMES = ("google-chrome-stable", "google-chrome", "chromium",
                 "chromium-browser", "chrome", "brave-browser")


def chrome_binary() -> str | None:
    """Path to a usable Chrome/Chromium, or None if none is available."""
    if settings.chrome_path:
        return settings.chrome_path if os.path.exists(settings.chrome_path) else None
    for name in _CHROME_NAMES:
        p = shutil.which(name)
        if p:
            return p
    return None


def available() -> bool:
    """True if the headless fallback can actually run."""
    return settings.render_enabled and chrome_binary() is not None


def render_html(url: str, *, timeout: float | None = None,
                wait_ms: int = 6000, check_robots: bool = True) -> str | None:
    """Return the fully-rendered DOM of ``url`` via headless Chrome, or None.

    Honours the same SSRF guard and robots.txt gate as the static fetcher.
    Never raises — any failure (no browser, timeout, crash) returns None so the
    caller can fall back to static content.
    """
    if not settings.render_enabled:
        return None
    binary = chrome_binary()
    if not binary:
        return None

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    try:
        net._guard_ssrf(parts.netloc.lower())
    except net.FetchError:
        log.warning("render blocked by SSRF guard: %s", url)
        return None
    if check_robots and settings.respect_robots and not robots.allowed(url):
        log.info("render blocked by robots.txt: %s", url)
        return None

    tmp = tempfile.mkdtemp(prefix="studyweb-chrome-")
    args = [
        binary,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-background-networking",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-scrollbars",
        "--window-size=1920,1080",
        f"--user-data-dir={tmp}",
        f"--user-agent={settings.user_agent}",
        f"--virtual-time-budget={wait_ms}",
        "--dump-dom",
        url,
    ]
    try:
        proc = subprocess.run(
            args, capture_output=True, timeout=timeout or settings.render_timeout,
            text=True, errors="replace")
        html = proc.stdout or ""
        if len(html) < 40:  # empty/failed render
            log.info("render produced no content for %s (rc=%s)", url, proc.returncode)
            return None
        return html
    except subprocess.TimeoutExpired:
        log.warning("render timed out for %s", url)
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("render failed for %s: %s", url, exc)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
