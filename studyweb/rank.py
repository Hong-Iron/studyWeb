"""Local relevance ranking — the piece that lets this tool rival a hosted
answer API without calling one. Pure-Python BM25 over passages, plus an
extractive answer builder. Unicode-aware tokeniser handles Korean and English.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

# \w with re.UNICODE keeps Hangul syllables and CJK as word characters.
_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ɏ가-힣一-鿿]+", re.UNICODE)
# Runs where whitespace does not separate words (Korean/CJK/Kana). Such tokens
# get indexed as character bigrams too, so "삼성전자는" still matches "삼성전자".
_CJK_RE = re.compile(r"[가-힣一-鿿ぁ-ゟ゠-ヿ]")

_STOP = set("""
a an and are as at be by for from has have he in is it its of on that the to was were will with
this these those or nor not but if then else when while do does did done being been
""".split()) | set("""
그 이 저 것 수 등 및 는 은 이 가 을 를 에 의 와 과 도 로 으로 에서 부터 까지 그리고 그러나 하지만
""".split())


def _cjk_bigrams(token: str) -> list[str]:
    return [token[i:i + 2] for i in range(len(token) - 1)]


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        t = raw.lower()
        if len(t) <= 1 or t in _STOP:
            continue
        out.append(t)
        if _CJK_RE.search(t):
            # Add character bigrams so morphological variation (attached
            # particles, compounding) doesn't tank recall on Korean/CJK.
            out.extend(_cjk_bigrams(t))
    return out


@dataclass
class Passage:
    text: str
    source_url: str
    source_title: str = ""


class BM25:
    """Okapi BM25 over a fixed passage corpus."""

    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = docs
        self.N = len(docs) or 1
        self.dl = [len(d) for d in docs]
        self.avgdl = (sum(self.dl) / self.N) if self.N else 0.0
        df: Counter = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {
            t: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
            for t, n in df.items()
        }
        self._tf = [Counter(d) for d in docs]

    def score(self, query_tokens: list[str], i: int) -> float:
        if not self.dl[i]:
            return 0.0
        tf, dl = self._tf[i], self.dl[i]
        s = 0.0
        for t in query_tokens:
            if t not in tf:
                continue
            idf = self.idf.get(t, 0.0)
            freq = tf[t]
            denom = freq + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
            s += idf * (freq * (self.k1 + 1)) / denom
        return s

    def rank(self, query: str) -> list[tuple[int, float]]:
        q = tokenize(query)
        scored = [(i, self.score(q, i)) for i in range(self.N)]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored


def normalize_scores(scores: list[float]) -> list[float]:
    """Map raw BM25 scores to a stable 0..1 range for a Tavily-like ``score``."""
    if not scores:
        return []
    hi = max(scores)
    if hi <= 0:
        return [0.0] * len(scores)
    return [round(s / hi, 4) for s in scores]


def rank_passages(query: str, passages: list[Passage], top_k: int = 8
                  ) -> list[tuple[Passage, float]]:
    """Return the top_k passages most relevant to the query, with 0..1 scores."""
    if not passages:
        return []
    bm25 = BM25([tokenize(p.text) for p in passages])
    ranked = bm25.rank(query)
    raw = [ranked_score for _, ranked_score in ranked]
    norm = dict(zip((i for i, _ in ranked), normalize_scores(raw)))
    out = [(passages[i], norm[i]) for i, s in ranked if s > 0][:top_k]
    return out


_SENT_RE = re.compile(r"(?<=[.!?。！？])\s+|\n+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_MARKS = re.compile(r"[*`#>|]+")


def _strip_md(s: str) -> str:
    s = _MD_LINK.sub(r"\1", s)          # [text](url) -> text
    s = _MD_MARKS.sub("", s)            # drop **, `, #, >, | table pipes
    return re.sub(r"\s+", " ", s).strip()


_NUM_RUN = re.compile(r"(?:\b\d+\b[ ]){3,}")  # "1 2 3 4 ..." citation back-links


def _is_prose(s: str) -> bool:
    """Reject reference lists, number rows, and link/menu soup."""
    if not s:
        return False
    letters = sum(c.isalpha() for c in s)
    if letters / len(s) < 0.55 or len(s.split()) < 5:
        return False
    if _NUM_RUN.search(s):
        return False
    if s.count('"') >= 4 or s.count("“") + s.count("”") >= 3:
        return False
    return True


def extractive_answer(query: str, passages: list[Passage],
                      max_chars: int = 700) -> str:
    """Build a short, source-grounded answer by selecting the highest-scoring
    sentences across all passages — no LLM required."""
    sentences: list[Passage] = []
    for p in passages:
        for sent in _SENT_RE.split(p.text):
            sent = _strip_md(sent)
            if 30 <= len(sent) <= 400 and _is_prose(sent):
                sentences.append(Passage(sent, p.source_url, p.source_title))
    if not sentences:
        return ""
    ranked = rank_passages(query, sentences, top_k=6)
    picked, seen, total = [], set(), 0
    for p, _score in ranked:
        key = p.text[:60].lower()
        if key in seen:
            continue
        seen.add(key)
        if total + len(p.text) > max_chars and picked:
            break
        picked.append(p.text)
        total += len(p.text)
    return " ".join(picked)
