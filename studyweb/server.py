"""A zero-dependency local HTTP API — the Tavily-compatible one.

Point any Tavily client at  http://localhost:8787  and call POST /search with
the same body shape; the response carries the same fields. Also exposes native
/extract, /rag, /tool-schema and /health endpoints, plus the model layer:
/providers (connection status), /chat and /agent (run a local or cloud model),
/usage and /pricing (what it cost).

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
from .providers import ProviderError
from .__init__ import __version__

log = logging.getLogger("studyweb.server")

# Bound the number of concurrent research/rag pipelines so a burst of clients
# can't fan out into an unbounded number of outbound fetches.
_pipeline_gate = threading.Semaphore(max(1, settings.max_concurrent_requests))


class BadRequest(Exception):
    """Malformed client input — surfaced as HTTP 400."""


# A provider failure is the provider's fault, not ours: pass a status the client
# can act on (and a `kind` the GUI turns into a status light) instead of a 500.
_PROVIDER_STATUS = {
    "unknown_provider": 400,  # the client named something that doesn't exist
    "no_key": 428,            # precondition required — configure a key
    "no_model": 428,
    "not_installed": 428,
    "unauthorized": 401,
    "rate_limit": 429,
    "unreachable": 502,
    "server_error": 502,
    "bad_response": 502,
}


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
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if settings.cors_allow_origin:
                self.send_header("Access-Control-Allow-Origin", settings.cors_allow_origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # The client timed out or cancelled while we were still working.
            # There is no one left to tell, and the error path would just call
            # _send again — which is what turns this into a socketserver dump.
            log.info("%s - client hung up before the response was sent",
                     self.address_string())

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
            if path == "/providers":
                return self._send(200, self._providers(qs))
            if path == "/models":
                from . import providers as _p
                pid = qs.get("provider", [settings.llm_provider])[0]
                return self._send(200, {"provider": pid, "models": _p.list_models(pid)})
            if path == "/usage":
                from .usage import ledger
                days = int(qs.get("days", ["7"])[0] or 7)
                return self._send(200, ledger.summary(days=max(1, min(90, days))))
            if path == "/pricing":
                from .usage import load_pricing, pricing_path
                return self._send(200, {"pricing": load_pricing(refresh=True),
                                        "path": pricing_path(),
                                        "unit": "USD per 1M tokens"})
            return self._send(404, {"error": f"no route for GET {path}"})
        except BadRequest as exc:
            return self._send(400, {"error": str(exc)})
        except ProviderError as exc:
            return self._send(_PROVIDER_STATUS.get(exc.kind, 502), exc.to_dict())
        except (BrokenPipeError, ConnectionResetError):
            return log.info("%s - client hung up during GET %s", self.address_string(), path)
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
            if path == "/prices":
                q = body.get("query")
                if not q or not str(q).strip():
                    raise BadRequest("'query' is required")
                sites = body.get("sites")
                if sites is not None and not isinstance(sites, list):
                    raise BadRequest("'sites' must be an array of domains")
                from .prices import find_prices
                with _pipeline_gate:
                    return self._send(200, find_prices(
                        str(q), sites=sites,
                        per_site=_as_int(body, "per_site", 3, lo=1, hi=10)))
            if path == "/extract":
                urls = body.get("urls")
                if not isinstance(urls, list) or not urls:
                    raise BadRequest("'urls' must be a non-empty array")
                with _pipeline_gate:
                    return self._send(200, _extract_urls(
                        urls, include_raw_content=bool(body.get("include_raw_content", True))))
            if path == "/extract-data":
                url_ = body.get("url")
                if not url_ or not str(url_).strip():
                    raise BadRequest("'url' is required")
                render_mode = body.get("render", "auto")
                if render_mode not in ("auto", "always", "never"):
                    raise BadRequest("'render' must be auto|always|never")
                from .dataextract import extract_data
                with _pipeline_gate:
                    return self._send(200, extract_data(
                        str(url_), schema=body.get("fields") or body.get("schema"),
                        render_mode=render_mode,
                        use_llm=bool(body.get("use_llm", True)),
                        llm_provider=body.get("provider")))
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
            if path == "/chat":
                return self._run_chat(body)
            if path == "/agent":
                return self._run_agent(body)
            if path == "/usage/reset":
                from .usage import ledger
                scope = body.get("scope", "session")
                if scope not in ("session", "all"):
                    raise BadRequest("'scope' must be session|all")
                return self._send(200, ledger.reset(scope))
            return self._send(404, {"error": f"no route for POST {path}"})
        except BadRequest as exc:
            return self._send(400, {"error": str(exc)})
        except ProviderError as exc:
            return self._send(_PROVIDER_STATUS.get(exc.kind, 502), exc.to_dict())
        except (BrokenPipeError, ConnectionResetError):
            return log.info("%s - client hung up during POST %s", self.address_string(), path)
        except Exception as exc:  # noqa: BLE001 — report cleanly to the client
            log.exception("POST %s failed", path)
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    # -- model providers ---------------------------------------------------
    def _providers(self, qs: dict) -> dict:
        """Provider configuration, and their live connection status on request.

        ``?probe=1``           actually dial each provider (slower, authoritative)
        ``?only_configured=1`` don't dial providers with no key
        ``?provider=openai``   probe just one
        """
        from . import providers as _p
        probe = (qs.get("probe", ["0"])[0] or "0").lower() in ("1", "true", "yes")
        one = qs.get("provider", [""])[0]
        if one:
            return {"providers": [_p.check(one) if probe else _p.describe(_p.resolve(one))],
                    "default": settings.llm_provider, "probed": probe}
        if probe:
            only = (qs.get("only_configured", ["0"])[0] or "0").lower() in ("1", "true", "yes")
            rows = _p.check_all(only_configured=only)
        else:
            rows = _p.list_providers()
        return {"providers": rows, "default": settings.llm_provider, "probed": probe}

    def _run_chat(self, body: dict):
        """One model turn. The client owns the conversation; we own the wire
        format, the usage accounting and the error taxonomy."""
        from .providers import chat as _chat
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise BadRequest("'messages' must be a non-empty array")
        tools = body.get("tools")
        if tools is True:
            tools = TOOL_SCHEMAS
        elif tools is False:
            tools = None
        elif tools is not None and not isinstance(tools, list):
            raise BadRequest("'tools' must be true, false, or an array of tool schemas")
        out = _chat(messages, provider=body.get("provider"), model=body.get("model"),
                    tools=tools, temperature=body.get("temperature"),
                    max_tokens=body.get("max_tokens"),
                    label=body.get("label") or "chat")
        return self._send(200, {"message": out["message"], "usage": out["usage"].to_dict(),
                                "provider": out["provider"], "model": out["model"]})

    def _run_agent(self, body: dict):
        """Full tool-calling loop server-side: one question in, an answer plus
        the trace and what it cost out."""
        from .agent import run_agent, SYSTEM_PROMPT
        query = body.get("query") or body.get("q")
        if not query or not str(query).strip():
            raise BadRequest("'query' is required")
        with _pipeline_gate:
            res = run_agent(
                str(query), provider=body.get("provider"), model=body.get("model"),
                temperature=body.get("temperature"),
                max_steps=_as_int(body, "max_steps", 6, lo=1, hi=12),
                system=body.get("system") or SYSTEM_PROMPT)
        return self._send(200, res)

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
    print(f"studyweb API on http://{host}:{port}\n"
          f"  web    POST /search /extract /extract-data /prices /rag  ·  GET /health /tool-schema\n"
          f"  models POST /chat /agent /usage/reset  ·  GET /providers /models /usage /pricing")
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
