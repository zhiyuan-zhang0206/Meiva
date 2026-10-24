# meiva:note
# The web-sources:youtube skill (meiva_builtins/skills/integrations/web-sources/youtube/scripts).
#
# What is asked of yt-dlp and how its answers are read (yt_wire), which sources and videos a run
# takes (yt_plan) and what a fetched video becomes (yt_post) are values, so those tests pass
# values and compare what comes back. The implementation in yt_run.py runs against a stand-in
# `yt-dlp` program written to tmp_path that answers from a fixture file next to it (JSON listings,
# caption files, a video file), a stand-in transcription CLI, and a real mirror directory, so no
# test touches the network or replaces a name. The live behavior (real yt-dlp against real
# YouTube) was verified by hand during the build.

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script

_YOUTUBE = ("integrations", "web-sources", "youtube", "scripts")
wire = load_skill_script(*_YOUTUBE, "yt_wire.py", name="yt_wire")
plan = load_skill_script(*_YOUTUBE, "yt_plan.py", name="yt_plan")
post_mod = load_skill_script(*_YOUTUBE, "yt_post.py", name="yt_post")
run = load_skill_script(*_YOUTUBE, "yt_run.py", name="yt_run")
feed = load_skill_script(*_YOUTUBE, "feed.py", name="youtube_feed_under_test")

_FEED_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "web-sources"
    / "youtube"
    / "scripts"
    / "feed.py"
)
_CHANNEL = "UCdemodemodemodemodemo22"
_UPLOADS = "UUdemodemodemodemodemo22"
_PLAYLIST = "PLf2m23nhTg1P5BsOHUOXyQz5RhfUSSVUi"
_A, _B, _C, _D = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"
_VIDEO = "dRsjO-88nBs"

_VIDEO_META: dict[str, Any] = {
    "title": "A Title",
    "description": "A description",
    "channel_id": _CHANNEL,
    "channel": "Chan",
    "uploader": "Upl",
    "uploader_id": "@handle",
    "channel_url": f"https://www.youtube.com/channel/{_CHANNEL}",
    "timestamp": 1753986150,
    "duration": 123,
    "tags": ["a", "b"],
    "thumbnail": "https://img.example/x.jpg",
    "view_count": 10,
    "like_count": 2,
    "comment_count": 1,
}

_POST_KEYS = [
    "platform",
    "video_id",
    "url",
    "title",
    "description",
    "channel",
    "published_at",
    "duration_s",
    "tags",
    "thumbnail",
    "stats",
    "transcript",
    "transcript_kind",
    "fetched_at",
    "fetched_via",
]

_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.910 align:start position:0%
The<00:00:00.160><c> important</c> is your code good&#39;s&nbsp;value
"""


def _watch(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


# meiva:note
# A `yt-dlp` that answers from the fixture file next to it and logs its argv. `--version` prints
# the fixture version; `-J URL` prints the fixture JSON for that URL (a playlist listing is cut
# to the `-I 1:N` window, `-I 0` drops its entries, as the real one does); `--skip-download`
# writes the fixture captions of the requested kind into the `-o` template's directory, or
# nothing; `-f` writes a stand-in video file at the `-o` path. A URL with no fixture fails the
# way an unsupported one does.
_STAND_IN_YTDLP = """#!{python}
import json
import sys
from pathlib import Path

here = Path(__file__).parent
args = sys.argv[1:]
with (here / "calls.log").open("a") as log:
    log.write(json.dumps(args) + "\\n")
fixtures = json.loads((here / "fixtures.json").read_text())
if args == ["--version"]:
    print(fixtures["version"])
    sys.exit(0)
url = args[-1]
if "-J" in args:
    doc = fixtures["json"].get(url)
    if doc is None:
        sys.stderr.write("ERROR: Unsupported URL: " + url + "\\n")
        sys.exit(1)
    doc = dict(doc)
    if "-I" in args and "entries" in doc:
        window = args[args.index("-I") + 1]
        doc["entries"] = [] if window == "0" else doc["entries"][: int(window.split(":")[1])]
    print(json.dumps(doc))
    sys.exit(0)
