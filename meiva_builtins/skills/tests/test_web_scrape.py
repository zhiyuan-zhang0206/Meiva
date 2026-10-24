# meiva:note
# The web-sources:generic skill (meiva_builtins/skills/integrations/web-sources/scripts).
#
# What is sent, how a page or a Jina answer is read, which rung wins and which links are
# articles are values in scrape_wire, scrape_ladder and scrape_post, so those tests pass values
# and compare what comes back (trafilatura runs for real on fixture HTML). The shell in
# webscrape.py runs against a stand-in `curl` program written to tmp_path that plays both the
# site and the Jina endpoint from a route table, and writes the mirror to real files, so no test
# touches the network or replaces a name. The live behavior against real 403-prone sites was
# verified by hand during the build.

from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script

_SCRIPTS = ("integrations", "web-sources", "scripts")
wire = load_skill_script(*_SCRIPTS, "scrape_wire.py", name="scrape_wire")
ladder = load_skill_script(*_SCRIPTS, "scrape_ladder.py", name="scrape_ladder")
post_view = load_skill_script(*_SCRIPTS, "scrape_post.py", name="scrape_post")
scrape = load_skill_script(*_SCRIPTS, "webscrape.py")

_SKILLS = Path(__file__).resolve().parents[1] / "integrations" / "web-sources"
_GENERIC_ENTRY = _SKILLS / "generic" / "scripts" / "feed.py"
_WEBSCRAPE = _SKILLS / "scripts" / "webscrape.py"
_JINA = "https://jina.test/"

_ARTICLE_HTML = """<html><head><title>Headline Here</title>
<meta property="og:site_name" content="Example News"/>
<meta property="article:published_time" content="2026-06-08T10:00:00Z"/>
<meta name="author" content="Jane Doe"/></head>
<body><article><h1>Headline Here</h1>
<p>First paragraph of the body with enough words to be considered the main
content by the extractor algorithm running over this fixture page here.</p>
<p>Second paragraph also carries a fair amount of text so the extractor keeps it
in the extracted markdown output reliably across versions.</p>
</article><nav class="menu">home about contact junk to drop</nav></body></html>"""

_LIST_HTML = """<html><body>
<a href="/portal.php?mod=view&aid=101">Article 101</a>
<a href="portal.php?mod=view&aid=102">Article 102</a>
<a href="https://other.example.com/portal.php?mod=view&aid=103">Cross-site 103</a>
<a href="/portal.php?mod=view&aid=101#comments">Dup 101 with fragment</a>
<a href="/about.html">Not an article</a>
<a href="https://ad.example.com/banner">Ad</a>
</body></html>"""

_EMPTY_HTML = "<html><body></body></html>"

_WALL_HTML = "<html><head><title>Just a moment...</title></head><body>Enable JavaScript and cookies to continue</body></html>"

_JINA_RAW = """Title: Real Headline

URL Source: https://walled.example/article

Markdown Content:
# Real Headline

The full rendered body that only a server-side browser could reach, well past
the Cloudflare challenge that blocked the bare curl client.
"""

_MD_LIST = """Title: Forum Index

URL Source: https://walled.example/list

Markdown Content:
- [Article 101](https://walled.example/portal.php?mod=view&aid=101)
- [Article 102](https://walled.example/portal.php?mod=view&aid=102)
- [About](https://walled.example/about.html)
"""

_JINA_TARGET_ERROR = (
    "Title: 403 Forbidden\n\nURL Source: https://blocked.example/list\n\n"
    "Warning: Target URL returned error 403: Forbidden\n\nMarkdown Content:\n* * *\nwall"
)

# meiva:note
# A `curl` that answers from a route table next to it and logs the argv it was started with. A
# URL under the Jina base is a render request: the stand-in decodes the page URL it carries, as
# Jina Reader does, and looks the page up under `jina <url>`; any other URL is looked up under
# `site <url>`. A route is a page body, or an exit code with a stderr line (`curl -f` exits 22
# on an HTTP error); an unknown URL fails the way an unresolvable host does.
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

    def jina(self, url: str, raw: str) -> FakeWeb:
        self.routes[f"jina {url}"] = {"body": raw}
        return self

    def config(self, key: str | None = None) -> Any:
        (self.directory / "routes.json").write_text(json.dumps(self.routes))
        curl = self.directory / "curl"
        curl.write_text(_STAND_IN_CURL.format(python=sys.executable, jina=_JINA))
        curl.chmod(0o755)
        return wire.ScrapeConfig(curl=str(curl), jina_base=_JINA, jina_api_key=key)

    def calls(self) -> list[list[str]]:
        log = self.directory / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def web(tmp_path: Path) -> FakeWeb:
    directory = tmp_path / "bin"
    directory.mkdir()
    return FakeWeb(directory)


