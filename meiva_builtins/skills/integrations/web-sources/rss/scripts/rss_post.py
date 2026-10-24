# meiva:note
# What a feed is and what its entries become, over values: the feed a fetched response parses
# into, the post saved in the raw mirror for an entry, its markdown rendering and its projection
# onto the cross-platform S1 item. Nothing here fetches, reads the clock or writes a file; the
# caller supplies the fetch time and the mirror path.
# meiva:link meiva_builtins/skills/integrations/web-sources/docs/web-sources.meiva.okf.md

from __future__ import annotations

import calendar
import html
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict, cast

import feedparser
from feedparser.exceptions import ThingsNobodyCaresAboutButMe

_RE_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"[ \t]*\n[ \t]*")

# meiva:note
# The provenance tag of every post this adapter writes.
FETCHED_VIA = "rss-feed"


# meiva:note
# A feed could not be fetched or parsed into any entries.
class RssError(Exception):
    pass


# meiva:note
# One feed entry, reduced to what the adapter uses. `id` is the entry's guid, else its link,
# else its title (None when it has none of them). `published_ts` is unix seconds and
# `published_at` the ISO8601 `Z` string of the same instant (both None for an undated entry).
# `body_html` is the richest body the feed itself carries: `content:encoded` (full HTML) when
# present, else the summary.
@dataclass(frozen=True)
class Entry:
    id: str | None
    title: str | None
    url: str | None
    published_ts: float | None
    published_at: str | None
    body_html: str
    author: str | None


# meiva:note
# A parsed feed: the URL it was fetched from, its title (the URL when it names none) and its
# entries in feed order.
@dataclass(frozen=True)
class Feed:
    url: str
    title: str
    entries: tuple[Entry, ...]


# meiva:note
# The author an RSS post records: the feed URL is the stable follow key that every entry of a
# feed shares (the way the gmail adapter keys on List-Id), and the name is the entry's author or
# the feed title.
class Author(TypedDict):
    id: str | None
    name: str | None


# meiva:note
# The post saved in the raw mirror as `post.json`; the key order is the file's. `_full` records
# whether the text is a full article body fetched from the entry's link, rather than the feed's
# own summary.
class Post(TypedDict):
    platform: str
    url: str | None
    source_id: str
    feed_url: str
    feed_title: str
    title: str | None
    text: str
    summary_html: str
    author: Author
    published_at: str | None
    fetched_at: str
    fetched_via: str
    _full: bool


# meiva:note
# A post on the cross-platform S1 item schema shared by every `web-sources` adapter (see
# `web-sources/SKILL.md`).
class S1Item(TypedDict):
    platform: str
    source_id: str
    url: str | None
    author: Author
    matrix_entity: str | None
    title: str | None
    text: str | None
    transcript: None
    published_at: str | None
    fetched_at: str | None
    fetched_via: str | None
    media: list[str]
    stats: dict[str, int]
    raw_path: str


# meiva:note
# One entry as the `enum` listing prints it.
class Listed(TypedDict):
    id: str | None
    title: str | None
    url: str | None
    published_at: str | None


# meiva:note
# The `enum` listing line of an entry.
def listing(entry: Entry) -> Listed:
    return Listed(id=entry.id, title=entry.title, url=entry.url, published_at=entry.published_at)


# meiva:note
# The feed bytes a finished `curl` produced.
#
# Raises:
#     RssError: the process failed (`-f` turns an HTTP error into a nonzero exit).
def feed_bytes(url: str, returncode: int, stdout: bytes, stderr: bytes) -> bytes:
    if returncode != 0:
        raise RssError(
            f"curl failed for {url} (rc={returncode}): {stderr.decode(errors='replace')[:200]}"
        )
    return stdout


# meiva:note
# A feedparser `*_parsed` struct_time (already UTC) as an ISO8601 `Z` string.
def _iso(struct: time.struct_time) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", struct)


def _entry(raw: Any) -> Entry:
    published = raw.get("published_parsed") or raw.get("updated_parsed")
    link = raw.get("link")
    content = raw.get("content")
    body = content[0]["value"] if content and content[0].get("value") else raw.get("summary", "")
    return Entry(
        id=raw.get("id") or link or raw.get("title"),
        title=raw.get("title"),
        url=link,
        published_ts=calendar.timegm(published) if published else None,
        published_at=_iso(published) if published else None,
        body_html=body,
        author=raw.get("author"),
    )


