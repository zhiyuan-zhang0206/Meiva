# meiva:note
# Web-scrape adapter — the `web-sources:generic` skill's logic.
#
# The invoked CLI is the thin wrapper at `skills/web-sources/generic/scripts/feed.py`; the agent
# runs it as a subprocess (no SDK import, no browser automation), e.g.
#
#     .venv/bin/python skills/web-sources/generic/scripts/feed.py fetch --url https://site/article
#     .venv/bin/python skills/web-sources/generic/scripts/feed.py enum --url https://site/list --link-pattern 'mod=view&aid=\d+'
#     .venv/bin/python skills/web-sources/generic/scripts/feed.py sync --url https://site/list --link-pattern '...' --limit 20
#
# and reads the JSON printed to stdout. This is the generic fallback adapter for sources with no
# platform API and no RSS feed (official newspaper sites, forum portals, blog index pages). Two
# stages:
#
#   enum   — fetch a list/index page and harvest the article links on it whose absolute URL
#            matches a caller-supplied regex (`--link-pattern`). The pattern is how the agent
#            says "these anchors are articles" — a general crawler can't guess that per-site, so
#            it's an explicit knob. Runs the same curl -> Jina ladder as `fetch`, so a list page
#            that 403s a bare client still enumerates (the Jina rung returns markdown, harvested
#            via its `[text](url)` links).
#   fetch  — fetch one page, extract the main article (boilerplate stripped) to markdown +
#            metadata (title / author / date / site) into the raw mirror
#            `$MEIVA_HOME/state/mirrors/web-scrape/<slug>/`, projected to S1.
#
# `sync` ties them: harvest the list -> fetch each new one -> print the S1 items.
#
# The HTTP fetch is a two-rung ladder. Rung 1 is `curl` with an honest browser User-Agent (many
# sites, Chinese official/news especially, 403 a bare client or time out the WebFetch tool but
# answer a browser UA) + `trafilatura` extraction (main-content detection + HTML->markdown +
# metadata). When that trips an anti-bot wall (a Cloudflare challenge, a JS-render shell), rung 2
# escalates to Jina Reader (`r.jina.ai`), a server-side headless browser that renders past the
# wall. A page that even a render can't read (login-gated) raises, pointing at a logged-in
# browser (chrome MCP) or the matching platform adapter. `curl` + `trafilatura` are the only
# third-party pieces; Jina is a plain HTTP call.
#
# This file is the shell: it runs the HTTP client, reads and writes the mirror and prints. What
# is sent, how a page or an answer is read and which rung wins are values in `scrape_wire.py`,
# `scrape_ladder.py` and `scrape_post.py`. How the run reaches the web is a `ScrapeConfig` the
# composition root (`main`, or an installed adapter) builds from the environment.
#
# `fetch_article` is the shared laddered primitive the `rss` adapter imports for its `--full`
# mode (pulling an entry's full body from its link). A heavy SPA whose content needs a real
# interactive browser is out of scope here.
# meiva:link meiva_builtins/skills/integrations/web-sources/docs/web-sources.meiva.okf.md

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import TypeGuard, assert_never

from loguru import logger

from base.paths.home import resolve_meiva_home

# meiva:note
# The pure modules are siblings in this skill's own scripts/ dir, not an importable package
# (PYTHONSAFEPATH=1 keeps a script's own directory off sys.path). Structure Rule 6 recognizes
# this exact __file__-derived, within-skill shape as the endorsed guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scrape_ladder import (
    VIA_CURL,
    VIA_JINA,
    Accept,
    Article,
    Escalate,
    NoBody,
    PageFormat,
    has_wall_marker,
    jina_article,
    ladder_step,
    raw_links,
    select_links,
)
from scrape_post import Post, S1Item, build_post, extract_article, render_markdown, to_s1
from scrape_wire import (
    ScrapeConfig,
    WebScrapeError,
    config_from_env,
    curl_argv,
    html_text,
    jina_argv,
    refusal,
    render_text,
)


