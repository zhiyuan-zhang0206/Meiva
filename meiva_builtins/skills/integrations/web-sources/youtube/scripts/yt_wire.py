# meiva:note
# What the youtube adapter asks of yt-dlp and the transcription CLI, and how it reads the
# answers, as plain values: the argv of each yt-dlp call, the playlist entries, source facts and
# video metadata a `-J` answer carries, the prose a WebVTT caption file holds, and the text the
# transcription CLI prints. Nothing here starts a process or touches a file; `yt_run.py` runs the
# argv and hands the output back. A field of the wrong type is refused rather than written into
# the mirror.
# meiva:link meiva_builtins/skills/integrations/web-sources/docs/web-sources.meiva.okf.md

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Literal, TypeGuard, assert_never

SubKind = Literal["human", "auto"]

# meiva:note
# The caption kinds in the order they are tried: author-provided subtitles first (cleaner), then
# ASR auto-captions (near-universal).
SUB_KINDS: tuple[SubKind, ...] = ("human", "auto")

# meiva:note
# Inline timing tags in a caption line: `<00:00:00.160><c> ... </c>`.
_RE_VTT_TAG = re.compile(r"<[^>]+>")


# meiva:note
# `yt-dlp -J` for `url`: the single JSON object it prints. `flat` adds `--flat-playlist` (ids and
# titles only, no per-video probe — fast for enumeration). `playlist_items` slices via `-I`
# (e.g. "1:200" to bound a walk, "0" for metadata only).
@dataclass(frozen=True)
class JsonRequest:
    url: str
    flat: bool
    playlist_items: str | None


# meiva:note
# Write the `kind` captions of `url` in `prefer_lang` (and its `-orig`, `-US`, `-GB` variants) as
# WebVTT to `out_template`, without downloading the video.
@dataclass(frozen=True)
class SubsRequest:
    url: str
    kind: SubKind
    prefer_lang: str
    out_template: str


# meiva:note
# Download the video file of `url` to `dest` (opt-in; the digest rarely needs it).
@dataclass(frozen=True)
class DownloadRequest:
    url: str
    dest: str


# meiva:note
# Print the yt-dlp version.
@dataclass(frozen=True)
class VersionRequest:
    pass


YtDlpRequest = JsonRequest | SubsRequest | DownloadRequest | VersionRequest


# meiva:note
# One playlist entry in a flat listing: its video id and title.
@dataclass(frozen=True)
class PlaylistEntry:
    id: str | None
    title: str | None


# meiva:note
# What a channel, handle or playlist URL resolves to: its id (`""` when the object names none),
# the channel id and name it carries, and its title.
@dataclass(frozen=True)
class SourceMeta:
    id: str
    channel_id: str | None
    channel: str | None
    title: str | None


# meiva:note
# The metadata of one video, as yt-dlp reports it. `timestamp` is unix seconds; the counters are
# None when yt-dlp does not know them.
@dataclass(frozen=True)
class VideoMeta:
    title: str | None
    description: str | None
    channel_id: str | None
    channel: str | None
    uploader: str | None
    uploader_id: str | None
    channel_url: str | None
    timestamp: int | float | None
    duration: int | float | None
    tags: tuple[str, ...]
    thumbnail: str | None
    view_count: int | float | None
    like_count: int | float | None
    comment_count: int | float | None


# meiva:note
# The argv that runs `request` with the yt-dlp program `program`.
def ytdlp_argv(program: str, request: YtDlpRequest) -> tuple[str, ...]:
    match request:
        case JsonRequest(url=url, flat=flat, playlist_items=items):
            flat_arg = ("--flat-playlist",) if flat else ()
            slice_args = ("-I", items) if items is not None else ()
            return (program, "-J", *flat_arg, *slice_args, url)
        case SubsRequest(url=url, kind=kind, prefer_lang=lang, out_template=template):
            langs = f"{lang},{lang}-orig,{lang}-US,{lang}-GB"
            return (
                program,
                "--skip-download",
                _subs_flag(kind),
                "--sub-langs",
                langs,
                "--sub-format",
                "vtt",
                "-o",
                template,
                url,
            )
        case DownloadRequest(url=url, dest=dest):
            return (program, "-f", "bv*+ba/b", "--merge-output-format", "mp4", "-o", dest, url)
        case VersionRequest():
            return (program, "--version")
        case _:
            assert_never(request)


def _subs_flag(kind: SubKind) -> str:
    match kind:
        case "human":
            return "--write-subs"
        case "auto":
            return "--write-auto-subs"
        case _:
            assert_never(kind)


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _as_object(value: object, what: str) -> dict[str, object]:
    if not _is_object_dict(value):
        raise TypeError(f"{what}: expected a JSON object, got {type(value).__name__}")
    return {str(k): v for k, v in value.items()}


