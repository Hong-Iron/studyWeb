"""Pluggable fetch engines — one transport contract, several backends.

studyweb's policy layer (robots.txt, the SSRF guard, per-host throttling, the
size cap and the on-disk cache) lives in :mod:`studyweb.net` and wraps *every*
engine, so adding a stronger backend never costs politeness or safety. An
engine answers exactly one question: given a URL, what bytes come back?

    static     requests + a browser UA           default, no extra dependency
    scrapling  Scrapling ``Fetcher``             TLS/JA3 fingerprint impersonation
    chrome     system Chrome ``--dump-dom``      JS-rendered pages, no dependency
    dynamic    Scrapling ``DynamicFetcher``      real Playwright browser
    stealth    Scrapling ``StealthyFetcher``     anti-bot fingerprint spoofing

The Scrapling-backed engines need :data:`INSTALL_HINT`. Without it they report
themselves unavailable and the escalation ladder simply skips them — nothing
else in studyweb changes, and the two-dependency install stays the default.

Engines are ordered by ``tier``: cost and intrusiveness both rise with it, so
:func:`ladder` walks upward only as far as a page actually forces it.

``stealth`` is never on the default ladder. It exists so that a site you are
allowed to read — one whose robots.txt permits the path — stops rejecting you
for looking like a script. It is not a robots.txt bypass: the gate in
:mod:`studyweb.net` runs before any engine is chosen, whichever engine that is.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable

from .config import settings

log = logging.getLogger("studyweb.engines")

# How to unlock the Scrapling-backed engines, in a form that works wherever this
# message is printed. studyweb is not published to PyPI, so `pip install
# "studyweb[scrapling]"` cannot resolve — the extra exists only in a source
# checkout's metadata, and a hint shown on an unknown machine cannot assume one
# is present. Installing the dependency directly is equivalent and always works.
INSTALL_HINT = 'pip install "scrapling[fetchers]" && scrapling install'

# The same thing from a checkout of this repo, where the extra does resolve.
INSTALL_HINT_SOURCE = 'pip install ".[scrapling]" && scrapling install'


class EngineError(Exception):
    """An engine could not complete a fetch (missing backend, crash, timeout).

    Recoverable by definition: :mod:`studyweb.net` retries, then escalates to
    the next engine on the ladder.
    """


class TooLarge(EngineError):
    """The response blew past ``max_bytes``.

    A policy violation rather than a transport failure — retrying or escalating
    would only download the same oversized page again, so net treats it as
    fatal for the URL.
    """


@dataclass
class Fetched:
    """Raw transport result. :mod:`studyweb.net` turns this into a ``Response``."""

    url: str                      # final URL after redirects
    status: int
    content: bytes
    headers: dict = field(default_factory=dict)
    engine: str = ""


# --- block detection --------------------------------------------------------

# Body text that means "a human gate stands between you and the content".
# Kept here rather than in search.py so every caller escalates on the same rule.
BLOCK_MARKERS = (
    "captcha", "unusual traffic", "verify you are human", "are you a robot",
    "consent.bing", "/tou/", "before you continue", "cf-browser-verification",
    "checking your browser", "just a moment", "enable javascript and cookies",
    "access denied", "ddos protection by",
)

# Statuses that usually mean "bot detected" rather than "gone". 404 is absent
# on purpose: a stronger engine will not conjure a page that does not exist.
BLOCK_STATUSES = (403, 429, 503)


def looks_blocked(status: int, content: bytes | str) -> bool:
    """True if this response looks like an anti-bot wall rather than content."""
    if status in BLOCK_STATUSES:
        return True
    if isinstance(content, bytes):
        text = content[:20000].decode("utf-8", "replace")
    else:
        text = (content or "")[:20000]
    low = text.lower()
    return any(m in low for m in BLOCK_MARKERS)


# --- the engine contract ----------------------------------------------------

class Engine:
    """Base transport. Subclasses implement :meth:`fetch` and :meth:`available`."""

    name = "base"
    tier = 0
    #: True if the engine drives a real browser (slow — never the default).
    heavy = False
    #: Human-readable reason the engine cannot run, filled by :meth:`available`.
    why_unavailable = ""

    def available(self) -> bool:
        return True

    def fetch(self, url: str, *, params: dict | None = None,
              headers: dict | None = None, timeout: float | None = None,
              on_redirect: Callable[[str], None] | None = None) -> Fetched:
        """Fetch ``url``.

        ``on_redirect`` is called with the final URL as early as the backend
        allows, so the caller can re-run its robots/SSRF gate on a destination
        it did not choose. Raising from it aborts the fetch.
        """
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Engine {self.name} tier={self.tier}>"


class StaticEngine(Engine):
    """The original ``requests`` transport — studyweb's default."""

    name = "static"
    tier = 0

    def fetch(self, url: str, *, params: dict | None = None,
              headers: dict | None = None, timeout: float | None = None,
              on_redirect: Callable[[str], None] | None = None) -> Fetched:
        # Imported here: net owns the shared session, and net imports this
        # module, so a top-level import would be circular.
        from . import net

        r = net.session().get(url, params=params, headers=headers,
                              timeout=timeout or settings.timeout,
                              stream=True, allow_redirects=True)
        # Streaming means the body has not arrived yet — this is the one engine
        # that can reject a disallowed redirect before paying for its content.
        if on_redirect is not None and r.url != url:
            try:
                on_redirect(r.url)
            except Exception:
                r.close()
                raise
        chunks, total = [], 0
        for chunk in r.iter_content(64 * 1024):
            chunks.append(chunk)
            total += len(chunk)
            if total > settings.max_bytes:
                r.close()
                raise TooLarge(f"response exceeds max_bytes ({settings.max_bytes})")
        return Fetched(url=r.url, status=r.status_code, content=b"".join(chunks),
                       headers=dict(r.headers), engine=self.name)


