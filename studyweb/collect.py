"""Persist collected material to disk for study and data collection.

A Corpus is just a directory:
    <root>/
      documents.jsonl   one cleaned document per line
      chunks.jsonl      RAG-ready chunks  (embed these)
      pages/*.md        human-readable Markdown of each page
      manifest.json     what's inside + provenance
"""

from __future__ import annotations

import json
import os
import re
import time

from .fetch import Document
from .clean import clean as _clean_fn, to_chunks as _to_chunks


def _slug(url: str, n: int = 60) -> str:
    s = re.sub(r"^https?://", "", url)
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")
    return s[:n] or "page"


class Corpus:
    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.pages_dir = os.path.join(self.root, "pages")
        os.makedirs(self.pages_dir, exist_ok=True)
        self._docs: list[Document] = []
        self._seen: set[str] = set()

    # -- adding ------------------------------------------------------------
    def add(self, doc: Document, *, write_markdown: bool = True) -> bool:
        if not doc.ok or doc.url in self._seen:
            return False
        self._seen.add(doc.url)
        self._docs.append(doc)
        if write_markdown:
            self._write_markdown(doc)
        return True

    def add_many(self, docs: list[Document]) -> int:
        return sum(1 for d in docs if self.add(d))

    def _write_markdown(self, doc: Document) -> None:
        path = os.path.join(self.pages_dir, _slug(doc.url) + ".md")
        header = f"# {doc.title}\n\n> source: {doc.url}\n\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(header + (doc.markdown or doc.text))

    # -- output ------------------------------------------------------------
    def save(self, *, chunk_size: int = 1000, overlap: int = 150,
             min_quality: float = 0.25) -> dict:
        """Write documents.jsonl, chunks.jsonl (RAG-ready) and manifest.json."""
        doc_path = os.path.join(self.root, "documents.jsonl")
        chunk_path = os.path.join(self.root, "chunks.jsonl")
        n_chunks = 0
        with open(doc_path, "w", encoding="utf-8") as df, \
                open(chunk_path, "w", encoding="utf-8") as cf:
            for d in self._docs:
                cleaned = _clean_fn(d.markdown or d.text)
                df.write(json.dumps({
                    "url": d.url, "title": d.title, "host": d.meta.get("host", ""),
                    "text": cleaned, "meta": d.meta, "word_count": len(cleaned.split()),
                }, ensure_ascii=False) + "\n")
                for ch in _to_chunks(d, chunk_size=chunk_size, overlap=overlap,
                                     min_quality=min_quality):
                    cf.write(json.dumps(ch.to_record(), ensure_ascii=False) + "\n")
                    n_chunks += 1

        manifest = {
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "n_documents": len(self._docs), "n_chunks": n_chunks,
            "urls": [d.url for d in self._docs],
            "files": {"documents": "documents.jsonl", "chunks": "chunks.jsonl",
                      "pages": "pages/"},
        }
        with open(os.path.join(self.root, "manifest.json"), "w", encoding="utf-8") as mf:
            json.dump(manifest, mf, ensure_ascii=False, indent=2)
        return manifest

    def save_rag_records(self, records: list[dict], name: str = "chunks.jsonl") -> str:
        """Write pre-built RAG records (e.g. from research.build_rag) to JSONL."""
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return path

    def __len__(self) -> int:
        return len(self._docs)
