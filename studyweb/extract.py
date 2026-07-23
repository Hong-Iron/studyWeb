"""Turn raw HTML into clean, LLM-ready content.

Produces a title, plain text, Markdown, a list of passages (for ranking),
outbound links (for crawling), and metadata — using only lxml, no heavyweight
readability dependency. The main-content heuristic is a compact re-implementation
of the classic "score blocks by paragraph text density" idea."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from lxml import html as LH
from lxml import etree

# Tags that never carry article content — removed globally, everywhere.
_HARD_STRIP = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "link", "meta", "head", "form", "button", "input", "select", "textarea",
}
# Chrome tags — removed only *inside* the chosen main container.
_CHROME_TAGS = {"nav", "footer", "header", "aside", "figure", "figcaption"}
# ARIA roles that mark page chrome.
_NOISE_ROLES = {"navigation", "banner", "complementary", "contentinfo",
                "search", "menu", "menubar", "dialog", "alertdialog", "tablist"}
# class/id substrings that mark chrome — matched only inside the main container,
# so broad tokens can't nuke <body>. Anchored to avoid over-matching.
_NOISE_RE = re.compile(
    r"(^|[-_ ])(nav|navbar|menu|sidebar|footer|masthead|comment|share|social|"
    r"related|recommend|promo|advert|ad|ads|banner|cookie|consent|popup|modal|"
    r"overlay|breadcrumb|pagination|paging|newsletter|subscribe|widget|toolbar|"
    r"toc|portlet|catlink|editsection|skip|sitenotice|noprint|hatnote|reflist|"
    r"reference|references|citation|footnote|mw-cite|navbox|metadata)"
    r"($|[-_ ])",
    re.I,
)
# Semantic containers that usually hold the main article, best-first.
_CONTENT_XPATHS = (
    '//*[@role="main"]', '//main', '//article',
    '//*[@id="mw-content-text"]', '//*[contains(concat(" ",@class," ")," mw-parser-output ")]',
    '//*[@itemprop="articleBody"]', '//*[contains(@class,"article-body")]',
    '//*[contains(@class,"article__body")]', '//*[contains(@class,"post-content")]',
    '//*[contains(@class,"entry-content")]', '//*[contains(@class,"post-body")]',
    '//*[@id="content"]', '//*[contains(@class,"content-body")]',
)
@dataclass
class Link:
    url: str
    text: str


@dataclass
class Extracted:
    url: str
    title: str = ""
    text: str = ""
    markdown: str = ""
    passages: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def _localname(el) -> str:
    """Lower-cased local tag name, tolerant of exotic/namespaced tags such as
    ``<tiles:putAttribute>`` that make ``etree.QName()`` raise."""
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[1]
    return tag.lower()


def _clean_text(s: str) -> str:
    return re.sub(r"[ \t ]+", " ", (s or "")).strip()


def _meta(doc, name: str) -> str:
    for xp in (f'//meta[@property="{name}"]/@content',
               f'//meta[@name="{name}"]/@content'):
        v = doc.xpath(xp)
        if v and v[0].strip():
            return v[0].strip()
    return ""


def _extract_meta(doc, url: str) -> dict:
    m = {
        "description": _meta(doc, "og:description") or _meta(doc, "description"),
        "site_name": _meta(doc, "og:site_name"),
        "author": _meta(doc, "author") or _meta(doc, "article:author"),
        "published": _meta(doc, "article:published_time") or _meta(doc, "og:updated_time"),
        "lang": (doc.xpath("//html/@lang") or [""])[0],
        "canonical": (doc.xpath('//link[@rel="canonical"]/@href') or [""])[0],
        "host": urlsplit(url).netloc,
    }
    return {k: v for k, v in m.items() if v}


def _title(doc, url: str) -> str:
    for cand in (_meta(doc, "og:title"),
                 (doc.xpath("//title/text()") or [""])[0],
                 (doc.xpath("//h1//text()") or [""])[0]):
        cand = _clean_text(cand)
        if cand:
            # trim trailing " - Site Name" style suffixes lightly
            return cand
    return urlsplit(url).netloc


def _remove_all(elements) -> None:
    # Collect-then-remove so we never mutate a tree we're still iterating.
    for el in elements:
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def _strip_hard(tree) -> None:
    """Remove non-content tags (script/style/etc) and comments everywhere.
    Safe on the whole document — these tags never hold article text."""
    doomed = []
    for el in tree.iter():
        if not isinstance(el.tag, str):
            if el.tag is etree.Comment:
                doomed.append(el)
            continue
        if _localname(el) in _HARD_STRIP:
            doomed.append(el)
    _remove_all(doomed)


def _strip_chrome(root) -> None:
    """Remove nav/menu/footer/etc chrome — scoped to inside ``root`` so it can
    never delete the page body. ``root`` itself is always kept."""
    doomed = []
    for el in root.iter():
        if el is root or not isinstance(el.tag, str):
            continue
        tag = _localname(el)
        if tag in _CHROME_TAGS:
            doomed.append(el)
            continue
        if (el.get("role") or "").lower() in _NOISE_ROLES:
            doomed.append(el)
            continue
        if (el.get("aria-hidden") or "").lower() == "true":
            doomed.append(el)
            continue
        ident = " ".join(filter(None, [el.get("class", ""), el.get("id", "")]))
        if ident and _NOISE_RE.search(ident):
            doomed.append(el)
    _remove_all(doomed)


def _score_node(node) -> float:
    """Higher score = more likely to be the main content container."""
    ps = node.xpath(".//p")
    text = _clean_text(node.text_content())
    if not text:
        return 0.0
    score = len(ps) * 3.0
    score += min(text.count(","), 50)
    score += min(len(text) / 100.0, 60)
    tag = _localname(node)
    if tag in ("article", "main"):
        score += 30
    ident = " ".join(filter(None, [node.get("class", ""), node.get("id", "")]))
    if re.search(r"(article|content|post|entry|main|body|story|text)", ident, re.I):
        score += 20
    # penalise link-dense blocks (menus)
    link_text = sum(len(_clean_text(a.text_content())) for a in node.xpath(".//a"))
    if len(text) > 0 and link_text / len(text) > 0.5:
        score *= 0.3
    return score


def _pick_main(tree):
    # 1) Prefer an explicit semantic content container when one has real text.
    for xp in _CONTENT_XPATHS:
        nodes = [n for n in tree.xpath(xp)
                 if len(_clean_text(n.text_content())) > 200]
        if nodes:
            return max(nodes, key=lambda n: len(n.text_content()))

    # 2) Otherwise score block containers by paragraph text density.
    candidates = [n for n in tree.iter() if isinstance(n.tag, str)
                  and _localname(n) in ("article", "main", "section", "div")]
    best, best_score = None, 0.0
    for n in candidates:
        s = _score_node(n)
        if s > best_score:
            best, best_score = n, s
    body = tree.xpath("//body")
    if best is None or best_score < 20:
        return body[0] if body else tree
    return best


# --- markdown rendering -----------------------------------------------------

def _inline_md(el, base_url: str) -> str:
    """Render inline content of an element to Markdown."""
    out = []
    if el.text:
        out.append(el.text)
    for child in el:
        tag = _localname(child) if isinstance(child.tag, str) else ""
        inner = _inline_md(child, base_url)
        if tag in ("strong", "b"):
            out.append(f"**{inner.strip()}**" if inner.strip() else "")
        elif tag in ("em", "i"):
            out.append(f"*{inner.strip()}*" if inner.strip() else "")
        elif tag == "code":
            out.append(f"`{inner.strip()}`" if inner.strip() else "")
        elif tag == "a":
            href = child.get("href", "")
            txt = inner.strip() or href
            if href and not href.startswith(("javascript:", "#")):
                out.append(f"[{txt}]({urljoin(base_url, href)})")
            else:
                out.append(txt)
        elif tag == "br":
            out.append("\n")
        else:
            out.append(inner)
        if child.tail:
            out.append(child.tail)
    return _clean_text("".join(out))


def _to_markdown(node, base_url: str) -> list[str]:
    """Walk a content node into a list of Markdown block strings."""
    blocks: list[str] = []
    for el in node.iterchildren():
        if not isinstance(el.tag, str):
            continue
        tag = _localname(el)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            lvl = int(tag[1])
            txt = _inline_md(el, base_url)
            if txt:
                blocks.append("#" * lvl + " " + txt)
        elif tag == "p":
            txt = _inline_md(el, base_url)
            if txt:
                blocks.append(txt)
        elif tag in ("ul", "ol"):
            blocks.extend(_render_list(el, base_url, ordered=(tag == "ol")))
        elif tag == "blockquote":
            txt = _inline_md(el, base_url)
            if txt:
                blocks.append("\n".join("> " + line for line in txt.splitlines()))
        elif tag == "pre":
            code = el.text_content().rstrip()
            if code:
                blocks.append("```\n" + code + "\n```")
        elif tag == "table":
            md = _render_table(el, base_url)
            if md:
                blocks.append(md)
        elif tag in ("div", "section", "article", "main", "figure"):
            blocks.extend(_to_markdown(el, base_url))
        else:
            txt = _inline_md(el, base_url)
            if txt:
                blocks.append(txt)
    return [b for b in blocks if b.strip()]


def _render_list(el, base_url: str, ordered: bool) -> list[str]:
    lines = []
    for i, li in enumerate(el.xpath("./li"), 1):
        marker = f"{i}. " if ordered else "- "
        lines.append(marker + _inline_md(li, base_url))
    return ["\n".join(lines)] if lines else []


def _render_table(el, base_url: str) -> str:
    rows = []
    for tr in el.xpath(".//tr"):
        cells = [_clean_text(c.text_content()) for c in tr.xpath("./th|./td")]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join(["---"] * width) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _collect_links(doc, base_url: str) -> list[Link]:
    seen, out = set(), []
    for a in doc.xpath("//a[@href]"):
        href = a.get("href", "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absu = urljoin(base_url, href).split("#", 1)[0]
        if absu in seen or urlsplit(absu).scheme not in ("http", "https"):
            continue
        seen.add(absu)
        out.append(Link(url=absu, text=_clean_text(a.text_content())[:200]))
    return out


def extract(html_source: str | bytes, url: str) -> Extracted:
    """Extract clean content from an HTML document."""
    if isinstance(html_source, bytes):
        html_source = html_source.decode("utf-8", "replace")
    try:
        doc = LH.fromstring(html_source)
    except (etree.ParserError, ValueError):
        text = _clean_text(re.sub(r"<[^>]+>", " ", html_source))
        return Extracted(url=url, text=text, markdown=text,
                         passages=[text[:1000]] if text else [])

    base = (doc.xpath("//base/@href") or [url])[0]
    base = urljoin(url, base)

    meta = _extract_meta(doc, url)
    title = _title(doc, url)
    links = _collect_links(doc, base)

    # Work on a copy for content so link collection above sees the full page.
    content_tree = LH.fromstring(html_source)
    _strip_hard(content_tree)          # drop script/style/etc across whole doc
    main = _pick_main(content_tree)    # choose the main content container
    _strip_chrome(main)                # remove nav/menu chrome *within* it

    blocks = _to_markdown(main, base)
    markdown = "\n\n".join(blocks).strip()
    if not markdown:  # fallback: whole-body text
        markdown = _clean_text(main.text_content())
    text = re.sub(r"\n{3,}", "\n\n",
                  "\n\n".join(re.sub(r"[*`#>|\-]", "", b) for b in blocks)).strip() \
        or _clean_text(main.text_content())

    # Passages: paragraph-ish blocks, good granularity for BM25 ranking.
    passages = [b.strip() for b in blocks
                if len(b.strip()) >= 40 and not b.startswith("|")]
    if not passages and text:
        passages = [text[i:i + 700] for i in range(0, min(len(text), 4000), 700)]

    return Extracted(url=url, title=title, text=text, markdown=markdown,
                     passages=passages, links=links, meta=meta)