def _load_object(stdout: str, what: str) -> dict[str, object]:
    return _as_object(json.loads(stdout), what)


def _text(obj: dict[str, object], key: str) -> str | None:
    value = obj.get(key)
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"yt-dlp field {key!r} is {type(value).__name__}, not text")


def _number(obj: dict[str, object], key: str) -> int | float | None:
    value = obj.get(key)
    if value is None or (isinstance(value, int | float) and not isinstance(value, bool)):
        return value
    raise TypeError(f"yt-dlp field {key!r} is {type(value).__name__}, not a number")


# meiva:note
# The entries of a flat playlist listing (`yt-dlp -J --flat-playlist`), in playlist order.
#
# Raises:
#     ValueError: the answer has no `entries` (`playlist` names the playlist asked for).
#     TypeError: `entries` is not a list, or an entry's id or title is not text.
def playlist_entries(stdout: str, playlist: str) -> tuple[PlaylistEntry, ...]:
    entries = _load_object(stdout, "playlist listing").get("entries")
    if entries is None:
        raise ValueError(f"yt-dlp returned no 'entries' for playlist {playlist!r}")
    if not _is_object_list(entries):
        raise TypeError(f"yt-dlp 'entries' for playlist {playlist!r} is not a list")
    objects = [_as_object(raw, "playlist entry") for raw in entries]
    return tuple(PlaylistEntry(id=_text(o, "id"), title=_text(o, "title")) for o in objects)


# meiva:note
# The channel or playlist object a handle or URL resolves to (`yt-dlp -J --flat-playlist -I 0`).
def source_meta(stdout: str) -> SourceMeta:
    obj = _load_object(stdout, "source")
    return SourceMeta(
        id=_text(obj, "id") or "",
        channel_id=_text(obj, "channel_id"),
        channel=_text(obj, "channel"),
        title=_text(obj, "title"),
    )


# meiva:note
# The metadata of one video (`yt-dlp -J`).
#
# Raises:
#     TypeError: a field is of the wrong type.
def video_meta(stdout: str) -> VideoMeta:
    obj = _load_object(stdout, "video")
    raw_tags: object = obj.get("tags") or []
    if not _is_object_list(raw_tags):
        raise TypeError("yt-dlp field 'tags' is not a list of text")
    tags = [t for t in raw_tags if isinstance(t, str)]
    if len(tags) != len(raw_tags):
        raise TypeError("yt-dlp field 'tags' is not a list of text")
    return VideoMeta(
        title=_text(obj, "title"),
        description=_text(obj, "description"),
        channel_id=_text(obj, "channel_id"),
        channel=_text(obj, "channel"),
        uploader=_text(obj, "uploader"),
        uploader_id=_text(obj, "uploader_id"),
        channel_url=_text(obj, "channel_url"),
        timestamp=_number(obj, "timestamp"),
        duration=_number(obj, "duration"),
        tags=tuple(tags),
        thumbnail=_text(obj, "thumbnail"),
        view_count=_number(obj, "view_count"),
        like_count=_number(obj, "like_count"),
        comment_count=_number(obj, "comment_count"),
    )


# meiva:note
# WebVTT as plain prose: drop headers, cues and timestamps, strip inline `<c>` word-timing tags,
# and collapse YouTube's rolling-caption repeats.
#
# YouTube re-emits each caption line verbatim as the next scrolls in, so the duplication is
# *consecutive exact* lines — dropping those is sufficient and lossless (empirically the only
# repeat shape produced). A broader substring-containment dedup was tried and removed: it dropped
# zero extra lines on real captions yet risked deleting a legitimately distinct short line that
# happened to be a substring of its predecessor.
def clean_vtt(vtt: str) -> str:
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or "-->" in line:
            continue
        if line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        text = html.unescape(_RE_VTT_TAG.sub("", line))
        text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
        if not text:
            continue
        if lines and lines[-1] == text:
            continue
        lines.append(text)
    return "\n".join(lines)


# meiva:note
# The text the transcription CLI printed (`--json` on stdout), or None when it came back empty.
#
# Raises:
#     ValueError: the output has no `text`.
#     TypeError: the output is not a JSON object, or its `text` is not text.
def whisper_text(stdout: str) -> str | None:
    obj = _load_object(stdout, "transcription")
    if "text" not in obj:
        raise ValueError("the transcription output has no 'text'")
    text = obj["text"]
    if not isinstance(text, str):
        raise TypeError("the transcription output 'text' is not text")
    return text or None