if "--skip-download" in args:
    kind = "human" if "--write-subs" in args else "auto"
    video_id = url.split("v=")[1]
    vtt = fixtures["subs"].get(video_id, {{}}).get(kind)
    if vtt is not None:
        template = args[args.index("-o") + 1]
        out = template.replace("%(id)s", video_id).replace("%(ext)s", "en.vtt")
        Path(out).write_text(vtt)
    sys.exit(0)
if "-f" in args:
    Path(args[args.index("-o") + 1]).write_bytes(b"VIDEO-BYTES")
    sys.exit(0)
sys.exit(2)
"""

# meiva:note
# A transcription CLI that prints its fixture as JSON, or exits with the fixture's code and
# error, and logs its argv.
_STAND_IN_TRANSCRIBE = """#!{python}
import json
import sys
from pathlib import Path

here = Path(__file__).parent
with (here / "transcribe.log").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
fixture = json.loads((here / "transcribe.json").read_text())
if fixture["exit_code"] is not None:
    sys.stderr.write(fixture["stderr"])
    sys.exit(fixture["exit_code"])
print(json.dumps({{"text": fixture["text"]}}))
"""


# meiva:note
# What the stand-in transcription CLI does when it runs: print `text` as JSON, or, when an
# `exit_code` is given, write `stderr` and exit with that code.
@dataclass(frozen=True)
class Transcription:
    text: str = "whispered text"
    exit_code: int | None = None
    stderr: str = ""


_WHISPERED = Transcription()


class FakeYtdlp:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.fixtures: dict[str, Any] = {"version": "2099.01.01", "json": {}, "subs": {}}

    def video(self, video_id: str, **subs: str | None) -> FakeYtdlp:
        self.fixtures["json"][_watch(video_id)] = _VIDEO_META
        self.fixtures["subs"][video_id] = {k: v for k, v in subs.items() if v is not None}
        return self

    def listing(self, playlist_id: str, ids: list[str]) -> FakeYtdlp:
        entries = [{"id": i, "title": f"t-{i}"} for i in ids]
        self.fixtures["json"][_playlist_url(playlist_id)] = {"id": playlist_id, "entries": entries}
        return self

    def source(self, url: str, meta: dict[str, Any]) -> FakeYtdlp:
        self.fixtures["json"][url] = meta
        return self

    def config(self, root: Path, transcription: Transcription = _WHISPERED) -> Any:
        (self.directory / "fixtures.json").write_text(json.dumps(self.fixtures))
        (self.directory / "transcribe.json").write_text(json.dumps(asdict(transcription)))
        ytdlp = self.directory / "yt-dlp"
        ytdlp.write_text(_STAND_IN_YTDLP.format(python=sys.executable))
        ytdlp.chmod(0o755)
        transcribe = self.directory / "transcribe.py"
        transcribe.write_text(_STAND_IN_TRANSCRIBE.format(python=sys.executable))
        transcribe.chmod(0o755)
        return run.YoutubeConfig(ytdlp=str(ytdlp), transcribe=(str(transcribe),), root=root)

    def calls(self) -> list[list[str]]:
        return self._logged("calls.log")

    def transcriptions(self) -> list[list[str]]:
        return self._logged("transcribe.log")

    def _logged(self, name: str) -> list[list[str]]:
        log = self.directory / name
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def ytdlp(tmp_path: Path) -> FakeYtdlp:
    directory = tmp_path / "bin"
    directory.mkdir()
    return FakeYtdlp(directory)


def _entries(*ids: str | None) -> list[Any]:
    return [wire.PlaylistEntry(id=i, title=f"t-{i}") for i in ids]


def test_channel_id_resolves_to_uploads_playlist() -> None:
    s = plan.plan_source(_CHANNEL)
    assert (s.kind, s.source_id, s.enum_playlist) == ("channel", _CHANNEL, _UPLOADS)


def test_uploads_playlist_recovers_channel_id() -> None:
    s = plan.plan_source(_UPLOADS)
    assert (s.kind, s.source_id, s.enum_playlist) == ("channel", _CHANNEL, _UPLOADS)


def test_playlist_id_resolves_to_itself() -> None:
    s = plan.plan_source(_PLAYLIST)
    assert (s.kind, s.source_id, s.enum_playlist) == ("playlist", _PLAYLIST, _PLAYLIST)


def test_bad_spec_raises() -> None:
    with pytest.raises(ValueError, match="unrecognized YouTube source spec"):
        plan.plan_source("not a url or id")


def test_a_handle_or_a_url_is_looked_up() -> None:
    assert plan.plan_source("  @demo-channel ") == plan.Lookup(
        "https://www.youtube.com/@demo-channel"
    )
    url = "https://www.youtube.com/playlist?list=" + _PLAYLIST
    assert plan.plan_source(url) == plan.Lookup(url)


@pytest.mark.parametrize(
    ("meta", "resolved"),
    [
        pytest.param(
            wire.SourceMeta("@x", _CHANNEL, "Anthropic", None),
            ("channel", _CHANNEL, _UPLOADS, "Anthropic"),
            id="channel",
        ),
        pytest.param(
            wire.SourceMeta(_CHANNEL, None, None, None),
            ("channel", _CHANNEL, _UPLOADS, None),
            id="channel-by-its-id",
        ),
        pytest.param(
            wire.SourceMeta(_PLAYLIST, None, None, "Conf"),
            ("playlist", _PLAYLIST, _PLAYLIST, "Conf"),
            id="playlist",
        ),
    ],
)
def test_a_looked_up_object_resolves_to_a_source(meta: Any, resolved: tuple[Any, ...]) -> None:
    s = plan.source_from_meta("@demo-channel", meta)
    assert (s.kind, s.source_id, s.enum_playlist, s.title) == resolved


def test_a_looked_up_object_that_is_neither_raises() -> None:
    meta = wire.SourceMeta(id="nope", channel_id=None, channel=None, title=None)
    with pytest.raises(ValueError, match="could not resolve '@who' to a channel/playlist"):
        plan.source_from_meta("@who", meta)


def test_handle_resolves_via_ytdlp(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    ytdlp.source(
        "https://www.youtube.com/@demo-channel",
        {"id": "@x", "channel_id": _CHANNEL, "channel": "Anthropic"},
    )
    s = run.resolve_source(ytdlp.config(tmp_path / "m"), "@demo-channel")
    assert s.kind == "channel"
    assert s.source_id == _CHANNEL
    assert s.title == "Anthropic"
    assert ytdlp.calls() == [
        ["-J", "--flat-playlist", "-I", "0", "https://www.youtube.com/@demo-channel"]
    ]


def test_a_bare_id_needs_no_ytdlp_and_an_unknown_handle_fails_fast(
    ytdlp: FakeYtdlp, tmp_path: Path
) -> None:
    config = ytdlp.config(tmp_path / "m")
    assert run.resolve_source(config, _CHANNEL).enum_playlist == _UPLOADS
    assert ytdlp.calls() == []
    with pytest.raises(subprocess.CalledProcessError):
        run.resolve_source(config, "@nobody")


def test_clean_vtt_strips_tags_and_entities() -> None:
    out = wire.clean_vtt(_VTT)
    assert "<c>" not in out and "<00:" not in out
    assert "&nbsp;" not in out and "&#39;" not in out and "\xa0" not in out
    assert out == "The important is your code good's value"


def test_clean_vtt_dedups_consecutive_exact_only() -> None:
    vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000
hello world

00:00:02.000 --> 00:00:04.000
hello world

00:00:04.000 --> 00:00:06.000
hello world foo

00:00:06.000 --> 00:00:08.000
hello
"""
    assert wire.clean_vtt(vtt) == "hello world\nhello world foo\nhello"


