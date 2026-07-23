"""Polite HTTP layer: one shared session, per-host rate limiting, retries,
size caps, an on-disk cache, and a robots.txt gate. Everything that touches
the network goes through here."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

import requests

from .config import settings
from . import robots


class FetchError(Exception):
    """Raised when a URL cannot be fetched (network, HTTP, robots, or size)."""


@dataclass
class Response:
    url: str          # final URL after redirects
    status: int
    content: bytes
    headers: dict
    from_cache: bool = False

    @property
    def text(self) -> str:
        enc = self.encoding
        try:
            return self.content.decode(enc, "replace")
        except LookupError:
            return self.content.decode("utf-8", "replace")

    @property
    def encoding(self) -> str:
        ctype = self.headers.get("content-type", "")
        if "charset=" in ctype:
            return ctype.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
        return "utf-8"

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


# --- per-host rate limiting -------------------------------------------------

_host_locks: dict[str, threading.Lock] = {}
_host_last: dict[str, float] = {}
_hosts_guard = threading.Lock()


def _throttle(host: str) -> None:
    delay = settings.per_host_delay
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
            }, fh)
        os.replace(tmp, path)
    except Exception:
        pass  # cache failures are never fatal


# --- fetching ---------------------------------------------------------------

def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def get(url: str, *, params: dict | None = None, use_cache: bool = True,
        check_robots: bool = True, accept: str | None = None) -> Response:
    """GET a URL politely. Honours robots.txt, rate limits per host, retries
    with backoff, caps download size, and caches successful GETs."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError(f"unsupported scheme: {parts.scheme!r}")
    host = parts.netloc.lower()

    if check_robots and settings.respect_robots and not robots.allowed(url):
        raise FetchError(f"blocked by robots.txt: {url}")

    cache_key = url + ("?" + json.dumps(params, sort_keys=True) if params else "")
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    headers = {"Accept": accept} if accept else None
    last_exc: Exception | None = None
    for attempt in range(settings.max_retries + 1):
        _throttle(host)
        try:
            r = session().get(url, params=params, headers=headers,
                              timeout=settings.timeout, stream=True,
                              allow_redirects=True)
            chunks, total = [], 0
            for chunk in r.iter_content(64 * 1024):
                chunks.append(chunk)
                total += len(chunk)
                if total > settings.max_bytes:
                    r.close()
                    raise FetchError(f"response exceeds max_bytes ({settings.max_bytes})")
            resp = Response(url=r.url, status=r.status_code,
                            content=b"".join(chunks), headers=dict(r.headers))
            if resp.status in (429, 500, 502, 503, 504) and attempt < settings.max_retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            if use_cache:
                _cache_put(cache_key, resp)
            return resp
        except FetchError:
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < settings.max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
    raise FetchError(f"failed to fetch {url}: {last_exc}")


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
