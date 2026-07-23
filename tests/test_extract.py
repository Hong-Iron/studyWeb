"""Extraction edge cases, including regressions for bugs found on live sites."""

from studyweb.extract import extract, _localname
from lxml import html as LH


def test_empty_and_malformed_html_no_crash():
    for src in ["", "   ", "<not real html", "<html><body></body></html>",
                "<<<", "\x00\x01garbage"]:
        ex = extract(src, "https://example.com/")
        assert ex.url == "https://example.com/"
        assert isinstance(ex.text, str) and isinstance(ex.markdown, str)


def test_namespaced_tag_does_not_crash():
    # Regression: <tiles:putAttribute> made etree.QName() raise on Samsung's page.
    html = ("<html><body><tiles:putAttribute name='x'>hi</tiles:putAttribute>"
            "<article><p>" + "Real body text here. " * 10 + "</p></article></body></html>")
    ex = extract(html, "https://example.com/")
    assert "Real body text" in ex.text


def test_body_with_vector_class_not_deleted():
    # Regression: broad "vector-" noise pattern deleted <body> entirely.
    html = ("<html><body class='mediawiki vector-feature-x vector-toc-enabled'>"
            "<div id='mw-content-text'><div class='mw-parser-output'>"
            "<p>" + "Genuine article content that must survive. " * 8 + "</p>"
            "</div></div></body></html>")
    ex = extract(html, "https://en.wikipedia.org/wiki/X")
    assert "Genuine article content" in ex.text
    assert ex.word_count > 20


def test_chrome_removed_content_kept():
    html = """
    <html><body>
      <nav><a href="/a">Home</a><a href="/b">About</a></nav>
      <header>Site header</header>
      <article><h1>Title</h1><p>The main paragraph with real substance here.</p>
        <ul><li>one</li><li>two</li></ul></article>
      <footer>copyright 2020</footer>
      <aside class="sidebar">promo junk</aside>
    </body></html>"""
    ex = extract(html, "https://example.com/post")
    assert "main paragraph with real substance" in ex.text
    assert "Site header" not in ex.markdown
    assert "promo junk" not in ex.markdown


def test_markdown_rendering_features():
    html = """<html><body><article>
      <h2>Heading</h2>
      <p>Para with <a href="/rel">link</a> and <strong>bold</strong> and <code>x=1</code>.</p>
      <ul><li>alpha</li><li>beta</li></ul>
      <blockquote>quoted</blockquote>
      <pre>code block</pre>
    </article></body></html>"""
    ex = extract(html, "https://example.com/")
    md = ex.markdown
    assert "## Heading" in md
    assert "[link](https://example.com/rel)" in md
    assert "**bold**" in md
    assert "`x=1`" in md
    assert "- alpha" in md
    assert "> quoted" in md
    assert "```" in md


def test_links_absolute_and_filtered():
    html = """<html><body><article><p>text text text text text text</p>
      <a href="/rel/page">rel</a>
      <a href="https://other.com/x">abs</a>
      <a href="mailto:a@b.com">mail</a>
      <a href="javascript:void(0)">js</a>
      <a href="#frag">frag</a>
    </article></body></html>"""
    ex = extract(html, "https://example.com/dir/")
    urls = [l.url for l in ex.links]
    assert "https://example.com/rel/page" in urls
    assert "https://other.com/x" in urls
    assert not any("mailto" in u or "javascript" in u for u in urls)


def test_title_and_meta():
    html = """<html><head><title>Doc Title</title>
      <meta property="og:description" content="A description">
      <meta name="author" content="Jane">
      <link rel="canonical" href="https://example.com/canon"></head>
      <body><article><p>body body body body body</p></article></body></html>"""
    ex = extract(html, "https://example.com/")
    assert ex.title == "Doc Title"
    assert ex.meta.get("description") == "A description"
    assert ex.meta.get("author") == "Jane"


def test_localname_helper():
    el = LH.fromstring("<div></div>")
    assert _localname(el) == "div"
    weird = LH.fromstring("<html><body><foo:bar/></body></html>").xpath("//*")
    # none should raise
    assert all(isinstance(_localname(e), str) for e in weird)
