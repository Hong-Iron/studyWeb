"""A zero-dependency local HTTP API — the Tavily-compatible one.

Point any Tavily client at  http://localhost:8787  and call POST /search with
the same body shape; the response carries the same fields. Also exposes native
/extract, /rag, /tool-schema and /health endpoints.

    python -m studyweb serve --port 8787
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .config import settings
from .research import research as _research_fn, extract_urls as _extract_urls, build_rag as _build_rag
from .lms import TOOL_SCHEMAS
from .__init__ import __version__


def _auth_ok(headers, body: dict) -> bool:
    if not settings.api_key:
        return True
    supplied = body.get("api_key") or ""
    auth = headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    return supplied == settings.api_key


class Handler(BaseHTTPRequestHandler):
    server_version = f"studyweb/{__version__}"

    # -- helpers -----------------------------------------------------------
    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt, *args):  # quieter default logging
        pass

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        if path in ("/", "/health"):
            return self._send(200, {"status": "ok", "service": "studyweb",
                                    "version": __version__, "config": settings.as_dict()})
        if path in ("/tool-schema", "/tools"):
            return self._send(200, {"tools": TOOL_SCHEMAS})
        if path == "/search" and "q" in qs:
            return self._run_search({"query": qs["q"][0],
                                     "max_results": int(qs.get("max_results", ["6"])[0]),
                                     "search_depth": qs.get("search_depth", ["advanced"])[0]})
        return self._send(404, {"error": f"no route for GET {path}"})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        if not _auth_ok(self.headers, body):
            return self._send(401, {"error": "invalid or missing api_key"})
        try:
            if path == "/search":
                return self._run_search(body)
            if path == "/extract":
                return self._send(200, _extract_urls(
                    body.get("urls", []),
                    include_raw_content=bool(body.get("include_raw_content", True))))
            if path == "/rag":
                return self._send(200, _build_rag(
                    query=body.get("query"), urls=body.get("urls"),
                    max_results=body.get("max_results"),
                    crawl_depth=int(body.get("crawl_depth", 0)),
                    max_pages=int(body.get("max_pages", 20)),
                    chunk_size=int(body.get("chunk_size", 1000)),
                    overlap=int(body.get("overlap", 150)),
                    provider=body.get("provider"),
                    include_domains=body.get("include_domains"),
                    exclude_domains=body.get("exclude_domains")))
            return self._send(404, {"error": f"no route for POST {path}"})
        except Exception as exc:  # noqa: BLE001 — report cleanly to the client
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _run_search(self, body: dict):
        res = _research_fn(
            body["query"],
            max_results=body.get("max_results"),
            search_depth=body.get("search_depth", "advanced"),
            include_answer=bool(body.get("include_answer", True)),
            include_raw_content=bool(body.get("include_raw_content", False)),
            provider=body.get("provider"),
            site=body.get("site"),
            include_domains=body.get("include_domains"),
            exclude_domains=body.get("exclude_domains"))
        return self._send(200, res)


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"studyweb API on http://{host}:{port}  "
          f"(POST /search  /extract  /rag  ·  GET /health  /tool-schema)")
    if settings.api_key:
        print("  auth: STUDYWEB_API_KEY required")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        httpd.shutdown()
