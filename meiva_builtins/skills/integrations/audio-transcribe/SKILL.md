---
name: audio-transcribe
description: "Transcribes speech from audio, video, YouTube, or media URLs. Use when a transcript or spoken-content extraction is needed."
---

# audio-transcribe

The **transcription fallback** for the [`web-sources`](../web-sources/SKILL.md) feed adapters:
when a feed item has no subtitles (most non-YouTube sources, music videos, newly released videos, or videos where the author disabled subtitles), extract the audio and send it here for transcription. Adapters like `web-sources:youtube` call it in the `transcript is None` branch.

**Cloud-based, does not run local models** (user chose the official service). Default is `gpt-4o-transcribe` — OpenAI's most accurate transcription model (WER lower than `whisper-1`), outputs plain text (no segment-level timestamps), exactly the prose needed for digest. If you need SRT/VTT timestamps, pass `model="whisper-1"` instead.

## Usage

```python
import os
import sys
sys.path.insert(0, os.path.join(os.environ["MEIVA_HOME"], "skills", "audio-transcribe", "scripts"))
import transcribe
from base.config import settings
from openai import OpenAI

client = OpenAI(api_key=settings.lm.openai_api_key.get_secret_value())
tools = transcribe.SYSTEM_TOOLS  # ffmpeg / ffprobe / yt-dlp on PATH

# Local audio/video file → plain text
r = transcribe.transcribe("/path/to/talk.mp3", client=client, tools=tools)
print(r["text"], r["chunks"], r["audio_seconds"])

# YouTube video id / any media URL (internally uses yt-dlp to extract audio)
r = transcribe.transcribe("dRsjO-88nBs", client=client, tools=tools)
r = transcribe.transcribe("https://example.com/podcast.mp3", client=client, tools=tools, language="en")
```

A caller in another skill instead runs it as a subprocess CLI —
`$MEIVA_HOME/source/.venv/bin/python $MEIVA_HOME/skills/audio-transcribe/scripts/transcribe.py <source> --json`
— and reads the JSON from stdout (see `web-sources:youtube`'s whisper fallback). The CLI reads the
key and the program names once, in its entry point; the CLI flags are unchanged.

`transcribe(source, *, client, tools, model="gpt-4o-transcribe", language=None, prompt=None,
limits=Limits())` returns `{text, model, chunks, audio_seconds, source}`. `client` and `tools` are
required: the caller builds the OpenAI client and names the programs. `language` is an optional
ISO-639-1 hint; `prompt` is optional, used to bias proper noun spellings. `limits` bundles the size
ceilings (`Limits(max_bytes=24MB, max_segment_s=900, max_duration_s=4h)`); a `max_bytes` above the
25 MB API cap is refused when the `Limits` is built.

## How to handle the 25MB limit

OpenAI single upload ≤25MB. Process: first use ffmpeg to convert to **mono 16kHz mp3 @32kbps** (16kHz is the model's native sample rate, lossless for ASR but compresses ~10x) → if still exceeds limit (very long speech), slice by duration, transcribe each slice separately and then concatenate. Slice length is calculated such that "the byte size of that slice < limit (leaving 0.9 headroom)"; at constant bitrate, bytes scale linearly with duration.

## Dependencies

- `ffmpeg` (transcoding + segmentation) and `ffprobe` (duration measurement) on PATH.
- `yt-dlp` (only when source is a URL/video id to extract audio), from the `skill-tools`
  dependency group.
- `OPENAI_API_KEY` in `~/.meiva/.env`, read via `base.config.settings`.

## What it does NOT do

- **Does not store files**: only returns text; saving to raw mirror is the responsibility of the caller (e.g., web-sources:youtube).
- **No timestamps/segment alignment**: the default model only outputs plain text. Use `whisper-1` for SRT/VTT.
- **No speaker diarization**: OpenAI transcription does not distinguish speakers.

## Troubleshooting

- **`OPENAI_API_KEY not set`**: make sure `~/.meiva/.env` has `OPENAI_API_KEY=...`.
- **`ffmpeg`/`ffprobe` not found**: `brew install ffmpeg` (both transcoding and duration measurement depend on it).
- **`yt-dlp` not found**: install the `skill-tools` group (`uv sync --group skill-tools` in a
  checkout). `MissingSkillToolError` names it.
- **Proper nouns misspelled** (e.g., Claude→Cloud): common ASR issue, same as YouTube auto-captions; passing a `prompt` with term context can mitigate.
- **Very long audio is slow/expensive**: serial transcription per slice; each slice is one API call, a 2-hour speech is ~6 slices.
