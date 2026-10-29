# meiva:note
# What a fetched web page becomes, over values: the article trafilatura extracts from its HTML,
# the post saved in the raw mirror, the markdown rendering of that post and its projection onto
# the cross-platform S1 item. Nothing here fetches, reads the clock or writes a file; the caller
# supplies the URL, the host, the fetch time and the mirror path.
# meiva:link meiva_builtins/skills/integrations/web-sources/docs/web-sources.meiva.okf.md

from __future__ import annotations

from typing import TypedDict

from scrape_ladder import Article

from meiva_builtins.skills.skill_tools import missing


# meiva:note
# Who published a post: the site's host as the durable follow key, and the name to show.
class Author(TypedDict):
    id: str | None
    name: str | None


# meiva:note
# The post saved in the raw mirror as `post.json`; the key order is the file's.
class Post(TypedDict):
    platform: str
    url: str
    source_id: str
    title: str | None
    text: str
    description: str | None
    author: Author
    sitename: str | None
    published_at: str | None
    fetched_at: str
    fetched_via: str


# meiva:note
# A post on the cross-platform S1 item schema shared by every `web-sources` adapter (see
# `web-sources/SKILL.md`).
class S1Item(TypedDict):
    platform: str
    source_id: str
    url: str
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
# Extract the main article from `html` into markdown plus metadata. `text` is the
# boilerplate-stripped body as markdown (empty if the extractor found no main content, e.g. a
# pure index page). `host` is the URL's host, the fallback when the page names none. This is the
# primitive `rss --full` reuses to pull an entry's full body.
def extract_article(html: str, url: str, host: str | None) -> Article:
    try:
        import trafilatura
    except ImportError as exc:
        raise missing("web-sources", "the `trafilatura` library") from exc
    body = trafilatura.extract(
        html, url=url, output_format="markdown", include_links=True, include_images=True
    )
    meta = trafilatura.extract_metadata(html, default_url=url)
    return Article(
        title=getattr(meta, "title", None),
        author=getattr(meta, "author", None),
        date=getattr(meta, "date", None),
        sitename=getattr(meta, "sitename", None),
        hostname=getattr(meta, "hostname", None) or host,
        description=getattr(meta, "description", None),
        text=body or "",
    )


# meiva:note
# The post for `url` from its article. `via` records which rung produced the body
# (`web-scrape` or `web-scrape+jina`); `platform` tags the S1 item (default `web`; locally
# installed adapters pass their own value so downstream consumers can tell which adapter
# produced the item).
def build_post(article: Article, *, url: str, platform: str, via: str, fetched_at: str) -> Post:
    return Post(
        platform=platform,
        url=url,
        source_id=url,
        title=article["title"],
        text=article["text"],
        description=article["description"],
        author=Author(
            id=article["hostname"],
            name=article["author"] or article["sitename"] or article["hostname"],
        ),
        sitename=article["sitename"],
        published_at=article["date"],
        fetched_at=fetched_at,
        fetched_via=via,
    )


# meiva:note
# The `post.md` rendering of a post.
def render_markdown(post: Post) -> str:
    a = post["author"]
    lines = [
        f"# {post.get('title') or post['url']}",
        "",
        f"- **site**: {post.get('sitename')} ({a.get('id')})",
        f"- **author**: {a.get('name')}",
        f"- **url**: {post['url']}",
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
        platform=post.get("platform", "web"),
        source_id=post["url"],
        url=post["url"],
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
