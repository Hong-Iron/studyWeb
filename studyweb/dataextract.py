"""General, cross-site structured extraction — the orchestrator.

Combines everything into one call, ``extract_data``:

  acquire content   static fetch  ->  (headless render fallback if thin/JS)
  extract           Layer 1 structured markup (JSON-LD/microdata/OG)
                    Layer 3 local-LLM schema extraction (any readable page)

So a caller can ask "give me {name, price, specs} from this URL" and it works
across sites without per-site code — using the cheap deterministic path when a
site exposes standards, and the LLM only when it doesn't.
"""

from __future__ import annotations

import json
import logging
import re

from .config import settings
from . import net, render
from .fetch import fetch_page
from .extract import extract as _extract
from .structured import extract_structured

log = logging.getLogger("studyweb.dataextract")


# --------------------------------------------------------------------------- #
#  Local LLM (OpenAI-compatible) schema extraction                            #
# --------------------------------------------------------------------------- #

class LLMError(Exception):
    pass


def _resolve_model(base_url: str, timeout: float) -> str:
    if settings.llm_model:
        return settings.llm_model
    r = net.session().get(f"{base_url}/models", timeout=min(timeout, 15))
    r.raise_for_status()
    ids = [m["id"] for m in r.json().get("data", [])]
    if not ids:
        raise LLMError("no model loaded in the local LLM server")
    return ids[0]


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a model reply (tolerates ``` fences / prose)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.I | re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    depth = 0
    for i in range(start, len(text)) if start >= 0 else []:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    break
    raise LLMError("LLM did not return valid JSON")


def _schema_desc(schema) -> str:
    if isinstance(schema, (list, tuple)):
        return "these fields: " + ", ".join(str(s) for s in schema)
    if isinstance(schema, dict):
        return "this JSON schema: " + json.dumps(schema, ensure_ascii=False)
    return str(schema)


def llm_extract(content: str, schema, *, model: str | None = None,
                base_url: str | None = None, timeout: float | None = None,
                max_chars: int = 12000) -> dict:
    """Ask the local LLM to extract ``schema`` from ``content`` as JSON."""
    base_url = (base_url or settings.llm_base_url).rstrip("/")
    timeout = timeout or settings.llm_timeout
    model = model or _resolve_model(base_url, timeout)
    system = ("You extract structured data from web page text and return ONLY a "
              "JSON object. Use null for anything not present in the text. Never "
              "invent values. Keep prices exactly as written (including currency).")
    user = (f"Extract {_schema_desc(schema)}.\n\n"
            f"Return a JSON object with those fields.\n\n"
            f"--- PAGE CONTENT ---\n{content[:max_chars]}")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    try:
        r = net.session().post(f"{base_url}/chat/completions", json=payload, timeout=timeout)
        if r.status_code >= 400:  # some servers reject response_format; retry without
            payload.pop("response_format", None)
            r = net.session().post(f"{base_url}/chat/completions", json=payload, timeout=timeout)
        r.raise_for_status()
        reply = r.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"local LLM request failed: {exc}") from exc
    return _extract_json(reply)


# --------------------------------------------------------------------------- #
#  Orchestrator                                                               #
# --------------------------------------------------------------------------- #

def _looks_thin(doc) -> bool:
    return (not doc.ok) or doc.word_count < settings.render_thin_threshold


def extract_data(url: str, *, schema=None, render_mode: str = "auto",
                 use_llm: bool = True) -> dict:
    """Extract structured data from ``url`` across sites.

    ``schema``      list of field names / JSON schema / instruction. When given,
                    the LLM layer targets exactly these fields. Defaults to a
                    product schema.
    ``render_mode`` "auto" (render only when static is thin / has no structured
                    data and a browser is available), "always", or "never".
    ``use_llm``     allow the local-LLM fallback when structured markup is absent.

    Returns: {url, method, rendered, data, warnings}
    """
    schema = schema or ["name", "price", "currency", "brand", "description", "specs"]
    warnings: list[str] = []
    rendered = False
    recovered_from = None

    # 1) acquire (static first)
    doc = fetch_page(url)

    # If the URL failed (often a hallucinated/404 link), recover the intended
    # page by searching the same site before falling through to render/LLM.
    if not doc.ok and settings.recover_urls:
        from .recover import open_best
        rdoc, real_url, cands = open_best(
            url, max_candidates=settings.recover_max_candidates)
        if rdoc is not None and rdoc.ok:
            recovered_from, url, doc = url, real_url, rdoc
            warnings.append(f"원래 URL 실패 → 복구된 URL 사용: {real_url}")
        elif cands:
            warnings.append("요청 URL을 열 수 없음; 후보: "
                            + ", ".join(c.url for c in cands[:3]))

    html: bytes | None = None
    enc = None

    # 2) Layer 1 on static content
    data = None
    if doc.ok:
        # re-fetch raw bytes cheaply from cache for structured parsing
        try:
            resp = net.get(url, use_cache=True)
            html, enc = resp.content, resp.declared_encoding
            data = extract_structured(html, url, enc)
        except net.FetchError:
            pass

    # 3) headless render fallback
    want_render = render_mode == "always" or (
        render_mode == "auto" and (data is None) and (_looks_thin(doc) or not doc.ok))
    if want_render and render.available():
        html_str = render.render_html(url)
        if html_str:
            rendered = True
            html = html_str.encode("utf-8", "replace")
            enc = "utf-8"
            data = extract_structured(html, url, enc) or data
            doc = _rendered_document(url, html_str)
        else:
            warnings.append("headless render unavailable or produced no content")
    elif want_render and not render.available():
        warnings.append("render requested but no headless browser is available "
                        "(install Chrome/Chromium or set STUDYWEB_CHROME)")

    # 4) structured hit? return it (cheap path)
    if data and (data.get("price") or data.get("name")):
        return {"url": url, "method": f"structured:{data['source']}",
                "rendered": rendered, "recovered_from": recovered_from,
                "data": data, "warnings": warnings}

    # 5) LLM fallback
    if use_llm:
        content = doc.markdown or doc.text
        if not content:
            warnings.append("no readable content to extract from")
            return {"url": url, "method": "none", "rendered": rendered,
                    "recovered_from": recovered_from, "data": None,
                    "warnings": warnings}
        try:
            extracted = llm_extract(content, schema)
            return {"url": url, "method": "llm", "rendered": rendered,
                    "recovered_from": recovered_from, "data": extracted,
                    "warnings": warnings}
        except LLMError as exc:
            warnings.append(str(exc))

    return {"url": url, "method": "none", "rendered": rendered,
            "recovered_from": recovered_from, "data": None, "warnings": warnings}


def _rendered_document(url: str, html_str: str):
    """Build a Document from rendered HTML (bypasses the network layer)."""
    from .fetch import Document
    import time
    ex = _extract(html_str.encode("utf-8", "replace"), url, encoding="utf-8")
    return Document(url=url, final_url=url, status=200, title=ex.title,
                    text=ex.text, markdown=ex.markdown, passages=ex.passages,
                    links=ex.links, meta=ex.meta, content_type="text/html",
                    fetched_at=time.time())