# --- headless Chrome (system binary, no Python dependency) ------------------

_CHROME_NAMES = ("google-chrome-stable", "google-chrome", "chromium",
                 "chromium-browser", "chrome", "brave-browser")


def chrome_binary() -> str | None:
    """Path to a usable Chrome/Chromium, or None if none is installed."""
    if settings.chrome_path:
        return settings.chrome_path if os.path.exists(settings.chrome_path) else None
    for name in _CHROME_NAMES:
        p = shutil.which(name)
        if p:
            return p
    return None


def chrome_dump_dom(url: str, *, timeout: float | None = None,
                    wait_ms: int = 6000) -> str | None:
    """Fully-rendered DOM of ``url`` via system Chrome, or None on any failure.

    Pure transport — callers are responsible for the robots/SSRF gate.
    :func:`studyweb.render.render_html` is the guarded public entry point.
    """
    binary = chrome_binary()
    if not binary:
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
        proc = subprocess.run(args, capture_output=True, text=True, errors="replace",
                              timeout=timeout or settings.render_timeout)
        html = proc.stdout or ""
        if len(html) < 40:  # empty / failed render
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


class ChromeEngine(Engine):
    """System Chrome in headless mode. Handles JS without any Python dependency."""

    name = "chrome"
    tier = 2
    heavy = True

    def available(self) -> bool:
        if not settings.render_enabled:
            self.why_unavailable = "STUDYWEB_RENDER is off"
            return False
        if chrome_binary() is None:
            self.why_unavailable = "no Chrome/Chromium binary found"
            return False
        return True

    def fetch(self, url: str, *, params: dict | None = None,
              headers: dict | None = None, timeout: float | None = None,
              on_redirect: Callable[[str], None] | None = None) -> Fetched:
        if params:
            raise EngineError("chrome engine cannot send query params separately")
        html = chrome_dump_dom(url, timeout=timeout)
        if html is None:
            raise EngineError("headless Chrome produced no content")
        # Chrome hands back a decoded DOM; re-encode as UTF-8 and say so, so the
        # extractor does not re-guess a charset that has already been resolved.
        return Fetched(url=url, status=200, content=html.encode("utf-8", "replace"),
                       headers={"content-type": "text/html; charset=utf-8"},
                       engine=self.name)


# --- Scrapling-backed engines (optional dependency) -------------------------

def _scrapling_fetcher(class_name: str):
    """Return a Scrapling fetcher class, or None when it cannot be loaded.

    ``scrapling.fetchers`` resolves its classes through a lazy ``__getattr__``,
    so importing the module proves nothing — the real import runs on attribute
    access and raises there. A core-only ``pip install scrapling`` (no
    ``[fetchers]``, so no curl_cffi) reaches exactly that path, which is why the
    getattr has to be inside the guard too.
    """
    try:
        from scrapling import fetchers

        return getattr(fetchers, class_name, None)
    except Exception:  # noqa: BLE001 — ImportError, or any import-time crash
        return None


