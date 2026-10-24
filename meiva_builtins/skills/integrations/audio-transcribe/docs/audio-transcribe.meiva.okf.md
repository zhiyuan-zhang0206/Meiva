---
type: doc
title: audio-transcribe skill — Audio/video to text
description: Convert local audio/video files, YouTube videos, or media URLs to plain text via OpenAI. Requires ffmpeg + OPENAI_API_KEY. Used when transcription of audio/video/podcast is needed, or when a feed item lacks subtitles/transcription for supplementation.
tags:
- extensions
- agent-instruction
---

# audio-transcribe skill — Audio/video to text

## What it is
A skill (`$MEIVA_HOME/skills/audio-transcribe/`) that converts local audio/video files, YouTube videos, or media URLs to plain text via OpenAI transcription. It exists as a **supplementation layer** for multimodal sources: when a feed item **lacks** existing subtitles/transcription, the web-sources side falls through here to transcribe the audio track into text, allowing downstream to process it like text.

## Dependency form
Requires `ffmpeg` (extract / split audio track) + `OPENAI_API_KEY`. It's a metered API path (contrast with web-ai's "use logged-in browser to avoid credit" — transcription has no corresponding free web surface, so it goes through API).

## Structure
`scripts/transcribe_plan.py` (a `strict` module) holds the decisions over values: where a source comes from (`plan_source`), the argv of each program (`Tools`), whether the audio fits one request or how long a segment is (`segment_seconds`), the duration and part-size gates, and the answer shape (`Transcript`). `scripts/transcribe.py` is the shell: it runs the programs, calls OpenAI and prints. The OpenAI client, the program names (`Tools`) and the size ceilings (`Limits`) are inputs of `transcribe()`; only the CLI entry point reads the key from settings. A program that exits non-zero raises with the tail of its stderr.

## Key dependencies
- [[meiva_builtins/skills/docs/skills.meiva.okf.md|Skills index]] — full skills catalog
- [[meiva_builtins/skills/integrations/web-sources/docs/web-sources.meiva.okf.md|web-sources]] — upstream: items without subtitles are sent here for transcription