def test_clean_vtt_empty() -> None:
    assert wire.clean_vtt("WEBVTT\n\n") == ""


def _argv(request: Any, program: str = "yt-dlp") -> str:
    return " ".join(wire.ytdlp_argv(program, request))


def test_ytdlp_argv_json_requests() -> None:
    flat = wire.JsonRequest("https://u", flat=True, playlist_items="1:200")
    assert _argv(flat) == "yt-dlp -J --flat-playlist -I 1:200 https://u"
    video = wire.JsonRequest("https://u", flat=False, playlist_items=None)
    assert _argv(video) == "yt-dlp -J https://u"


@pytest.mark.parametrize(
    ("kind", "flag"), [("human", "--write-subs"), ("auto", "--write-auto-subs")]
)
def test_ytdlp_argv_subs_request(kind: str, flag: str) -> None:
    request = wire.SubsRequest("https://u", kind, "en", "out/%(id)s.%(ext)s")
    assert _argv(request) == (
        f"yt-dlp --skip-download {flag} --sub-langs en,en-orig,en-US,en-GB "
        "--sub-format vtt -o out/%(id)s.%(ext)s https://u"
    )


def test_ytdlp_argv_download_and_version_requests() -> None:
    download = wire.DownloadRequest("https://u", "/m/v.mp4")
    assert _argv(download) == "yt-dlp -f bv*+ba/b --merge-output-format mp4 -o /m/v.mp4 https://u"
    assert _argv(wire.VersionRequest(), "/opt/yt-dlp") == "/opt/yt-dlp --version"


