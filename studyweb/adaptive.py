"""Self-healing selectors — the piece of Scrapling's parser studyweb wants.

studyweb's generic pipeline deliberately avoids CSS selectors: :mod:`extract`
scores text density, :mod:`structured` reads JSON-LD/microdata, and
:mod:`dataextract` falls back to an LLM. Nothing there names a class, so
nothing there breaks when a site is redesigned.

Site adapters are the exception, and they have to be. ``ItmayaAdapter`` names
``.pay_start`` and ``.group_component`` because the per-component prices live
nowhere else on the page — no JSON-LD, no microdata. Hardcoded class names are
precisely what a redesign breaks, and that is the failure mode Scrapling's
adaptive tracking exists for: select an element once with ``auto_save`` and it
records a fingerprint (tag, attributes, text, siblings, position); when the
selector later matches nothing, it relocates the element by similarity instead
of returning empty.

Optional dependency, and the fallback is total. Without Scrapling installed,
:func:`select` returns None and every caller keeps its existing lxml path, so
adapters behave exactly as they did before this module existed.

    els = adaptive.select(raw_html, url, ".pay_start", identifier="itmaya:pay")
    if els is None:               # Scrapling absent — use the lxml path
        els = doc.xpath(...)

Fingerprints live in a SQLite file under the studyweb state dir, keyed by
domain, so one site's layout can never be used to relocate another's.
"""

from __future__ import annotations

import logging
import os
import threading
from urllib.parse import urlsplit

from .config import settings

log = logging.getLogger("studyweb.adaptive")

_lock = threading.Lock()
_warned = False


def storage_path() -> str:
    """Where element fingerprints are kept."""
    from .usage import state_dir

    return os.path.join(state_dir(), "adaptive.db")


def _selector_class():
    """Scrapling's ``Selector``, or None when the extra is not installed."""
    try:
        from scrapling import Selector
    except Exception:  # noqa: BLE001 — ImportError, or any import-time failure
        return None
    return Selector


def enabled() -> bool:
    """True if adaptive selectors are switched on *and* available."""
    if not settings.adaptive_selectors:
        return False
    return _selector_class() is not None


def _warn_once() -> None:
    global _warned
    with _lock:
        if not _warned:
            _warned = True
            from .engines import INSTALL_HINT
            log.info("adaptive selectors requested but scrapling is not "
                     "installed; install with: %s", INSTALL_HINT)


def parse(html: bytes | str, url: str, encoding: str | None = None):
    """An adaptive Scrapling ``Selector`` over ``html``, or None if unavailable.

    Use this when you want several selectors against one document; each
    :func:`select` call reparses.
    """
    if not settings.adaptive_selectors:
        return None
    Selector = _selector_class()
    if Selector is None:
        _warn_once()
        return None

    path = storage_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as exc:
        log.warning("cannot create adaptive storage dir: %s", exc)
        return None

    domain = urlsplit(url).netloc.lower()
    try:
        return Selector(
            content=html,
            url=url,
            encoding=encoding or "utf-8",
            adaptive=True,
            # Key fingerprints by domain rather than full URL: every product
            # page on a site shares a template, and per-URL keys would store a
            # fingerprint per product and never get a hit on a new one.
            adaptive_domain=domain,
            storage_args={"storage_file": path, "url": domain},
        )
    except Exception as exc:  # noqa: BLE001 — never let this break a fetch
        log.warning("adaptive selector unavailable for %s: %s", url, exc)
        return None


def select(html: bytes | str, url: str, css: str, *, identifier: str,
           encoding: str | None = None, percentage: int | None = None):
    """Elements matching ``css``, relocated by similarity if the selector broke.

    Returns None — not an empty list — when adaptive selection is unavailable,
    so callers can tell "Scrapling is not installed, use the lxml path" apart
    from "Scrapling looked and this page genuinely has no such element".

    ``identifier`` names the thing being selected (e.g. ``"itmaya:pay_start"``),
    and is what the fingerprint is stored under. It must be stable across runs;
    change it and you start from a blank fingerprint.
    """
    sel = parse(html, url, encoding)
    if sel is None:
        return None
    return select_in(sel, css, identifier=identifier, percentage=percentage)


def select_in(sel, css: str, *, identifier: str, percentage: int | None = None):
    """:func:`select` against an already-parsed :func:`parse` result."""
    if sel is None:
        return None
    try:
        found = sel.css(css, adaptive=True, auto_save=True, identifier=identifier,
                        percentage=percentage or settings.adaptive_percentage)
    except Exception as exc:  # noqa: BLE001 — a bad selector must not break a fetch
        log.warning("adaptive select failed for %r (%s): %s", css, identifier, exc)
        return None
    return list(found or [])


def text_of(el) -> str:
    """All text under a Scrapling element, as a plain ``str``.

    The one place that touches Scrapling's element API, so callers can stay
    written against lxml and never grow a second element vocabulary.
    """
    for attr in ("get_all_text", "text"):
        try:
            val = getattr(el, attr)
            val = val() if callable(val) else val
        except Exception:  # noqa: BLE001
            continue
        if val:
            return str(val).strip()
    return ""


def first_text(html: bytes | str, url: str, css: str, *, identifier: str,
               encoding: str | None = None) -> str | None:
    """Text of the first element matching ``css``, relocated if the selector
    broke. None means "adaptive is unavailable or found nothing" — either way
    the caller should keep whatever its own parser produced.
    """
    els = select(html, url, css, identifier=identifier, encoding=encoding)
    if not els:
        return None
    return text_of(els[0]) or None


def status() -> dict:
    """Reported on ``GET /health`` so a stale fingerprint DB is discoverable."""
    Selector = _selector_class()
    path = storage_path()
    return {
        "enabled": bool(settings.adaptive_selectors),
        "available": Selector is not None,
        "storage": path,
        "storage_exists": os.path.exists(path),
    }
