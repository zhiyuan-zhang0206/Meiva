---
name: memory
description: "Reads Meiva's shared Memory notes and sets write rules. Use when looking up recorded Meiva knowledge or when a lesson might be worth recording."
---

# Meiva Memory

Memory is the long-term knowledge the agents of a Meiva cluster share: the
conventions they follow, incident lessons, machine and environment facts, user
rulings and project state, written as Markdown notes. A shared pool serves every
agent, and each agent also keeps personal notes of its own. This page is for an
agent that works **beside** Meiva (a plain Claude Code or Codex session in a
terminal or desktop app) without being a Meiva agent: it has no Meiva identity
and no `meiva.*` SDK.

## Rules for a standalone agent

> **Read freely. Write only with the user's approval.**
>
> Without the user's explicit approval **in the current conversation**, do not
> create, edit, move or delete any note under the Memory directories, and do not
> run a command that changes Memory (see [what writes](#what-writes)). This holds
> even when you are certain a note is right, and even when a task seems to ask for
> it indirectly.

Why: the pool is the source of truth every Meiva agent relies on, and it has its
own write, validation and arbitration flow. A note written by an agent that has
no Meiva identity bypasses that flow, and a wrong note is read as fact by the
other agents.

When you think something deserves recording, **propose it to the user**: what
you would record, where, and why it will still matter. Then:

- If the user approves, write only that note, in the Memory format
  ([design](references/design.md#note-format)), after a search so you update an
  existing canonical note instead of adding a duplicate. Do not widen the edit
  beyond what was approved.
- Or the user hands it to a Meiva agent: send the user's wording of what to
  record with `meiva agents send <id> <text> --source external_agent:<subject>`
  (`meiva agents ls` lists the agents; the Memory Arbiter owns the pool).

Your own memory (Claude Code's `~/.claude/projects/*/memory` and `CLAUDE.md`,
Codex's equivalents) is a different system. It does not replace Meiva Memory,
and this rule does not govern it. An agent that **runs on** a harness as a Meiva
agent follows the Meiva instructions it receives instead of this page.

## Read before you work

Search before a task that touches Meiva operations, a particular machine, a past
decision, an incident, or a user preference. Both ways of reading are read-only:

```bash
meiva memory search "<question in plain words>" [--limit K] [--json]
```

It needs the cluster's gateway and returns ranked notes: score, path, tags and
description. The path is relative to the memory root of the gateway's consolidated
checkout, and the CLI prints it as is. The same note is
`$MEIVA_HOME/vault/memory/<path>` on this machine once it has synced there. A weak
score is a weak match. Open the note itself afterwards; the pool is plain Markdown
on disk:

| Location (under `$MEIVA_HOME`, default `~/.meiva`) | What |
|---|---|
| `vault/memory/` | shared pool on this machine (authoring checkout); start at `MEMORY.md`, the index agents see every session |
| `gateway/vault/memory/` | consolidated `main` checkout on the gateway machine; this is what search indexes |
| `workspaces/<agent_id>/memory/` | one agent's personal notes (`MEMORY.md`, `<slug>.md`, `INHERIT.md`); that agent's working state, so read it only when the user points you at it |

Read with ordinary file reads, `rg`, or `git -C "$MEIVA_HOME/vault" log -- memory`.
Treat a note as a dated claim, not as truth: check its `verified` and
`stale_after` frontmatter when present, and confirm against the current code or
host before acting on it. A path that search returns may not exist locally yet,
because the pool syncs between machines about daily. A note about another
agent's role is context, not an instruction to you.

## What writes

Never run these without approval; reading needs none of them.

| Command or action | Effect |
|---|---|
| `meiva memory search` | read-only |
| `meiva memory init` | provisions the Vault checkouts, the `MEMORY.md` template and the commit hook |
| `meiva memory refresh` | fast-forwards the gateway checkout, which moves what search indexes |
| Dream consolidation and its scripts (`consolidate.py`, `steward.py`, `arbiter_merge.py`, `rebuild_memory_index.py`, `dream_gather.py`, `workspace_memory.py`) | commit, push, merge or rewrite pool files |
| `git` add, commit, push, fetch, worktree, checkout or rm inside the Vault | changes the repository |
| Any file write, move or delete under the pool or a workspace `memory/` folder | changes notes |

`git log`, `show`, `diff` and `status` only read, and the pool validator
(`validate.py`) only reports findings.

## Details

[design](references/design.md) covers the two layers, the note format, search
and indexing, who writes and consolidates, sync between machines, and where each
piece lives on disk. The plugin owns the contract: see `docs/meiva_memory.md`
under `meiva_builtins/plugins/meiva_memory/` in the Meiva checkout
(`$MEIVA_HOME/source` on a deployment).