# meiva:note
# Parse the feed bytes fetched from `feed_url`.
#
# An empty feed (a valid feed dialect with no entries) is not an error — it has no entries. It
# raises only when feedparser could not identify the content as a known feed dialect
# (``parsed.version`` is empty), or when a *fatal* parse error prevented entry recovery
# (``bozo`` is set but the exception is not a benign ``ThingsNobodyCaresAboutButMe`` like
# ``CharacterEncodingOverride`` — the feed is still valid, just has encoding quirks).
def parse_feed(data: bytes, feed_url: str) -> Feed:
    parse = cast(Callable[[bytes], dict[str, Any]], feedparser.parse)
    parsed = parse(data)
    feed_meta: dict[str, Any] = parsed["feed"]
    entries: list[Any] = parsed["entries"]
    if not entries:
        cause = parsed.get("bozo_exception")
        fatal_bozo = parsed.get("bozo") and not isinstance(cause, ThingsNobodyCaresAboutButMe)
        if fatal_bozo or not parsed.get("version"):
            raise RssError(
                f"not recognized as a feed: {feed_url!r}" + (f" ({cause})" if cause else "")
            )
    title: str = feed_meta.get("title") or feed_url
    return Feed(url=feed_url, title=title, entries=tuple(_entry(e) for e in entries))


# meiva:note
# Strip tags and unescape an RSS summary/content fragment to plain text. The feed summary is
# short prose, so a tag strip (not a full markdown render) is enough; `--full` is the path to a
# real article body.
def _html_to_text(fragment: str) -> str:
    text = html.unescape(_RE_TAG.sub("", fragment))
    return _RE_WS.sub("\n", text).strip()


# meiva:note
# The post for one feed entry, saved under `source_id` (the entry's id).
#
# The body is the feed-provided summary/content stripped to text; when `full_text` (the entry's
# full article body, as markdown) is non-blank it replaces that and the post is marked `_full`,
# so a later `--full` run knows this entry needs no retry. `author.id` is the feed URL.
def build_post(
    feed: Feed, entry: Entry, source_id: str, *, full_text: str | None, fetched_at: str
) -> Post:
    if full_text is not None and full_text.strip():
        text, got_full = full_text, True
    else:
        text, got_full = _html_to_text(entry.body_html), False
    return Post(
        platform="rss",
        url=entry.url,
        source_id=source_id,
        feed_url=feed.url,
        feed_title=feed.title,
        title=entry.title,
        text=text,
        summary_html=entry.body_html,
        author=Author(id=feed.url, name=entry.author or feed.title),
        published_at=entry.published_at,
        fetched_at=fetched_at,
        fetched_via=FETCHED_VIA,
        _full=got_full,
    )


# meiva:note
# The `post.md` rendering of a post.
def render_markdown(post: Post) -> str:
    lines = [
        f"# {post.get('title') or post['source_id']}",
        "",
        f"- **feed**: {post.get('feed_title')} ({post.get('feed_url')})",
        f"- **author**: {post['author'].get('name')}",
        f"- **url**: {post.get('url')}",
        f"- **published**: {post.get('published_at')}",
        "",
        post.get("text") or "_(no body)_",
    ]
    return "\n".join(lines) + "\n"


# meiva:note
# Project a saved post onto the S1 item schema. `raw_path` is the mirror directory the post was
# saved under, with a trailing slash.
def to_s1(post: Post, *, matrix_entity: str | None, raw_path: str) -> S1Item:
    a = post["author"]
    return S1Item(
        platform="rss",
        source_id=post["source_id"],
        url=post.get("url"),
        author=Author(id=a.get("id"), name=a.get("name")),
        matrix_entity=matrix_entity,
        title=post.get("title"),
        text=post.get("text"),
        transcript=None,
        published_at=post.get("published_at"),
        fetched_at=post.get("fetched_at"),
        fetched_via=post.get("fetched_via"),
        media=[],
        stats={},
        raw_path=raw_path,
    )
