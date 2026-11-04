# meiva:note
# The saved-post guard both web-sources adapters share a contract with: `_is_post` in
# `web-sources/scripts/webscrape.py` (the `generic` adapter) and in
# `web-sources/rss/scripts/feed.py` (the `rss` adapter). Each answers `TypeGuard[Post]`, so each
# owes its caller a proof of the fields its own `Post` TypedDict declares — every one of them,
# since neither marks a field `NotRequired`. These tests are one module because the two guards are
# one subject: a document that lost a required field (a truncated write, a hand-edited mirror, a
# post some other adapter wrote) must be refused where the file is read, instead of reaching
# `render_markdown` / `to_s1` / `is_complete` and raising `KeyError` a caller later.
#
# Both adapters' `Post` shapes differ, so each is proven against its own writer: the values below
# are compared verbatim against what `build_post` produces, which is what the mirror holds.

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

from base.host.boundary.untyped import object_of
from meiva_builtins.skills.tests.skill_script import load_skill_script

_SCRAPE = ("integrations", "web-sources", "scripts")
_RSS = ("integrations", "web-sources", "rss", "scripts")


# meiva:note
# A skill script under the name the adapters' plain imports resolve through (`from scrape_wire
# import ...`, `import webscrape`), reusing the copy this process already holds. Loading a second
# copy would put it in `sys.modules` under a name another module's reader still resolves, and a
# `WebScrapeError` raised from the new copy is a different class object from the one that reader
# catches — a test module must not change what the adapter it is testing sees.
def _loaded(*parts: str, name: str) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    return load_skill_script(*parts, name=name)


_loaded(*_SCRAPE, "scrape_wire.py", name="scrape_wire")
ladder = _loaded(*_SCRAPE, "scrape_ladder.py", name="scrape_ladder")
post_view = _loaded(*_SCRAPE, "scrape_post.py", name="scrape_post")
scrape = _loaded(*_SCRAPE, "webscrape.py", name="webscrape")
post_mod = _loaded(*_RSS, "rss_post.py", name="rss_post")
_loaded(*_RSS, "rss_plan.py", name="rss_plan")
feed = _loaded(*_RSS, "feed.py", name="rss_feed_under_test")

_URL = "https://example.com/a"
_FETCHED_AT = "2026-06-08T10:00:05Z"
_FEED_URL = "https://ex.com/feed"
_RSS_FETCHED_AT = "2026-06-09T00:00:00Z"

_SAVED_POST: dict[str, object] = {
    "platform": "web",
    "url": _URL,
    "source_id": _URL,
    "title": "Headline",
    "text": "Body text",
    "description": "A description",
    "author": {"id": "example.com", "name": "Jane Doe"},
    "sitename": "Example",
    "published_at": "2026-06-08T10:00:00Z",
    "fetched_at": _FETCHED_AT,
    "fetched_via": "web-scrape",
}

_SAVED_RSS_POST: dict[str, object] = {
    "platform": "rss",
    "url": "https://ex.com/1",
    "source_id": "https://ex.com/1",
    "feed_url": _FEED_URL,
    "feed_title": "Example Feed",
    "title": "First Post",
    "text": "Full body one",
    "summary_html": "<p>Full body one</p>",
    "author": {"id": _FEED_URL, "name": "Jane Doe"},
    "published_at": "2026-06-08T10:00:00Z",
    "fetched_at": _RSS_FETCHED_AT,
    "fetched_via": "rss-feed",
    "_full": False,
}


def _saved_post_from_the_writer() -> object:
    article = ladder.Article(
        title="Headline",
        author="Jane Doe",
        date="2026-06-08T10:00:00Z",  # time-bomb-ok: article publication metadata
        sitename="Example",
        hostname="example.com",
        description="A description",
        text="Body text",
    )
    saved = post_view.build_post(
        article, url=_URL, platform="web", via="web-scrape", fetched_at=_FETCHED_AT
    )
    return object_of(json.loads, json.dumps(saved))


def _saved_rss_post_from_the_writer() -> object:
    parsed = post_mod.Feed(url=_FEED_URL, title="Example Feed", entries=())
    entry = post_mod.Entry(
        id="https://ex.com/1",
        title="First Post",
        url="https://ex.com/1",
        published_ts=None,
        published_at="2026-06-08T10:00:00Z",
        body_html="<p>Full body one</p>",
        author="Jane Doe",
    )
    saved = post_mod.build_post(
        parsed, entry, "https://ex.com/1", full_text=None, fetched_at=_RSS_FETCHED_AT
    )
    return object_of(json.loads, json.dumps(saved))


def test_the_generic_guard_accepts_the_post_the_writer_saves() -> None:
    saved = _saved_post_from_the_writer()
    assert set(_SAVED_POST) == post_view.Post.__required_keys__
    assert scrape._is_post(saved)
    assert saved == _SAVED_POST


