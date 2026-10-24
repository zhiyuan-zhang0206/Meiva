# meiva:note
# The web-sources:rss skill (meiva_builtins/skills/integrations/web-sources/rss/scripts).
#
# What a feed and its entries are, which entries a run takes and what each becomes are values in
# rss_post and rss_plan (feedparser runs for real on fixture XML), so those tests pass values and
# compare what comes back. The shell in feed.py runs against a stand-in `curl` program written to
# tmp_path that plays the feed host, the article pages and the Jina endpoint from a route table,
# and writes the mirror to real files, so no test touches the network or replaces a name.

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script

_SCRAPE = ("integrations", "web-sources", "scripts")
_RSS = ("integrations", "web-sources", "rss", "scripts")
wire = load_skill_script(*_SCRAPE, "scrape_wire.py", name="scrape_wire")
load_skill_script(*_SCRAPE, "scrape_ladder.py", name="scrape_ladder")
load_skill_script(*_SCRAPE, "scrape_post.py", name="scrape_post")
load_skill_script(*_SCRAPE, "webscrape.py", name="webscrape")
post_mod = load_skill_script(*_RSS, "rss_post.py", name="rss_post")
plan = load_skill_script(*_RSS, "rss_plan.py", name="rss_plan")
feed = load_skill_script(*_RSS, "feed.py", name="rss_feed_under_test")

_RSS_FEED_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "web-sources"
    / "rss"
    / "scripts"
    / "feed.py"
)
_JINA = "https://jina.test/"
_FEED_URL = "https://ex.com/feed"
_NOW = datetime(2026, 6, 9, tzinfo=UTC).timestamp()
_JAN_2026 = datetime(2026, 1, 1, tzinfo=UTC).timestamp()


def _rss(items_xml: str, title: str = "Example Feed") -> str:
    return (
        '<?xml version="1.0"?><rss version="2.0"'
        ' xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>'
        f"<title>{title}</title><link>https://ex.com</link>"
        f"{items_xml}</channel></rss>"
    )


_ITEM_FULL = """<item><title>First Post</title><link>https://ex.com/1</link>
<guid>https://ex.com/1</guid><pubDate>Sun, 08 Jun 2026 10:00:00 GMT</pubDate>
<description>Summary one with &lt;b&gt;markup&lt;/b&gt;</description>
<content:encoded>&lt;p&gt;Full body one&lt;/p&gt;</content:encoded></item>"""

_ITEM_OLD = """<item><title>Old Post</title><link>https://ex.com/old</link>
<guid>https://ex.com/old</guid><pubDate>Tue, 01 Jan 2019 00:00:00 GMT</pubDate>
<description>ancient</description></item>"""

_ITEM_UNDATED = """<item><title>No Date Post</title><link>https://ex.com/nd</link>
<guid>https://ex.com/nd</guid><description>no date here</description></item>"""

_ITEM_UNDATED_TWO = """<item><title>Also No Date</title><link>https://ex.com/nd2</link>
<guid>https://ex.com/nd2</guid><description>no date either</description></item>"""

_ITEM_NO_KEY = "<item><description>only a description</description></item>"

_ARTICLE_HTML = """<html><head><title>Full Article</title></head>
<body><article><h1>Full Article</h1>
<p>First paragraph of the article body with enough words to be considered the main
content by the extractor algorithm running over this fixture page here.</p>
<p>Second paragraph also carries a fair amount of text so the extractor keeps it
in the extracted markdown output reliably across versions.</p>
</article></body></html>"""


def _entries(items_xml: str) -> Any:
    return post_mod.parse_feed(_rss(items_xml).encode(), _FEED_URL)


# meiva:note
# A `curl` that answers from a route table next to it. A URL under the Jina base is a render
# request: the stand-in decodes the page URL it carries, as Jina Reader does, and looks the page
# up under `jina <url>`; any other URL is looked up under `site <url>`. A route is a body, or an
# exit code with a stderr line (`curl -f` exits 22 on an HTTP error); an unknown URL fails the way
# an unresolvable host does. Every start is logged as one line of argv.
_STAND_IN_CURL = """#!{python}
import json
import sys
import urllib.parse
from pathlib import Path

here = Path(__file__).parent
with (here / "calls.log").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
routes = json.loads((here / "routes.json").read_text())
url = sys.argv[-1]
base = {jina!r}
if url.startswith(base):
    key = "jina " + urllib.parse.unquote(url[len(base):])
else:
    key = "site " + url
route = routes.get(key)
if route is None:
    sys.stderr.write("curl: (6) Could not resolve host\\n")
    sys.exit(6)
if "exit" in route:
    sys.stderr.write(route["stderr"])
    sys.exit(route["exit"])
sys.stdout.write(route["body"])
"""


