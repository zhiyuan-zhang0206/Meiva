# meiva:note
# YouTube feed-discovery adapter of the `web-sources` family: the value-taking implementation.
#
# Two stages, both driven by `yt-dlp` and both login-free:
#
#   enum   — given a *source* (a followed channel, or a playlist for a "series task"), list its
#            videos newest-first and return the ones not yet in the raw mirror. A channel
#            enumerates through its "uploads" playlist (`UC...` -> `UU...`, 2nd char flipped).
#   fetch  — given a video id, pull metadata + description + transcript (and optionally the
#            video file) into the raw mirror at `<root>/<id>/`, mirroring the `*-fetch` skills.
#
# `sync()` ties them together: resolve -> enum not-yet-mirrored -> fetch each -> return the new
# items projected to the S1 schema shared by every `web-sources` adapter (see
# `web-sources/SKILL.md`).
#
# Two source modes, matching how the user consumes YouTube:
#   - **follow** (a channel): steady-state incremental. The uploads playlist is
#     reverse-chronological, so "new" = walk from the top until the first video already in the
#     mirror. First run is capped to `initial_limit` so we don't backfill years of history.
#   - **series task** (`backfill=True`, typically a playlist like a conference): a bounded
#     one-shot — take every video (up to `max_scan`), no watermark stop.
#
# Login-free, so this runs anywhere (not Mac-mini-bound). Pure stdlib + `yt-dlp` on PATH; no
# `chrome` MCP, no `meiva` SDK.
#
# This file is the shell: it runs yt-dlp and the transcription CLI, reads and writes the mirror
# and reads the clock. What is asked of yt-dlp and how its answers are read are values in
# `yt_wire.py`, which sources and videos a run takes are `yt_plan.py`, and what a fetched video
# becomes is `yt_post.py`. Where yt-dlp, the transcription CLI and the mirror are is a
# `YoutubeConfig` the caller supplies; `feed.py` builds it from the process for agents.
# meiva:link meiva_builtins/skills/integrations/web-sources/docs/web-sources.meiva.okf.md

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

# meiva:note
# The pure modules are siblings in this skill's own scripts/ dir, not an importable package
# (PYTHONSAFEPATH=1 keeps a script's own directory off sys.path). Structure Rule 6 recognizes
# this exact __file__-derived, within-skill shape as the endorsed guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from yt_plan import (
    FeedItem,
    Lookup,
    Source,
    is_video_id,
    plan_source,
    plan_sync,
    source_from_meta,
    video_url,
)
from yt_post import Post, S1Item, Transcript, build_post, render_markdown, require_video_id, to_s1
from yt_wire import (
    SUB_KINDS,
    DownloadRequest,
    JsonRequest,
    SubsRequest,
    VersionRequest,
    YtDlpRequest,
    clean_vtt,
    playlist_entries,
    source_meta,
    video_meta,
    whisper_text,
    ytdlp_argv,
)


# meiva:note
# Where the adapter's effects land: the yt-dlp program, the argv prefix that runs the
# `audio-transcribe` skill's CLI (a separate skill, run as a subprocess so the two skill
# directories stay independently deployable) and the raw-mirror dir `<home>/state/mirrors/youtube`
# — shared derived state doubling as the sync watermark. The per-content subdir is the stable
# video id, so a re-run reuses it (dedup).
@dataclass(frozen=True)
class YoutubeConfig:
    ytdlp: str
    transcribe: tuple[str, ...]
    root: Path


# meiva:note
# What `fetch` pulls: the video file as well (`with_video`), the transcript at all
# (`want_transcript`), and the audio-transcribe fallback for a video with no captions
# (`whisper_fallback`; needs `OPENAI_API_KEY` + ffmpeg). `prefer_lang` is the caption language
# to ask yt-dlp for first.
@dataclass(frozen=True)
class FetchOptions:
    with_video: bool = False
    want_transcript: bool = True
    whisper_fallback: bool = False
    prefer_lang: str = "en"


