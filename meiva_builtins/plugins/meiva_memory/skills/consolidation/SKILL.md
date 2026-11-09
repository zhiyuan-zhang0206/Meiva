---
name: consolidation
description: "Consolidates Meiva's shared memory. Use for assigned single-box maintenance or multi-host arbiter/steward work."
---

# Memory pool consolidation (Meiva Dream)

The shared memory pool (`meiva.memory.PATH`) is a git repo. During the day, agents
write notes into the working tree. Consolidation runs the four-phase **Meiva Dream**
cycle (`Orientation` -> `Gather Signal` -> `Consolidation` -> `Prune & Index`) so
the pool stays canonical, conflict-free, committed, pushed, and searchable.

All index-rebuild / validation / refresh operations are wrapped in scripts under
`../scripts/` and the pool root:
- `python ../scripts/gen_indexes.py` and `python ../scripts/rebuild_memory_index.py` — rebuild every directory `index.md` and the root `MEMORY.md` pointers (resolve the pool from `MEIVA_HOME` and take no arguments)
- `python3 validate.py` (in the pool or worktree root) — enforce frontmatter, type tag, description, character caps, directory limits, pointer integrity, and `<!-- fact-check: ... -->` probes
- `python ../scripts/consolidate.py -m "..."` — remote-backed single-box wrapper: rebuild indexes, stage, commit, push, refresh
- `python ../scripts/steward.py -m "..."` — per-machine branch wrapper: rebuild indexes, stage, commit, push, create PR
- `python ../scripts/arbiter_merge.py` — inspect/merge open PRs whose `validate` check is green + refresh (`--apply` to merge; exit 1 = alert)
- `meiva memory refresh` — fast-forward the gateway checkout to `origin/main` and trigger semantic index re-embedding

**Worktree + PR discipline (remote-backed multi-host pools):** Never commit
directly on the shared checkout's `main` (`~/.meiva/memory` pre-commit hook refuses
direct commits on `main`, and switching the shared checkout onto a `machine-<name>`
branch is retired). Always create an isolated temporary git worktree from
`origin/main` (`git -C ~/.meiva/memory worktree add -q -B <branch> $W origin/main`),
point a temporary `MEIVA_HOME` symlink (`$FH/memory -> $W`) when running index
scripts against the worktree, open and squash-merge the PR once `validate` is
green, and remove/prune the temporary worktree immediately afterwards.

**Which procedure applies depends on the deployment.** On a single box (multihost
off, the default — `meiva.agents.spawn` has no machine argument there) follow
"Single box" below and ignore the arbiter/steward roles entirely. On a multi-host
cluster, your spawn prompt tells you which role you are: **arbiter** (one, on the
always-on machine, holds the daily schedule and orchestrates; this is the role the
Memory Arbiter agent plays) or **per-machine steward** (one per machine,
spawned by the arbiter or resident as a local scheduler).

**Triggers — fire early and often** (a missed trigger leaves notes unsearchable;
an extra one only costs a cheap commit):
1. daily `04:00` full Meiva Dream consolidation (`memory-arbiter` schedule) and `07:00` daily health check;
2. per-machine watch every 1–5 minutes: >10 memory files changed since last
   sync, or pending diff >200 lines, or uncommitted changes >30 min old →
   publish a branch PR from an isolated worktree and notify the arbiter;
3. arbiter merge watch: merge ready PRs whose `validate` check is green as they arrive.

## The Four-Phase Meiva Dream Workflow

Every full consolidation executes four phases in order inside the target worktree
(or local checkout on a local-only single box):

### Phase 1 — Orientation
- Scan `MEMORY.md` and directory `index.md` files across the pool to map existing
  topic directories, canonical filenames, and current size/entry budgets (`MEMORY.md`
  ≤ 16000 chars, note ≤ 8000 chars, ≤ 20 `.md` notes and ≤ 20 subdirectories per
  directory).

### Phase 2 — Gather Signal (Three Incremental Signal Sources)
1. **Recent Session Transcripts (`USER_INPUT`)**: Scan recent session transcripts
   for user corrections, rulings, reversals, and explicit decisions in chronological
   order so newer rulings supersede older ones.
2. **World Truth Probes (External Ground-Truth Verification)**: Verify claims in
   existing notes against observable ground truth across multiple domains rather
   than assuming a single codebase:
   - *Multi-repo codebases & file trees* (e.g. `~/.meiva/source`, project checkouts):
     check `git log`, file path existence, symbol/contract definitions, and deleted
     scripts/queues so notes citing removed paths or retired contracts are caught.
   - *Cluster runtime & database registries* (`agents`, `schedules`, `tasks`,
     machine topology): check active vs. terminated agents, live schedule triggers,
     and task status so notes naming terminated role holders or obsolete cron times
     are caught.
   - *Host environment & embedded probes*: check machine configuration and execute
     `<!-- fact-check: <cmd> -->` directives.
