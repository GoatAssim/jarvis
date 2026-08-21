"""Web search and page fetch for Jarvis.

Search uses the ``ddgs`` package (DuckDuckGo). Page fetch strips markup and
caps text so a library-sized HTML dump never hits the model.
"""

import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

FETCH_TIMEOUT = 15
FETCH_MAX_CHARS = 4000
SEARCH_MAX = 8

_BLOCKED_HOSTS = {
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "playnite",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


# ── helpers ──────────────────────────────────────────────────────────


def _clean_text(text):
    text = unescape(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


class _PageTextParser(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "iframe", "nav", "footer", "header"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self._in_title = False
        self.title = ""
        self._title_buf = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        if tag == "title" and self._skip == 0:
            self._in_title = True
        if tag in ("p", "br", "li", "h1", "h2", "h3", "tr") and self._skip == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            self.title = _clean_text("".join(self._title_buf))
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag in ("p", "div", "section", "article", "li", "h1", "h2", "h3") and self._skip == 0:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self._title_buf.append(data)
            return
        self.parts.append(data)


# ── web_search ───────────────────────────────────────────────────────


def tool_web_search(args):
    args = args or {}
    query = (args.get("query") or args.get("q") or "").strip()
    if not query:
        return {"needs_clarification": True, "message": "What should I search for?"}
    if len(query) < 2:
        return {"needs_clarification": True, "message": "Search query is too short."}

    limit = args.get("limit")
    try:
        limit = int(limit) if limit is not None else SEARCH_MAX
    except (TypeError, ValueError):
        limit = SEARCH_MAX
    limit = max(1, min(limit, SEARCH_MAX))

    try:
        from ddgs import DDGS
    except ImportError:
        return {"error": "ddgs package not installed — run: pip install ddgs"}

    try:
        raw = list(DDGS().text(query, max_results=limit))
    except Exception as e:
        return {"error": f"Search failed: {e}"}

    results = []
    seen = set()
    for r in raw:
        url = (r.get("href") or "").split("#")[0]
        if not url or url in seen:
            continue
        seen.add(url)
        results.append({
            "title": (r.get("title") or "")[:160],
            "url": url,
            "snippet": (r.get("body") or "")[:280],
        })

    if not results:
        return {"error": "No search results. Try a more specific query."}

    return {
        "query": query,
        "showing": len(results),
        "results": results,
        "note": "Summarize for the user and cite these URLs as sources. Fetch 1–3 pages with web_fetch if snippets are too thin.",
    }


# ── web_fetch ────────────────────────────────────────────────────────


def _host_allowed(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in _BLOCKED_HOSTS:
        return False
    if host.endswith(".local") or host.endswith(".localhost"):
        return False
    if host.startswith("192.168.") or host.startswith("10.") or host.startswith("172."):
        return False
    return True


def tool_web_fetch(args):
    args = args or {}
    url = (args.get("url") or "").strip()
    if not url:
        return {"needs_clarification": True, "message": "Need a URL from web_search results."}
    if not urlparse(url).scheme:
        url = "https://" + url
    if not _host_allowed(url):
        return {"error": "That URL isn't allowed (only public http/https pages)."}

    try:
        import requests
    except ImportError:
        return {"error": "requests package not installed"}

    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=FETCH_TIMEOUT,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        return {"error": f"Request timed out after {FETCH_TIMEOUT}s"}
    except requests.exceptions.RequestException as e:
        return {"error": f"Request failed: {e}"}

    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "html" not in ctype and "xml" not in ctype and "text/plain" not in ctype and "json" not in ctype:
        return {
            "url": str(resp.url),
            "error": f"Not a text page (content-type: {ctype or 'unknown'}).",
        }

    if "json" in ctype or "text/plain" in ctype:
        text = (resp.text or "")[:FETCH_MAX_CHARS]
        return {"url": str(resp.url), "title": urlparse(str(resp.url)).path, "text": text}

    parser = _PageTextParser()
    try:
        parser.feed(resp.text)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", resp.text or "")
        text = _clean_text(text)[:FETCH_MAX_CHARS]
        return {"url": str(resp.url), "title": "", "text": text}

    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    truncated = len(text) > FETCH_MAX_CHARS
    text = text[:FETCH_MAX_CHARS]
    return {
        "url": str(resp.url),
        "title": parser.title or urlparse(str(resp.url)).path,
        "text": text,
        "truncated": truncated,
    }


# ── schemas ──────────────────────────────────────────────────────────


WEB_TOOL_SCHEMAS = [
    {
        "name": "web_search",
        "description": (
            "Search the public web (DuckDuckGo). REQUIRED for 'best X', news, "
            "current facts, product comparisons, or anything that may have changed. "
            "Returns compact title/url/snippet. Then web_fetch 1–3 sources and cite URLs "
            "in the answer. Never invent sources."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query, specific and current."},
                "limit": {"type": "integer", "description": "Max results, default 8, max 8."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch a public web page and return extracted text (capped at 4k chars). Use after web_search "
            "on 1–3 promising URLs so you can summarize with real sources."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full http(s) URL from web_search."},
            },
            "required": ["url"],
        },
    },
]

WEB_TOOLS = {
    "web_search": tool_web_search,
    "web_fetch": tool_web_fetch,
}
