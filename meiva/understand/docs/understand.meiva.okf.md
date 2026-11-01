---
type: doc
title: meiva.understand — Multimodal Understanding Primitive
description: '`meiva.understand` is the SDK''s single multimodal LLM primitive: asks the model to answer a prompt based on a file or a piece of text, supporting text/image/video/audio/PDF.'
tags:
- agent-view
- sdk
- agent-lifecycle
---

# meiva.understand — Multimodal Understanding Primitive

## What it is

`meiva.understand` is the core SDK's single multimodal LLM primitive—asks the model to answer a `prompt` based on given material. `meiva.web.fetch` also uses it internally to 'read' web pages.

## Core API

- `understand(targets: list[dict], max_concurrent=12) → list[str]` — Batch-only. Each target is a dict with `prompt` plus exactly one of `text` / `paths`. Answers come back in input order.
  - `paths`: non-empty list of file paths (relative path resolution same as other file operations) — a single file is a one-element list. Text files are read as UTF-8; images/video/audio/PDF are classified by suffix (the shared `ATTACH_MEDIA_MIME` table, one owner for suffix → type) and sent as the provider-native blocks of the shared attach packing; the per-file size limits (shared 20 MiB ceiling, then the provider's own) apply. Every file is sent in ONE model call as separate parts, so the model can compare them (e.g. a design frame plus a page screenshot); the content types present pick that one model ([Routing](#routing)).
  - `text`: material itself (literal string).

There is no single-call form — one question is a one-element list:

```python
[answer] = meiva.understand([{"prompt": "summarize this", "paths": ["notes.md"]}])
```

Targets run with a default ceiling of 12; pass a positive `max_concurrent=N` to choose another in-flight limit. The shared DeepSeek cap queues excess provider calls, and terminal typed provider failures surface as `UnderstandError`. Same batch shape as [[meiva/web/docs/web.meiva.okf.md]]'s `search` / `fetch`.

## Constraints & Errors
- `TypeError` — `targets` is not a list, or an element is not a dict.
- `ValueError` — a target is missing `prompt`, does not carry exactly one of `text` / `paths`, or `paths` is an empty list.
- `TypeError` — `targets[i]['paths']` is not a list, or an element is not a path string.
- `FileNotFoundError` — a target's `paths` entry does not point to an existing file.
- `UnderstandError` — a typed provider call failed permanently or exhausted its retries, a file in `paths` is binary, cannot be read as UTF-8, and its extension is not in supported media suffixes, a media file is over a size limit of the chosen model, a configured model does not accept its own type (named by its `MEIVA_UNDERSTAND_<TYPE>_MODEL`), or no configured model accepts every type present.

Validation runs over every target before any model call, so a malformed batch fails before spending tokens. The first error from a running batch propagates.
Unknown model-invocation errors retain their original exception and traceback
after one attempt; error wording alone never grants provider retry authority.

## Key Dependencies
- [[meiva/docs/files.meiva.okf.md]] — `paths` uses the same path resolution (workspace-relative)
- [[base/lm/docs/lm/lm.meiva.okf.md]] — underlying chat model factory (the model of each content type comes from settings)

## Routing

Each content type has its own configured model; the media-type vocabulary is the catalog's `ModelSpec.media_types` (image / pdf / audio / video), and text files count as `text`.

| Type | Setting | Default |
|---|---|---|
| text | `MEIVA_UNDERSTAND_TEXT_MODEL` | `deepseek-flash` |
| image | `MEIVA_UNDERSTAND_IMAGE_MODEL` | `deepseek-flash` |
| pdf | `MEIVA_UNDERSTAND_PDF_MODEL` | `gemini-3.8-flash` |
| audio | `MEIVA_UNDERSTAND_AUDIO_MODEL` | `gemini-3.8-flash` |
| video | `MEIVA_UNDERSTAND_VIDEO_MODEL` | `gemini-3.8-flash` |

One target (all its `paths`) is one call, decided by `routing.route` from values before any model is built:

1. The set of types present is computed. Every present media type's configured model must declare that type in the catalog; otherwise the call fails naming the setting (a configuration error, not a fallback).
2. The candidates are the configured models of the present types in the order video, audio, pdf, image, text. The first whose catalog `media_types` covers every non-text type present answers; text needs no capability, so an all-text target goes to the text model.
3. When no candidate covers the set, `UnderstandError` names the types and the configured models.

The usage source of the call is `understand` for text and `understand.<type>` (`image`, `pdf`, `audio`, `video`; the richest type present) for media.

## Notes

`MEIVA_UNDERSTAND_MEDIA_MODEL` is retired: the field is deleted and a stale key in an environment file is inert, like every retired setting (it is not an alias; set the per-type settings instead). Each model's provider is picked by its prefix through the same model factory every LLM path uses (`base/lm/factory.py`).

Images go as standard `image_url` data-URI blocks, which every provider client decodes (DeepSeek, Gemini, Claude, OpenAI …). PDF, audio and video go as the block the shared packing builds for the model's provider: a `document` block for a Claude model, an inline `media` part for Gemini. Audio and video exist in the catalog only on Gemini models, so only a Gemini model can answer them.

The Gemini-only knobs `MEIVA_UNDERSTAND_MEDIA_RESOLUTION`, `MEIVA_UNDERSTAND_MEDIA_THINKING_LEVEL` and `MEIVA_UNDERSTAND_MEDIA_BASE_URL` apply only when the chosen model is a Gemini model. For any other model `effort` goes through that model's own effort levels (DeepSeek: `high` / `max`) like a text call. Media goes inline; a file over a size limit raises `UnderstandError` naming the path.

`meiva.understand` and `meiva.self.attach` ([[meiva/self/docs/self.meiva.okf.md]]) are the agent's two ways to use a media file: attach puts the pixels in the agent's own context (it sees them itself, and they stay in the conversation); `understand` has a separate model answer in text and keeps the context small.

## Structure
`meiva/understand/__init__.py` is the namespace function: it validates through `plans.normalize` (no ambient reads), builds this process's `UnderstandDeps` in `wiring.py` (the only place that reads the configured model of each type and the knobs, the model catalog, the bound agent's workspace and output site) and calls `answer.run` with the `FindingDelivery` the facade read (scanning enabled, exec turn running). The deps are plain values; `understand_deps_for` takes the configuration and the process facts as arguments, `understand_deps` reads them once. What a target becomes is decided from values: `routing.py` holds the configured model of each type and picks the one model of a target, `content.py` classifies the files read as parts and builds the content blocks that model receives, `calls.py` plans the model call (build options, which configuration is refused before a model is built, how a suffix classifies a file) and `outputs.py` renders the saved copy of an answer and names the files that fall out of the ring. `answer.py` is the shell: it reads the files, builds the planned model through the real provider factory, invokes it, scans what goes in and out (the findings travel as values next to the answers and are delivered once through `meiva.security.deliver`, also when a target or a save fails), and writes the saved files. Tests build the deps from a configuration authority, give files under `tmp_path` and script the models through `llm_override`; the plans are asserted on values.