def test_ytdlp_argv_refuses_an_unknown_request() -> None:
    with pytest.raises(AssertionError):
        wire.ytdlp_argv("yt-dlp", "https://u")


def test_playlist_entries_reads_ids_and_titles() -> None:
    stdout = json.dumps({"entries": [{"id": _A, "title": "t"}, {"id": None}, {}]})
    assert wire.playlist_entries(stdout, "PL") == (
        wire.PlaylistEntry(_A, "t"),
        wire.PlaylistEntry(None, None),
        wire.PlaylistEntry(None, None),
    )
    assert wire.playlist_entries(json.dumps({"entries": []}), "PL") == ()


def test_enum_raises_on_missing_entries_key() -> None:
    with pytest.raises(ValueError, match="no 'entries'"):
        wire.playlist_entries(json.dumps({"id": "UUxxx"}), "UUxxx")


@pytest.mark.parametrize(
    "stdout",
    [
        pytest.param(json.dumps([1]), id="not-an-object"),
        pytest.param(json.dumps({"entries": "x"}), id="entries-not-a-list"),
        pytest.param(json.dumps({"entries": [{"id": 7}]}), id="id-not-text"),
        pytest.param(json.dumps({"entries": ["x"]}), id="entry-not-an-object"),
    ],
)
def test_playlist_entries_refuses_a_malformed_listing(stdout: str) -> None:
    with pytest.raises(TypeError):
        wire.playlist_entries(stdout, "UUxxx")


def test_source_meta_reads_the_resolved_object() -> None:
    meta = wire.source_meta(json.dumps({"id": "UCx", "channel_id": "UCy", "channel": "C"}))
    assert meta == wire.SourceMeta(id="UCx", channel_id="UCy", channel="C", title=None)
    assert wire.source_meta("{}").id == ""


def test_video_meta_reads_the_fields() -> None:
    meta = wire.video_meta(json.dumps(_VIDEO_META))
    assert meta.title == "A Title"
    assert meta.channel == "Chan" and meta.uploader == "Upl" and meta.uploader_id == "@handle"
    assert meta.timestamp == 1753986150 and meta.duration == 123
    assert meta.tags == ("a", "b")
    assert (meta.view_count, meta.like_count, meta.comment_count) == (10, 2, 1)


def test_video_meta_defaults_to_nothing() -> None:
    meta = wire.video_meta(json.dumps({"title": "T", "channel": "C", "tags": None}))
    assert meta.description is None and meta.timestamp is None and meta.view_count is None
    assert meta.tags == ()


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param({"title": 5}, id="title-not-text"),
        pytest.param({"duration": "12"}, id="duration-not-a-number"),
        pytest.param({"view_count": True}, id="count-is-a-bool"),
        pytest.param({"tags": "a,b"}, id="tags-not-a-list"),
        pytest.param({"tags": ["a", 3]}, id="tag-not-text"),
    ],
)
def test_video_meta_refuses_a_field_of_the_wrong_type(doc: dict[str, Any]) -> None:
    with pytest.raises(TypeError):
        wire.video_meta(json.dumps(doc))


