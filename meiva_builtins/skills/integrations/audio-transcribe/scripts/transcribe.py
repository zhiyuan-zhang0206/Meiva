"""Audio transcription via OpenAI's hosted speech-to-text.

The transcription fallback for the `web-sources` feed adapters: when a feed
item has no captions (most non-YouTube sources, music videos, brand-new
uploads), pull the audio and transcribe it here. `web-sources:youtube` calls
this CLI as a subprocess on its `transcript is None` path — audio-transcribe
and web-sources are separate skills, so the call crosses a skill boundary and
goes through the CLI rather than an in-process import.

Cloud-only by design (the user picked the hosted service over a local model):
default model `gpt-4o-transcribe` — OpenAI's most accurate transcription model
(lower WER than `whisper-1`). It returns text only (no segment timestamps); the
digest wants prose, so that is the right trade. Pass `model="whisper-1"` if you
later need SRT/VTT timestamps.

Handles the OpenAI **25 MB per-request limit**: audio is first transcoded to a
compact mono 16 kHz mp3 (16 kHz is the model's native rate, so this is lossless
for ASR while shrinking ~10x), and anything still over the limit (very long
talks) is split into time segments, each transcribed and concatenated.

Requires `ffmpeg` on PATH, `yt-dlp` (only when the source is a URL/video id),
and `OPENAI_API_KEY` in `~/.meiva/.env` (read via `base.config.settings`).

Usage:

    .venv/bin/python skills/audio-transcribe/scripts/transcribe.py <source> ...
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import assert_never

from openai import OpenAI, omit

from base.config import settings
from meiva_builtins.skills.skill_tools import missing

# meiva:note
# `transcribe_plan` is a sibling in this skill's own scripts/ dir, not an importable
# package (PYTHONSAFEPATH=1 keeps a script's own directory off sys.path). Structure Rule 6
# recognizes this exact __file__-derived, within-skill shape as the endorsed guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from transcribe_plan import (
    DEFAULT_MODEL,
    MAX_SEGMENT_SECONDS,
    MAX_TOTAL_SECONDS,
    SAFE_MAX_BYTES,
    Download,
    Limits,
    LocalFile,
    Tools,
    Transcript,
    check_duration,
    check_parts,
    parse_duration,
    plan_source,
    segment_seconds,
)

SYSTEM_TOOLS = Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", yt_dlp="yt-dlp")
DEFAULT_LIMITS = Limits()


# meiva:note
# The OpenAI credentials of this process; `base_url` None means the SDK's default endpoint.
@dataclass(frozen=True)
class OpenAIAccess:
    api_key: str
    base_url: str | None


# meiva:note
# Run one program to completion and return it. A non-zero exit raises with the tail of
# its stderr, so the cause (a codec error, a missing input) reaches the caller.
def run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} exited with status {proc.returncode}: {proc.stderr.strip()[-500:]}"
        )
    return proc


# meiva:note
# A local media file is returned as-is; a YouTube id / URL is downloaded to an mp3 via
# yt-dlp. (Video files are handled by the transcode step.)
def resolve_to_audio(source: str, workdir: Path, tools: Tools) -> Path:
    expanded = Path(source).expanduser()
    plan = plan_source(source, expanded, expanded.exists())
    match plan:
        case LocalFile(path=path):
            return path
        case Download(url=url):
            program = shutil.which(tools.yt_dlp)
            if program is None:
                raise missing("audio-transcribe", f"the `{tools.yt_dlp}` program")
            tools = replace(tools, yt_dlp=program)
            run_command(tools.download(url, str(workdir / "src.%(ext)s")))
            got = workdir / "src.mp3"
            if not got.exists():
                raise RuntimeError(f"yt-dlp produced no mp3 audio for {source!r}")
            return got
        case _:
            assert_never(plan)


def audio_seconds(media: Path, tools: Tools) -> float:
    return parse_duration(run_command(tools.probe(str(media))).stdout)


# meiva:note
# Transcode to a compact mono 16 kHz mp3, then split into segments under *both* the byte
# cap and the segment duration if either binds. Returns the ordered chunk paths to
# transcribe.
def prepare_chunks(media: Path, workdir: Path, tools: Tools, limits: Limits) -> list[Path]:
    compact = workdir / "compact.mp3"
    run_command(tools.transcode(str(media), str(compact)))
    seconds = segment_seconds(compact.stat().st_size, audio_seconds(compact, tools), limits)
    if seconds is None:
        return [compact]
    run_command(tools.segment(str(compact), seconds, str(workdir / "part_%03d.mp3")))
    parts = sorted(workdir.glob("part_*.mp3"))
    if not parts:
        raise RuntimeError(f"ffmpeg segmentation produced no parts for {media}")
    check_parts([(p.name, p.stat().st_size) for p in parts], limits)
    return parts


def transcribe_file(
    client: OpenAI, path: Path, *, model: str, language: str | None, prompt: str | None
) -> str:
    with path.open("rb") as f:
        return str(
            client.audio.transcriptions.create(
                file=f,
                model=model,
                response_format="text",
                language=language or omit,
                prompt=prompt or omit,
            )
        ).strip()


# meiva:note
# Transcribe `source` to plain text via OpenAI.
#
# `source` is a local audio/video file, a YouTube video id, or any media URL (downloaded
# via yt-dlp). Long audio is segmented under both the 25 MB API limit and
# `limits.max_segment_s`, and the per-segment texts are concatenated.
#
# `limits.max_duration_s` is a sanity ceiling on total audio (one API call per segment
# costs money): raise it or pass `Limits(max_duration_s=None)` to override for a long job.
#
# Returns `{text, model, chunks, audio_seconds, source}`. `language` is an optional
# ISO-639-1 hint; `prompt` is optional context to bias spelling.
def transcribe(
    source: str,
    *,
    client: OpenAI,
    tools: Tools,
    model: str = DEFAULT_MODEL,
    language: str | None = None,
    prompt: str | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        media = resolve_to_audio(source, workdir, tools)
        seconds = audio_seconds(media, tools)
        check_duration(seconds, limits)
        chunks = prepare_chunks(media, workdir, tools, limits)
        texts = tuple(
            transcribe_file(client, c, model=model, language=language, prompt=prompt)
            for c in chunks
        )
        return Transcript(texts=texts, model=model, seconds=seconds, source=source).as_json()


# meiva:note
# The CLI: parse `argv`, transcribe, print the text (or the full result with --json).
# `access` is None when no OpenAI key is configured, which is refused after the arguments
# are validated.
def main(argv: Sequence[str], *, access: OpenAIAccess | None, tools: Tools) -> None:
    p = argparse.ArgumentParser(
        description="Transcribe audio/video to text via OpenAI speech-to-text."
    )
    p.add_argument("source", help="Local media file, YouTube video ID, or media URL")
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Transcription model (default: gpt-4o-transcribe)",
    )
    p.add_argument("--language", default=None, help="Optional ISO-639-1 language hint")
    p.add_argument(
        "--prompt", default=None, help="Optional context to bias spelling of proper nouns"
    )
    p.add_argument(
        "--max-bytes", type=int, default=SAFE_MAX_BYTES, help="Max segment bytes, under 25 MB"
    )
    p.add_argument(
        "--max-segment-s",
        type=int,
        default=MAX_SEGMENT_SECONDS,
        help="Max segment duration in seconds",
    )
    p.add_argument(
        "--max-duration-s",
        type=float,
        default=MAX_TOTAL_SECONDS,
        help="Max total audio duration (safety ceiling)",
    )
    p.add_argument(
        "--json", action="store_true", help="Output full result as JSON instead of plain text"
    )
    args = p.parse_args(argv)

    limits = Limits(
        max_bytes=args.max_bytes,
        max_segment_s=args.max_segment_s,
        max_duration_s=args.max_duration_s,
    )
    if access is None:
        raise RuntimeError("OPENAI_API_KEY not set (expected in ~/.meiva/.env) — cannot transcribe")
    result = transcribe(
        args.source,
        client=OpenAI(api_key=access.api_key, base_url=access.base_url),
        tools=tools,
        model=args.model,
        language=args.language,
        prompt=args.prompt,
        limits=limits,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["text"])


if __name__ == "__main__":
    api_key = settings.lm.openai_api_key
    main(
        sys.argv[1:],
        access=None if api_key is None else OpenAIAccess(api_key.get_secret_value(), None),
        tools=SYSTEM_TOOLS,
    )
