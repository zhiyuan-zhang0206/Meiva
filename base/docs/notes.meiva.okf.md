---
type: doc
title: "Markdown Note Model"
description: '`base/packages/docs/notes.py` — one parser for the whole repo: decides a `---`-frontmatter markdown text is a note and lifts the fields both graph consumers need. Note/parse_note/normalize_tags (the pool walker `walk_notes` is the shell `base/packages/note_files.py`), plus the lenient type-preserving `parse_frontmatter_typed` in `base/packages/docs/frontmatter.py`.'
tags:
- base
- library
- markdown
---

# Markdown Note Model

## What it is

`base/packages/docs/notes.py` is the single place that turns the text of a `---`-frontmatter markdown
file into a typed `Note` — the model behind the memory pool concept graph
(`gateway/routers/memory_api/memory.py`) and the OKF bundle graph
(`base/packages/docs/okf_graph.py`). Before it existed, frontmatter parsing was inlined
three times (memory graph, memory search metadata, okf bundle) with two
different tags-normalization rules; now the delimiter scan lives once in
`base/packages/docs/frontmatter.py:_split_frontmatter`, shared by the strict parser
(`parse_frontmatter`, skills/commands) and the lenient typed variant
(`parse_frontmatter_typed`, note pools).

## Contract

- `parse_note(text, rel) -> Note | None` — `None` when the file is not a note
  (no frontmatter, unterminated block, bad YAML, non-dict block). **One bad
  note is skipped, never fatal**: a note pool's contract is that a single
  malformed file must not take down the whole graph.
- `normalize_tags(raw)` — tags are a string list and nothing else: a bare
  string is wrapped, a list keeps only its string elements, anything else is
  `()` (`tags: 5` used to TypeError the graph endpoint).
- `base/packages/note_files.py:walk_notes(root, skip_names=..., warnings=...)` — the shell: yields
  `(path, note)` in sorted order, reading each file whole; unreadable files surface as warnings,
  reserved names and non-notes are skipped silently.

## Notes

- `Note.rel` is the posix path relative to the pool root, sans extension — the
  stable node id both graph consumers key on. `Note.fm` keeps the raw
  frontmatter dict for callers that read fields the dataclass does not lift.
- Graph-specific semantics (primary_tag policy, edge filtering to known nodes)
  deliberately live in the callers, not here — this module stays schema-free.