def test_whisper_text_reads_the_cli_output() -> None:
    assert wire.whisper_text(json.dumps({"text": "hello"})) == "hello"
    assert wire.whisper_text(json.dumps({"text": ""})) is None
    with pytest.raises(ValueError, match="no 'text'"):
        wire.whisper_text("{}")
    with pytest.raises(TypeError):
        wire.whisper_text(json.dumps({"text": 5}))


def test_enum_stops_at_watermark() -> None:
    walk = plan.enum_items(_entries(_A, _B, _C, _D), frozenset({_C}), None)
    assert [i.video_id for i in walk.items] == [_A, _B]
    assert walk.hit_watermark is True


def test_enum_limit_and_skips_bad_ids() -> None:
    walk = plan.enum_items(_entries(_A, "tooShort", _B, _C), frozenset(), 2)
    assert [i.video_id for i in walk.items] == [_A, _B]
    assert walk.hit_watermark is False


def test_enum_items_carry_the_permalink_title_and_position() -> None:
    walk = plan.enum_items(_entries(None, "tooShort", _B), frozenset(), None)
    assert walk.items == (plan.FeedItem(_B, _watch(_B), "t-" + _B, 3),)


def test_enum_limit_zero_takes_nothing() -> None:
    walk = plan.enum_items(_entries(_A, _B), frozenset(), 0)
    assert walk.items == () and walk.hit_watermark is False


def test_enum_no_watermark_when_none_mirrored() -> None:
    walk = plan.enum_items(_entries(_A, _B, _C), frozenset(), None)
    assert [i.video_id for i in walk.items] == [_A, _B, _C]
    assert walk.hit_watermark is False


def test_plan_sync_incremental_fetches_all_new() -> None:
    result = plan.plan_sync(
        _entries("newest12345", "older123456", _D), frozenset({_D}), backfill=False, initial_limit=1
    )
    assert [i.video_id for i in result.to_fetch] == ["newest12345", "older123456"]
    assert result.skipped_ids == ()


def test_plan_sync_first_run_caps_to_initial_limit() -> None:
    result = plan.plan_sync(_entries(_A, _B, _C), frozenset(), backfill=False, initial_limit=2)
    assert [i.video_id for i in result.enumerated] == [_A, _B]
    assert [i.video_id for i in result.to_fetch] == [_A, _B]


def test_plan_sync_backfill_skips_mirrored_no_cap() -> None:
    result = plan.plan_sync(_entries(_A, _B, _C), frozenset({_B}), backfill=True, initial_limit=1)
    assert [i.video_id for i in result.to_fetch] == [_A, _C]
    assert result.skipped_ids == (_B,)
    assert [i.video_id for i in result.enumerated] == [_A, _B, _C]


def _serve_channel(ytdlp: FakeYtdlp, ids: list[str]) -> None:
    ytdlp.listing(_UPLOADS, ids)
    for vid in ids:
        ytdlp.video(vid, human=_VTT)


def _mirror(root: Path, video_id: str) -> None:
    (root / video_id).mkdir(parents=True)
    (root / video_id / "post.json").write_text("{}")