# meiva:note
# The raw-mirror dir `<home>/state/mirrors/web-scrape/` — shared derived state doubling as the
# sync watermark. The per-content subdir is the stable URL slug, so a re-run reuses it (dedup).
def default_mirror(home: Path) -> Path:
    return home / "state" / "mirrors" / "web-scrape"


# meiva:note
# A stable, filesystem-safe directory name for `url`. Deterministic (so a re-run of the same URL
# reuses its mirror dir): a readable tail from the path plus a short hash of the full URL for
# uniqueness. The hash is a dedup id, not a security measure.
def _url_slug(url: str) -> str:
    h = hashlib.sha1(url.encode()).hexdigest()[:10]  # noqa: S324
    parsed = urllib.parse.urlparse(url)
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1] or parsed.netloc
    tail = re.sub(r"[^A-Za-z0-9._-]+", "-", tail)[:40].strip("-") or parsed.netloc.replace(".", "-")
    return f"{tail}-{h}"


# meiva:note
# The `raw_path` of the post for `url`: its mirror directory under `root`, with a trailing slash.
def _raw_path(root: Path, url: str) -> str:
    return str(root / _url_slug(url)) + "/"


# meiva:note
# Reject what the tripwire in `scrape_wire.refusal` refuses before curl sees the URL.
def _assert_fetchable(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    reason = refusal(url, parsed.scheme, parsed.hostname, noun="URL")
    if reason is not None:
        raise WebScrapeError(reason)


# meiva:note
# Fetch `url` as text via curl with a browser UA. Raises on HTTP error (`curl -f`) or transport
# failure.
def fetch_html(config: ScrapeConfig, url: str) -> str:
    _assert_fetchable(url)
    proc = subprocess.run(curl_argv(config.curl, url), capture_output=True, check=False)
    return html_text(url, proc.returncode, proc.stdout, proc.stderr)


# meiva:note
# Render `url` through Jina Reader (server-side headless browser) and return the raw markdown
# response. The shared escalation rung for both article bodies and list pages.
def jina_render(config: ScrapeConfig, url: str) -> str:
    _assert_fetchable(url)
    endpoint = config.jina_base + urllib.parse.quote(url, safe="")
    proc = subprocess.run(jina_argv(config, endpoint), capture_output=True, check=False)
    return render_text(url, proc.returncode, proc.stdout, proc.stderr)


# meiva:note
# Fetch one URL's article body, escalating past anti-bot walls. Returns `(article, fetched_via)`.
#
# Ladder: curl + trafilatura first; if that trips a wall marker (Cloudflare challenge, JS shell)
# or curl is blocked outright, escalate to a Jina render. A plain index/login page with no wall
# signature is NOT escalated — it raises so the caller switches to `enum --link-pattern`. This is
# the primitive both `fetch` and `rss --full` use.
#
# Raises:
#     WebScrapeError: no extractable body via either rung (index page, or a login-gated page
#         that even a render can't read).
def fetch_article(config: ScrapeConfig, url: str) -> tuple[Article, str]:
    host = urllib.parse.urlparse(url).hostname
    page = ""
    article: Article | None = None
    try:
        page = fetch_html(config, url)
        article = extract_article(page, url, host)
    except WebScrapeError as e:
        logger.warning("WebScrapeError while fetching article html for {}: {}", url, e)
    step = ladder_step(url, page, article)
    match step:
        case Accept(article=accepted):
            return accepted, VIA_CURL
        case NoBody(message=message):
            raise WebScrapeError(message)
        case Escalate():
            return jina_article(jina_render(config, url), url, host), VIA_JINA
        case _:
            assert_never(step)


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat().replace("+00:00", "Z")


# meiva:note
# Fetch one web page into the raw mirror `<root>/<slug>/`.
#
# Writes `post.json` + `post.md` and returns the curated post. Runs the curl -> Jina fetch ladder
# (see `fetch_article`); `fetched_via` records which rung produced the body (`web-scrape` or
# `web-scrape+jina`). `platform` tags the S1 item (default `web`); locally-installed adapters
# pass their own value so downstream consumers can tell which adapter produced the item.
# Raises if the page yields no extractable body on either rung (a list/index page fetched as an
# article, or a login wall) — a real "nothing to ingest", not a silent empty.
def fetch(config: ScrapeConfig, url: str, *, root: Path, platform: str = "web") -> Post:
    article, via = fetch_article(config, url)
    post = build_post(article, url=url, platform=platform, via=via, fetched_at=_now_iso())
    save(post, root)
    return post


# meiva:note
# Persist a fetched post to `<root>/<slug>/`.
def save(post: Post, root: Path) -> Path:
    outdir = root / _url_slug(post["url"])
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2))
    (outdir / "post.md").write_text(render_markdown(post))
    return outdir