def test_url_slug_is_stable_and_safe() -> None:
    url = "https://forum.example/portal.php?mod=view&aid=51906"
    s1 = scrape._url_slug(url)
    s2 = scrape._url_slug(url)
    assert s1 == s2
    assert "/" not in s1 and "?" not in s1 and "&" not in s1
    assert scrape._url_slug(url + "x") != s1


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://localhost/admin",
        "http://localhost./admin",
        "http://admin.localhost/x",
        "http://127.0.0.1/x",
        "http://[::1]/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
    ],
)
def test_assert_fetchable_rejects(url: str) -> None:
    with pytest.raises(wire.WebScrapeError):
        scrape._assert_fetchable(url)


@pytest.mark.parametrize("url", ["https://example.com/a", "http://news.site.cn/x.html"])
def test_assert_fetchable_allows(url: str) -> None:
    scrape._assert_fetchable(url)


def test_refusal_names_the_reason() -> None:
    assert (
        wire.refusal("ftp://x/y", "ftp", "x", noun="URL") == "refusing non-http(s) URL: 'ftp://x/y'"
    )
    assert (
        wire.refusal("ftp://x/y", "ftp", "x", noun="feed URL")
        == "refusing non-http(s) feed URL: 'ftp://x/y'"
    )
    assert wire.refusal("http://LocalHost./", "http", "LocalHost.", noun="URL") == (
        "refusing internal host: 'localhost'"
    )
    assert wire.refusal("http://10.1.2.3/", "http", "10.1.2.3", noun="URL") == (
        "refusing internal IP: '10.1.2.3'"
    )
    assert wire.refusal("https://example.com/", "https", "example.com", noun="URL") is None
    assert wire.refusal("http:///x", "http", None, noun="URL") is None


def test_curl_argv_fetches_with_a_browser_ua_and_fails_on_http_errors() -> None:
    argv = wire.curl_argv("curl", "https://example.com/a")
    assert argv[0] == "curl"
    assert argv[-1] == "https://example.com/a"
    assert "-f" in argv[1]
    assert argv[argv.index("--max-time") + 1] == "45"
    assert "Chrome" in argv[argv.index("-A") + 1]


def test_jina_argv_carries_the_key_only_when_the_config_has_one() -> None:
    plain = wire.jina_argv(wire.ScrapeConfig("curl", _JINA, None), _JINA + "x")
    assert "-H" not in plain
    assert plain[-1] == _JINA + "x"
    keyed = wire.jina_argv(wire.ScrapeConfig("curl", _JINA, "k3y"), _JINA + "x")
    assert keyed[keyed.index("-H") + 1] == "Authorization: Bearer k3y"
    assert keyed[-1] == _JINA + "x"
    assert keyed[0] == "curl"


def test_config_from_env_reads_the_jina_key() -> None:
    assert wire.config_from_env({}).jina_api_key is None
    assert wire.config_from_env({"JINA_API_KEY": "abc"}).jina_api_key == "abc"
    config = wire.config_from_env({"PATH": "/bin"})
    assert (config.curl, config.jina_base) == (wire.CURL, wire.JINA_BASE)


def test_html_text_decodes_or_names_the_failure() -> None:
    assert wire.html_text("https://e.com", 0, "caf\u00e9".encode(), b"") == "caf\u00e9"
    assert wire.html_text("https://e.com", 0, b"ok\xff", b"") == "ok\ufffd"
    with pytest.raises(wire.WebScrapeError, match=r"curl failed for https://e.com \(rc=22\): boom"):
        wire.html_text("https://e.com", 22, b"", b"boom")


