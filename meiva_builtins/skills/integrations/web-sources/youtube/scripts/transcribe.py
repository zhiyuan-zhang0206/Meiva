# meiva:note
# YouTube's opt-in fallback invokes this subprocess when caption tracks are unavailable.
# It extracts compact mono audio and splits requests that exceed the OpenAI upload limit.
# The CLI requires ffmpeg, yt-dlp for video sources, and a configured OpenAI API key.
# meiva:link meiva_builtins/skills/integrations/web-sources/youtube/SKILL.md

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from openai import OpenAI, omit

from base.config import settings
from base.host.boundary.options import options

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
# The flags `main` reads, one field per namespace attribute; `access` and `tools` are not fields
# because the caller binds them. `json_output` is the `--json` flag under another name, because
# `json` already names the module the result is printed with.
@dataclass(frozen=True)
class _TranscribeOptions:
    source: str
    model: str
    language: str | None
    prompt: str | None
    max_bytes: int
    max_segment_s: int
    max_duration_s: float
    json_output: bool


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
    # meiva:note
    # argparse converts only a *string* default through `type`; this flag declares a float, so its
    # default is written as one.
    p.add_argument(
        "--max-duration-s",
        type=float,
        default=float(MAX_TOTAL_SECONDS),
        help="Max total audio duration (safety ceiling)",
    )
    p.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output full result as JSON instead of plain text",
    )
    opts = options(p.parse_args(argv), _TranscribeOptions)

    limits = Limits(
        max_bytes=opts.max_bytes,
        max_segment_s=opts.max_segment_s,
        max_duration_s=opts.max_duration_s,
    )
    if access is None:
        raise RuntimeError("OPENAI_API_KEY not set (expected in ~/.meiva/.env) — cannot transcribe")
    result = transcribe(
        opts.source,
        client=OpenAI(api_key=access.api_key, base_url=access.base_url),
        tools=tools,
        model=opts.model,
        language=opts.language,
        prompt=opts.prompt,
        limits=limits,
    )
    if opts.json_output:
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
