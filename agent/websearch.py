"""Looking for other ways: a small web search and page reader with no account and no key by default.

The default provider reads DuckDuckGo's plain HTML results (polite: a clear User-Agent, a gap between queries, timeouts). With `SEARCH_API_KEY` set the Brave
Search API is used instead. `fetch_page` reads one page as plain text and its links (https only, no cookies, no JavaScript, size-capped), used to read a site's
terms or privacy page. A query never carries the owner's data: `clean_query` drops anything that looks like an account, a secret or a number.
"""
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from agent import sensitive

USER_AGENT = "MobileAgent/1.0 (+https://github.com/DharanesshMD/mobile-agent; owner-run research assistant, reads public pages)"
MAX_QUERIES = 3
QUERY_GAP_S = 10.0  # between two searches to the same provider
MAX_PAGE_BYTES = 400_000
MAX_PAGE_CHARS = 24_000
TIMEOUT_S = 10


def clean_query(text: str) -> str:
    """A search phrase with nothing personal in it: no e-mail address, no long numbers, no tokens, no URLs with credentials."""
    t = re.sub(r"\S+@\S+", " ", text)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"\b[A-Za-z0-9_\-]{24,}\b", " ", t)  # a token or key
    t = re.sub(r"\d{5,}", " ", t)
    return " ".join(re.sub(r"[^\w .,'+&:/-]", " ", t).split())[:160]


def plan_queries(query: str, goal: str = "") -> list[str]:
    """One to three searches: the question itself, cloud/browser ways (the third-party sites), and the official way."""
    if sensitive.find(query or "") or sensitive.find(goal or ""):  # a card, an ID or a password in the words: nothing is searched, so nothing leaves the computer
        return []
    base = clean_query(query or goal)
    if len(base) < 3:
        return []
    return [base, f"{base} in a browser cloud gaming site", f"{base} official way"][:MAX_QUERIES]


# --- providers ----------------------------------------------------------------------------------------------------------------------------------
class SearchBlocked(Exception):
    """The search site asked for a human check. It is never worked around: the caller says search was unavailable."""


class Provider:
    name = "base"

    def search(self, query: str) -> list[dict]:
        raise NotImplementedError


class _DDGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self._cur: dict | None = None
        self._field = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and "result__a" in cls:
            self._cur = {"title": "", "url": unwrap(a.get("href", "")), "snippet": ""}
            self._field = "title"
        elif tag in ("a", "div") and "result__snippet" in cls and self._cur is not None:
            self._field = "snippet"

    def handle_endtag(self, tag):
        if tag == "a" and self._field == "title":
            self._field = ""
        elif tag in ("a", "div") and self._field == "snippet":
            self._field = ""
            if self._cur and self._cur["url"]:
                self.results.append(self._cur)
            self._cur = None

    def handle_data(self, data):
        if self._cur is not None and self._field:
            self._cur[self._field] += data


def unwrap(href: str) -> str:
    """DuckDuckGo wraps result links (//duckduckgo.com/l/?uddg=<real url>): return the real address."""
    if href.startswith("//"):
        href = "https:" + href
    q = urllib.parse.urlparse(href)
    if "duckduckgo.com" in q.netloc and q.path.startswith("/l/"):
        real = urllib.parse.parse_qs(q.query).get("uddg", [""])[0]
        return real
    return href


def parse_ddg(html: str) -> list[dict]:
    p = _DDGParser()
    p.feed(html)
    out = []
    for r in p.results:
        r = {k: " ".join(v.split()) for k, v in r.items()}
        if r["url"].startswith("https://") and r["title"]:
            out.append(r)
    return out