def test_render_text_refuses_a_failed_render_and_a_target_error() -> None:
    assert wire.render_text("https://e.com", 0, b"Title: T", b"") == "Title: T"
    with pytest.raises(wire.WebScrapeError, match=r"jina render failed for https://e.com \(rc=6\)"):
        wire.render_text("https://e.com", 6, b"", b"no host")
    with pytest.raises(wire.WebScrapeError, match="target refused") as caught:
        wire.render_text("https://blocked.example/list", 0, _JINA_TARGET_ERROR.encode(), b"")
    assert "Title: 403 Forbidden" in str(caught.value)


def test_extract_article_real() -> None:
    art = post_view.extract_article(
        _ARTICLE_HTML, "https://example.com/news/headline", "example.com"
    )
    assert art["title"] == "Headline Here"
    assert art["author"] == "Jane Doe"
    assert art["date"] == "2026-06-08"
    assert art["sitename"] == "Example News"
    assert "First paragraph of the body" in art["text"]
    assert "junk to drop" not in art["text"]


def test_extract_article_without_a_body_is_empty_text() -> None:
    art = post_view.extract_article(_EMPTY_HTML, "https://example.com/list", "example.com")
    assert art["text"] == ""
    assert art["hostname"] == "example.com"


def _article(text: str) -> dict[str, Any]:
    return {
        "title": "T",
        "author": None,
        "date": None,
        "sitename": None,
        "hostname": "example.com",
        "description": None,
        "text": text,
    }


def test_has_wall_marker() -> None:
    assert ladder.has_wall_marker(_WALL_HTML)
    assert not ladder.has_wall_marker("<html><body><p>a normal article body</p></body></html>")


def test_ladder_accepts_a_body_with_no_wall() -> None:
    article = _article("Real body content here.")
    step = ladder.ladder_step("https://e.com/a", "<html>fine</html>", article)
    assert step == ladder.Accept(article)


def test_ladder_does_not_escalate_an_empty_page_with_no_wall() -> None:
    step = ladder.ladder_step("https://e.com/list", _EMPTY_HTML, _article("  "))
    assert isinstance(step, ladder.NoBody)
    assert "no article body extracted from https://e.com/list" in step.message


@pytest.mark.parametrize(
    ("page", "article"),
    [
        pytest.param("", None, id="curl-failed"),
        pytest.param(_WALL_HTML, _article(""), id="wall-in-html"),
        pytest.param(
            "<html>fine</html>", _article("Please wait while we verify"), id="wall-in-body"
        ),
    ],
)
def test_ladder_escalates_a_walled_or_missing_result(
    page: str, article: dict[str, Any] | None
) -> None:
    assert ladder.ladder_step("https://e.com/a", page, article) == ladder.Escalate()


def test_split_jina_strips_header() -> None:
    art = ladder.jina_article(_JINA_RAW, "https://walled.example/article", "walled.example")
    assert art["title"] == "Real Headline"
    assert art["text"].startswith("# Real Headline")
    assert "Title:" not in art["text"] and "Markdown Content:" not in art["text"]
    assert art["hostname"] == "walled.example"
    assert art["author"] is None and art["date"] is None


def test_jina_article_without_a_divider_takes_the_whole_answer() -> None:
    art = ladder.jina_article("just a rendered page body", "https://e.com/a", "e.com")
    assert art["title"] is None
    assert art["text"] == "just a rendered page body"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("Title: X\n\nMarkdown Content:\n   ", id="empty"),
        pytest.param("Title: X\n\nMarkdown Content:\nJust a moment...", id="walled"),
    ],
)
def test_jina_article_refuses_an_empty_or_walled_render(raw: str) -> None:
    with pytest.raises(wire.WebScrapeError, match="login-gated"):
        ladder.jina_article(raw, "https://walled.example/article", "walled.example")


def test_raw_links_reads_hrefs_and_decodes_entities() -> None:
    html = "<a href=\"/portal.php?mod=view&amp;aid=7\">x</a> <A HREF='/b'>y</A>"
    assert ladder.raw_links(html, "html") == ("/portal.php?mod=view&aid=7", "/b")


