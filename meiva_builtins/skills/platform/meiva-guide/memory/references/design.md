# Memory design

Read this reference to understand how Meiva Memory is organized well enough to
find and interpret notes. The `meiva_memory` plugin owns the contract
(`meiva_builtins/plugins/meiva_memory/` in the Meiva checkout, with `docs/` and
`skills/` beside its code); where this page and the plugin differ, the plugin
wins. The write rule for standalone agents is in the entrypoint: read freely,
write only with the user's approval.

## Layers

- **Shared pool**: notes every agent can read, kept restrained on purpose.
  It holds reusable rules (each with a why and a how to apply), facts many agents
  look up repeatedly, and user rulings. Events stay out because git history
  carries them; live status and who currently holds which role are cluster
  state, not memory.
- **Personal notes**: each agent's own durable state in its workspace
  (`workspaces/<agent_id>/memory/`), maintained by that agent and visible only
  to it. Its `MEMORY.md` is the agent's index. `INHERIT.md` (plus optional
  `inherit/<slug>.md`) is the one personal file passed to descendants the agent
  spawns on the same machine.
- **Vault**: the Git repository that holds the pool (`$MEIVA_HOME/vault`). Notes
  live in its `memory/` subtree; original evidence (rulings, papers, long
  documents) sits in sibling `sources/`, `papers/` and `repos/` directories that
  search does not index. A note cites evidence with an inline Markdown file link,
  such as `[ruling](/sources/rulings.jsonl)`; a leading `/` resolves from the
  Vault root and `/repos/<alias>/...` from a mapped code checkout.

## Note format

A note is a Markdown file about one topic, at most 8000 characters:

```markdown
# Title

One paragraph stating the rule or fact.

Body: details, Why, How to apply. Dates are written as YYYY-MM-DD.
```

Working agents write plain Markdown like this, with no frontmatter and no
write-time validation. During consolidation the arbiter may add YAML frontmatter
with `description` (the one line a search result or index pointer shows),
`verified` and optional `stale_after`, all quoted `YYYY-MM-DD` values. A
`sources` key is not allowed; provenance is inline file links. One canonical
dateless file serves each rule or topic: a changed ruling overwrites the note, so
the pool should not hold dated fragments of the same slot.

Structure: `MEMORY.md` at the pool root is the index (a short `## Setup` of facts
every agent needs, then one pointer line per note, at most 16000 characters). A
directory may carry an optional `index.md` introducing it. A directory holds at
most 20 notes and 20 subdirectories. `machines/<name>/` holds machine-specific
notes.

## Search and indexing

The gateway's memory indexer patrols the consolidated checkout
(`gateway/vault/memory/`), splits notes into chunks by heading, and embeds them.
A search finds the closest notes by vector, then a decision model scores each
candidate's best passage for relevance and orders results by that probability.
Every match is returned with its score, so judge a weak one yourself. Meiva
agents also get the pool index in context each session and matching notes
injected on demand (passive recall); a standalone agent uses
`meiva memory search` instead.

## Who writes and consolidates

- Meiva agents write notes directly into the shared pool or their personal
  folder under the discipline in their system prompt: record corrections,
  durable environment facts and user rulings, search first, update in place.
- **Dream** is the consolidation cycle, with four phases: orientation, gathering
  signal (session transcripts, probes that check notes against code and host
  state, workspace-note candidates), consolidation into canonical one-topic
  notes with frontmatter added and stale notes removed, and pruning plus index
  rebuild. The built-in `memory-arbiter` schedule runs it daily at 04:00 and a
  health check at 07:00; the Memory Arbiter agent leads it.
- The pool validator (`validate.py`, run by the Vault's pre-commit hook and the
  PR check) enforces encoding, frontmatter syntax, size caps, link targets,
  index pointers and the directory limits.

## Sync between machines

By default the Vault syncs through a private Git remote
(`MEIVA_MEMORY_REMOTE`): each machine publishes its changes from an isolated
worktree as a pull request, the arbiter merges validated ones into `main`, and
`meiva memory refresh` brings the gateway checkout up to date. Direct commits to
`main` in the shared checkout are refused. With `MEIVA_MEMORY_KEEP_LOCAL` the
pool has no remote and its notes never leave the machine. Because of this, a
note may take up to about a day to appear on another machine.

## On disk

| Path under `$MEIVA_HOME` | Role |
|---|---|
| `vault/` | authoring Git checkout; `memory/` is the shared pool |
| `gateway/vault/` | consolidated checkout the indexer reads (gateway machine) |
| `workspaces/<agent_id>/memory/` | personal notes |

Plugin documents for deeper detail, under `meiva_builtins/plugins/meiva_memory/`:
`docs/meiva_memory.md` (surface and configuration), `docs/memory-pool-scaffold.md`
(Vault, validator, LFS), `skills/SKILL.md` and `skills/consolidation/SKILL.md`
(note discipline and the Dream procedure).