def scrapling_available() -> bool:
    """True if the optional Scrapling dependency can be imported."""
    return _scrapling_fetcher("Fetcher") is not None


def scrapling_version() -> str:
    try:
        from importlib.metadata import version

        return version("scrapling")
    except Exception:  # noqa: BLE001
        return ""


def _from_scrapling(resp, url: str, engine: str) -> Fetched:
    """Normalise a Scrapling ``Response`` into a :class:`Fetched`.

    Scrapling's ``.body`` is ``str`` or ``bytes`` depending on the backend. When
    it is already decoded we re-encode to UTF-8 and declare that, so studyweb's
    charset logic does not second-guess a decision Scrapling already made — the
    EUC-KR pages this matters for come out right either way.
    """
    headers = dict(getattr(resp, "headers", {}) or {})
    body = getattr(resp, "body", None)
    if body is None:
        body = getattr(resp, "html_content", "")
    if isinstance(body, str):
        content = body.encode("utf-8", "replace")
        ctype = headers.get("content-type") or headers.get("Content-Type") or "text/html"
        headers["content-type"] = ctype.split(";", 1)[0].strip() + "; charset=utf-8"
    else:
        content = bytes(body)
    return Fetched(url=str(getattr(resp, "url", url) or url),
                   status=int(getattr(resp, "status", 200) or 200),
                   content=content, headers=headers, engine=engine)


class ScraplingEngine(Engine):
    """Scrapling's ``Fetcher``: a plain HTTP GET wearing a real browser's TLS
    fingerprint. No browser process, so it is cheap enough to sit right above
    ``static`` on the ladder and fixes most "403 to scripts only" sites."""

    name = "scrapling"
    tier = 1

    def available(self) -> bool:
        if not scrapling_available():
            self.why_unavailable = "needs the scrapling extra"
            return False
        return True

    def fetch(self, url: str, *, params: dict | None = None,
              headers: dict | None = None, timeout: float | None = None,
              on_redirect: Callable[[str], None] | None = None) -> Fetched:
        Fetcher = _scrapling_fetcher("Fetcher")
        if Fetcher is None:
            raise EngineError("scrapling is not installed")
        kwargs: dict = {
            "timeout": timeout or settings.timeout,   # curl_cffi: seconds
            "impersonate": settings.scrapling_impersonate,
            "stealthy_headers": True,
            "follow_redirects": True,
            # Scrapling counts *total attempts*, not extra retries, so 1 means
            # "try once" and 0 means "never send the request" (its retry loop is
            # range(retries), which then falls through to a RuntimeError). One
            # attempt is what we want: studyweb.net owns retrying, and a second
            # loop here would multiply against it and blow past the host delay.
            "retries": 1,
        }
        if params:
            kwargs["params"] = params
        if headers:
            kwargs["headers"] = headers
        try:
            resp = Fetcher.get(url, **kwargs)
        except Exception as exc:  # noqa: BLE001 — any backend failure is a fetch failure
            raise EngineError(f"scrapling fetch failed: {exc}") from exc
        out = _from_scrapling(resp, url, self.name)
        # Redirects are already followed by the time we get here, so the gate
        # runs on the body we hold rather than before fetching it.
        if on_redirect is not None and out.url != url:
            on_redirect(out.url)
        return out