# meiva:note
# What `sync` does. `backfill` is the "series task" mode: every video up to `max_scan`, no early
# stop, only skipping the ones already mirrored. Otherwise it is a follow, whose first run (or a
# gap larger than the window) is capped to `initial_limit`. `do_fetch=False` is a non-mutating
# preview (enumerate only). `matrix_entity` tags the S1 items.
@dataclass(frozen=True)
class SyncOptions:
    backfill: bool = False
    matrix_entity: str | None = None
    initial_limit: int = 10
    max_scan: int = 200
    do_fetch: bool = True
    with_video: bool = False


# meiva:note
# The outcome of a sync: the source, the new S1 items, the items enumerated (the preview when
# nothing was fetched) and the ids skipped because they were already in the mirror.
@dataclass(frozen=True)
class SyncResult:
    source: Source
    new_items: list[S1Item]
    enumerated: list[FeedItem]
    skipped_ids: list[str]


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat().replace("+00:00", "Z")


# meiva:note
# Run a yt-dlp request and return what it printed. Metadata fetches fail fast (`check=True`).
def _ytdlp_stdout(config: YoutubeConfig, request: YtDlpRequest) -> str:
    done = subprocess.run(
        ytdlp_argv(config.ytdlp, request), capture_output=True, text=True, check=True
    )
    return done.stdout


# meiva:note
# The yt-dlp version, recorded in `fetched_via`.
def ytdlp_version(config: YoutubeConfig) -> str:
    return _ytdlp_stdout(config, VersionRequest()).strip()


# meiva:note
# Resolve a user-facing spec to a :class:`Source`. Accepts a channel id (`UC...`), an uploads
# playlist (`UU...`), any playlist id (`PL.../FL.../...`), an `@handle`, or a full channel/playlist
# URL. Handles and URLs cost one `yt-dlp` metadata call to resolve; bare ids are resolved
# offline.
def resolve_source(config: YoutubeConfig, spec: str) -> Source:
    planned = plan_source(spec)
    match planned:
        case Source():
            return planned
        case Lookup(url=url):
            request = JsonRequest(url, flat=True, playlist_items="0")
            return source_from_meta(spec, source_meta(_ytdlp_stdout(config, request)))
        case _:
            assert_never(planned)


# meiva:note
# Persist a fetched post to `<root>/<video_id>/`.
def save(post: Post, root: Path) -> Path:
    outdir = root / require_video_id(post["video_id"])
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2))
    (outdir / "post.md").write_text(render_markdown(post))
    transcript = post["transcript"]
    if transcript:
        (outdir / "transcript.txt").write_text(transcript)
    return outdir


# meiva:note
# The transcript of a video, or None when it has no captions. Prefers author-provided subs
# (cleaner), falls back to ASR auto-captions (near-universal). yt-dlp writes the VTT into a
# scratch dir; it is cleaned to prose.
def fetch_transcript(config: YoutubeConfig, video_id: str, prefer_lang: str) -> Transcript | None:
    for kind in SUB_KINDS:
        with tempfile.TemporaryDirectory() as tmp:
            template = str(Path(tmp) / "%(id)s.%(ext)s")
            request = SubsRequest(video_url(video_id), kind, prefer_lang, template)
            subprocess.run(
                ytdlp_argv(config.ytdlp, request), capture_output=True, text=True, check=False
            )
            vtts = sorted(Path(tmp).glob(f"{video_id}*.vtt"))
            if vtts:
                text = clean_vtt(vtts[0].read_text(encoding="utf-8", errors="replace"))
                if text:
                    return Transcript(text, kind)
    return None


# meiva:note
# Transcribe a caption-less video's audio via the sibling `audio-transcribe` skill (OpenAI).
# Opt-in through `FetchOptions.whisper_fallback`. A real failure (missing key / ffmpeg / network)
# propagates — only empty output yields None, so this never silently masks a transcription
# error.
def whisper_transcript(config: YoutubeConfig, video_id: str) -> str | None:
    done = subprocess.run(
        [*config.transcribe, video_id, "--json"], capture_output=True, text=True, check=True
    )
    return whisper_text(done.stdout)


