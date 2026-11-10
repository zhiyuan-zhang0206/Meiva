---
name: meiva-memory
description: "Maintains and queries Meiva's shared memory. Use when finding shared facts, preserving user preferences, or performing assigned memory maintenance."
---

# Meiva shared memory

Use the shared pool for facts and preferences other agents need; keep detailed
working state in the current agent's workspace. This skill does not assign an
arbiter or steward role. Follow the role and scope supplied by the current task.

## Select the needed guide

- For a query, use `meiva.memory.search("<query>")`; each match carries a relevance
  `score` and the `chunk` that matched, so weigh a weak score before opening the
  note, then read the matching note and inspect its ownership. Another agent's role is context,
  not your identity. Do not start maintenance merely to answer a query.
- To write or update a note, search first, then write it with `meiva.files.write`
  under `meiva.memory.PATH` (shared) or `meiva.memory.PERSONAL_PATH` (your own)
  directly in **pure Markdown** (`# Title` + a concise high-level description
  paragraph + body, ≤ 8000 chars) with **zero write-time validation or YAML
  frontmatter burden** (legacy `type`, `meiva_agent`, `meiva_machine`,
  `timestamp`, `tags`, or HTML attribution comments are retired — `validate.py`,
  canonical OKF frontmatter (`description`, `verified`, and optional
  `stale_after`), inline body source references, and `MEMORY.md` index rebuilds run **only during the Meiva
  Dream consolidation stage**). Maintain **one canonical dateless file per rule
  or topic slot**: when a ruling or fact changes, overwrite the existing
  canonical note in place (and `git rm` superseded date-suffixed fragments
  during consolidation) rather than creating a new date-suffixed fork, and write
  explicit `YYYY-MM-DD` dates instead of relative time words. Directory
  `index.md` files are optional high-level directory descriptions rather than
  generated file inventories. Never commit directly on the shared checkout's
  `main`; remote-backed pool commits use an isolated git worktree + PR.
- When recording user preferences or corrections, read
  [user-dimension maintenance](reference/user-dimension.md). Search first,
  update the existing standing note, and keep durable shared facts in the pool.
- For consolidation, read [consolidation](consolidation/SKILL.md) to run the
  four-phase **Meiva Dream** cycle (`Orientation` -> `Gather Signal` ->
  `Consolidation` -> `Prune & Index`), then follow only the single-box, arbiter,
  or steward procedure matching this deployment and the explicitly assigned role.
  Stewards publish local work via worktree PRs; arbiters run Dream consolidation,
  merge validated PRs, and verify search refresh.
- Only when acting as the assigned Memory Arbiter, read
  [the arbiter role guide](reference/arbiter-role.md) for Dream consolidation,
  health checks, workspace-note curation, schedules, and collaboration
  responsibilities.

During Meiva Dream consolidation, run `validate.py`, enrich OKF metadata, and
rebuild indexes. Working agents write pure Markdown with zero write-time
validation or frontmatter burden. Large source material belongs in the Vault
with a searchable pointer. A successful commit or merge does not prove
the gateway search index was refreshed. Preserve the user's existing authority
for writes, messages, schedules, and maintenance.

## Vault paths and source references

The Vault Git repository is `$MEIVA_HOME/vault` (`$MEIVA_HOME/gateway/vault`
for the gateway checkout). `meiva.memory.PATH` names its `memory/` subtree;
Git operations and repository configuration belong at the Vault root. Store
original evidence in sibling `sources/` or `papers/` directories.

Cite evidence with inline Markdown file links in the note body:

```markdown
See [ruling](/sources/rulings.jsonl), [paper](/papers/example.pdf),
[related note](../other/note.md), or [code](/repos/meiva/base/paths/__init__.py).
```

A leading `/` resolves from the Vault root. `/repos/<alias>/<path>` resolves
against the mapped code checkout (`--repo alias=/path` for Dream probes);
other relative links resolve from the note's directory and must stay inside
Vault. Targets must be files. Fragments are ignored for source verification.
Links inside fenced or inline code are ignored; protocol targets containing
`:` (including HTTP, mail, and legacy `repo:`/`db:` anchors) are rejected.
Archive external evidence as a Vault file before citing it.

Dream checks each source file's Git history against `verified`, recording
`git_changed`, `missing_path`, `not_a_file`, or `malformed_source` findings.
Notes without inline file links appear in `missing_okf_notes` with
`missing_fields: ["sources"]`; `MEMORY.md`, `INHERIT.md`, and `index.md` are
exempt from that provenance reminder. YAML `sources` is always an error.