class DuckDuckGo(Provider):
    name = "duckduckgo"
    _lock = threading.Lock()
    _last = 0.0

    def __init__(self, gap_s: float = QUERY_GAP_S):
        self.gap_s = gap_s

    def search(self, query: str) -> list[dict]:
        with DuckDuckGo._lock:  # polite: one query at a time with a gap between them
            wait = DuckDuckGo._last + self.gap_s - time.time()
            if wait > 0:
                time.sleep(wait)
            DuckDuckGo._last = time.time()
        req = urllib.request.Request("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            body = r.read(MAX_PAGE_BYTES).decode("utf-8", "replace")
            found = parse_ddg(body)
            if not found and (r.status == 202 or "anomaly" in body.lower() or "captcha" in body.lower()):
                raise SearchBlocked("DuckDuckGo asked for a human check (a captcha), so it is not used from here")  # no retry, no workaround
            return found[:8]


class Brave(Provider):
    name = "brave"

    def __init__(self, key: str):
        self.key = key

    def search(self, query: str) -> list[dict]:
        req = urllib.request.Request("https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": 8}),
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json", "X-Subscription-Token": self.key})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            data = json.loads(r.read(MAX_PAGE_BYTES))
        return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": re.sub(r"<[^>]+>", "", x.get("description", ""))}
                for x in data.get("web", {}).get("results", []) if str(x.get("url", "")).startswith("https://")][:8]


class ModelSearch(Provider):
    """The model provider's own web search grounding (the same one the chat uses): no scraping, no captcha, a few cents per thousand queries."""
    name = "model"

    def __init__(self, model):
        self.model = model

    def search(self, query: str) -> list[dict]:
        r = self.model.search(query)
        return [{"title": " ".join((s.get("title") or s["url"]).split()), "url": s["url"], "snippet": " ".join((s.get("snippet") or "").split())[:240]}
                for s in r.get("sources", []) if str(s.get("url", "")).startswith("https://")][:8]


def choose_provider(model=None) -> Provider:
    """Brave when a key is set, else the model's own search grounding, else DuckDuckGo's plain page (which may ask for a human check)."""
    key = os.environ.get("SEARCH_API_KEY", "").strip()
    if key:
        return Brave(key)
    if model is not None and hasattr(model, "search"):
        return ModelSearch(model)
    return DuckDuckGo()


def default_provider() -> Provider:
    return choose_provider(None)


# --- reading a page -----------------------------------------------------------------------------------------------------------------------------
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "head"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._href = ""
        self._label: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "a":
            self._href, self._label = dict(attrs).get("href", "") or "", []
        if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "a" and self._href:
            self.links.append((" ".join("".join(self._label).split()), self._href))
            self._href = ""

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)
            if self._href:
                self._label.append(data)


def html_text(html: str) -> tuple[str, list[tuple[str, str]]]:
    p = _Text()
    p.feed(html)
    text = re.sub(r"[ \t]+", " ", "".join(p.parts))
    return re.sub(r"\n\s*\n+", "\n", text).strip()[:MAX_PAGE_CHARS], p.links


def _public_host(host: str) -> bool:
    """Only the public internet: never this computer or a private network, whatever a link says."""
    try:
        for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except (OSError, ValueError):
        return False


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://") or not _public_host(urllib.parse.urlparse(newurl).hostname or ""):
            raise urllib.error.URLError("redirect to a non-https or non-public address refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_page(url: str) -> tuple[str, list[tuple[str, str]]]:
    """(plain text, links) of one public https page: no cookies, no JavaScript, size-capped. Raises on anything else."""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not u.hostname or u.username or u.password or not _public_host(u.hostname):
        raise ValueError("only public https pages can be read")
    opener = urllib.request.build_opener(_HttpsOnly())  # no cookie processor: nothing is remembered between pages
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"})
    with opener.open(req, timeout=TIMEOUT_S) as r:
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype and "text" not in ctype:
            raise ValueError("not a text page")
        raw = r.read(MAX_PAGE_BYTES).decode("utf-8", "replace")
    return html_text(raw)


def fetch_page_text(url: str) -> str:
    return fetch_page(url)[0]


POLICY_LINK = re.compile(r"terms|privacy|conditions|legal|tos\b|user agreement", re.I)


def policy_links(base: str, links: list[tuple[str, str]], limit: int = 2) -> list[str]:
    """The terms and privacy pages a site links to (same site, https), when the link is obvious."""
    host = urllib.parse.urlparse(base).hostname or ""
    out: list[str] = []
    for label, href in links:
        if not POLICY_LINK.search(label) and not POLICY_LINK.search(href):
            continue
        full = urllib.parse.urljoin(base, href)
        fh = urllib.parse.urlparse(full).hostname or ""
        if full.startswith("https://") and (fh == host or fh.endswith("." + host) or host.endswith("." + fh)) and full not in out:
            out.append(full.split("#")[0])
    return out[:limit]