# meiva:note
# Fetch one video into the raw mirror `<root>/<id>/`, recording `version` as the yt-dlp version.
#
# Writes `post.json` (+ `post.md`, `transcript.txt`, and `video.mp4` when `with_video`). Returns
# the curated post. Metadata + transcript only by default — the digest does not need the video
# bytes.
#
# Transcript is **best-effort**: many videos legitimately have no captions, so a missing
# transcript is `None` with `transcript_kind=None`, not an error. Metadata fetch, by contrast,
# fails fast (`check=True`). `whisper_fallback` routes a caption-less video through the
# `audio-transcribe` skill, setting `transcript_kind="whisper"`; a real transcription failure
# propagates, only "video genuinely has no captions" yields None.
def _fetch_with_version(
    config: YoutubeConfig, video_id: str, options: FetchOptions, version: str
) -> Post:
    require_video_id(video_id)
    request = JsonRequest(video_url(video_id), flat=False, playlist_items=None)
    meta = video_meta(_ytdlp_stdout(config, request))
    transcript: Transcript | None = None
    if options.want_transcript:
        transcript = fetch_transcript(config, video_id, options.prefer_lang)
        if transcript is None and options.whisper_fallback:
            whispered = whisper_transcript(config, video_id)
            transcript = Transcript(whispered, "whisper") if whispered else None
    post = build_post(video_id, meta, transcript, fetched_at=_now_iso(), version=version)

    if options.with_video:
        outdir = config.root / video_id
        outdir.mkdir(parents=True, exist_ok=True)
        dest = outdir / "video.mp4"
        _ytdlp_stdout(config, DownloadRequest(video_url(video_id), str(dest)))
        post["video_path"] = str(dest)
    save(post, config.root)
    return post


# meiva:note
# Fetch one video into the raw mirror (see `_fetch_with_version`), asking yt-dlp for its version.
def fetch(config: YoutubeConfig, video_id: str, options: FetchOptions) -> Post:
    require_video_id(video_id)
    return _fetch_with_version(config, video_id, options, ytdlp_version(config))


# meiva:note
# The `raw_path` of the post for a video: its mirror directory, with a trailing slash.
def raw_path_of(config: YoutubeConfig, video_id: str) -> str:
    return str(config.root / video_id) + "/"


# meiva:note
# Resolve `spec`, enumerate what's not yet mirrored, fetch each.
#
# The raw mirror is the watermark: the walk goes down the uploads playlist from the top and
# stops at the first already-mirrored video. See `SyncOptions` for the follow, backfill and
# preview modes.
def sync(config: YoutubeConfig, spec: str, options: SyncOptions) -> SyncResult:
    source = resolve_source(config, spec)
    listing = _ytdlp_stdout(
        config,
        JsonRequest(
            f"https://www.youtube.com/playlist?list={source.enum_playlist}",
            flat=True,
            playlist_items=f"1:{options.max_scan}",
        ),
    )
    entries = playlist_entries(listing, source.enum_playlist)
    mirrored = frozenset(
        e.id
        for e in entries
        if e.id and is_video_id(e.id) and (config.root / e.id / "post.json").exists()
    )
    plan = plan_sync(
        entries, mirrored, backfill=options.backfill, initial_limit=options.initial_limit
    )
    if not options.do_fetch:
        return SyncResult(source, [], list(plan.enumerated), [])

    fetch_options = FetchOptions(with_video=options.with_video)
    new_items: list[S1Item] = []
    if plan.to_fetch:
        version = ytdlp_version(config)
        for item in plan.to_fetch:
            post = _fetch_with_version(config, item.video_id, fetch_options, version)
            new_items.append(
                to_s1(
                    post,
                    matrix_entity=options.matrix_entity,
                    raw_path=raw_path_of(config, item.video_id),
                )
            )
    return SyncResult(source, new_items, list(plan.enumerated), list(plan.skipped_ids))