def test_raw_links_reads_markdown_links() -> None:
    assert ladder.raw_links(_MD_LIST, "markdown") == (
        "https://walled.example/portal.php?mod=view&aid=101",
        "https://walled.example/portal.php?mod=view&aid=102",
        "https://walled.example/about.html",
    )


def test_select_links_filters_dedups_and_keeps_page_order() -> None:
    absolute = ["https://s/a?aid=2", "https://s/x", "https://s/a?aid=1", "https://s/a?aid=2"]
    assert ladder.select_links(absolute, r"aid=\d+", None) == (
        "https://s/a?aid=2",
        "https://s/a?aid=1",
    )
    assert ladder.select_links(absolute, None, None) == (
        "https://s/a?aid=2",
        "https://s/x",
        "https://s/a?aid=1",
    )


def test_select_links_limit_takes_the_first_n_and_zero_takes_none() -> None:
    absolute = [f"https://s/a?aid={n}" for n in range(5)]
    assert ladder.select_links(absolute, r"aid=", 2) == ("https://s/a?aid=0", "https://s/a?aid=1")
    assert ladder.select_links(absolute, r"aid=", 0) == ()


def test_harvest_links_pattern_absolute_dedup(web: FakeWeb) -> None:
    config = web.site("https://forum.example/portal.php", _LIST_HTML).config()
    links = scrape.harvest_links(config, "https://forum.example/portal.php", r"mod=view&aid=\d+")
    assert links == [
        "https://forum.example/portal.php?mod=view&aid=101",
        "https://forum.example/portal.php?mod=view&aid=102",
        "https://other.example.com/portal.php?mod=view&aid=103",
    ]


def test_harvest_links_limit(web: FakeWeb) -> None:
    config = web.site("https://forum.example/portal.php", _LIST_HTML).config()
    links = scrape.harvest_links(
        config, "https://forum.example/portal.php", r"mod=view&aid=\d+", limit=2
    )
    assert len(links) == 2


def test_harvest_links_unescapes_entities(web: FakeWeb) -> None:
    config = web.site(
        "https://s.cn/portal.php", '<a href="/portal.php?mod=view&amp;aid=7">x</a>'
    ).config()
    links = scrape.harvest_links(config, "https://s.cn/portal.php", r"mod=view&aid=\d+")
    assert links == ["https://s.cn/portal.php?mod=view&aid=7"]


def test_harvest_escalates_to_jina_on_curl_403(web: FakeWeb) -> None:
    url = "https://walled.example/list"
    config = web.blocked(url).jina(url, _MD_LIST).config()
    links = scrape.harvest_links(config, url, r"mod=view&aid=\d+")
    assert links == [
        "https://walled.example/portal.php?mod=view&aid=101",
        "https://walled.example/portal.php?mod=view&aid=102",
    ]


def test_harvest_escalates_on_wall_marker(web: FakeWeb) -> None:
    url = "https://walled.example/list"
    config = web.site(url, _WALL_HTML).jina(url, _MD_LIST).config()
    assert len(scrape.harvest_links(config, url, r"mod=view&aid=\d+")) == 2


def test_harvest_markdown_relative_links_absolutized(web: FakeWeb) -> None:
    url = "https://walled.example/list"
    md = "Title: X\n\nMarkdown Content:\n- [A](/portal.php?mod=view&aid=7)\n"
    config = web.blocked(url).jina(url, md).config()
    assert scrape.harvest_links(config, url, r"aid=\d+") == [
        "https://walled.example/portal.php?mod=view&aid=7"
    ]


def test_harvest_no_escalation_when_curl_succeeds(web: FakeWeb) -> None:
    url = "https://forum.example/portal.php"
    config = web.site(url, _LIST_HTML).config()
    links = scrape.harvest_links(config, url, r"mod=view&aid=\d+")
    assert len(links) == 3
    assert all(not call[-1].startswith(_JINA) for call in web.calls())


def test_harvest_both_rungs_fail_raises(web: FakeWeb) -> None:
    config = web.blocked("https://walled.example/list").config()
    with pytest.raises(wire.WebScrapeError, match="jina render failed"):
        scrape.harvest_links(config, "https://walled.example/list", r"aid=\d+")


