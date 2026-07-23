"""The high-level pipelines.

``research()`` is the Tavily-compatible one: search -> fetch -> locally rank
passages -> clean answer + per-result content. No hosted API involved.

``build_rag()`` is the study/data-collection one: search or crawl -> strip ->
clean -> chunk -> RAG-ready records, ready to embed and feed to an LLM.
"""

from __future__ import annotations

import time

from .config import settings
from .search import search
from .fetch import fetch_many, Document
from .crawl import crawl
from .rank import Passage, rank_passages, extractive_answer
from .clean import to_chunks as _to_chunks


def research(query: str, *, max_results: int | None = None,
             search_depth: str = "advanced", include_answer: bool = True,
             include_raw_content: bool = False, provider: str | None = None,
             include_domains: list[str] | None = None,
             exclude_domains: list[str] | None = None,
             site: str | None = None,
             max_content_chars: int | None = None) -> dict:
    """Tavily-shaped web research, computed entirely locally.

    Pass ``site`` (e.g. "danawa.com") to search that site directly with no
    external search engine.

    Returns: {query, answer, results:[{title,url,content,score,raw_content?}],
              response_time, provider, ...}
    """
    t0 = time.time()
    n = max_results or settings.default_max_results
    cap = max_content_chars or settings.passage_chars

    results = search(query, provider=provider, max_results=n, site=site,
                     include_domains=include_domains, exclude_domains=exclude_domains)

    if search_depth == "basic":
        # Fast path: rank on snippets, don't fetch full pages.
        out = [{
            "title": r.title, "url": r.url,
            "content": r.snippet, "score": r.score or 0.0,
            "source": r.source,
        } for r in results]
        answer = ""
        if include_answer:
            ps = [Passage(r.snippet, r.url, r.title) for r in results if r.snippet]
            answer = extractive_answer(query, ps, max_chars=cap)
        return {
            "query": query, "answer": answer, "results": out,
            "provider": results[0].source if results else (provider or "auto"),
            "search_depth": "basic",
            "response_time": round(time.time() - t0, 3),
        }

    # Advanced path: fetch pages concurrently, rank passages locally.
    docs = fetch_many([r.url for r in results])
    doc_by_url = {d.url: d for d in docs}

    out = []
    ranked_by_url: dict[str, list] = {}
    for r in results:
        d = doc_by_url.get(r.url)
        content, score, raw = r.snippet, 0.0, None
        if d and d.ok:
            ranked = rank_passages(query, [Passage(p, d.url, d.title) for p in d.passages], top_k=4)
            ranked_by_url[r.url] = ranked
            if ranked:
                content = "\n\n".join(p.text for p, _ in ranked)[:cap]
                score = round(max(s for _, s in ranked), 4)
            else:
                content = (d.text or r.snippet)[:cap]
            if include_raw_content:
                raw = d.markdown or d.text
        item = {"title": (d.title if d and d.title else r.title), "url": r.url,
                "content": content, "score": score, "source": r.source}
        if include_raw_content:
            item["raw_content"] = raw
        out.append(item)

    out.sort(key=lambda x: x["score"], reverse=True)

    answer = ""
    if include_answer:
        # Ground the answer in the best passages of the top few results only,
        # so an off-topic hit can't inject sentences via a common keyword.
        answer_pool: list[Passage] = []
        for item in out[:3]:
            for p, _ in ranked_by_url.get(item["url"], [])[:3]:
                answer_pool.append(p)
        answer = extractive_answer(query, answer_pool, max_chars=cap)

    return {
        "query": query, "answer": answer, "results": out,
        "provider": results[0].source if results else (provider or "auto"),
        "search_depth": "advanced",
        "response_time": round(time.time() - t0, 3),
    }


def extract_urls(urls: list[str], *, include_raw_content: bool = True) -> dict:
    """Tavily /extract-compatible: fetch URLs and return clean content."""
    t0 = time.time()
    docs = fetch_many(urls)
    ok, failed = [], []
    for d in docs:
        if d.ok:
            item = {"url": d.url, "title": d.title,
                    "content": d.markdown or d.text}
            if include_raw_content:
                item["raw_content"] = d.text
            ok.append(item)
        else:
            failed.append({"url": d.url, "error": d.error or f"status {d.status}"})
    return {"results": ok, "failed_results": failed,
            "response_time": round(time.time() - t0, 3)}


def build_rag(*, query: str | None = None, urls: list[str] | None = None,
              max_results: int | None = None, crawl_depth: int = 0,
              max_pages: int = 20, chunk_size: int = 1000, overlap: int = 150,
              min_quality: float = 0.25, provider: str | None = None,
              include_domains: list[str] | None = None,
              exclude_domains: list[str] | None = None) -> dict:
    """Crawl -> strip -> clean -> chunk -> RAG records.

    Provide a ``query`` (searches, then optionally crawls each hit to
    ``crawl_depth``) and/or explicit ``urls``. Output is ready to embed.
    """
    t0 = time.time()
    seeds: list[str] = list(urls or [])
    if query:
        hits = search(query, provider=provider,
                      max_results=max_results or settings.default_max_results,
                      include_domains=include_domains, exclude_domains=exclude_domains)
        seeds.extend(h.url for h in hits)

    # de-dup seeds, preserve order
    seen, ordered = set(), []
    for u in seeds:
        if u not in seen:
            seen.add(u); ordered.append(u)

    docs: list[Document] = []
    if crawl_depth > 0:
        per_seed = max(1, max_pages // max(len(ordered), 1))
        for u in ordered:
            docs.extend(crawl(u, depth=crawl_depth, max_pages=per_seed))
            if len(docs) >= max_pages:
                break
    else:
        docs = fetch_many(ordered)

    records, doc_summ = [], []
    for d in docs:
        chunks = _to_chunks(d, chunk_size=chunk_size, overlap=overlap,
                            min_quality=min_quality)
        if not d.ok and not chunks:
            doc_summ.append({"url": d.url, "ok": False, "error": d.error, "n_chunks": 0})
            continue
        records.extend(c.to_record() for c in chunks)
        doc_summ.append({"url": d.url, "title": d.title, "ok": d.ok, "n_chunks": len(chunks)})

    return {
        "query": query, "n_documents": len(docs), "n_chunks": len(records),
        "documents": doc_summ, "chunks": records,
        "params": {"chunk_size": chunk_size, "overlap": overlap,
                   "crawl_depth": crawl_depth, "min_quality": min_quality},
        "response_time": round(time.time() - t0, 3),
    }
