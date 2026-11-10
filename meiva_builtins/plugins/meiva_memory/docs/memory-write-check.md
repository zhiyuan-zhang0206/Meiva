# Memory write check

## Format

There is no writer function: the agent writes the whole note directly in pure
Markdown (YAML frontmatter is optional) with `meiva.files.write` (or `append`)
under `PATH` or `PERSONAL_PATH`. Nothing is generated, merged or indexed for it,
and `meiva.files` never rewrites the content (it no longer stamps
`injection-risk` into memory notes).

```markdown
# <Title>

<One-paragraph high-level description of the rule or fact.>

<Note body>
```

Checked by the pool validator (`template/validate.py::validate_note`): valid
UTF-8, valid YAML mapping without unquoted `' #'` or `': '` truncation when
optional YAML frontmatter is present, and a note stays under 8000 characters.
YAML `sources` is forbidden; provenance uses inline body file links whose targets
resolve to files inside Vault or a mapped code repository. Code examples are
excluded from source extraction.

The cap counts characters as `len()` — the measurement the shared budget names
`Unit.CHARS` — and stays with the standalone validator (which also runs where
`base.*` is unimportable); the write check adds no second cap and only surfaces
its finding.

## Write check

The `meiva_memory` plugin wraps `meiva.files.write` / `append` / `edit` (outside `meiva_code`'s
cwd-tracking wraps of the same targets: plugins stack in name order, the later one is
outermost). After the layer below succeeds, a `.md` path under `PATH` or `PERSONAL_PATH` is
added to the plugin state (`shared_notes` / `personal_notes`, relative paths, union-merged);
the wrap returns the lower layer's result and lets its exception through, and its own
failures are only logged. The exec subprocess sends no request and imports no `httpx` or
decision-model code.

The `after_exec` hook then reads and clears the record and checks, under
`memory_write_check_deadline_seconds` (5s, shared by all notes of the call):

- every written note against `template/validate.py::validate_note` (the single-note rules
  and the 8000-character cap; directory, index and pointer rules stay with the steward);
- every written shared note against the pool: `POST /api/memory/search` with
  `purpose="dedup"`, query = the note's description and body (4000 characters at most),
  `k = 5` (the gateway ranks `memory_dedup_candidates`, default 5, not the 30 of recall/search; the note itself is set aside, so an already indexed note leaves four). The gateway restores the text of its
  closest candidates and a decision model answers, per candidate, whether the two notes govern
  the same specific rule, policy, or subject slot — including revisions, exceptions, or superseding decisions (`usage_kind=memory_write_dedup`). Candidates at or above
  `memory_write_dedup_threshold` (0.80; measured on 2026-10-19 on two 200-write samples, where
  about one shared write in ten got a reminder and about half of those named a real duplicate)
  are named; every probability is logged (`[memory-write-check]`). Personal notes are not
  searched.

A finding becomes one `memory_write_check` system note after the exec result; no finding
adds nothing. Nothing is blocked, rewritten or raised: a timeout, an unavailable search
or any unexpected error is logged and the notes go unreported.

Every written note — shared and personal — is checked against the pool validator's single-note rules (template/validate.py::validate_note); only the notes with findings are reported, shared notes first, each group in path order, and a note that is gone or is not a note is skipped. Nothing is changed, blocked or rejected: the findings come back as one reminder.

A written shared note is searched against the pool with purpose dedup: the query is the note's description then body, capped at 4000 characters; k = 5, with the note itself set aside, so an already-indexed note leaves four candidates, and only the five closest others are considered. Candidates at or above memory_write_dedup_threshold are named with their probability. Personal notes are not searched.

## Key Dependencies

- [meiva.memory — Long-term Memory Pool (Plugin Provided)](memory-api.md) — the `meiva.memory` surface the notes live under
- [meiva_memory — Memory Pool Plugin](meiva_memory.md) — the owning plugin
- [Memory Search Read Path — Retrieval and Decision-Model Ranking](../../../../services/docs/gateway_side/memory_indexer/memory-rerank.md) — the `dedup` purpose of the search ranking
