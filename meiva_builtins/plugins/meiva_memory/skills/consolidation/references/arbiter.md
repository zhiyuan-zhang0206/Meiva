# Multi-host memory arbiter (Meiva Dream)

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Arbiter (multi-host only)

You orchestrate the daily **Meiva Dream** consolidation and cross-machine PR merges.
The built-in `memory-arbiter` schedule (`schedules/memory-steward-schedule.py`) wakes
you daily at `04:00` for full Dream consolidation and at `07:00` for the health check.
You are also woken immediately whenever the user or an agent asks you to consolidate.

**Worktree + PR rule:** Never commit consolidation work on the shared checkouts
(`~/.meiva/vault` is on this machine's `machine-<name>` branch, or `main` on a
gateway-only unit; `~/.meiva/gateway/vault` is on `main`). The pool pre-commit
hook checks size caps and runs `validate.py` but does not refuse commits by
branch, so this is a procedure. Perform all
consolidation edits and index rebuilds inside an isolated temporary git worktree
branched from `origin/main`, land them via a PR whose `validate` check is green,
and remove/prune the temporary worktree immediately afterwards.

**Each time you are woken to consolidate (Meiva Dream 4-Phase Workflow):**

1. **Create an isolated worktree and fake `MEIVA_HOME`:**
   ```bash
   W=$(mktemp -d)/pool
   FH=$(dirname "$W")/fh
   git -C ~/.meiva/vault fetch origin main
   git -C ~/.meiva/vault worktree add -q -B "mem/dream-$(date +%m%d-%H%M)" "$W" origin/main
   mkdir -p "$FH"
   ln -sfn "$W" "$FH/vault"
   cd "$W"
   ```
   (`rebuild_memory_index.py` resolves the pool from `MEIVA_HOME` and takes no
   arguments; the fake home points it at `$W`.)

2. **Phase 1 — Orientation:**
   - Scan `memory/MEMORY.md` and directory `index.md` files in `$W/memory` to map existing
     topic directories, canonical filenames, and size/entry counts (`MEMORY.md`
     ≤ 16000 chars, note ≤ 8000 chars, ≤ 20 `.md` notes and ≤ 20 subdirectories
     per directory).
   - If other machines have pending local notes, either collect their open PRs or
     spawn per-machine stewards to open worktree PRs against `main`.

### Workspace promotion closed loop

1. **Ledger-backed cross-machine gather.** On each participating machine
   (`company-mini`, `ubuntu`, `company-air`, `macbook-air`, `macmini`), run
   `dream_gather.py --machine <name> --collect-workspace-candidates` and save its
   JSON list. Transfer exports to the arbiter, then repeat
   `--remote-candidates <json_file>` when gathering against the shared pool.
   Use a consistent `--machine` name for local scans. The default review ledger
   is `<vault>/sources/dream/workspace-memory-ledger.json`; `--ledger <path>`
   selects another ledger. `--include-unreviewed` includes historical notes
   even before a ledger exists. Unseen hashes and changed hashes need review;
   unchanged reviewed notes are skipped and counted by `skipped_reviewed_count`.
   Keep the ledger with the Vault across runs. The lookback remains useful for
   repository drift and churn; it must not hide never-reviewed old notes.
2. **P0 Scope-Trap promotion.** Review `## Scope-Trapped Promotion Candidates`
   first. Deterministic topic overlap across different agents, or matching
   cross-agent demand with low shared-pool coverage, signals knowledge trapped
   in private scope. Supply `--user-demand-jsonl <path>` when demand evidence is
   available. Each JSONL object has `text` plus an `agent_id` (string or integer),
   or `kind` set to `user-preference` or `platform-rule`. Similarity uses token
   Jaccard with exact multiword headings weighted to 0.8; shared-pool coverage
   below 0.45 allows promotion triage. Verify the source notes and current evidence before promoting;
   lexical similarity is a triage signal. Consolidate durable user preferences,
   platform rules and reusable facts into the existing canonical topic whenever
   possible, preserving source evidence.
