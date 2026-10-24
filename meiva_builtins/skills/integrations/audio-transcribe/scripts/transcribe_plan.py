# meiva:note
# The decisions of the audio-transcribe skill, over values: where a source comes from, the
# commands that fetch and transcode it, whether the audio fits one request or must be cut
# into segments (and how long a segment is), whether the parts fit the API, and the shape
# of the answer. Running the programs, the OpenAI call and printing live in transcribe.py.

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# meiva:note
# OpenAI caps an upload at 25 MB; the default segment size stays under it with a margin.
OPENAI_MAX_BYTES = 25 * 1024 * 1024
SAFE_MAX_BYTES = 24 * 1024 * 1024

# meiva:note
# A per-request *duration* ceiling, independent of bytes: at 32 kbps a 24 MB chunk is
# ~100 min, which can exceed the transcription model's per-call processing budget even
# while under the size cap. Segment on whichever binds first (bytes or seconds). 15 min
# is comfortably safe.
MAX_SEGMENT_SECONDS = 900

# meiva:note
# A sanity ceiling on total audio so a mis-passed huge file can't run up an unbounded
# transcription bill; callers raise it explicitly to override.
MAX_TOTAL_SECONDS = 4 * 3600

DEFAULT_MODEL = "gpt-4o-transcribe"

_SEGMENT_FLOOR_SECONDS = 60
_BYTE_SHARE_MARGIN = 0.9

# meiva:note
# A bare 11-char YouTube id or any http(s) URL is fetched via yt-dlp; anything else is
# treated as a local media path.
_RE_VIDEO_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")


# meiva:note
# The three programs the skill runs, by name or path. Each method is the argv for one step.
@dataclass(frozen=True)
class Tools:
    ffmpeg: str
    ffprobe: str
    yt_dlp: str

    # meiva:note
    # --no-config: ignore user yt-dlp config that could write sidecars (info json /
    # thumbnail) which a glob might then mis-pick. --audio-format mp3 is forced, so the
    # audio is exactly src.mp3.
    def download(self, url: str, out_template: str) -> list[str]:
        return [self.yt_dlp, "--no-config", "-x", "--audio-format", "mp3", "-o", out_template, url]

    def probe(self, media: str) -> list[str]:
        return [
            self.ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            media,
        ]

    def transcode(self, media: str, compact: str) -> list[str]:
        return [
            self.ffmpeg,
            "-nostdin",
            "-y",
            "-i",
            media,
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "32k",
            compact,
        ]

    def segment(self, compact: str, seconds: int, parts_template: str) -> list[str]:
        return [
            self.ffmpeg,
            "-nostdin",
            "-y",
            "-i",
            compact,
            "-f",
            "segment",
            "-segment_time",
            str(seconds),
            "-c",
            "copy",
            parts_template,
        ]


# meiva:note
# The size ceilings of one transcription: `max_bytes` and `max_segment_s` bound a request,
# `max_duration_s` bounds the whole job (None lifts it), `api_cap_bytes` is the API's own
# upload cap. A request size above the API cap is refused at construction.
@dataclass(frozen=True)
class Limits:
    max_bytes: int = SAFE_MAX_BYTES
    max_segment_s: int = MAX_SEGMENT_SECONDS
    max_duration_s: float | None = MAX_TOTAL_SECONDS
    api_cap_bytes: int = OPENAI_MAX_BYTES

    def __post_init__(self) -> None:
        if self.max_bytes > self.api_cap_bytes:
            raise ValueError(f"max_bytes {self.max_bytes} exceeds the OpenAI 25 MB limit")


@dataclass(frozen=True)
class LocalFile:
    path: Path


@dataclass(frozen=True)
class Download:
    url: str


# meiva:note
# What the final answer is made of: one text per transcribed chunk, in order. `as_json` is
# the `{text, model, chunks, audio_seconds, source}` object the CLI prints.
@dataclass(frozen=True)
class Transcript:
    texts: tuple[str, ...]
    model: str
    seconds: float
    source: str

    def as_json(self) -> dict[str, object]:
        return {
            "text": "\n".join(t for t in self.texts if t),
            "model": self.model,
            "chunks": len(self.texts),
            "audio_seconds": round(self.seconds, 1),
            "source": self.source,
        }


# meiva:note
# Where `source` comes from. A local media file (`exists`, with `expanded` its ~-expanded
# path) is used as is; a YouTube id or http(s) URL is downloaded; anything else is refused.
def plan_source(source: str, expanded: Path, exists: bool) -> LocalFile | Download:
    if exists:
        return LocalFile(expanded)
    is_video_id = _RE_VIDEO_ID.match(source) is not None
    if not (is_video_id or source.startswith(("http://", "https://"))):
        raise FileNotFoundError(f"source is neither a local file nor a URL/video id: {source!r}")
    return Download(f"https://www.youtube.com/watch?v={source}" if is_video_id else source)


def parse_duration(probe_stdout: str) -> float:
    return float(probe_stdout.strip())


def check_duration(seconds: float, limits: Limits) -> None:
    if limits.max_duration_s is not None and seconds > limits.max_duration_s:
        raise ValueError(
            f"audio is {seconds:.0f}s, over max_duration_s={limits.max_duration_s:.0f}; "
            f"pass a larger max_duration_s to override"
        )


# meiva:note
# The segment length to cut the compact audio into, in seconds, or None when it fits one
# request under both the byte cap and the duration ceiling. The audio is fixed-bitrate, so
# bytes scale with time: the length whose byte share stays under the cap (0.9 margin),
# clamped by the duration ceiling and a sane floor.
def segment_seconds(size: int, duration: float, limits: Limits) -> int | None:
    if size <= limits.max_bytes and duration <= limits.max_segment_s:
        return None
    byte_seg = (
        int(duration * limits.max_bytes / size * _BYTE_SHARE_MARGIN)
        if size
        else limits.max_segment_s
    )
    return max(_SEGMENT_FLOOR_SECONDS, min(byte_seg, limits.max_segment_s))


# meiva:note
# Refuse segments that are still over the API cap after the split. `parts` is (name, bytes).
def check_parts(parts: Sequence[tuple[str, int]], limits: Limits) -> None:
    oversize = [name for name, size in parts if size > limits.api_cap_bytes]
    if oversize:
        raise RuntimeError(f"segment(s) still over the 25 MB API cap after split: {oversize}")