def test_jina_render_target_error_raises(web: FakeWeb) -> None:
    url = "https://blocked.example/list"
    config = web.blocked(url).jina(url, _JINA_TARGET_ERROR).config()
    with pytest.raises(wire.WebScrapeError, match="target refused"):
        scrape.harvest_links(config, url, r"aid=\d+")


def test_fetch_writes_mirror_and_s1(web: FakeWeb, tmp_path: Path) -> None:
    url = "https://example.com/news/headline"
    config = web.site(url, _ARTICLE_HTML).config()
    root = tmp_path / "mirror"
    post = scrape.fetch(config, url, root=root)
    outdir = root / scrape._url_slug(url)
    assert (outdir / "post.json").exists()
    assert (outdir / "post.md").exists()
    assert post["author"]["id"] == "example.com"
    assert json.loads((outdir / "post.json").read_text()) == post
    assert list(post) == [
        "platform",
        "url",
        "source_id",
        "title",
        "text",
        "description",
        "author",
        "sitename",
        "published_at",
        "fetched_at",
        "fetched_via",
    ]

    s1 = post_view.to_s1(post, matrix_entity="ent", raw_path=scrape._raw_path(root, url))
    assert s1["platform"] == "web"
    assert s1["source_id"] == url
    assert s1["author"]["id"] == "example.com"
    assert s1["matrix_entity"] == "ent"
    assert s1["raw_path"] == str(outdir) + "/"


def test_fetch_markdown_rendering(web: FakeWeb, tmp_path: Path) -> None:
    url = "https://example.com/news/headline"
    config = web.site(url, _ARTICLE_HTML).config()
    post = scrape.fetch(config, url, root=tmp_path)
    markdown = (tmp_path / scrape._url_slug(url) / "post.md").read_text()
    assert markdown == post_view.render_markdown(post)
    assert markdown.startswith("# Headline Here\n\n- **site**: Example News (example.com)\n")


def test_fetch_no_body_raises(web: FakeWeb, tmp_path: Path) -> None:
    url = "https://example.com/list"
    config = web.site(url, _EMPTY_HTML).config()
    root = tmp_path / "mirror"
    with pytest.raises(wire.WebScrapeError, match="no article body"):
        scrape.fetch(config, url, root=root)
    assert not root.exists()


def test_fetch_refuses_an_internal_host_without_starting_curl(web: FakeWeb, tmp_path: Path) -> None:
    config = web.config()
    with pytest.raises(wire.WebScrapeError, match="refusing internal host"):
        scrape.fetch(config, "http://localhost/admin", root=tmp_path)
    assert web.calls() == []


def test_fetch_platform_tag(web: FakeWeb, tmp_path: Path) -> None:
    url = "https://example.com/news/headline"
    config = web.site(url, _ARTICLE_HTML).config()
    post = scrape.fetch(config, url, root=tmp_path, platform="local-news")
    assert post["platform"] == "local-news"
    s1 = post_view.to_s1(post, matrix_entity=None, raw_path=scrape._raw_path(tmp_path, url))
    assert s1["platform"] == "local-news"


def test_fetch_escalates_to_jina_on_wall(web: FakeWeb, tmp_path: Path) -> None:
    url = "https://walled.example/article"
    config = web.site(url, _WALL_HTML).jina(url, _JINA_RAW).config()
    post = scrape.fetch(config, url, root=tmp_path)
    assert post["fetched_via"] == "web-scrape+jina"
    assert "full rendered body" in post["text"]
    assert post["title"] == "Real Headline"
    assert post["author"] == {"id": "walled.example", "name": "walled.example"}


def test_fetch_curl_403_escalates_to_jina(web: FakeWeb, tmp_path: Path) -> None:
    url = "https://walled.example/article"
    config = web.blocked(url).jina(url, _JINA_RAW).config()
    post = scrape.fetch(config, url, root=tmp_path)
    assert post["fetched_via"] == "web-scrape+jina"


def test_fetch_index_page_does_not_escalate(web: FakeWeb, tmp_path: Path) -> None:
    url = "https://example.com/list"
    config = web.site(url, _EMPTY_HTML).config()
    with pytest.raises(wire.WebScrapeError, match="no article body"):
        scrape.fetch(config, url, root=tmp_path)
    assert all(not call[-1].startswith(_JINA) for call in web.calls())


