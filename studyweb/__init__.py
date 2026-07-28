"""studyweb — a web search & crawl toolkit for Learning Management Systems.

Four ways to use it:

    # 1. As a library
    from studyweb import search, fetch_page, crawl, Corpus
    results = search("photosynthesis", max_results=8)
    doc = fetch_page(results[0].url)

    # 2. As a CLI          ->  python -m studyweb --help
    # 3. As an HTTP API    ->  python -m studyweb serve
    # 4. As an LLM tool    ->  from studyweb.lms import TOOL_SCHEMAS, dispatch_tool

Designed to be a good web citizen: an identifiable User-Agent, robots.txt
respect, per-host rate limiting, and size/depth caps are all on by default.
"""

from .config import Settings, settings
from . import adaptive, engines
from .search import search, SearchResult
from .sitesearch import site_search, build_search_url
from .fetch import fetch_page, fetch_many, Document
from .crawl import crawl
from .collect import Corpus
from .research import research, extract_urls, build_rag
from .clean import clean, to_chunks, chunk_text
from .structured import extract_structured
from .dataextract import extract_data
from .providers import PROVIDERS, chat, check, check_all, list_models, ProviderError
from .agent import run_agent
from .usage import Usage, ledger

__version__ = "0.3.1"

__all__ = [
    "search",
    "SearchResult",
    "site_search",
    "build_search_url",
    "fetch_page",
    "fetch_many",
    "Document",
    "crawl",
    "Corpus",
    "research",
    "extract_urls",
    "build_rag",
    "extract_structured",
    "extract_data",
    "PROVIDERS",
    "chat",
    "check",
    "check_all",
    "list_models",
    "ProviderError",
    "run_agent",
    "Usage",
    "ledger",
    "clean",
    "to_chunks",
    "chunk_text",
    "Settings",
    "settings",
    "engines",
    "adaptive",
    "__version__",
]
