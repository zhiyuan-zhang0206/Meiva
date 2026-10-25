# meiva:note
# RSS/Atom feed adapter — one self-contained CLI.
#
# The agent runs this as a subprocess (no SDK import, no browser automation), e.g.
#
#     .venv/bin/python skills/web-sources/rss/scripts/feed.py enum --url <feed-url>
#     .venv/bin/python skills/web-sources/rss/scripts/feed.py sync --url <feed-url> --since 14d
#     .venv/bin/python skills/web-sources/rss/scripts/feed.py sync --url <feed-url> --full
#
# and reads the JSON printed to stdout. A feed IS the collection (no per-account enumeration), so
# there are two stages only:
#
#   enum   — fetch + parse the feed, list its current entries newest-first
#            (`{id, title, url, published_at}`), bounded by `--since` / `--limit`.
#   sync   — same, but persist each entry into the raw mirror
#            `$MEIVA_HOME/state/mirrors/rss/<slug>/` (`post.json` + `post.md`) and project to S1.
#            `--full` additionally fetches each entry's link and extracts the full article body
#            (most feeds carry only a summary).
#
# There is no per-item `fetch` subcommand: the entry's content already comes inside the feed, and
# pulling one arbitrary article's full body is the `generic` skill's job (which `--full` reuses
# internally).
#
# Parsing is `feedparser` (handles every RSS/Atom dialect + date normalization); the feed bytes
# are pulled with `curl` + a browser UA so a feed host that 403s a bare client still answers.
# `--full` imports `generic`'s `fetch_article`, whose curl -> Jina ladder pulls each entry's full
# body even past a Cloudflare wall. Cursor model: no state file — each run re-parses the whole
# current feed and dedups against the mirror by entry id (a feed only holds its recent window, so
# taking all current entries is bounded and idempotent, like the gmail `--since`-window mirror
# dedup).
#
# This file is the shell: it runs the HTTP client, reads and writes the mirror and prints. What a
# feed and its entries are, which entries a run takes and what each becomes are values in
# `rss_post.py` and `rss_plan.py`. How the run reaches the web is an `RssConfig` the composition
# root (`main`) builds from the environment.
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
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from base.paths.home import resolve_meiva_home

# meiva:note
# The pure modules are siblings in this skill's own scripts/ dir, and `scrape_wire` (the HTTP
# client's argv and the internal-host tripwire) is in the parent skill's scripts/ dir, a sibling
# tree within this same web-sources skill. Neither is an importable package (PYTHONSAFEPATH=1
# keeps a script's own directory off sys.path). Structure Rule 6 recognizes these exact
# __file__-derived, within-skill shapes as the endorsed guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from rss_plan import is_complete, plan_sync, select_entries, since_to_ts
from rss_post import (
    Feed,
    Listed,
    Post,
    RssError,
    S1Item,
    build_post,
    feed_bytes,
    listing,
    parse_feed,
    render_markdown,
    to_s1,
)
from scrape_wire import ScrapeConfig, WebScrapeError, config_from_env, curl_argv, refusal


# meiva:note
# How a sync reaches the web and where it writes: the HTTP client program, the raw-mirror dir
# and, for `--full`, the scrape config that pulls each entry's article body from its link (None
# keeps the feed's own summary).
@dataclass(frozen=True)
class RssConfig:
    curl: str
    root: Path
    full: ScrapeConfig | None


# meiva:note
# The raw-mirror dir `<home>/state/mirrors/rss/` — shared derived state doubling as the sync
# watermark. The per-content subdir is the stable entry slug, so a re-run reuses it (dedup).
def default_mirror(home: Path) -> Path:
    return home / "state" / "mirrors" / "rss"