def test_fetch_both_rungs_walled_raises(web: FakeWeb, tmp_path: Path) -> None:
    url = "https://walled.example/article"
    config = (
        web.site(url, _WALL_HTML)
        .jina(url, "Title: x\n\nMarkdown Content:\nJust a moment...")
        .config()
    )
    with pytest.raises(wire.WebScrapeError, match="login-gated"):
        scrape.fetch(config, url, root=tmp_path)


def test_the_jina_key_reaches_the_render_request(web: FakeWeb, tmp_path: Path) -> None:
    url = "https://walled.example/article"
    config = web.blocked(url).jina(url, _JINA_RAW).config(key="k3y")
    scrape.fetch(config, url, root=tmp_path)
    render = next(call for call in web.calls() if call[-1].startswith(_JINA))
    assert render[render.index("-H") + 1] == "Authorization: Bearer k3y"
    assert render[-1] == _JINA + urllib.parse.quote(url, safe="")


def test_sync_dedups_and_skips_dead(web: FakeWeb, tmp_path: Path) -> None:
    good = "https://example.com/portal.php?mod=view&aid=101"
    dead = "https://example.com/portal.php?mod=view&aid=102"
    listing = "https://example.com/list"
    web.site(listing, f'<a href="{good}">a</a><a href="{dead}">b</a>')
    web.site(good, _ARTICLE_HTML).site(dead, _EMPTY_HTML)
    root = tmp_path / "mirror"
    items = scrape.sync(web.config(), listing, link_pattern=r"aid=\d+", root=root)
    assert [i["url"] for i in items] == [good]

    web.blocked(good)
    again = scrape.sync(web.config(), listing, link_pattern=r"aid=\d+", root=root)
    assert [i["url"] for i in again] == [good]