def _is_post(value: object) -> TypeGuard[Post]:
    return isinstance(value, dict)


# meiva:note
# The saved post for `url` if its mirror already has `post.json` (so a re-run reuses it instead
# of re-fetching), else None.
def _load_if_complete(url: str, root: Path) -> Post | None:
    post_json = root / _url_slug(url) / "post.json"
    if not post_json.exists():
        return None
    loaded: object = json.loads(post_json.read_text())
    return loaded if _is_post(loaded) else None


# meiva:note
# Fetch a list/index page for link harvesting, escalating past anti-bot walls exactly like
# `fetch_article` does for article bodies.
#
# Returns `(content, fmt, via)`: rung 1 is curl + a browser UA (HTML); if curl is blocked
# (403/timeout) or returns a wall shell (Cloudflare challenge, JS shell), rung 2 is a Jina render
# whose markdown comes back instead (`fmt` is then `"markdown"`, and `via` records
# `web-scrape+jina`). A plain page with no wall signature is NOT escalated — an empty harvest is
# a pattern problem, not something a render fixes.
def _fetch_list_page(config: ScrapeConfig, url: str) -> tuple[str, PageFormat, str]:
    content = ""
    try:
        content = fetch_html(config, url)
    except WebScrapeError as e:
        logger.warning("enum: curl failed for {}: {}", url, e)
    if not content or has_wall_marker(content):
        return jina_render(config, url), "markdown", VIA_JINA
    return content, "html", VIA_CURL


# meiva:note
# Fetch `list_url` and return the absolute article links on it.
#
# Every `href` is resolved against `list_url` (fragment dropped) and kept only when its absolute
# URL matches `link_pattern` (all of them without one). Order is preserved and duplicates
# dropped, so the first `limit` are the page's first N matching links (list pages are
# newest-first, so that is the newest N).
#
# Runs the same two-rung fetch ladder as `fetch_article`: curl + browser UA first, then a Jina
# render when the list page is blocked (403/timeout) or walled — a list page that 403s a bare
# client still enumerates. The Jina rung returns markdown, so links are harvested from its
# `[text](url)` links.
def harvest_links(
    config: ScrapeConfig, list_url: str, link_pattern: str | None, *, limit: int | None = None
) -> list[str]:
    page, fmt, via = _fetch_list_page(config, list_url)
    absolute = [
        urllib.parse.urljoin(list_url, raw).split("#", 1)[0] for raw in raw_links(page, fmt)
    ]
    links = select_links(absolute, link_pattern, limit)
    logger.info("enum {}: {} link(s) via {}", list_url, len(links), via)
    return list(links)