# meiva:note
# A stable, filesystem-safe directory name for an entry, derived ONLY from its id (guid). Keying
# the dir purely on the guid means a re-run reuses it even if the feed's `link` field later
# changes (canonical redirect, slug edit) — the guid is the dedup key, so the dir must not move
# when the link moves. A readable tail (the id's last path segment if it is a URL, else the id
# text) plus a short hash of the id. The hash is a dedup id, not a security measure.
def _entry_slug(entry_id: str) -> str:
    h = hashlib.sha1(entry_id.encode()).hexdigest()[:10]  # noqa: S324
    parsed = urllib.parse.urlparse(entry_id)
    raw = parsed.path.rstrip("/").rsplit("/", 1)[-1] if parsed.scheme else entry_id
    tail = re.sub(r"[^A-Za-z0-9._-]+", "-", raw)[:40].strip("-")
    return f"{tail or 'entry'}-{h}"


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat().replace("+00:00", "Z")


# meiva:note
# Reject what the tripwire in `scrape_wire.refusal` refuses before curl sees the feed URL — a
# feed URL is just as agent-supplied as a page URL. A hostname resolving to a private IP, or a
# redirect to one, is not chased (curl caps redirects at `--max-redirs`; the agent driving this
# is trusted).
def _assert_fetchable(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    reason = refusal(url, parsed.scheme, parsed.hostname, noun="feed URL")
    if reason is not None:
        raise RssError(reason)


# meiva:note
# Fetch `feed_url` with curl and a browser UA and parse it. Raises on HTTP/transport failure
# (`curl -f`) and on content that is not a feed.
def fetch_feed(curl: str, feed_url: str) -> Feed:
    _assert_fetchable(feed_url)
    proc = subprocess.run(curl_argv(curl, feed_url), capture_output=True, check=False)
    return parse_feed(feed_bytes(feed_url, proc.returncode, proc.stdout, proc.stderr), feed_url)


# meiva:note
# List a feed's current entries newest-first, as `{id, title, url, published_at}`. `since_ts`
# drops entries older than the cutoff (undated entries are kept — a missing date is not evidence
# of being old); `limit` caps the count.
def enum(
    curl: str, feed_url: str, *, since_ts: float | None, limit: int | None, now: float
) -> list[Listed]:
    feed = fetch_feed(curl, feed_url)
    selected = select_entries(feed.entries, since_ts=since_ts, now=now)
    return [listing(e) for e in selected[:limit]]


# meiva:note
# Persist a post to `<root>/<slug>/`.
def save(post: Post, root: Path) -> Path:
    outdir = root / _entry_slug(post["source_id"])
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2))
    (outdir / "post.md").write_text(render_markdown(post))
    return outdir


def _is_post(value: object) -> TypeGuard[Post]:
    return isinstance(value, dict)


# meiva:note
# The saved post for an entry id, or None when its mirror has no `post.json`.
def _read_post(root: Path, entry_id: str) -> Post | None:
    post_json = root / _entry_slug(entry_id) / "post.json"
    if not post_json.exists():
        return None
    loaded: object = json.loads(post_json.read_text())
    return loaded if _is_post(loaded) else None


# meiva:note
# True if the mirror holds a post for the entry id that has everything this run would produce.
def _is_saved_complete(root: Path, entry_id: str, full: bool) -> bool:
    post = _read_post(root, entry_id)
    return post is not None and is_complete(post, full=full)


# meiva:note
# The full article body behind `url`, or None when the page cannot be read. The `generic` skill's
# `fetch_article` runs the curl -> Jina ladder, so a Cloudflare-fronted host (which 403s a bare
# curl) still yields full text via the render rung. Only when BOTH rungs fail (a login-gated
# page) does it raise; that is swallowed here, the feed summary stays, and the post is left not
# `_full` so a later --full run retries this one. The whole feed sync must not abort here.
# `webscrape` imports trafilatura, so it is loaded only when `--full` is actually used.
def _full_text(scrape: ScrapeConfig, url: str) -> str | None:
    import webscrape

    try:
        article, _via = webscrape.fetch_article(scrape, url)
    except WebScrapeError as e:
        print(f"[full-fallback] {url}: {e}", file=sys.stderr)
        return None
    return article["text"]


