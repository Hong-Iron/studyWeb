"""A zero-dependency local HTTP API — the Tavily-compatible one.

Point any Tavily client at  http://localhost:8787  and call POST /search with
the same body shape; the response carries the same fields. Also exposes native
/extract, /rag, /tool-schema and /health endpoints.

    python -m studyweb serve --port 8787
"""

from __future__ import annotations

import hmac
import json
import logging
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .config import settings
from .research import research as _research_fn, extract_urls as _extract_urls, build_rag as _build_rag
from .lms import TOOL_SCHEMAS
from .__init__ import __version__

log = logging.getLogger("studyweb.server")

# Bound the number of concurrent research/rag pipelines so a burst of clients
# can't fan out into an unbounded number of outbound fetches.
_pipeline_gate = threading.Semaphore(max(1, settings.max_concurrent_requests))


class BadRequest(Exception):
    """Malformed client input — surfaced as HTTP 400."""


def _auth_ok(headers, body: dict) -> bool:
    if not settings.api_key:
        return True
    supplied = body.get("api_key") or ""
    auth = headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    # Constant-time compare so the key can't be recovered by timing.
    return hmac.compare_digest(str(supplied), settings.api_key)


def _as_int(body: dict, key: str, default: int, *, lo: int, hi: int) -> int:
    """Coerce a body field to an int and clamp it to [lo, hi]. Raises
    BadRequest on non-numeric input so callers get a 400, not a 500."""
    v = body.get(key, default)
    if v is None:
        v = default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise BadRequest(f"{key!r} must be an integer, got {v!r}")
    return max(lo, min(hi, n))


class Handler(BaseHTTPRequestHandler):
    server_version = f"studyweb/{__version__}"

    # -- helpers -----------------------------------------------------------
    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if settings.cors_allow_origin:
            self.send_header("Access-Control-Allow-Origin", settings.cors_allow_origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        if n > settings.max_request_bytes:
            raise BadRequest(f"request body too large ({n} > {settings.max_request_bytes} bytes)")
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            raise BadRequest("request body is not valid JSON")

    def log_message(self, fmt, *args):  # route default logging through logging
        log.info("%s - %s", self.address_string(), fmt % args)

    # -- routes ------------------------------------------------------------
    def do_OPTIONS(self):
        # CORS preflight — only meaningful when an origin is configured.
        self.send_response(204)
        if settings.cors_allow_origin:
            self.send_header("Access-Control-Allow-Origin", settings.cors_allow_origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        # Public, unauthenticated endpoints.
        if path in ("/", "/health"):
            return self._send(200, {"status": "ok", "service": "studyweb",
                                    "version": __version__, "config": settings.as_dict()})
        if path in ("/tool-schema", "/tools"):
            return self._send(200, {"tools": TOOL_SCHEMAS})
        # Everything else requires auth (GET /search was previously unguarded).
        if not _auth_ok(self.headers, {}):
            return self._send(401, {"error": "invalid or missing api_key"})
        try:
            if path == "/search" and "q" in qs:
                return self._run_search({
                    "query": qs["q"][0],
                    "max_results": qs.get("max_results", ["6"])[0],
                    "search_depth": qs.get("search_depth", ["advanced"])[0]})
            return self._send(404, {"error": f"no route for GET {path}"})
        except BadRequest as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 — report cleanly to the client
            log.exception("GET %s failed", path)
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
        except BadRequest as exc:
            return self._send(400, {"error": str(exc)})
        if not _auth_ok(self.headers, body):
            return self._send(401, {"error": "invalid or missing api_key"})
        try:
            if path == "/search":
                return self._run_search(body)
            if path == "/extract":
                urls = body.get("urls")
                if not isinstance(urls, list) or not urls:
                    raise BadRequest("'urls' must be a non-empty array")
                with _pipeline_gate:
                    return self._send(200, _extract_urls(
                        urls, include_raw_content=bool(body.get("include_raw_content", True))))
            if path == "/rag":
                if not body.get("query") and not body.get("urls"):
                    raise BadRequest("provide 'query' and/or 'urls'")
                with _pipeline_gate:
                    return self._send(200, _build_rag(
                        query=body.get("query"), urls=body.get("urls"),
                        max_results=_as_int(body, "max_results", settings.default_max_results, lo=1, hi=25),
                        crawl_depth=_as_int(body, "crawl_depth", 0, lo=0, hi=3),
                        max_pages=_as_int(body, "max_pages", 20, lo=1, hi=100),
                        chunk_size=_as_int(body, "chunk_size", 1000, lo=100, hi=8000),
                        overlap=_as_int(body, "overlap", 150, lo=0, hi=2000),
                        provider=body.get("provider"),
                        include_domains=body.get("include_domains"),
                        exclude_domains=body.get("exclude_domains")))
            return self._send(404, {"error": f"no route for POST {path}"})
        except BadRequest as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 — report cleanly to the client
            log.exception("POST %s failed", path)
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _run_search(self, body: dict):
        query = body.get("query")
        if not query or not str(query).strip():
            raise BadRequest("'query' is required")
        with _pipeline_gate:
            res = _research_fn(
                str(query),
                max_results=_as_int(body, "max_results", settings.default_max_results, lo=1, hi=25),
                search_depth=body.get("search_depth", "advanced"),
                include_answer=bool(body.get("include_answer", True)),
                include_raw_content=bool(body.get("include_raw_content", False)),
                provider=body.get("provider"),
                site=body.get("site"),
                include_domains=body.get("include_domains"),
                exclude_domains=body.get("exclude_domains"))
        return self._send(200, res)


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"studyweb API on http://{host}:{port}  "
          f"(POST /search  /extract  /rag  ·  GET /health  /tool-schema)")
    if settings.api_key:
        print("  auth: STUDYWEB_API_KEY required")

    def _stop(signum, _frame):
        print(f"\nreceived signal {signum}, shutting down")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
