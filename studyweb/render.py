"""Headless-browser fallback for JS-rendered pages.

The transport lives in :mod:`studyweb.engines` (the ``chrome`` engine, a system
Chrome/Chromium driven via ``--dump-dom``, so no Python dependency is added).
This module is the *guarded* entry point: it applies the same SSRF guard and
robots.txt gate as the static fetcher before a browser is ever started.

    html = render.render_html("https://example.com")   # -> str | None
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from .config import settings
from . import engines, net, robots

log = logging.getLogger("studyweb.render")

# Re-exported so existing callers keep working after the engine refactor.
chrome_binary = engines.chrome_binary


def available() -> bool:
    """True if the headless fallback can actually run.

    Deliberately not delegated to the chrome engine: callers (and tests) patch
    ``render.chrome_binary``, and that has to keep deciding the answer.
    """
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
    if chrome_binary() is None:
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

    return engines.chrome_dump_dom(url, timeout=timeout, wait_ms=wait_ms)