def test_sync_skip_is_reported_on_stderr(
    web: FakeWeb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = "https://example.com/portal.php?mod=view&aid=101"
    dead = "https://example.com/portal.php?mod=view&aid=102"
    listing = "https://example.com/list"
    web.site(listing, f'<a href="{good}">a</a><a href="{dead}">b</a>')
    web.site(good, _ARTICLE_HTML).site(dead, _EMPTY_HTML)
    scrape.sync(web.config(), listing, link_pattern=r"aid=\d+", root=tmp_path)
    assert f"[skip] {dead}: no article body" in capsys.readouterr().err


def test_sync_all_links_fail_raises(web: FakeWeb, tmp_path: Path) -> None:
    listing = "https://e.com/list"
    web.site(listing, '<a href="https://e.com/x?aid=1">a</a><a href="https://e.com/y?aid=2">b</a>')
    web.site("https://e.com/x?aid=1", _EMPTY_HTML).site("https://e.com/y?aid=2", _EMPTY_HTML)
    with pytest.raises(wire.WebScrapeError, match="none yielded an article"):
        scrape.sync(web.config(), listing, link_pattern=r"aid=\d+", root=tmp_path)


def test_sync_no_matching_links_is_an_empty_window(web: FakeWeb, tmp_path: Path) -> None:
    listing = "https://e.com/list"
    web.site(listing, '<a href="https://e.com/about">a</a>')
    assert scrape.sync(web.config(), listing, link_pattern=r"aid=\d+", root=tmp_path) == []


def test_sync_limit_caps_new_fetches_and_advances(web: FakeWeb, tmp_path: Path) -> None:
    urls = [f"https://example.com/p.php?aid={n}" for n in (1, 2, 3)]
    listing = "https://example.com/list"
    web.site(listing, "".join(f'<a href="{u}">x</a>' for u in urls))
    for u in urls:
        web.site(u, _ARTICLE_HTML)
    first = scrape.sync(web.config(), listing, link_pattern=r"aid=", limit=2, root=tmp_path)
    assert [i["url"] for i in first] == urls[:2]
    second = scrape.sync(web.config(), listing, link_pattern=r"aid=", limit=2, root=tmp_path)
    assert [i["url"] for i in second] == urls[:2] + urls[2:3]


def test_sync_escalates_list_page_to_jina(web: FakeWeb, tmp_path: Path) -> None:
    listing = "https://walled.example/list"
    a = "https://walled.example/portal.php?mod=view&aid=101"
    b = "https://walled.example/portal.php?mod=view&aid=102"
    web.blocked(listing).jina(listing, _MD_LIST).site(a, _ARTICLE_HTML).site(b, _ARTICLE_HTML)
    items = scrape.sync(web.config(), listing, link_pattern=r"aid=\d+", root=tmp_path)
    assert [i["url"] for i in items] == [a, b]


def test_default_root_under_meiva_home(tmp_path: Path) -> None:
    assert scrape.default_mirror(tmp_path) == tmp_path / "state" / "mirrors" / "web-scrape"


def test_to_s1_uses_passed_root(tmp_path: Path) -> None:
    post = {
        "url": "https://example.com/x",
        "title": "T",
        "text": "body",
        "author": {"id": "example.com", "name": "Example News"},
        "published_at": "2026-06-08",
        "fetched_at": "2026-06-08T00:00:00Z",
        "fetched_via": "web-scrape",
    }
    raw_path = scrape._raw_path(tmp_path, post["url"])
    s1 = post_view.to_s1(post, matrix_entity=None, raw_path=raw_path)
    assert s1["raw_path"] == str(tmp_path / scrape._url_slug(post["url"])) + "/"
    assert s1["platform"] == "web"
    assert json.dumps(s1)


def test_run_fetch_prints_the_s1_item_and_writes_the_mirror(web: FakeWeb, tmp_path: Path) -> None:
    url = "https://example.com/news/headline"
    config = web.site(url, _ARTICLE_HTML).config()
    out = tmp_path / "mirror"
    result = scrape.run(
        ["fetch", "--url", url, "--out", str(out), "--matrix-entity", "ent"], config
    )
    assert result["source_id"] == url
    assert result["matrix_entity"] == "ent"
    assert result["raw_path"] == str(out / scrape._url_slug(url)) + "/"
    assert (out / scrape._url_slug(url) / "post.json").exists()


def test_run_fetch_defaults_to_the_mirror_under_the_home(web: FakeWeb, unit_home: Path) -> None:
    url = "https://example.com/news/headline"
    config = web.site(url, _ARTICLE_HTML).config()
    result = scrape.run(["fetch", "--url", url], config)
    mirror = unit_home / "state" / "mirrors" / "web-scrape"
    assert result["raw_path"] == str(mirror / scrape._url_slug(url)) + "/"
    assert (mirror / scrape._url_slug(url) / "post.json").exists()


def test_run_enum_lists_the_links(web: FakeWeb) -> None:
    listing = "https://forum.example/portal.php"
    config = web.site(listing, _LIST_HTML).config()
    links = scrape.run(
        ["enum", "--url", listing, "--link-pattern", r"aid=10[12]", "--limit", "1"], config
    )
    assert links == ["https://forum.example/portal.php?mod=view&aid=101"]


def test_run_sync_returns_the_items(web: FakeWeb, tmp_path: Path) -> None:
    listing = "https://example.com/list"
    a = "https://example.com/p.php?aid=1"
    web.site(listing, f'<a href="{a}">x</a>').site(a, _ARTICLE_HTML)
    items = scrape.run(
        ["sync", "--url", listing, "--link-pattern", "aid=", "--out", str(tmp_path / "m")],
        web.config(),
    )
    assert [i["url"] for i in items] == [a]


# meiva:note
# The scripts are started the way the agent starts them: a child interpreter with
# PYTHONSAFEPATH=1, which keeps the script's own directory off sys.path, so the sibling modules
# load only through the sys.path guard the script carries.
def test_the_generic_entry_starts_with_its_own_directory_off_the_path() -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(_GENERIC_ENTRY), "fetch", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONSAFEPATH": "1", "PATH": ""},
    )
    assert done.returncode == 0, done.stderr
    assert "--matrix-entity" in done.stdout


# meiva:note
# The same start for the module the entry imports: its own sys.path guard is the one that finds
# the sibling modules when it is run directly.
def test_webscrape_starts_with_its_own_directory_off_the_path() -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(_WEBSCRAPE), "fetch", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONSAFEPATH": "1", "PATH": ""},
    )
    assert done.returncode == 0, done.stderr
    assert "--matrix-entity" in done.stdout
