# meiva:note
# The fetch ladder and the list-page harvest of the web-scrape adapter, over values: which rung
# produced a usable article, how a Jina Reader answer becomes one, and which links on a list page
# are articles. `webscrape.py` fetches the pages, runs these decisions and escalates when they
# say so.
# meiva:link meiva_builtins/skills/integrations/web-sources/docs/web-sources.meiva.okf.md

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict, assert_never

from scrape_wire import WebScrapeError

# meiva:note
# The provenance tag of an article the curl rung produced; it is also the name of the mirror
# directory under `$MEIVA_HOME/state/mirrors/`.
VIA_CURL = "web-scrape"

# meiva:note
# The provenance tag of an article the Jina rung produced.
VIA_JINA = "web-scrape+jina"

PageFormat = Literal["html", "markdown"]

# meiva:note
# href="..." / href='...' on a list page; absolute-resolved and pattern-filtered by the caller.
_RE_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

# meiva:note
# Markdown `[text](url)` links on a Jina-rendered list page. The enum escalation rung returns
# markdown, so link harvesting runs over these in place of `href="..."`.
_RE_MD_LINK = re.compile(r"""\[[^\]]*\]\(([^)\s]+)\)""")

# meiva:note
# Substrings that mean the bytes we got are an anti-bot / JS-render wall, not the article. They
# are checked against both the raw HTML and the extracted body; tripping one (or a curl
# transport failure) is what escalates the fetch ladder to Jina.
_WALL_MARKERS = (
    "just a moment",
    "enable javascript and cookies",
    "checking your browser",
    "verify you are human",
    "attention required",
    "security verification",
    "please complete security verification",
    "please wait",
)


# meiva:note
# One article, as the extractor or the Jina rung yields it: the metadata a page carries and its
# main content as markdown (an empty `text` means the extractor found no main content, e.g. a
# pure index page).
class Article(TypedDict):
    title: str | None
    author: str | None
    date: str | None
    sitename: str | None
    hostname: str | None
    description: str | None
    text: str


# meiva:note
# The curl rung's article is usable as is.
@dataclass(frozen=True)
class Accept:
    article: Article


# meiva:note
# Transport worked but there is no article and no wall signature: an index or login page, not
# something a render fixes. `message` tells the caller so plainly.
@dataclass(frozen=True)
class NoBody:
    message: str


# meiva:note
# The curl rung is walled, blocked or empty of a transport result: escalate to a Jina render.
@dataclass(frozen=True)
class Escalate:
    pass


Step = Accept | NoBody | Escalate


# meiva:note
# True if `text` looks like an anti-bot / JS-render wall rather than content.
def has_wall_marker(text: str) -> bool:
    head = text[:1000].lower()
    return any(marker in head for marker in _WALL_MARKERS)


# meiva:note
# What the curl rung's result means. `page` is "" and `article` None when curl failed outright.
# A body with no wall signature is accepted; no body and no wall signature is an index or login
# page (not escalated, so no Jina call is burned); a wall in the raw HTML or in the extracted
# body, or no result at all, escalates.
def ladder_step(url: str, page: str, article: Article | None) -> Step:
    body = article["text"].strip() if article is not None else ""
    walled = has_wall_marker(page) or (bool(body) and has_wall_marker(body))
    if article is not None and body and not walled:
        return Accept(article)
    if article is not None and not walled:
        return NoBody(
            f"no article body extracted from {url} "
            "(is it a list/index page? use enum --link-pattern, or it may be a login wall)"
        )
    return Escalate()


# meiva:note
# Split a Jina Reader response into `(title, markdown_body)`. Jina prefixes `Title:` /
# `URL Source:` lines and a `Markdown Content:` divider before the body; if the divider is
# absent the whole response is treated as the body.
def _split_jina(raw: str) -> tuple[str | None, str]:
    m = re.search(r"^Title:\s*(.+)$", raw, re.MULTILINE)
    title = m.group(1).strip() if m else None
    i = raw.find("Markdown Content:")
    body = raw[i + len("Markdown Content:") :] if i != -1 else raw
    return title, body.strip()


# meiva:note
# A Jina Reader answer for `url` (served by `host`) in the shape of an extracted article.
#
# Raises:
#     WebScrapeError: the render came back empty or walled too; the page is likely login-gated
#         and needs a logged-in browser.
def jina_article(raw: str, url: str, host: str | None) -> Article:
    title, body = _split_jina(raw)
    if not body or has_wall_marker(body):
        raise WebScrapeError(
            f"no body from {url} via curl or jina render — the page is likely "
            "login-gated; use a logged-in browser (chrome MCP) or the platform adapter"
        )
    return Article(
        title=title,
        author=None,
        date=None,
        sitename=None,
        hostname=host,
        description=None,
        text=body,
    )


# meiva:note
# The raw hrefs on a list page: `href="..."` from HTML (the curl rung, with HTML entities
# decoded so `?mod=view&amp;aid=1` still matches a `&aid=` pattern) or the markdown
# `[text](url)` links of a Jina render (already plain).
def raw_links(content: str, fmt: PageFormat) -> tuple[str, ...]:
    match fmt:
        case "html":
            return tuple(html.unescape(href) for href in _RE_HREF.findall(content))
        case "markdown":
            return tuple(_RE_MD_LINK.findall(content))
        case _:
            assert_never(fmt)


# meiva:note
# The article links among `absolute` URLs: those matching `link_pattern` (all of them without
# one), in page order with duplicates dropped, at most `limit` (list pages are newest-first, so
# the first N are the newest N).
def select_links(
    absolute: Sequence[str], link_pattern: str | None, limit: int | None
) -> tuple[str, ...]:
    pattern = re.compile(link_pattern) if link_pattern else None
    seen: set[str] = set()
    out: list[str] = []
    for url in absolute:
        if limit is not None and len(out) >= limit:
            break
        if pattern is not None and not pattern.search(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return tuple(out)