class FakeWeb:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.routes: dict[str, dict[str, object]] = {}

    def site(self, url: str, body: str) -> FakeWeb:
        self.routes[f"site {url}"] = {"body": body}
        return self

    def blocked(self, url: str) -> FakeWeb:
        self.routes[f"site {url}"] = {
            "exit": 22,
            "stderr": "curl: (22) The requested URL returned error: 403",
        }
        return self

    def curl(self) -> str:
        (self.directory / "routes.json").write_text(json.dumps(self.routes))
        curl = self.directory / "curl"
        curl.write_text(_STAND_IN_CURL.format(python=sys.executable, jina=_JINA))
        curl.chmod(0o755)
        return str(curl)

    def scrape(self) -> Any:
        return wire.ScrapeConfig(curl=self.curl(), jina_base=_JINA, jina_api_key=None)

    def config(self, root: Path, *, full: bool = False) -> Any:
        scrape = self.scrape()
        return feed.RssConfig(curl=scrape.curl, root=root, full=scrape if full else None)

    def calls(self) -> list[list[str]]:
        log = self.directory / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def web(tmp_path: Path) -> FakeWeb:
    directory = tmp_path / "bin"
    directory.mkdir()
    return FakeWeb(directory).site(_FEED_URL, _rss(_ITEM_FULL))


def _sync(
    web: FakeWeb,
    root: Path,
    *,
    full: bool = False,
    limit: int | None = None,
    since_ts: float | None = None,
) -> list[Any]:
    return feed.sync(
        web.config(root, full=full),
        _FEED_URL,
        since_ts=since_ts,
        limit=limit,
        matrix_entity=None,
        now=_NOW,
    )


def test_parse_feed_entries() -> None:
    parsed = _entries(_ITEM_FULL)
    assert parsed.title == "Example Feed"
    assert parsed.url == _FEED_URL
    assert len(parsed.entries) == 1
    entry = parsed.entries[0]
    assert entry.id == "https://ex.com/1"
    assert entry.title == "First Post"
    assert entry.url == "https://ex.com/1"
    assert entry.published_at == "2026-06-08T10:00:00Z"
    assert entry.published_ts == 1780912800


@pytest.mark.parametrize(
    "xml_bytes,should_raise",
    [
        pytest.param(
            b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title></channel></rss>',
            False,
            id="empty-rss20",
        ),
        pytest.param(
            b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>T</title></feed>',
            False,
            id="empty-atom10",
        ),
        pytest.param(
            b'<?xml version="1.0"?>'
            b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
            b' xmlns="http://purl.org/rss/1.0/">'
            b"<channel><title>T</title></channel></rdf:RDF>",
            False,
            id="empty-rss10-rdf",
        ),
        pytest.param(
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<rss version="2.0"><channel><title>T\xe9st</title></channel></rss>',
            False,
            id="charset-mismatch-benign-bozo",
        ),
        pytest.param(b"<html>not a feed</html>", True, id="html"),
        pytest.param(b'{"error": "not found"}', True, id="json"),
        pytest.param(
            b'<?xml version="1.0"?>'
            b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"</sitemapindex>",
            True,
            id="sitemap-xml",
        ),
        pytest.param(b"hello world", True, id="plain-text"),
        pytest.param(b"", True, id="empty-bytes"),
        pytest.param(
            b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</ti',
            True,
            id="truncated-fatal-bozo",
        ),
    ],
)
def test_parse_feed_empty_or_invalid(xml_bytes: bytes, should_raise: bool) -> None:
    if should_raise:
        with pytest.raises(post_mod.RssError, match="not recognized as a feed"):
            post_mod.parse_feed(xml_bytes, "https://ex.com/test")
    else:
        parsed = post_mod.parse_feed(xml_bytes, "https://ex.com/test")
        assert parsed.entries == ()