3. **Record and prune after promotion.** After the canonical shared note is
   available, record each reviewed source with `--record-review <key> --status
   promoted --promoted-to <pool_rel_path>`. Record private-only content with
   `--status local_only`. Keys identify `<machine>:<agent_id>/<rel_path>`;
   legacy unprefixed keys are accepted. For promoted local notes, add
   `--prune-private` to remove redundant content or retain a pointer when
   referenced, and repair the source agent's `memory/MEMORY.md` link to
   `~/.meiva/vault/memory/<promoted_to>`. Perform remote private-file cleanup on
   the source machine with `--machine <source_name>` and the same review ledger. The ledger retains the
   reviewed SHA-256, status, shared destination and review date, closing the
   loop so unchanged notes do not recur nightly.

3. **Phase 2 — Gather Signal (Three Incremental Sources):**
   - Run the deterministic Phase 2 probe helper first against the worktree:
     ```bash
     ~/.meiva/source/.venv/bin/python ~/.meiva/source/meiva_builtins/plugins/meiva_memory/skills/consolidation/scripts/dream_gather.py --pool "$W/memory"
     ```
     (`dream_gather.py` scans inline file links and optional `verified` / `stale_after` across
     notes, intersects `/repos/<alias>/<path>` file links with repository file trees and
     recent `git log` commits after `verified`, flags expired `stale_after` and
     aging `verified` notes, lists pure-Markdown or legacy notes needing Dream OKF
     headers, and surfaces recent pool churn/conflict hotspots.)
   - **Recent Session Transcripts (`USER_INPUT`)**: Scan recent session transcripts
     for user corrections, rulings, reversals, and explicit decisions, ordered by
     timestamp so newer rulings supersede older ones.
   - **World Truth Probes (External Ground-Truth Verification)**: Verify claims in
     existing notes against observable ground truth across multiple domains:
     - *Multi-repo codebases & file trees* (`~/.meiva/source`, project checkouts):
       inspect every `missing_path` and `git_changed` item reported by
       `dream_gather.py`, plus symbol/contract existence and retired scripts.
     - *Cluster runtime & database registries* (`agents`, `schedules`, `tasks`,
       machine topology): check active vs. terminated agents, live schedule triggers,
       and task status.
     - *Host environment & embedded probes*: check machine configuration and
       `<!-- fact-check: <cmd> -->` directives.
   - **Per-Agent Workspace Memory (`~/.meiva/workspaces/*/memory/`)**: Scan agent
     workspace memory directories (and legacy `MEMORY.md` files) to harvest
     durable, reusable cross-agent rules and facts into the shared pool.

4. **Phase 3 — Consolidation (Canonical One-Topic-One-File, State Overwrite & Auto-OKF):**
   - **One topic, one canonical file**: Keep a single dateless canonical note per
     rule or topic slot (e.g. `meiva/rulings/merge/meiva-merge-policy.md`, or root
     standing files like `collaboration-preferences.md`), rather than accumulating
     date-suffixed fragments (`*-2026xxxx.md`).
   - **State overwrite in place & `git rm` superseded fragments**: When a ruling is
     revised, amended, or overturned, or when World Truth Probes show drift, update
     the canonical note in place with the current truth and `git rm` superseded,
     reversed, or fragmented historical notes. Do not leave conflicting old notes
     or append chronological patch disclaimers at the bottom of stale notes.
   - **Two-Stage OKF Maintenance (Write-Time Pure Markdown -> Dream Auto-OKF)**:
     Working agents write pure Markdown without YAML frontmatter. During Dream
     consolidation, populate or refresh the canonical OKF fields on canonical
     notes (and strip legacy `type`, `meiva_agent`, `meiva_machine`, `tags`
     boilerplate):
     - `description`: One-line high-density summary for `MEMORY.md` pointers and previews.
     - Inline Markdown file links in the body: bound source files for drift detection.
     - `verified`: Quoted `"YYYY-MM-DD"` date of this World Truth verification.
     - `stale_after`: Optional quoted `"YYYY-MM-DD"` expiration date for temporary rules.
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
     git -C ~/.meiva/vault worktree remove --force "$W"
     git -C ~/.meiva/vault worktree prune
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