def test_sync_incremental_fetches_all_new(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    ids = ["newest12345", "older123456", _D]
    _serve_channel(ytdlp, ids)
    root = tmp_path / "mirror"
    _mirror(root, _D)
    r = run.sync(ytdlp.config(root), _CHANNEL, run.SyncOptions())
    assert [i["source_id"] for i in r.new_items] == ["newest12345", "older123456"]
    assert r.skipped_ids == ()
    assert r.source.enum_playlist == _UPLOADS
    assert (root / "newest12345" / "post.json").exists()


def test_sync_first_run_caps_to_initial_limit(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    ids = ["v1234567890", "v2234567890", "v3234567890"]
    _serve_channel(ytdlp, ids)
    r = run.sync(ytdlp.config(tmp_path / "mirror"), _CHANNEL, run.SyncOptions(initial_limit=2))
    assert [i["source_id"] for i in r.new_items] == ["v1234567890", "v2234567890"]


def test_sync_backfill_skips_mirrored_no_cap(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    ids = ["a1234567890", "b1234567890", "c1234567890"]
    ytdlp.listing(_PLAYLIST, ids)
    for vid in ids:
        ytdlp.video(vid)
    root = tmp_path / "mirror"
    _mirror(root, "b1234567890")
    r = run.sync(ytdlp.config(root), _PLAYLIST, run.SyncOptions(backfill=True, initial_limit=1))
    assert [i["source_id"] for i in r.new_items] == ["a1234567890", "c1234567890"]
    assert r.skipped_ids == ("b1234567890",)


def test_sync_preview_no_fetch(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    _serve_channel(ytdlp, [_A, _B, _C])
    root = tmp_path / "mirror"
    r = run.sync(ytdlp.config(root), _CHANNEL, run.SyncOptions(do_fetch=False, max_scan=2))
    assert [i.video_id for i in r.enumerated] == [_A, _B]
    assert r.new_items == () and not root.exists()
    listing = next(c for c in ytdlp.calls() if "--flat-playlist" in c)
    assert listing[listing.index("-I") + 1] == "1:2"
    assert all("--version" not in call for call in ytdlp.calls())


def test_sync_asks_for_the_version_once_per_run(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    _serve_channel(ytdlp, [_A, _B, _C])
    r = run.sync(ytdlp.config(tmp_path / "m"), _CHANNEL, run.SyncOptions())
    assert len(r.new_items) == 3
    assert sum(call == ["--version"] for call in ytdlp.calls()) == 1
    assert {i["fetched_via"] for i in r.new_items} == {"youtube-feed (yt-dlp 2099.01.01)"}


def test_sync_with_nothing_new_does_not_ask_for_the_version(
    ytdlp: FakeYtdlp, tmp_path: Path
) -> None:
    _serve_channel(ytdlp, [_A])
    root = tmp_path / "mirror"
    _mirror(root, _A)
    r = run.sync(ytdlp.config(root), _CHANNEL, run.SyncOptions())
    assert r.new_items == ()
    assert ["--version"] not in ytdlp.calls()


def test_sync_items_point_into_the_configured_mirror(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    _serve_channel(ytdlp, [_A])
    root = tmp_path / "mirror"
    options = run.SyncOptions(matrix_entity="quantbit", with_video=True)
    item = run.sync(ytdlp.config(root), _CHANNEL, options).new_items[0]
    assert item["raw_path"] == str(root / _A) + "/"
    assert item["matrix_entity"] == "quantbit"
    assert item["media"] == [{"kind": "video", "path": str(root / _A / "video.mp4")}]


def test_sync_of_an_unlisted_playlist_fails_fast(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        run.sync(ytdlp.config(tmp_path / "m"), _CHANNEL, run.SyncOptions())


def test_fetch_writes_the_mirror(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    ytdlp.video(_VIDEO, human=_VTT)
    root = tmp_path / "mirror"
    post = run.fetch(ytdlp.config(root), _VIDEO, run.FetchOptions())
    outdir = root / _VIDEO
    assert json.loads((outdir / "post.json").read_text()) == post
    assert (outdir / "transcript.txt").read_text() == "The important is your code good's value"
    markdown = (outdir / "post.md").read_text()
    assert markdown == post_mod.render_markdown(post)
    assert "- **transcript**: human" in markdown and "## Transcript" in markdown
    assert post["transcript_kind"] == "human"
    assert post["fetched_via"] == "youtube-feed (yt-dlp 2099.01.01)"
    assert post["channel"]["handle"] == "@handle"
    assert post["published_at"] == "2025-07-31T18:22:30Z"
    assert list(post) == _POST_KEYS


def test_fetch_prefers_author_subtitles_over_auto_captions(
    ytdlp: FakeYtdlp, tmp_path: Path
) -> None:
    ytdlp.video(_VIDEO, human="WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nauthor\n", auto=_VTT)
    post = run.fetch(ytdlp.config(tmp_path / "m"), _VIDEO, run.FetchOptions())
    assert (post["transcript"], post["transcript_kind"]) == ("author", "human")


def test_fetch_falls_back_to_auto_captions(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    ytdlp.video(_VIDEO, auto=_VTT)
    post = run.fetch(ytdlp.config(tmp_path / "m"), _VIDEO, run.FetchOptions())
    assert post["transcript_kind"] == "auto"
    subs_calls = [c for c in ytdlp.calls() if "--skip-download" in c]
    assert ["--write-subs" in c for c in subs_calls] == [True, False]
    assert "en,en-orig,en-US,en-GB" in subs_calls[0]


def test_fetch_without_captions_is_not_an_error(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    ytdlp.video(_VIDEO)
    root = tmp_path / "m"
    post = run.fetch(ytdlp.config(root), _VIDEO, run.FetchOptions())
    assert post["transcript"] is None and post["transcript_kind"] is None
    assert ytdlp.transcriptions() == []
    assert not (root / _VIDEO / "transcript.txt").exists()
    assert "- **transcript**: none" in (root / _VIDEO / "post.md").read_text()


def test_fetch_without_a_wanted_transcript_asks_for_no_captions(
    ytdlp: FakeYtdlp, tmp_path: Path
) -> None:
    ytdlp.video(_VIDEO, human=_VTT)
    post = run.fetch(ytdlp.config(tmp_path / "m"), _VIDEO, run.FetchOptions(want_transcript=False))
    assert post["transcript"] is None
    assert not [c for c in ytdlp.calls() if "--skip-download" in c]


def test_fetch_asks_for_the_preferred_caption_language(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    ytdlp.video(_VIDEO)
    run.fetch(ytdlp.config(tmp_path / "m"), _VIDEO, run.FetchOptions(prefer_lang="zh"))
    subs_call = next(c for c in ytdlp.calls() if "--skip-download" in c)
    assert subs_call[subs_call.index("--sub-langs") + 1] == "zh,zh-orig,zh-US,zh-GB"


def test_fetch_whisper_fallback_fills_caption_gap(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    ytdlp.video(_VIDEO)
    root = tmp_path / "m"
    post = run.fetch(ytdlp.config(root), _VIDEO, run.FetchOptions(whisper_fallback=True))
    assert post["transcript"] == "whispered text"
    assert post["transcript_kind"] == "whisper"
    assert (root / _VIDEO / "transcript.txt").read_text() == "whispered text"
    assert ytdlp.transcriptions() == [[_VIDEO, "--json"]]


@pytest.mark.parametrize(
    ("captions", "whisper", "kind"),
    [(None, False, None), ({"human": _VTT}, True, "human")],
    ids=["disabled", "captions-exist"],
)
def test_fetch_does_not_transcribe_without_a_gap_or_the_opt_in(
    ytdlp: FakeYtdlp,
    tmp_path: Path,
    captions: dict[str, str] | None,
    whisper: bool,
    kind: str | None,
) -> None:
    ytdlp.video(_VIDEO, **(captions or {}))
    options = run.FetchOptions(whisper_fallback=whisper)
    post = run.fetch(ytdlp.config(tmp_path / "m"), _VIDEO, options)
    assert post["transcript_kind"] == kind
    assert ytdlp.transcriptions() == []


def test_fetch_whisper_empty_output_leaves_the_gap(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    ytdlp.video(_VIDEO)
    config = ytdlp.config(tmp_path / "m", Transcription(text=""))
    post = run.fetch(config, _VIDEO, run.FetchOptions(whisper_fallback=True))
    assert post["transcript"] is None and post["transcript_kind"] is None


def test_fetch_whisper_failure_propagates(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    ytdlp.video(_VIDEO)
    root = tmp_path / "m"
    config = ytdlp.config(root, Transcription(exit_code=3, stderr="OPENAI_API_KEY is not set"))
    with pytest.raises(subprocess.CalledProcessError) as caught:
        run.fetch(config, _VIDEO, run.FetchOptions(whisper_fallback=True))
    assert "OPENAI_API_KEY" in caught.value.stderr
    assert not root.exists()


def test_fetch_with_video_records_its_path(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    ytdlp.video(_VIDEO)
    root = tmp_path / "m"
    post = run.fetch(ytdlp.config(root), _VIDEO, run.FetchOptions(with_video=True))
    video = root / _VIDEO / "video.mp4"
    assert video.read_bytes() == b"VIDEO-BYTES"
    assert post["video_path"] == str(video)
    assert json.loads((root / _VIDEO / "post.json").read_text())["video_path"] == str(video)
    s1 = post_mod.to_s1(
        post, matrix_entity=None, raw_path=run.raw_path_of(ytdlp.config(root), _VIDEO)
    )
    assert s1["media"] == [{"kind": "video", "path": str(video)}]


def test_fetch_rejects_a_bad_video_id_before_running_ytdlp(
    ytdlp: FakeYtdlp, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="not a YouTube video id"):
        run.fetch(ytdlp.config(tmp_path / "m"), "../../etc", run.FetchOptions())
    assert ytdlp.calls() == []


def test_fetch_fails_fast_when_metadata_cannot_be_read(ytdlp: FakeYtdlp, tmp_path: Path) -> None:
    root = tmp_path / "m"
    with pytest.raises(subprocess.CalledProcessError) as caught:
        run.fetch(ytdlp.config(root), _VIDEO, run.FetchOptions())
    assert "Unsupported URL" in caught.value.stderr
    assert not root.exists()


def test_save_refuses_a_bad_video_id(tmp_path: Path) -> None:
    post = {"video_id": "../x", "transcript": None}
    with pytest.raises(ValueError, match="not a YouTube video id"):
        run.save(post, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_to_s1_shape() -> None:
    channel = {"id": _CHANNEL, "name": "\u793a\u4f8b\u9891\u9053"}
    post = {"video_id": _VIDEO, "url": _watch(_VIDEO), "title": "T", "description": "D"}
    post |= {"channel": channel, "transcript": "hello", "stats": {"views": 1}}
    s1 = post_mod.to_s1(post, matrix_entity="quantbit", raw_path=f"/mirror/{_VIDEO}/")
    assert (s1["platform"], s1["source_id"], s1["matrix_entity"]) == ("youtube", _VIDEO, "quantbit")
    assert s1["author"] == channel
    assert s1["text"] == "D" and s1["transcript"] == "hello" and s1["media"] == []
    assert s1["raw_path"] == f"/mirror/{_VIDEO}/" and s1["stats"] == {"views": 1}


def test_build_post_without_a_channel_name_uses_the_uploader() -> None:
    meta = wire.video_meta(json.dumps({"title": "T", "uploader": "Upl"}))
    post = post_mod.build_post(_VIDEO, meta, None, fetched_at="t", version="1")
    assert post["channel"]["name"] == "Upl"
    assert post["tags"] == [] and post["stats"] == {"views": None, "likes": None, "comments": None}


def test_require_video_id() -> None:
    assert post_mod.require_video_id(_VIDEO) == _VIDEO
    for bad in ("short", "has/slash___", "way-too-long-for-a-video-id"):
        with pytest.raises(ValueError, match="not a YouTube video id"):
            post_mod.require_video_id(bad)


def test_the_process_config_names_the_homes_mirror_and_the_transcription_cli(
    unit_home: Path,
) -> None:
    config = feed._process_config()
    assert config.root == unit_home / "state" / "mirrors" / "youtube"
    assert config.ytdlp == "yt-dlp"
    assert config.transcribe[0] == sys.executable
    assert Path(config.transcribe[1]).name == "transcribe.py"
    assert Path(config.transcribe[1]).is_file()


def test_to_s1_points_raw_path_into_the_homes_mirror(unit_home: Path) -> None:
    post: dict[str, Any] = {"video_id": _VIDEO, "url": _watch(_VIDEO), "channel": {}, "stats": {}}
    s1 = feed.to_s1(post, matrix_entity="m")
    assert s1["raw_path"] == str(unit_home / "state" / "mirrors" / "youtube" / _VIDEO) + "/"
    assert s1["matrix_entity"] == "m"


def test_resolving_a_bare_id_through_the_entry_needs_no_ytdlp(unit_home: Path) -> None:
    assert feed.resolve_source(_UPLOADS).source_id == _CHANNEL


# meiva:note
# The module is started as a script, with PYTHONSAFEPATH=1 keeping the cwd and the script's own
# directory off sys.path: it defines functions and exits, so a zero exit means its sibling
# modules load through the sys.path guard the module carries.
def test_the_entry_loads_with_its_own_directory_off_the_path() -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(_FEED_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONSAFEPATH": "1", "PATH": ""},
    )
    assert done.returncode == 0, done.stderr