# meiva:note
# Parse a feed, persist each current entry, return the S1 items.
#
# Entries already mirrored are reused from disk. Without `--limit` every entry currently in the
# feed is taken (a feed holds only its recent window, so this is bounded) — `--since` windows by
# publish date. `--limit` caps the number of *newly built* entries (reused ones don't count), so
# a re-run advances through the backlog instead of re-examining the same leading N — without
# this, a `--limit` smaller than the count of new entries would strand the rest before they roll
# off the feed. An entry with no id, link or title has nothing to key its mirror dir by: it is
# noted on stderr and left out.
def sync(
    config: RssConfig,
    feed_url: str,
    *,
    since_ts: float | None,
    limit: int | None,
    matrix_entity: str | None,
    now: float,
) -> list[S1Item]:
    feed = fetch_feed(config.curl, feed_url)
    selected = select_entries(feed.entries, since_ts=since_ts, now=now)
    full = config.full is not None
    complete = frozenset(
        e.id for e in selected if e.id is not None and _is_saved_complete(config.root, e.id, full)
    )
    plan = plan_sync(selected, complete, limit)
    for _ in plan.unkeyed:
        print("[skip] feed entry without an id, link or title", file=sys.stderr)
    out: list[S1Item] = []
    for step in plan.steps:
        if step.build:
            body = (
                _full_text(config.full, step.entry.url)
                if config.full is not None and step.entry.url
                else None
            )
            post = build_post(feed, step.entry, step.key, full_text=body, fetched_at=_now_iso())
            save(post, config.root)
        else:
            reused = _read_post(config.root, step.key)
            if reused is None:
                raise RssError(f"the mirror lost the saved post for {step.key!r}")
            post = reused
        raw_path = str(config.root / _entry_slug(step.key)) + "/"
        out.append(to_s1(post, matrix_entity=matrix_entity, raw_path=raw_path))
    return out


def _mirror_from(out: str | None) -> Path:
    return Path(out).expanduser() if out else default_mirror(resolve_meiva_home())


# meiva:note
# Parse `argv` and run the command it names with `scrape` as the way to the web and `now` (unix
# seconds) as the current time, returning the JSON value to print: the listing for `enum` and the
# S1 items for `sync`.
def run(argv: list[str], scrape: ScrapeConfig, now: float) -> object:
    p = argparse.ArgumentParser(
        prog="rss-feed",
        description="enumerate / sync an RSS/Atom feed -> raw mirror + S1 JSON",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("enum", help="list a feed's current entries")
    pe.add_argument("--url", required=True, help="the feed URL")
    pe.add_argument("--since", help="time bound: 30d / 12h / 2026-05-07")
    pe.add_argument("--limit", type=int, help="max entries")

    ps = sub.add_parser("sync", help="sync a feed's entries into the mirror + S1")
    ps.add_argument("--url", required=True, help="the feed URL")
    ps.add_argument("--since", help="time bound: 30d / 12h / 2026-05-07")
    ps.add_argument("--limit", type=int, help="max entries")
    ps.add_argument(
        "--full", action="store_true", help="fetch each entry's link for the full article body"
    )
    ps.add_argument("--matrix-entity", help="cross-platform entity tag for S1")
    ps.add_argument("--out", help="raw-mirror dir (default $MEIVA_HOME/state/mirrors/rss)")

    args = p.parse_args(argv)
    since_ts = since_to_ts(args.since, now)
    match args.cmd:
        case "enum":
            return enum(scrape.curl, args.url, since_ts=since_ts, limit=args.limit, now=now)
        case "sync":
            config = RssConfig(
                curl=scrape.curl,
                root=_mirror_from(args.out),
                full=scrape if args.full else None,
            )
            return sync(
                config,
                args.url,
                since_ts=since_ts,
                limit=args.limit,
                matrix_entity=args.matrix_entity,
                now=now,
            )
        case _:
            raise ValueError(f"unknown command {args.cmd!r}")


# meiva:note
# The command line: build the scrape config from the process environment and read the clock
# once, run the command and print its JSON.
def main(argv: list[str] | None = None) -> None:
    result = run(sys.argv[1:] if argv is None else argv, config_from_env(os.environ), time.time())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
