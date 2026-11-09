# Multi-host memory arbiter (Meiva Dream)

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Arbiter (multi-host only)

You orchestrate the daily **Meiva Dream** consolidation and cross-machine PR merges.
The built-in `memory-arbiter` schedule (`schedules/memory-steward-schedule.py`) wakes
you daily at `04:00` for full Dream consolidation and at `07:00` for the health check.
You are also woken immediately whenever the user or an agent asks you to consolidate.

**Worktree + PR rule:** Never commit directly on the shared checkout's `main`
(`~/.meiva/memory` pre-commit hook refuses direct commits on `main`, and switching
the shared checkout onto a `machine-<name>` branch is retired). Perform all
consolidation edits and index rebuilds inside an isolated temporary git worktree
branched from `origin/main`, land them via a PR whose `validate` check is green,
and remove/prune the temporary worktree immediately afterwards.

**Each time you are woken to consolidate (Meiva Dream 4-Phase Workflow):**

1. **Create an isolated worktree and fake `MEIVA_HOME`:**
   ```bash
   W=$(mktemp -d)/pool
   FH=$(dirname "$W")/fh
   git -C ~/.meiva/memory fetch origin main
   git -C ~/.meiva/memory worktree add -q -B "mem/dream-$(date +%m%d-%H%M)" "$W" origin/main
   mkdir -p "$FH"
   ln -sfn "$W" "$FH/memory"
   cd "$W"
   ```
   (`rebuild_memory_index.py` resolves the pool from `MEIVA_HOME` and takes no
   arguments; the fake home points it at `$W`.)

2. **Phase 1 — Orientation:**
   - Scan `MEMORY.md` and directory `index.md` files in `$W` to map existing
     topic directories, canonical filenames, and size/entry counts (`MEMORY.md`
     ≤ 16000 chars, note ≤ 8000 chars, ≤ 20 `.md` notes and ≤ 20 subdirectories
     per directory).
   - If other machines have pending local notes, either collect their open PRs or
     spawn per-machine stewards to open worktree PRs against `main`.

3. **Phase 2 — Gather Signal (Three Incremental Sources):**
   - **Recent Session Transcripts (`USER_INPUT`)**: Scan recent session transcripts
     for user corrections, rulings, reversals, and explicit decisions, ordered by
     timestamp so newer rulings supersede older ones.
   - **World Truth Probes (External Ground-Truth Verification)**: Verify claims in
     existing notes against observable ground truth across multiple domains:
     - *Multi-repo codebases & file trees* (`~/.meiva/source`, project checkouts):
       check `git log`, path/symbol existence, and retired contracts or scripts.
     - *Cluster runtime & database registries* (`agents`, `schedules`, `tasks`,
       machine topology): check active vs. terminated agents, live schedule triggers,
       and task status.
     - *Host environment & embedded probes*: check machine configuration and
       `<!-- fact-check: <cmd> -->` directives.
   - **Per-Agent Workspace Memory (`~/.meiva/workspaces/*/memory/`)**: Scan agent
     workspace memory directories (and legacy `MEMORY.md` files) to harvest
     durable, reusable cross-agent rules and facts into the shared pool.

4. **Phase 3 — Consolidation (Canonical One-Topic-One-File & State Overwrite):**
   - **One topic, one canonical file**: Keep a single dateless canonical note per
     rule or topic slot (e.g. `meiva/rulings/merge/meiva-merge-policy.md`, or root
     standing files like `collaboration-preferences.md`), rather than accumulating
     date-suffixed fragments (`*-2026xxxx.md`).
   - **State overwrite in place & `git rm` superseded fragments**: When a ruling is
     revised, amended, or overturned, or when World Truth Probes show drift, update
     the canonical note in place with the current truth and `git rm` superseded,
     reversed, or fragmented historical notes. Do not leave conflicting old notes
     or append chronological patch disclaimers at the bottom of stale notes.
   - **De-relativize time**: Convert relative time words (`"today"`, `"yesterday"`,
     `"this morning"`) into explicit ISO dates (`YYYY-MM-DD`).

5. **Phase 4 — Prune & Index:**
   - Keep `MEMORY.md` conflict-free, self-consistent, and concise under the
     16000-char cap (preserve `## Setup`, inline only current standing rules and
     canonical pointers, remove superseded timeline entries).
   - Rebuild the root index and validate inside the worktree:
     ```bash
     MEIVA_HOME="$FH" ~/.meiva/source/.venv/bin/python ~/.meiva/source/meiva_builtins/plugins/meiva_memory/skills/scripts/rebuild_memory_index.py
     python3 validate.py
     ```
   - Commit, push the worktree branch, open a PR targeting `main`, and squash-merge
     it once the `validate` check is green.
   - Clean up the temporary worktree so the registry does not grow:
     ```bash
     git -C ~/.meiva/memory worktree remove --force "$W"
     git -C ~/.meiva/memory worktree prune
     rm -rf "$(dirname "$W")"
     ```
   - Merge any remaining open review PRs whose `validate` check is green and whose
     author is not the merger, writing the collector snapshot (each face's HEAD +
     dirty count) into the PR body first (`python ../scripts/arbiter_merge.py --apply`).
   - Refresh the gateway checkout and semantic search index:
     ```bash
     meiva memory refresh
     ```
     Treat a non-zero exit as an alert and report it rather than moving on silently.