def test_the_rss_guard_accepts_the_post_the_writer_saves() -> None:
    saved = _saved_rss_post_from_the_writer()
    assert set(_SAVED_RSS_POST) == post_mod.Post.__required_keys__
    assert feed._is_post(saved)
    assert saved == _SAVED_RSS_POST


def test_the_generic_guard_refuses_a_saved_post_missing_a_required_field() -> None:
    for field in _SAVED_POST:
        broken = {key: value for key, value in _SAVED_POST.items() if key != field}
        assert not scrape._is_post(broken), field
    assert not scrape._is_post({**_SAVED_POST, "author": {"name": "Jane Doe"}})


def test_the_rss_guard_refuses_a_saved_post_missing_a_required_field() -> None:
    for field in _SAVED_RSS_POST:
        broken = {key: value for key, value in _SAVED_RSS_POST.items() if key != field}
        assert not feed._is_post(broken), field
    assert not feed._is_post({**_SAVED_RSS_POST, "author": {"id": _FEED_URL}})


def test_the_generic_guard_refuses_a_saved_post_with_a_field_of_the_wrong_type() -> None:
    wrong: dict[str, object] = {
        "platform": None,
        "url": 5,
        "source_id": [_URL],
        "title": 7,
        "text": None,
        "description": 3,
        "author": "Jane Doe",
        "sitename": False,
        "published_at": 0,
        "fetched_at": None,
        "fetched_via": b"web-scrape",
    }
    for field, value in wrong.items():
        assert not scrape._is_post({**_SAVED_POST, field: value}), field
    assert not scrape._is_post({**_SAVED_POST, "author": {"id": 5, "name": "Jane Doe"}})


def test_the_rss_guard_refuses_a_saved_post_with_a_field_of_the_wrong_type() -> None:
    wrong: dict[str, object] = {
        "platform": None,
        "url": 5,
        "source_id": ["https://ex.com/1"],
        "feed_url": None,
        "feed_title": 3,
        "title": 7,
        "text": None,
        "summary_html": False,
        "author": "Jane Doe",
        "published_at": 0,
        "fetched_at": None,
        "fetched_via": b"rss-feed",
        "_full": "yes",
    }
    for field, value in wrong.items():
        assert not feed._is_post({**_SAVED_RSS_POST, field: value}), field
    assert not feed._is_post({**_SAVED_RSS_POST, "author": {"id": 5, "name": "Jane Doe"}})


# meiva:note
# The other half of the guard's contract: a value the adapter may legitimately write as `None` (no
# title on the page, no date, no author host, an entry with no link) is a value, not a missing
# field, so a post carrying them is still read.
def test_the_generic_guard_reads_a_saved_post_whose_nullable_values_are_none() -> None:
    post = {
        **_SAVED_POST,
        "title": None,
        "description": None,
        "sitename": None,
        "published_at": None,
        "author": {"id": None, "name": None},
    }
    assert scrape._is_post(post)


def test_the_rss_guard_reads_a_saved_post_whose_nullable_values_are_none() -> None:
    post = {
        **_SAVED_RSS_POST,
        "url": None,
        "title": None,
        "published_at": None,
        "author": {"id": None, "name": None},
    }
    assert feed._is_post(post)


# meiva:note
# The behavior the guard exists for, at the reader each adapter reuses a mirror through: a post
# that lost a field is not reused (the caller re-fetches or rebuilds), and the same file with
# every field present still is.
def test_the_generic_guard_refuses_a_mirrored_post_missing_a_field(tmp_path: Path) -> None:
    outdir = tmp_path / scrape._url_slug(_URL)
    outdir.mkdir(parents=True)
    broken = {key: value for key, value in _SAVED_POST.items() if key != "url"}
    (outdir / "post.json").write_text(json.dumps(broken))
    assert scrape._load_if_complete(_URL, tmp_path) is None
    (outdir / "post.json").write_text(json.dumps(_SAVED_POST))
    assert scrape._load_if_complete(_URL, tmp_path) == _SAVED_POST


def test_the_rss_guard_refuses_a_mirrored_post_missing_a_field(tmp_path: Path) -> None:
    outdir = tmp_path / "mirror" / feed._entry_slug("https://ex.com/1")
    outdir.mkdir(parents=True)
    broken = {key: value for key, value in _SAVED_RSS_POST.items() if key != "source_id"}
    (outdir / "post.json").write_text(json.dumps(broken))
    assert feed._read_post(tmp_path / "mirror", "https://ex.com/1") is None
    (outdir / "post.json").write_text(json.dumps(_SAVED_RSS_POST))
    assert feed._read_post(tmp_path / "mirror", "https://ex.com/1") == _SAVED_RSS_POST