def test_content_encoded_preferred_over_summary() -> None:
    assert _entries(_ITEM_FULL).entries[0].body_html == "<p>Full body one</p>"
    assert _entries(_ITEM_UNDATED).entries[0].body_html == "no date here"


def test_an_entry_without_a_guid_is_keyed_by_its_link_then_its_title() -> None:
    linked = "<item><title>T</title><link>https://ex.com/l</link></item>"
    titled = "<item><title>Only A Title</title></item>"
    assert _entries(linked).entries[0].id == "https://ex.com/l"
    assert _entries(titled).entries[0].id == "Only A Title"
    assert _entries(_ITEM_NO_KEY).entries[0].id is None


def test_html_to_text_strips() -> None:
    assert post_mod._html_to_text("Summary one with <b>markup</b>") == "Summary one with markup"


def test_feed_bytes_returns_the_body_or_names_the_failure() -> None:
    assert post_mod.feed_bytes("https://ex.com/f", 0, b"<rss/>", b"") == b"<rss/>"
    with pytest.raises(post_mod.RssError, match=r"curl failed for https://ex.com/f \(rc=22\): 403"):
        post_mod.feed_bytes("https://ex.com/f", 22, b"", b"403")


def test_listing_is_the_entry_without_its_body() -> None:
    entry = _entries(_ITEM_FULL).entries[0]
    assert post_mod.listing(entry) == {
        "id": "https://ex.com/1",
        "title": "First Post",
        "url": "https://ex.com/1",
        "published_at": "2026-06-08T10:00:00Z",
    }


def test_select_since_drops_old_keeps_undated() -> None:
    entries = _entries(_ITEM_FULL + _ITEM_OLD + _ITEM_UNDATED).entries
    cutoff = _JAN_2026
    titles = [e.title for e in plan.select_entries(entries, since_ts=cutoff, now=_NOW)]
    assert "Old Post" not in titles
    assert "First Post" in titles
    assert "No Date Post" in titles


def test_select_orders_newest_first_with_undated_on_top_in_feed_order() -> None:
    entries = _entries(_ITEM_OLD + _ITEM_UNDATED + _ITEM_FULL + _ITEM_UNDATED_TWO).entries
    selected = plan.select_entries(entries, since_ts=None, now=_NOW)
    assert [e.title for e in selected] == ["No Date Post", "Also No Date", "First Post", "Old Post"]


def test_since_to_ts_reads_relative_and_absolute_cutoffs() -> None:
    assert plan.since_to_ts(None, _NOW) is None
    assert plan.since_to_ts("2d", _NOW) == _NOW - 2 * 86400
    assert plan.since_to_ts("12h", _NOW) == _NOW - 12 * 3600
    assert plan.since_to_ts("2026-05-07", _NOW) == 1778112000
    assert plan.since_to_ts("2026-05-07T08:00:00+08:00", _NOW) == 1778112000
    with pytest.raises(ValueError):
        plan.since_to_ts("last week", _NOW)


def test_build_post_summary() -> None:
    parsed = _entries(_ITEM_FULL)
    entry = parsed.entries[0]
    post = post_mod.build_post(
        parsed, entry, "https://ex.com/1", full_text=None, fetched_at="2026-06-09T00:00:00Z"
    )
    assert post["platform"] == "rss"
    assert post["author"] == {"id": _FEED_URL, "name": "Example Feed"}
    assert post["text"] == "Full body one"
    assert post["source_id"] == "https://ex.com/1"
    assert post["summary_html"] == "<p>Full body one</p>"
    assert post["_full"] is False
    assert post["fetched_via"] == "rss-feed"
    assert list(post) == [
        "platform",
        "url",
        "source_id",
        "feed_url",
        "feed_title",
        "title",
        "text",
        "summary_html",
        "author",
        "published_at",
        "fetched_at",
        "fetched_via",
        "_full",
    ]


def test_build_post_full_uses_the_article_body() -> None:
    parsed = _entries(_ITEM_FULL)
    post = post_mod.build_post(
        parsed,
        parsed.entries[0],
        "https://ex.com/1",
        full_text="THE FULL ARTICLE BODY from the generic adapter",
        fetched_at="t",
    )
    assert post["text"] == "THE FULL ARTICLE BODY from the generic adapter"
    assert post["_full"] is True


