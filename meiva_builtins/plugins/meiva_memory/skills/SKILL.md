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
  canonical OKF frontmatter (`description`, `sources`, `verified`, and optional
  `stale_after`), and `MEMORY.md` index rebuilds run **only during the Meiva
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

During the Meiva Dream consolidation stage (and only then), `validate.py`, OKF
frontmatter enrichment, and index rebuilds enforce the pool's metadata,
locality, size, and pointer rules; large source material belongs in the
configured Vault with a searchable pointer. A successful commit or merge does
not prove the gateway search index was refreshed. Preserve the user's existing
authority for writes, messages, schedules, and maintenance.

Every `sources` item must point to an existing file: `repo:<alias>:<file>`
or a pool-relative file path such as `sources/user-rulings.md`, optionally
with a `#fragment`. Any file format is supported (Markdown, PDF, HTML, TXT,
JSONL, Python, TypeScript). Dump external content into a file, or record its
URL and explanation or dated user ruling in a file, then cite that file.
Pool-relative paths resolve as `repo:memory:<file>`; paths must stay relative
and must not traverse `..`. Dream checks existence and rejects directories;
the standalone validator checks syntax. `sources/*.md` participates in normal
recursive note collection, indexing and validation; other formats are evidence
attachments and do not enter the note index.
