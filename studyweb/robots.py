"""robots.txt gate with a small in-memory cache.

Kept deliberately separate from :mod:`studyweb.net` to avoid an import cycle:
net.get() calls allowed(), and allowed() fetches robots.txt with a bare
request (which must NOT itself be gated by robots)."""

from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests

from .config import settings

_cache: dict[str, tuple[RobotFileParser | None, float]] = {}
_lock = threading.Lock()
_TTL = 3600.0


def _robots_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/robots.txt", "", ""))


def _load(url: str) -> RobotFileParser | None:
    rp = RobotFileParser()
    try:
        r = requests.get(_robots_url(url),
                         headers={"User-Agent": settings.user_agent},
                         timeout=min(settings.timeout, 10.0))
        if r.status_code >= 400:
            # No robots.txt (or server error) => nothing disallowed.
            rp.parse([])
        else:
            rp.parse(r.text.splitlines())
        return rp
    except requests.RequestException:
        # If robots.txt is unreachable, fail open but cache briefly.
        return None


def allowed(url: str, user_agent: str | None = None) -> bool:
    """Return True if ``url`` may be fetched under robots.txt rules."""
    if not settings.respect_robots:
        return True
    ua = user_agent or settings.user_agent
    host = urlsplit(url).netloc.lower()
    now = time.monotonic()
    with _lock:
        entry = _cache.get(host)
        if entry is None or now - entry[1] > _TTL:
            entry = (_load(url), now)
            _cache[host] = entry
    rp = entry[0]
    if rp is None:
        return True  # unknown => permit
    try:
        return rp.can_fetch(ua, url)
    except Exception:
        return True


def crawl_delay(url: str, user_agent: str | None = None) -> float | None:
    """robots.txt Crawl-delay for this host, if declared."""
    ua = user_agent or settings.user_agent
    host = urlsplit(url).netloc.lower()
    with _lock:
        entry = _cache.get(host)
    if not entry or entry[0] is None:
        return None
    try:
        d = entry[0].crawl_delay(ua)
        return float(d) if d is not None else None
    except Exception:
        return None