@pytest.mark.parametrize("full_text", [None, "", "  \n"])
def test_build_post_full_falls_back_to_the_summary_without_a_body(full_text: str | None) -> None:
    parsed = _entries(_ITEM_FULL)
    post = post_mod.build_post(
        parsed, parsed.entries[0], "https://ex.com/1", full_text=full_text, fetched_at="t"
    )
    assert post["text"] == "Full body one"
    assert post["_full"] is False


def test_an_entry_author_replaces_the_feed_title_as_the_author_name() -> None:
    item = "<item><title>T</title><guid>g</guid><author>jane@ex.com (Jane)</author></item>"
    parsed = _entries(item)
    post = post_mod.build_post(parsed, parsed.entries[0], "g", full_text=None, fetched_at="t")
    assert post["author"] == {"id": _FEED_URL, "name": "jane@ex.com (Jane)"}


def test_render_markdown_and_to_s1() -> None:
    parsed = _entries(_ITEM_FULL)
    post = post_mod.build_post(
        parsed, parsed.entries[0], "https://ex.com/1", full_text=None, fetched_at="t"
    )
    assert post_mod.render_markdown(post) == (
        "# First Post\n\n- **feed**: Example Feed (https://ex.com/feed)\n- **author**: Example Feed\n"
        "- **url**: https://ex.com/1\n- **published**: 2026-06-08T10:00:00Z\n\nFull body one\n"
    )
    s1 = post_mod.to_s1(post, matrix_entity="m", raw_path="/mirror/x/")
    assert s1["platform"] == "rss"
    assert s1["source_id"] == "https://ex.com/1"
    assert s1["author"] == {"id": _FEED_URL, "name": "Example Feed"}
    assert s1["matrix_entity"] == "m"
    assert s1["raw_path"] == "/mirror/x/"
    assert s1["media"] == [] and s1["stats"] == {} and s1["transcript"] is None


def _stub_entry(entry_id: str | None, title: str) -> Any:
    return post_mod.Entry(
        id=entry_id,
        title=title,
        url=None,
        published_ts=None,
        published_at=None,
        body_html="",
        author=None,
    )


def test_is_complete_requires_a_full_body_only_for_a_full_run() -> None:
    summary = {"_full": False}
    full = {"_full": True}
    assert plan.is_complete(summary, full=False)
    assert not plan.is_complete(summary, full=True)
    assert plan.is_complete(full, full=True)


def test_plan_sync_reuses_complete_entries_and_builds_the_rest() -> None:
    selected = [_stub_entry("a", "A"), _stub_entry("b", "B"), _stub_entry("c", "C")]
    result = plan.plan_sync(selected, frozenset({"b"}), None)
    assert [(s.key, s.build) for s in result.steps] == [("a", True), ("b", False), ("c", True)]
    assert result.unkeyed == ()


def test_plan_sync_limit_counts_new_entries_and_stops_at_the_first_one_over() -> None:
    selected = [_stub_entry(k, k) for k in "abcde"]
    result = plan.plan_sync(selected, frozenset({"a", "d"}), 1)
    assert [(s.key, s.build) for s in result.steps] == [("a", False), ("b", True)]


def test_plan_sync_reuses_an_id_it_already_built() -> None:
    selected = [_stub_entry("a", "first"), _stub_entry("a", "again")]
    result = plan.plan_sync(selected, frozenset(), None)
    assert [(s.entry.title, s.build) for s in result.steps] == [("first", True), ("again", False)]


def test_plan_sync_leaves_out_entries_with_no_key() -> None:
    selected = [_stub_entry(None, "keyless"), _stub_entry("a", "A")]
    result = plan.plan_sync(selected, frozenset(), None)
    assert [s.key for s in result.steps] == ["a"]
    assert [e.title for e in result.unkeyed] == ["keyless"]


def test_entry_slug_is_keyed_on_the_id_only() -> None:
    slug = feed._entry_slug("https://ex.com/posts/hello-world/")
    assert slug.startswith("hello-world-")
    assert feed._entry_slug("https://ex.com/posts/hello-world/") == slug
    assert feed._entry_slug("tag:ex.com,2026:1").startswith("ex.com-2026-1-")
    assert feed._entry_slug("///").startswith("entry-")


