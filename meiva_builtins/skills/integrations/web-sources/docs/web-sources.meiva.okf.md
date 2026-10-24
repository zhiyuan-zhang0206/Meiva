---
type: doc
title: web-sources skill — Fetch content from any source
description: "Fetch content from any internet source — follow updates from a creator/site/feed, or fetch a single URL — via per-platform adapter sub-skills. Pick the adapter first, then read its own skill; subs: generic / rss / youtube."
tags:
- extensions
- agent-instruction
---

# web-sources skill — Fetch content from any source

## What it is
A set of skills (`$MEIVA_HOME/skills/web-sources/`) to fetch content from any internet source: follow updates from a creator / site / feed, or fetch a single URL. Its organizational choice is **per-platform adapters** — each platform gets a self-contained sub-skill (pick the adapter first, then read its own SKILL.md), instead of one giant generic scraper. The root is a routing index.

## Four adapter sub-skills
- **youtube** — enumerate new videos from channel/playlist + fetch metadata and transcription; no login required, runs on any machine.
- **rss** — parse RSS/Atom feeds: list items / ingest a window, optionally fetch full text of each item.
- **generic** — extract body text of any webpage (remove boilerplate → markdown + metadata), or enumerate article links from index pages by URL regex; it's the fallback for sources that have **no platform adapter and no RSS** (official sites / forums / newsletters). `enum` runs the same curl → Jina escalation ladder as `fetch`.

## Script structure
Every adapter script is a thin shell over pure sibling modules, loaded through the `__file__`-derived `sys.path` guard (scripts run with `PYTHONSAFEPATH=1`; the guard is the one path import structure Rule 6 allows inside a skill). The shell runs the effects (the HTTP client or `yt-dlp` as a child process, the mirror files, the clock); the `strict` modules hold the decisions over values:
- `scripts/` (generic, shared with rss `--full`): `scrape_wire.py` (curl and Jina argv, process outcome to text, the internal-host tripwire), `scrape_ladder.py` (which rung wins, Jina answer to article, list-page links), `scrape_post.py` (trafilatura extraction, post, markdown, S1), `webscrape.py` (shell and CLI).
- `rss/scripts/`: `rss_post.py` (parse a feed into entries, post, markdown, S1), `rss_plan.py` (`--since`, window and order, the sync plan against the mirror), `feed.py` (shell and CLI).
- `youtube/scripts/`: `yt_wire.py` (yt-dlp argv, listing/metadata/VTT/transcription decoding), `yt_plan.py` (source resolution, the mirror watermark walk, the sync plan), `yt_post.py` (post, markdown, S1), `yt_run.py` (the value-taking implementation), `feed.py` (the entry an agent imports; it reads the process once into a `YoutubeConfig`).

How a run reaches the outside is a value: a `ScrapeConfig` (the `curl` program, the Jina endpoint, the optional `JINA_API_KEY`), an `RssConfig`, a `YoutubeConfig` (the `yt-dlp` program, the transcription CLI, the mirror dir). Composition roots (`main`, `feed.py`) build them from the environment and home; the mirror dir and the current time are parameters below that. The tests pass values to the pure modules and run the shells against stand-in `curl`, `yt-dlp` and transcription programs written to `tmp_path` and real mirror files, so nothing patches a name or reaches the network.

## Key dependencies
- [[meiva_builtins/skills/docs/skills.meiva.okf.md|Skills index]] — full skills catalog
- [[meiva/web/docs/web.meiva.okf.md|meiva.web]] — fetching lands on `meiva.web.search/fetch`
- [[meiva_builtins/skills/integrations/audio-transcribe/docs/audio-transcribe.meiva.okf.md|audio-transcribe]] — transcription of audio/video items without subtitles relies on it