class DynamicEngine(Engine):
    """Scrapling's ``DynamicFetcher`` — a real Playwright browser. Use when a
    page needs JS *and* system Chrome could not do the job."""

    name = "dynamic"
    tier = 3
    heavy = True

    _fetcher_name = "DynamicFetcher"

    def available(self) -> bool:
        if not settings.render_enabled:
            self.why_unavailable = "STUDYWEB_RENDER is off"
            return False
        if _scrapling_fetcher(self._fetcher_name) is None:
            self.why_unavailable = "needs the scrapling extra"
            return False
        return True

    def _kwargs(self, timeout: float | None) -> dict:
        return {
            # Playwright counts in milliseconds, unlike the static fetchers.
            "timeout": int((timeout or settings.render_timeout) * 1000),
            "headless": True,
            "network_idle": True,
            "useragent": settings.user_agent,
        }

    def fetch(self, url: str, *, params: dict | None = None,
              headers: dict | None = None, timeout: float | None = None,
              on_redirect: Callable[[str], None] | None = None) -> Fetched:
        if params:
            raise EngineError(f"{self.name} engine cannot send query params separately")
        cls = _scrapling_fetcher(self._fetcher_name)
        if cls is None:
            raise EngineError("scrapling is not installed")
        try:
            resp = cls.fetch(url, **self._kwargs(timeout))
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"{self.name} fetch failed: {exc}") from exc
        out = _from_scrapling(resp, url, self.name)
        if on_redirect is not None and out.url != url:
            on_redirect(out.url)
        return out


class StealthEngine(DynamicEngine):
    """Scrapling's ``StealthyFetcher`` — browser-fingerprint spoofing, and
    optionally a Cloudflare Turnstile solver.

    Opt-in only (``STUDYWEB_STEALTH=true``), and it never joins the default
    ladder. The robots.txt gate still runs first: this changes how studyweb
    *looks* to a site that already allows the path, not whether it is allowed.
    Turning on ``STUDYWEB_SOLVE_CLOUDFLARE`` goes a step further and is yours
    to justify for the sites you point it at.
    """

    name = "stealth"
    tier = 4
    heavy = True

    _fetcher_name = "StealthyFetcher"

    def available(self) -> bool:
        if not settings.stealth_enabled:
            self.why_unavailable = "STUDYWEB_STEALTH is off"
            return False
        return super().available()

    def _kwargs(self, timeout: float | None) -> dict:
        kw = super()._kwargs(timeout)
        # StealthyFetcher generates its own matching UA; forcing ours would
        # contradict the fingerprint it builds and defeat the point.
        kw.pop("useragent", None)
        kw["solve_cloudflare"] = settings.solve_cloudflare
        return kw


# --- registry ---------------------------------------------------------------

_ENGINES: dict[str, Engine] = {
    e.name: e for e in (
        StaticEngine(), ScraplingEngine(), ChromeEngine(),
        DynamicEngine(), StealthEngine(),
    )
}


def get_engine(name: str) -> Engine:
    """Look up an engine by name. Raises ``EngineError`` for unknown names."""
    try:
        return _ENGINES[name]
    except KeyError:
        raise EngineError(
            f"unknown fetch engine {name!r}; known: {', '.join(_ENGINES)}") from None


def all_engines() -> list[Engine]:
    """Every registered engine, weakest first."""
    return sorted(_ENGINES.values(), key=lambda e: e.tier)


def status() -> list[dict]:
    """Availability of every engine — surfaced on ``GET /health``."""
    out = []
    for e in all_engines():
        ok = e.available()
        out.append({"name": e.name, "tier": e.tier, "heavy": e.heavy,
                    "available": ok, "reason": "" if ok else e.why_unavailable})
    return out


def ladder(start: str | None = None) -> list[Engine]:
    """The escalation path: available engines from ``start`` upward.

    Configured by ``STUDYWEB_FETCH_LADDER``. Unknown or unavailable names drop
    out silently, which is what keeps the ladder honest on a machine with
    neither Scrapling nor Chrome installed — it collapses to ``[static]``.
    """
    base = get_engine(start or settings.fetch_engine)
    chain: list[Engine] = []
    for name in settings.fetch_ladder:
        try:
            eng = get_engine(name)
        except EngineError:
            log.debug("ignoring unknown engine in ladder: %s", name)
            continue
        if eng.tier <= base.tier or eng.name in {c.name for c in chain}:
            continue
        if eng.available():
            chain.append(eng)
    chain.sort(key=lambda e: e.tier)
    return ([base] if base.available() else []) + chain


def escalation_targets(start: str | None = None) -> list[Engine]:
    """Stronger engines to try after ``start`` failed or was blocked.

    Excludes ``start`` by name rather than by position: when the starting engine
    is itself unavailable it is absent from the ladder, and dropping the first
    element would silently skip a rung that could have answered.
    """
    base = get_engine(start or settings.fetch_engine)
    return [e for e in ladder(start) if e.name != base.name]
