"""Polite HTTP layer: one shared session, per-host rate limiting, retries,
size caps, an on-disk cache, and a robots.txt gate. Everything that touches
the network goes through here."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

import requests

from .config import settings
from . import engines, robots

log = logging.getLogger("studyweb.net")


class FetchError(Exception):
    """Raised when a URL cannot be fetched (network, HTTP, robots, or size)."""


@dataclass
class Response:
    url: str          # final URL after redirects
    status: int
    content: bytes
    headers: dict
    from_cache: bool = False
    engine: str = ""  # which studyweb.engines backend produced this

    @property
    def text(self) -> str:
        enc = self.encoding
        try:
            return self.content.decode(enc, "replace")
        except LookupError:
            return self.content.decode("utf-8", "replace")

    @property
    def encoding(self) -> str:
        return self.declared_encoding or "utf-8"

    @property
    def declared_encoding(self) -> str | None:
        """Charset from the HTTP Content-Type header, or None if not declared
        (so the HTML parser can fall back to the page's own <meta charset>)."""
        ctype = self.headers.get("content-type", "") or self.headers.get("Content-Type", "")
        if "charset=" in ctype:
            return ctype.split("charset=", 1)[1].split(";", 1)[0].strip() or None
        return None

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()


# --- shared session ---------------------------------------------------------

_session_lock = threading.Lock()
_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                          "image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "Sec-Ch-Ua": '"Chromium";v="126", "Not.A/Brand";v="24"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Upgrade-Insecure-Requests": "1",
            })
            _session = s
        return _session


# --- SSRF guard -------------------------------------------------------------

def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


def _guard_ssrf(host: str) -> None:
    """Refuse to fetch hosts that resolve to private/loopback/link-local IPs.
    Blocks the classic cloud-metadata / internal-service SSRF via /extract and
    /rag. Disable with STUDYWEB_ALLOW_PRIVATE=true for intranet use."""
    if settings.allow_private_hosts:
        return
    bare = host.split(":", 1)[0]
    try:
        infos = socket.getaddrinfo(bare, None)
    except socket.gaierror:
        return  # let the normal request fail with a clear network error
    for info in infos:
        ip = info[4][0]
        if _is_private_ip(ip):
            raise FetchError(
                f"refusing to fetch private/loopback address for host {bare!r} "
                f"({ip}); set STUDYWEB_ALLOW_PRIVATE=true to allow")


# --- per-host rate limiting -------------------------------------------------

_host_locks: dict[str, threading.Lock] = {}
_host_last: dict[str, float] = {}
_hosts_guard = threading.Lock()


def _throttle(host: str, delay: float | None = None) -> None:
    delay = settings.per_host_delay if delay is None else delay
    if delay <= 0:
        return
    with _hosts_guard:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        last = _host_last.get(host, 0.0)
        wait = delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _host_last[host] = time.monotonic()


# --- on-disk cache ----------------------------------------------------------

def _cache_path(key: str) -> str:
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(settings.cache_dir, h[:2], h + ".json")


def _cache_get(key: str) -> Response | None:
    if not settings.cache_enabled:
        return None
    path = _cache_path(key)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    if settings.cache_ttl > 0 and time.time() - st.st_mtime > settings.cache_ttl:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return Response(
            url=data["url"], status=data["status"],
            content=bytes.fromhex(data["content"]),
            headers=data["headers"], from_cache=True,
            engine=data.get("engine", ""),  # absent in caches written pre-engines
        )
    except Exception:
        return None


def _cache_put(key: str, resp: Response) -> None:
    if not settings.cache_enabled or resp.status != 200:
        return
    path = _cache_path(key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({
                "url": resp.url, "status": resp.status,
                "content": resp.content.hex(), "headers": resp.headers,
                "engine": resp.engine,
            }, fh)
        os.replace(tmp, path)
    except Exception:
        pass  # cache failures are never fatal
    _maybe_evict_cache()


_evict_counter = 0
_evict_lock = threading.Lock()


def _maybe_evict_cache() -> None:
    """Occasionally enforce ``cache_max_mb`` by deleting oldest entries.
    Sampled (every ~50 writes) so it adds negligible overhead to the hot path."""
    global _evict_counter
    if settings.cache_max_mb <= 0:
        return
    with _evict_lock:
        _evict_counter += 1
        if _evict_counter % 50 != 1:
            return
    budget = int(settings.cache_max_mb * 1024 * 1024)
    try:
        entries = []
        total = 0
        for root, _dirs, files in os.walk(settings.cache_dir):
            for name in files:
                fp = os.path.join(root, name)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, fp))
                total += st.st_size
        if total <= budget:
            return
        entries.sort()  # oldest first
        for _mtime, size, fp in entries:
            if total <= budget:
                break
            try:
                os.remove(fp)
                total -= size
            except OSError:
                pass
    except OSError:
        pass


# --- fetching ---------------------------------------------------------------