# meiva:note
# Harvest a list page's article links, fetch each, return the S1 items.
#
# Pages whose mirror already has `post.json` are reused from disk (no re-fetch) — re-running the
# same list page is the natural dedup for a repeated digest. A page that yields no body is
# skipped (with its error noted on stderr), not fatal, so one dead link doesn't abort the whole
# sync. No link matching the pattern is a valid empty window and returns no items.
#
# `--limit` caps the number of *newly fetched* articles (reused ones don't count), so a re-run
# advances through the backlog instead of re-examining the same leading N (the rest wait for the
# next run). If links were harvested but none yielded an article, that is a real failure (bad
# `--link-pattern`, a login wall) -> raise, not a silent empty result. `platform` tags the S1
# items (default `web`); locally-installed adapters pass their own value so downstream consumers
# can tell which adapter produced the item.
def sync(
    config: ScrapeConfig,
    list_url: str,
    *,
    link_pattern: str | None,
    limit: int | None = None,
    matrix_entity: str | None = None,
    root: Path,
    platform: str = "web",
) -> list[S1Item]:
    urls = harvest_links(config, list_url, link_pattern)
    if not urls:
        return []
    out: list[S1Item] = []
    made = 0
    for u in urls:
        post = _load_if_complete(u, root)
        if post is None:
            if limit is not None and made >= limit:
                break
            try:
                post = fetch(config, u, root=root, platform=platform)
            except WebScrapeError as e:
                print(f"[skip] {u}: {e}", file=sys.stderr)
                continue
            made += 1
        out.append(to_s1(post, matrix_entity=matrix_entity, raw_path=_raw_path(root, u)))
    if not out:
        raise WebScrapeError(
            f"harvested {len(urls)} link(s) from {list_url} but none yielded an article "
            "— check --link-pattern (is it matching real article pages?) or a login wall"
        )
    return out


def _mirror_from(out: str | None) -> Path:
    return Path(out).expanduser() if out else default_mirror(resolve_meiva_home())


# meiva:note
# Parse `argv` and run the command it names against `config`, returning the JSON value to print:
# the links for `enum`, the S1 items for `sync` and the S1 item for `fetch`.
def run(argv: list[str], config: ScrapeConfig) -> object:
    p = argparse.ArgumentParser(
        prog="web-scrape",
        description="fetch / enumerate arbitrary web pages -> raw mirror + S1 JSON",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("enum", help="harvest article links off a list/index page")
    pe.add_argument("--url", required=True, help="the list/index page URL")
    pe.add_argument(
        "--link-pattern", help="regex an absolute href must match to count as an article link"
    )
    pe.add_argument("--limit", type=int, help="max links")

    ps = sub.add_parser("sync", help="harvest a list page + fetch each article")
    ps.add_argument("--url", required=True, help="the list/index page URL")
    ps.add_argument("--link-pattern", help="regex an absolute href must match")
    ps.add_argument("--limit", type=int, help="max articles")
    ps.add_argument("--matrix-entity", help="cross-platform entity tag for S1")
    ps.add_argument("--out", help="raw-mirror dir (default $MEIVA_HOME/state/mirrors/web-scrape)")

    pf = sub.add_parser("fetch", help="fetch one page as an article")
    pf.add_argument("--url", required=True, help="the article URL")
    pf.add_argument("--matrix-entity", help="cross-platform entity tag for S1")
    pf.add_argument("--out", help="raw-mirror dir (default $MEIVA_HOME/state/mirrors/web-scrape)")

    args = p.parse_args(argv)
    match args.cmd:
        case "enum":
            return harvest_links(config, args.url, args.link_pattern, limit=args.limit)
        case "sync":
            return sync(
                config,
                args.url,
                link_pattern=args.link_pattern,
                limit=args.limit,
                matrix_entity=args.matrix_entity,
                root=_mirror_from(args.out),
            )
        case "fetch":
            root = _mirror_from(args.out)
            post = fetch(config, args.url, root=root)
            return to_s1(post, matrix_entity=args.matrix_entity, raw_path=_raw_path(root, args.url))
        case _:
            raise ValueError(f"unknown command {args.cmd!r}")


# meiva:note
# The command line: build the scrape config from the process environment once, run the command
# and print its JSON.
def main(argv: list[str] | None = None) -> None:
    result = run(sys.argv[1:] if argv is None else argv, config_from_env(os.environ))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
