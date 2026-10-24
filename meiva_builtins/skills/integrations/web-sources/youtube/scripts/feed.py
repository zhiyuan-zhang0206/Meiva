# meiva:note
# YouTube feed-discovery adapter of the `web-sources` family — the entry an agent imports.
#
# The agent adds this skill's `scripts/` dir to `sys.path`, `import feed`, and calls
# `feed.sync(spec)` (follow a channel, or `backfill=True` for a series task), `feed.fetch(id)` and
# `feed.resolve_source(spec)`; see `youtube/SKILL.md`. Each entry reads the process once (the
# home's mirror dir, the `yt-dlp` on PATH, this interpreter) into a `YoutubeConfig` and runs the
# implementation in `yt_run.py` with it; that and the sibling pure modules carry the logic. Both
# stages — enumerate what is new, fetch metadata + transcript into
# `$MEIVA_HOME/state/mirrors/youtube/<id>/` — are login-free `yt-dlp` calls.
# meiva:link meiva_builtins/skills/integrations/web-sources/youtube/SKILL.md

from __future__ import annotations

import sys
from pathlib import Path

from base.paths.home import resolve_meiva_home

# meiva:note
# `yt_run` and the pure modules are siblings in this skill's own scripts/ dir, not an importable
# package (PYTHONSAFEPATH=1 keeps a script's own directory off sys.path). Structure Rule 6
# recognizes this exact __file__-derived, within-skill shape as the endorsed guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yt_run
from yt_plan import Source
from yt_post import Post, S1Item
from yt_post import to_s1 as project_to_s1


# meiva:note
# The configuration of this process: the mirror dir under this process's home, the `yt-dlp` on
# PATH, and this interpreter running the sibling `audio-transcribe` skill's CLI. That skill is
# separate, so its CLI runs as a subprocess and neither skill directory reaches into the other's
# files.
def _process_config() -> yt_run.YoutubeConfig:
    script = Path(__file__).resolve().parents[3] / "audio-transcribe" / "scripts" / "transcribe.py"
    return yt_run.YoutubeConfig(
        ytdlp="yt-dlp",
        transcribe=(sys.executable, str(script)),
        root=resolve_meiva_home() / "state" / "mirrors" / "youtube",
    )


# meiva:note
# Resolve a user-facing spec to a `Source`: a channel id (`UC...`), an uploads playlist
# (`UU...`), any playlist id (`PL.../FL.../...`), an `@handle` or a full channel/playlist URL.
# Handles and URLs cost one `yt-dlp` metadata call to resolve.
def resolve_source(spec: str) -> Source:
    return yt_run.resolve_source(_process_config(), spec)


# meiva:note
# Resolve `spec`, enumerate what is not yet in the raw mirror, fetch each. The mirror is the
# watermark: a follow walks the uploads playlist from the top and stops at the first video
# already mirrored (a first follow is capped to `initial_limit`); `backfill=True` is the series
# task mode, taking every video up to `max_scan` and only skipping the mirrored ones.
# `do_fetch=False` is a non-mutating preview. Returns a `SyncResult`.
def sync(
    spec: str,
    *,
    backfill: bool = False,
    matrix_entity: str | None = None,
    initial_limit: int = 10,
    max_scan: int = 200,
    do_fetch: bool = True,
    with_video: bool = False,
) -> yt_run.SyncResult:
    options = yt_run.SyncOptions(
        backfill=backfill,
        matrix_entity=matrix_entity,
        initial_limit=initial_limit,
        max_scan=max_scan,
        do_fetch=do_fetch,
        with_video=with_video,
    )
    return yt_run.sync(_process_config(), spec, options)


# meiva:note
# Fetch one video into the raw mirror and return its post: metadata, description and (best-effort)
# transcript, plus the video file when `with_video`. `whisper_fallback=True` (opt-in; needs
# `OPENAI_API_KEY` + ffmpeg) routes a caption-less video through the `audio-transcribe` skill; a
# real transcription failure propagates. `prefer_lang` is the caption language asked for first.
def fetch(
    video_id: str,
    *,
    with_video: bool = False,
    want_transcript: bool = True,
    whisper_fallback: bool = False,
    prefer_lang: str = "en",
) -> Post:
    options = yt_run.FetchOptions(
        with_video=with_video,
        want_transcript=want_transcript,
        whisper_fallback=whisper_fallback,
        prefer_lang=prefer_lang,
    )
    return yt_run.fetch(_process_config(), video_id, options)


# meiva:note
# Project a fetched post onto the cross-platform S1 item schema.
def to_s1(post: Post, *, matrix_entity: str | None = None) -> S1Item:
    raw_path = yt_run.raw_path_of(_process_config(), post["video_id"])
    return project_to_s1(post, matrix_entity=matrix_entity, raw_path=raw_path)