def test_default_root_under_meiva_home(tmp_path: Path) -> None:
    assert feed.default_mirror(tmp_path) == tmp_path / "state" / "mirrors" / "rss"


def test_fetch_feed_reads_the_feed_through_curl(web: FakeWeb) -> None:
    parsed = feed.fetch_feed(web.curl(), _FEED_URL)
    assert parsed.title == "Example Feed"
    assert [e.title for e in parsed.entries] == ["First Post"]


def test_fetch_feed_names_a_curl_failure(web: FakeWeb) -> None:
    with pytest.raises(post_mod.RssError, match=r"curl failed for https://ex.com/gone \(rc=6\)"):
        feed.fetch_feed(web.curl(), "https://ex.com/gone")


def test_fetch_feed_refuses_an_internal_url_without_starting_curl(web: FakeWeb) -> None:
    curl = web.curl()
    for url, message in [
        ("file:///etc/passwd", "refusing non-http(s) feed URL"),
        ("http://localhost./feed", "refusing internal host"),
        ("http://192.168.1.2/feed", "refusing internal IP"),
    ]:
        with pytest.raises(post_mod.RssError, match=re.escape(message)):
            feed.fetch_feed(curl, url)
    assert web.calls() == []


def test_enum_since_drops_old_keeps_undated(web: FakeWeb) -> None:
    web.site(_FEED_URL, _rss(_ITEM_FULL + _ITEM_OLD + _ITEM_UNDATED))
    cutoff = _JAN_2026
    metas = feed.enum(web.curl(), _FEED_URL, since_ts=cutoff, limit=None, now=_NOW)
    assert [m["title"] for m in metas] == ["No Date Post", "First Post"]
    assert set(metas[0]) == {"id", "title", "url", "published_at"}


def test_enum_limit(web: FakeWeb) -> None:
    web.site(_FEED_URL, _rss(_ITEM_FULL + _ITEM_OLD))
    metas = feed.enum(web.curl(), _FEED_URL, since_ts=None, limit=1, now=_NOW)
    assert len(metas) == 1
    assert metas[0]["title"] == "First Post"