3. **Per-Agent Workspace Memory (`~/.meiva/workspaces/*/memory/`)**: Scan agent
   workspace memory directories (and legacy `MEMORY.md` remnants) to extract
   durable, reusable cross-agent rules and facts into the shared pool.

### Phase 3 — Consolidation (Canonical One-Topic-One-File & State Overwrite)
- **One topic, one canonical file (dateless naming)**: Maintain a single dateless
  canonical note per rule or topic slot (e.g. `meiva/rulings/merge/meiva-merge-policy.md`,
  or root standing files like `collaboration-preferences.md`), rather than
  accumulating date-suffixed fragments (`*-2026xxxx.md`).
- **State overwrite in place & `git rm` superseded fragments**: When a ruling is
  revised, amended, or overturned, or when World Truth Probes reveal drift,
  overwrite the canonical note in place with the current truth and `git rm`
  superseded, reversed, or fragmented historical notes (git history preserves the
  audit trail). Never leave conflicting old notes in the pool or stack chronological
  patch disclaimers at the bottom of stale notes.
- **De-relativize time**: Convert relative time expressions (`"today"`,
  `"this morning"`, `"recently"`, `"next week"`) into explicit ISO dates
  (`YYYY-MM-DD`).

### Phase 4 — Prune & Index
- Keep `MEMORY.md` zero-conflict, self-consistent, and concise as a routing index:
  inline only current standing rules and pointers to canonical notes, and remove
  superseded timeline entries or broken pointers.
- Run `gen_indexes.py` + `rebuild_memory_index.py` + `validate.py`, commit and
  land via PR (or local commit on a keep-local single box), clean up any temporary
  worktree, and run `meiva memory refresh` to synchronize the gateway checkout and
  semantic search index.

## Repo layout (write and merge in the right place)
- Topic folders at the root (`people/`, `lessons/`, `prefs/`, ...) are shared.
- `machines/<name>/` holds notes specific to one machine (multi-host clusters).
- `MEMORY.md` is the pool's root. It is **injected into every agent's context
  each session**, so it is the one file that is always read. Keep it a small
  **curated index**: inline the most-used durable facts and short topic
  descriptions, point to the rest. A commit hook rejects it past **16000
  characters** (~a 200-line index, the Claude Code auto-memory scale) — that
  is your cue to move detail into a pointed-to note.

## Content Discipline

The memory pool is a curated, lightweight knowledge store. Not everything
belongs here — bulk content has a separate home in the user's Vault.

### What goes in the memory pool

- Fact summaries, decision records, and user preferences
- Cross-reference pointers (to other notes, to Vault files)
- Each note ≤ 8000 chars (enforced by the pre-commit hook)
- Content optimized for semantic search: short, self-contained, well-titled

### User-dimension notes are standing objects

`user-profile.md`, `user-preference-rules-v2.md`, `collaboration-preferences.md`
and `user-core-principle.md` are the canonical record of the **user dimension**
— repeatedly expressed preferences, recurring habits, corrected behaviors,
what the user values. They are maintained **in place**, not appended to:
a confirmed preference is folded into the canonical note, and an overlapping
new note is merged in during consolidation rather than left as a duplicate.
A duplicate written by two agents is a signal the canonical note is not
discoverable enough — fix the canonical note, don't just delete the newcomer.
(Full heuristic: `../reference/user-dimension.md` beside the root
`meiva.skills.meiva_memory` skill.)

### What does NOT go in memory — move to Vault

Large or opaque content that degrades search quality and bloats the pool:

- Handoff docs and long conversation transcripts
- Deployment logs and build output
- Bulk content exceeding ~8000 chars
- Binary files (PDFs, images, video, audio)

### Vault path convention

The Vault is a configured archive location (e.g. `~/Google Drive/My Drive/` in
the reference deployment). When a file is too large for the pool, move it to
the Vault and leave a pointer note in memory:

```
---
type: Memory
title: <topic>
description: Pointer to Vault content
meiva_agent: "<id>"
authors: ["#<id>"]
timestamp: YYYY-MM-DDTHH:MM
meiva_machine: <machine>
---
<!-- agent-<id> @ <machine>, YYYY-MM-DD HH:MM -->
# <topic>
Vault: <vault-path>/<path>
```

The pointer note carries the topic and the Vault path so semantic search can
still find it.

### Frontmatter fields

Every memory note MUST have a YAML frontmatter with at minimum `type` and
`meiva_agent`. These fields have defined semantics:

| Field | Meaning | When to set |
|------|------|--------|
| `type` | Note type, usually `Memory` | When created (required) |
| `tags` | Must carry **exactly one** `type/<x>` — see below | When created (required) |
| `meiva_agent` | The note's **owner** (who owns this record) | Set when created, does not change with edits |
| `authors` | The note's **editor list** (all agent IDs that contributed content) | Append `"#<your-id>"` on each edit |
| `title` | Note title | When created |
| `description` | One-line description | When created |
| `tags` | Free tag list | When created, can append |
| `timestamp` | ISO 8601 timestamp | Updated when created/edited |
| `meiva_machine` | Machine name where edited | When created/edited |

**`meiva_agent` vs `authors`:**
- `meiva_agent` is the note's **owner**—who created this record. Unchanged after creation.
- `authors` is the note's **editor list**—all agents that contributed content. Appended on each edit.

**The `type/<x>` tag** says how the note is meant to be *used*, which the
ownership fields (`meiva_agent` / `authors`) cannot say. Exactly one of:

| Tag | Holds |
|---|---|
| `type/user` | who the user is — role, expertise, preferences |
| `type/feedback` | how an agent should work, with the reason |
| `type/project` | ongoing work, goals, constraints |
| `type/reference` | pointers to external resources |
| `type/env` | machine and cluster facts |
| `type/role` | an agent's role and its boundaries |

Other tags stay free-form and are unaffected; the graph view groups on the first
non-`type/` tag. `python3 validate.py` enforces the "exactly one" rule.

**Editing rules:** When you edit any memory note, append your agent ID (format `"#<id>"`) to the `authors` list in the frontmatter. If the file does not yet have an `authors` field, create it. Do not add the same agent twice.

`authors` uses YAML array format: `authors: ["#238", "#779"]`

#### Identity safety when reading

Every agent must verify a note's ownership before acting on it:

1. **Read the frontmatter.** `meiva_agent` tells you whose note it is. A number
   like `238` means it belongs to that agent; `all` means it is a shared note
   that every agent should read as context, not identity.
2. **Compare with your own id.** `meiva.self.AGENT_ID` is your own agent id. If
   the note's `meiva_agent` is a different number, this is another agent's
   personal note — read it for context, but do not adopt its role, label, or
   tasks as your own.
3. **Check for HTML attribution comments.** Personal agent notes should carry
   an HTML comment like
   `<!-- ⚠️ This is agent #238's personal note. If you are not #238, this is someone else's identity, not yours. -->`. If you see this and the id does
   not match yours, the note is not about you.
4. **MEMORY.md is special.** `MEMORY.md` is injected into every agent's context
   as the standing memory index. Its `meiva_agent` is `all` — it is a shared user
   profile, not any single agent's identity. Its content (user facts, watchers,
   appointments) applies to all agents, but its role labels (e.g. "Agent #238 —
   Health Manager") describe other agents' roles, not yours.

**Example of the bug this prevents:** Agent #774 read `MEMORY.md` (which at the
time had `meiva_agent: 238`) and set its own label to "Health Manager", mistakenly
adopting another agent's identity. A quick `meiva_self.AGENT_ID` check against the
frontmatter would have caught this.
### Threshold triggers

To prevent the pool from drifting past the consolidation window:

- A diff watcher polls every 5 minutes, checking for ≥ 200 lines of unstaged
  changes in the pool.
- When the threshold is exceeded, the watcher wakes the Arbiter for immediate
  consolidation (outside the daily schedule).
- This runs alongside the daily 04:00 cron consolidation — the threshold
  trigger is a safety net, not a replacement.

## First-time setup (bootstrap, idempotent)

Pool setup is handled by `meiva converge` (which `meiva start` and `meiva update` both
run): it initializes the repo, lays the template down inside it — `MEMORY.md`
and the `.githooks/pre-commit` cap guard — and arms `core.hooksPath`. Idempotent
and non-destructive: it never overwrites a file the pool already has, so a
curated index survives.

The one thing it cannot do for you is the remote:

- Ensure `origin` points at the private GitHub remote:
  `gh repo create <owner>/MeivaMemory --private` then `git -C POOL remote add origin <url>`.

Two values used below:
- **POOL** = the local pool path, `meiva.memory.PATH` (`~/.meiva/memory`).
- **REPO** = the pool's GitHub `owner/name`. Get it once:
  `git -C POOL remote get-url origin` then take `owner/name` from the
  `git@github.com:owner/name.git` form.

## Single box

On a single-box deployment, read [single-box consolidation](references/single-box.md).
Keep-local mode stays local; ignore multi-host roles.

## Arbiter (multi-host only)

Only when assigned the multi-host arbiter role, read [arbiter](references/arbiter.md).
The arbiter owns cross-machine merges and post-merge search refresh.

## Steward (multi-host only)

Only when assigned the per-machine steward role, read [steward](references/steward.md).
Publish the local branch and report readiness; never merge other machine PRs.
