---
name: consolidation
description: "Consolidates Meiva's shared memory. Use for assigned single-box maintenance or multi-host arbiter/steward work."
---

# Memory pool consolidation (Meiva Dream)

The shared memory pool (`meiva.memory.SHARED_PATH`) is the Vault git repo’s `memory/` directory. Agents write notes into the working tree. Consolidation runs the four-phase **Meiva Dream**
cycle (`Orientation` -> `Gather Signal` -> `Consolidation` -> `Prune & Index`) to keep the pool canonical, committed and searchable.

Index rebuild, validation and refresh use scripts under `../scripts/`, `scripts/`, and the Vault root. `--pool` accepts the notes directory
(`$W/memory` for a Vault worktree), while Git operations use `$W`:

- `python scripts/dream_gather.py [--pool <path>] [--repo <alias>=<path>] [--json]` — Phase 2 (`Gather Signal`) deterministic probe: scan body file links and optional `verified` / `stale_after` frontmatter across pool notes, intersect `/repos/<alias>/<path>` file links against repository file trees and recent `git log` changes, flag expired `stale_after` and aging `verified` notes, list pure-Markdown or legacy notes needing Dream OKF enrichment, scan `workspaces/*/memory/*.md` and `workspaces/*/memory/inherit/*.md` for `## Workspace Memory Candidates`, and surface existing-file pool churn/conflict hotspots
- `python ../scripts/rebuild_memory_index.py` — rebuild the root `MEMORY.md` pointers (resolves the pool from `MEIVA_HOME` and takes no arguments)
- `python3 validate.py` (at the Vault or worktree root) — enforce UTF-8, optional YAML frontmatter syntax (plus rejection of YAML `sources`, inline file-link checks, optional date checks for `verified` and `stale_after`, and unquoted `' #'` / `': '` truncation checks), 8000-character note cap, 20-file / 20-subdirectory Locality limits, `MEMORY.md` pointer targets and coverage, and `<!-- fact-check: ... -->` probes
- `python ../scripts/consolidate.py -m "..."` — remote-backed single-box wrapper: rebuild indexes, stage, commit, push, refresh
- `python ../scripts/steward.py -m "..."` — per-machine branch wrapper: rebuild indexes, stage, commit, push, create PR
- `python ../scripts/arbiter_merge.py` — inspect/merge open PRs whose `validate` check is green + refresh (`--apply` to merge; exit 1 = alert)
- `meiva memory refresh` — fast-forward the gateway checkout to its configured branch; semantic indexing follows on the next indexer patrol

**Worktree + PR discipline (remote-backed multi-host pools):** Each machine's
shared authoring checkout (`~/.meiva/vault`) tracks `main`, as does the gateway's
consolidated checkout (`~/.meiva/gateway/vault`). The pool pre-commit hook blocks
direct commits on `main` and checks size caps and `validate.py`. Make all changes
and index rebuilds in an isolated temporary git worktree branched from
`origin/main`. Point a temporary `MEIVA_HOME` symlink (`$FH/vault -> $W`) at that
worktree for index scripts, land changes through a PR whose `validate` check is
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

### Workspace promotion closed loop
1. Export hosts with `--machine <name> --collect-workspace-candidates`; gather with
   repeatable `--remote-candidates <json_file>` and the persistent `--ledger <path>`.
   `--include-unreviewed` recovers historical notes before a ledger exists.
2. Review `## Scope-Trapped Promotion Candidates` first (P0), verifying evidence.
3. Record with `--record-review <key> --status promoted --promoted-to <pool_rel>`;
   add `--prune-private` on the source host to repair private index links.
   Record private-only notes with `--status local_only`; unchanged hashes are skipped.