def test_sync_writes_and_dedups(web: FakeWeb, tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    items = _sync(web, root)
    assert len(items) == 1
    s1 = items[0]
    assert s1["platform"] == "rss"
    assert s1["source_id"] == "https://ex.com/1"
    assert s1["author"]["id"] == _FEED_URL
    outdir = root / feed._entry_slug("https://ex.com/1")
    assert (outdir / "post.json").exists()
    assert (outdir / "post.md").exists()
    assert s1["raw_path"] == str(outdir) + "/"
    saved = json.loads((outdir / "post.json").read_text())
    assert saved["text"] == "Full body one" and saved["_full"] is False

    web.site(_FEED_URL, _rss(_ITEM_FULL.replace("Full body one", "Edited upstream")))
    again = _sync(web, root)
    assert [i["text"] for i in again] == ["Full body one"]


def test_sync_limit_counts_new_items_and_advances(web: FakeWeb, tmp_path: Path) -> None:
    web.site(_FEED_URL, _rss(_ITEM_FULL + _ITEM_OLD))
    root = tmp_path / "mirror"
    r1 = _sync(web, root, limit=1)
    assert [i["source_id"] for i in r1] == ["https://ex.com/1"]
    r2 = _sync(web, root, limit=1)
    ids = {i["source_id"] for i in r2}
    assert "https://ex.com/old" in ids


def test_sync_since_windows_the_feed(web: FakeWeb, tmp_path: Path) -> None:
    web.site(_FEED_URL, _rss(_ITEM_FULL + _ITEM_OLD))
    cutoff = _JAN_2026
    items = _sync(web, tmp_path / "mirror", since_ts=cutoff)
    assert [i["source_id"] for i in items] == ["https://ex.com/1"]


def test_sync_leaves_out_an_entry_with_no_key_and_says_so(
    web: FakeWeb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    web.site(_FEED_URL, _rss(_ITEM_NO_KEY + _ITEM_FULL))
    items = _sync(web, tmp_path / "mirror")
    assert [i["source_id"] for i in items] == ["https://ex.com/1"]
    assert "[skip] feed entry without an id, link or title" in capsys.readouterr().err


def test_sync_full_then_summary_reuse(web: FakeWeb, tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    _sync(web, root)
    assert feed._is_saved_complete(root, "https://ex.com/1", False)
    assert not feed._is_saved_complete(root, "https://ex.com/1", True)
    assert not feed._is_saved_complete(root, "https://ex.com/none", False)


def test_sync_full_fetches_each_entry_body_through_the_ladder(web: FakeWeb, tmp_path: Path) -> None:
    web.site("https://ex.com/1", _ARTICLE_HTML)
    root = tmp_path / "mirror"
    items = _sync(web, root, full=True)
    assert "First paragraph of the article body" in items[0]["text"]
    saved = feed._read_post(root, "https://ex.com/1")
    assert saved["_full"] is True
    assert saved["summary_html"] == "<p>Full body one</p>"


def test_sync_full_after_a_summary_sync_rebuilds_the_entry(web: FakeWeb, tmp_path: Path) -> None:
    web.site("https://ex.com/1", _ARTICLE_HTML)
    root = tmp_path / "mirror"
    _sync(web, root)
    assert feed._read_post(root, "https://ex.com/1")["_full"] is False
    _sync(web, root, full=True)
    assert feed._read_post(root, "https://ex.com/1")["_full"] is True
    before = len(web.calls())
    _sync(web, root, full=True)
    assert [c[-1] for c in web.calls()[before:]] == [_FEED_URL]


def test_sync_full_falls_back_to_the_summary_when_the_page_cannot_be_read(
    web: FakeWeb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "mirror"
    items = _sync(web, root, full=True)
    assert [i["text"] for i in items] == ["Full body one"]
    assert feed._read_post(root, "https://ex.com/1")["_full"] is False
    assert "[full-fallback] https://ex.com/1:" in capsys.readouterr().err


def test_sync_full_skips_the_fetch_for_an_entry_without_a_link(
    web: FakeWeb, tmp_path: Path
) -> None:
    keyed = "<item><title>T</title><guid>g1</guid><description>d</description></item>"
    web.site(_FEED_URL, _rss(keyed))
    items = _sync(web, tmp_path / "mirror", full=True)
    assert [i["text"] for i in items] == ["d"]
    assert [c[-1] for c in web.calls()] == [_FEED_URL]


def test_run_enum_prints_the_listing(web: FakeWeb) -> None:
    web.site(_FEED_URL, _rss(_ITEM_FULL + _ITEM_OLD))
    result = feed.run(
        ["enum", "--url", _FEED_URL, "--since", "2d", "--limit", "5"], web.scrape(), _NOW
    )
    assert [r["title"] for r in result] == ["First Post"]


def test_run_sync_writes_to_the_out_dir(web: FakeWeb, tmp_path: Path) -> None:
    out = tmp_path / "mirror"
    result = feed.run(
        ["sync", "--url", _FEED_URL, "--out", str(out), "--matrix-entity", "m"], web.scrape(), _NOW
    )
    assert [r["matrix_entity"] for r in result] == ["m"]
    assert (out / feed._entry_slug("https://ex.com/1") / "post.json").exists()


def test_run_sync_defaults_to_the_mirror_under_the_home(web: FakeWeb, unit_home: Path) -> None:
    result = feed.run(["sync", "--url", _FEED_URL], web.scrape(), _NOW)
    mirror = unit_home / "state" / "mirrors" / "rss"
    assert result[0]["raw_path"] == str(mirror / feed._entry_slug("https://ex.com/1")) + "/"


def test_run_sync_full_uses_the_scrape_config(web: FakeWeb, tmp_path: Path) -> None:
    web.site("https://ex.com/1", _ARTICLE_HTML)
    result = feed.run(
        ["sync", "--url", _FEED_URL, "--out", str(tmp_path / "m"), "--full"], web.scrape(), _NOW
    )
    assert "First paragraph of the article body" in result[0]["text"]


# meiva:note
# The script is started the way the agent starts it: a child interpreter with PYTHONSAFEPATH=1,
# which keeps the script's own directory off sys.path, so its sibling modules load only through
# the sys.path guards the script carries.
def test_the_script_starts_with_its_own_directory_off_the_path() -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(_RSS_FEED_SCRIPT), "sync", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONSAFEPATH": "1", "PATH": ""},
    )
    assert done.returncode == 0, done.stderr
    assert "--full" in done.stdout