def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def _try_engine(eng, url: str, *, params: dict | None, headers: dict | None,
                host: str, delay: float, on_redirect) -> Response:
    """Run one engine to completion, with this module's throttle and retries.

    Retrying belongs here rather than in the engine so that every backend obeys
    the same per-host delay — a Scrapling fetcher with its own retry loop would
    otherwise multiply against ours and hammer the host.
    """
    last_exc: Exception | None = None
    for attempt in range(settings.max_retries + 1):
        _throttle(host, delay)
        try:
            fetched = eng.fetch(url, params=params, headers=headers,
                                timeout=settings.timeout, on_redirect=on_redirect)
        except (FetchError, engines.TooLarge):
            raise  # policy failures are final: never retried, never escalated
        except (requests.RequestException, engines.EngineError) as exc:
            last_exc = exc
            if attempt < settings.max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise engines.EngineError(f"{eng.name}: {exc}") from exc

        resp = Response(url=fetched.url, status=fetched.status,
                        content=fetched.content, headers=fetched.headers,
                        engine=fetched.engine or eng.name)
        if resp.status in (429, 500, 502, 503, 504) and attempt < settings.max_retries:
            wait = _retry_after(resp.headers) or 1.5 * (attempt + 1)
            log.debug("retrying %s after %.1fs (status %s)", url, wait, resp.status)
            time.sleep(wait)
            continue
        return resp
    raise engines.EngineError(f"{eng.name}: {last_exc}")


def get(url: str, *, params: dict | None = None, use_cache: bool = True,
        check_robots: bool = True, accept: str | None = None,
        engine: str | None = None, escalate: bool | None = None) -> Response:
    """GET a URL politely. Honours robots.txt, rate limits per host, retries
    with backoff, caps download size, and caches successful GETs.

    The transport is pluggable (see :mod:`studyweb.engines`) but the policy
    above is not: robots, the SSRF guard, throttling, the size cap and the cache
    all run here, around whichever engine ends up doing the work.

    When ``escalate`` is on and a page comes back looking like an anti-bot wall
    rather than content, the next engine up the ladder is tried. If every engine
    is walled, the first walled response is returned as-is — callers read
    ``status`` themselves and a 403 is an answer, not an exception.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError(f"unsupported scheme: {parts.scheme!r}")
    host = parts.netloc.lower()

    _guard_ssrf(host)
    if check_robots and settings.respect_robots and not robots.allowed(url):
        raise FetchError(f"blocked by robots.txt: {url}")

    # Prefer a robots-declared Crawl-delay over the flat per-host delay.
    delay = settings.per_host_delay
    if check_robots and settings.respect_robots:
        cd = robots.crawl_delay(url)
        if cd is not None:
            delay = max(delay, cd)

    def gate_redirect(final_url: str) -> None:
        """A redirect may have crossed onto a disallowed path or a private
        host — re-check the destination we did not choose."""
        final_host = urlsplit(final_url).netloc.lower()
        if final_host != host:
            _guard_ssrf(final_host)
        if check_robots and settings.respect_robots and not robots.allowed(final_url):
            raise FetchError(f"redirect blocked by robots.txt: {final_url}")

    headers = {"Accept": accept} if accept else None
    chain = engines.ladder(engine)
    if not chain:
        raise FetchError(
            f"no fetch engine available (start: {engine or settings.fetch_engine!r})")
    if not (settings.fetch_escalate if escalate is None else escalate):
        chain = chain[:1]

    # Key the cache by the engine we *start* on, not the one that wins. Two
    # engines can see very different pages at the same URL, so sharing one key
    # would let a thin static response satisfy a later dynamic fetch; keying on
    # the start instead means a repeat call skips the whole ladder it already
    # walked and lands straight on the content that worked.
    cache_key = chain[0].name + "|" + url
    if params:
        cache_key += "?" + json.dumps(params, sort_keys=True)
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    walled: Response | None = None
    last_exc: Exception | None = None
    for eng in chain:
        try:
            resp = _try_engine(eng, url, params=params, headers=headers,
                               host=host, delay=delay, on_redirect=gate_redirect)
        except FetchError:
            raise
        except engines.TooLarge as exc:
            raise FetchError(str(exc)) from exc
        except engines.EngineError as exc:
            last_exc = exc
            log.debug("engine %s failed for %s: %s", eng.name, url, exc)
            continue

        if not engines.looks_blocked(resp.status, resp.content):
            if use_cache:
                _cache_put(cache_key, resp)
            return resp

        # Blocked: remember the first wall so we can still answer if the whole
        # ladder is walled, then try something stronger.
        if walled is None:
            walled = resp
        log.info("engine %s looks blocked for %s (status %s); escalating",
                 eng.name, url, resp.status)

    if walled is not None:
        return walled
    raise FetchError(f"failed to fetch {url}: {last_exc}")


def _retry_after(headers: dict) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds."""
    val = headers.get("Retry-After") or headers.get("retry-after")
    if not val:
        return None
    val = val.strip()
    if val.isdigit():
        return min(float(val), 30.0)  # cap so a hostile header can't hang us
    try:
        dt = parsedate_to_datetime(val)
        secs = (dt.timestamp() - time.time())
        return max(0.0, min(secs, 30.0))
    except (TypeError, ValueError):
        return None


def get_many(urls: Iterable[str], worker: Callable[[str], object] | None = None,
             max_workers: int | None = None) -> list:
    """Run ``worker(url)`` (default: :func:`get`) across URLs concurrently,
    preserving input order. Exceptions are captured and returned in-place."""
    urls = list(urls)
    fn = worker or get
    results: list = [None] * len(urls)
    workers = max_workers or settings.max_workers
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls) or 1))) as ex:
        futs = {ex.submit(fn, u): i for i, u in enumerate(urls)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as exc:  # noqa: BLE001 — surfaced to caller per-item
                results[i] = exc
    return results