See [arbiter workflow](references/arbiter.md#workspace-promotion-closed-loop) for details.
### Phase 2 — Gather Signal
1. **Recent Session Transcripts (`USER_INPUT`)**: Scan recent session transcripts
   for user corrections, rulings, reversals, and explicit decisions in chronological
   order so newer rulings supersede older ones.
2. **World Truth Probes (External Ground-Truth Verification)**: Run
   `python scripts/dream_gather.py --pool "$W/memory"` first to automatically intersect
   every note's inline Markdown file links against mapped repository
   file trees and recent `git log` commits since the note's `verified` date, flag
   expired `stale_after` notes, list `## Workspace Memory Candidates`, and identify
   recent pool churn/conflict hotspots among existing pool files;
   then verify claims in existing notes against observable ground truth across
   multiple domains rather than assuming a single codebase:
   - *Multi-repo codebases & file trees* (e.g. `~/.meiva/source`, project checkouts):
     check `git log`, file path existence, symbol/contract definitions, and deleted
     scripts/queues so notes citing removed paths or retired contracts are caught.
   - *Cluster runtime & database registries* (`agents`, `schedules`, `tasks`,
     machine topology): check active vs. terminated agents, live schedule triggers,
     and task status so notes naming terminated role holders or obsolete
     cron times are caught.
   - *Host environment & embedded probes*: check machine configuration and execute
     `<!-- fact-check: <cmd> -->` directives.
3. **Per-Agent Workspace Memory (`~/.meiva/workspaces/*/memory/`)**: Review the
   `## Workspace Memory Candidates` section reported by `dream_gather.py`
   (which deterministically scans `workspaces/*/memory/*.md` and
   `workspaces/*/memory/inherit/*.md` with non-empty content, using
   hash-ledger review state across all ages) alongside any legacy `MEMORY.md` remnants to extract durable,
   reusable cross-agent rules and facts into the shared pool.

### Phase 3 — Consolidation (Canonical One-Topic-One-File, State Overwrite & Auto-OKF)
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
- **Dream-Time Auto-OKF Maintenance**: While working agents write pure Markdown
  without YAML frontmatter or write-time validation, the Arbiter populates and refreshes canonical
  OKF provenance & lifecycle fields on consolidated canonical notes (and strips
  retired `type`, `meiva_agent`, `meiva_machine`, and `tags` boilerplate):
  - `description`: One-line high-density summary for `MEMORY.md` pointers and previews.
  - Inline Markdown file links in the body: provenance for subsequent Dream runs.
    `/repos/meiva/<file>` maps to `~/.meiva/source`; `/repos/monsora/<file>`
    maps to `~/MonsoraV2` when present, with Vault `repos/<alias>` as fallback.
    Global manifests (`pyproject.toml`, `meizel.toml`, `meizel.policy.toml`,
    `.pre-commit-config.yaml`) retain their drift-noise handling.
  - `verified`: Quoted `"YYYY-MM-DD"` date when the note was verified against World Truth.
  - `stale_after`: Optional quoted `"YYYY-MM-DD"` expiration date for temporary
    freezes or transition rules.
- **De-relativize time**: Convert relative time expressions (`"today"`,
  `"this morning"`, `"recently"`, `"next week"`) into explicit ISO dates
  (`YYYY-MM-DD`).

### Phase 4 — Prune & Index
- Keep `MEMORY.md` zero-conflict, self-consistent, and concise as a routing index:
  inline only current standing rules and pointers to canonical notes, and remove
  superseded timeline entries or broken pointers.
- Run `rebuild_memory_index.py` + `validate.py`, commit and
  land via PR (or local commit on a keep-local single box), clean up any temporary
  worktree, and run `meiva memory refresh` to synchronize the gateway checkout.
  The semantic search index catches up on the next indexer patrol; verify search
  after that pass.

Source syntax and validation follow [Vault paths and source references](../SKILL.md#vault-paths-and-source-references).

## Repo layout (write and merge in the right place)
- Topic folders under `memory/` (`people/`, `lessons/`, `prefs/`, ...) are shared.
- `machines/<name>/` holds notes specific to one machine (multi-host clusters).
- `memory/MEMORY.md` is the pool's root index. It is **injected into every agent's context
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

The Vault is the Git repository at `$MEIVA_HOME/vault`; shared notes live in
its `memory/` subtree. Store original evidence beside that subtree, for example
in `sources/` or `papers/`, and leave a searchable pointer note in memory:

```markdown
# <topic>

Pointer to Vault content for <topic>.

[Original evidence](/sources/<filename>)
```

The pointer note carries the topic and the Vault path so semantic search can
still find it.

### Note Format & Directory Structure Rules (Two-Stage OKF Discipline)

1. **Stage 1 — Write-Time Pure Markdown (Working Agents, Zero Frontmatter Burden)**:
   - Working agents write notes directly as pure Markdown: start with `# Title`,
     followed by a short high-level description paragraph stating the rule or fact,
     then the body:
     ```markdown
     # <Title>

     <One-paragraph high-level description of the rule or fact.>

     <Body with details, Why, and How to apply...>
     ```
   - YAML frontmatter (`--- ... ---`) is completely optional at write time. Never
     write legacy boilerplate (`type: Memory`, `meiva_agent`, `meiva_machine`,
     `timestamp`, `tags: [type/...]`) or inline HTML attribution comments.
2. **Stage 2 — Dream Consolidation Auto-OKF (Memory Arbiter)**:
   - During Dream Phase 3/4, the Memory Arbiter populates and refreshes
     canonical OKF provenance and lifecycle fields on canonical notes:
     ```yaml
     ---
     description: "<One-line summary distilled from the opening paragraph>"
     verified: "2026-11-09"
     stale_after: "2026-12-01"   # optional: only for temporary rules
     ---
     ```
   - When optional YAML frontmatter is present, `validate.py` checks that it is a
     valid YAML mapping without unquoted `' #'` or `': '` truncation, and lightly
     validates `verified` / `stale_after` (quoted `YYYY-MM-DD` date strings) if
     present. A `sources` key is forbidden, including null or empty values. Put
     source links in the body, for example `[verification script](/repos/meiva/scripts/hooks/diff_checks.py)`.
   - Every note must be valid UTF-8 and stay within `8000` characters.
3. **Optional Directory `index.md` (High-Level Description)**:
   - `index.md` is an optional folder-level high-level description, not a
     mandatory generated inventory of every file pointer and recursive count.
   - Adding or deleting a note does not require running `gen_indexes.py` to
     cascade-rewrite ancestor `index.md` files.
4. **Directory Locality Limits (`MAX_FILES_PER_DIR = 20`, `MAX_SUBDIRS_PER_DIR = 20`)**:
   - Every directory holds at most **20 `.md` notes** and at most **20 subdirectories**
     (enforced by `validate_structure` in `validate.py`). When a directory exceeds
     either cap, split it into topical subdirectories.
5. **Root `MEMORY.md` Pointers**:
   - `validate_pointers` checks that every pointer target in `MEMORY.md` exists,
     has no duplicate pointer lines, and covers every root-level note and top-level
     directory with notes (link text does not need to match any frontmatter `title`).

#### Identity safety when reading

Another agent's role or personal note is context, not your identity:
- Compare any agent ID mentioned in a note against `meiva.self.AGENT_ID`. Do not
  adopt another agent's role, label, or tasks as your own.
- `MEMORY.md` is injected into every agent's context as the standing memory index.
  Role labels mentioned in `MEMORY.md` describe other agents' roles, not yours.
### Threshold triggers

To prevent the pool from drifting past the consolidation window:

- A diff watcher polls every 5 minutes, checking for ≥ 200 lines of unstaged
  changes in the pool.
- When the threshold is exceeded, the watcher wakes the Arbiter for immediate
  consolidation (outside the daily schedule).
- This runs alongside the daily 04:00 cron consolidation — the threshold
  trigger is a safety net, not a replacement.

## First-time setup (bootstrap, idempotent)

Explicit `meiva memory init` initializes the Vault repo, lays down
`memory/MEMORY.md` and the root `.githooks/pre-commit` cap guard, and arms
`core.hooksPath`. Idempotent
and non-destructive: it never overwrites a file the pool already has, so a
curated index survives.

The one thing it cannot do for you is the remote:

- Ensure `origin` points at the private GitHub remote:
  `gh repo create <owner>/MeivaVault --private` then `git -C VAULT remote add origin <url>`.

Paths and remote used below:
- **VAULT** = the Git repository root (`~/.meiva/vault`).
- **POOL** = the notes path, `meiva.memory.SHARED_PATH` (`~/.meiva/vault/memory`).
- **REPO** = the pool's GitHub `owner/name`. Get it once:
  `git -C VAULT remote get-url origin` then take `owner/name` from the
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

Source targets may use any file format, including Markdown, PDF, HTML, TXT,
JSONL, Python, and TypeScript. Archive external content or a dated user ruling
in a Vault evidence file, then cite that file from the note body. Only the
`memory/` subtree enters recursive note collection, indexing, and validation.
