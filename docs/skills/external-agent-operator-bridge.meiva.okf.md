---
type: doc
title: External-agent operator skill bridge
description: Prod host convergence copies the complete Meiva Guide and impersonator guide into the global skill roots of every supported external agent surface present on the host (Codex, Claude Code, Gemini CLI, Grok, Qwen Code, Cursor Agent, Antigravity App/IDE and CLI, DeepSeek Harness), with externally bound ownership and serialized recovery that preserve conflicts.
tags:
- extensions
- agent-instruction
- lifecycle
---

# External-agent operator skill bridge

`cli/commands/extensions/external_skills.py` is a dedicated host-global
converge step. It publishes two complete skills —
`<repo>/meiva_builtins/skills/platform/meiva-guide/` and
`<repo>/meiva_builtins/skills/platform/impersonator-guide/` — into the
user-level skill directory of every external agent surface the user has.
`_CLIENTS` is the one owner of that set: one row per surface (Codex, Claude Code,
Gemini CLI, Grok, Qwen Code, Cursor Agent, Antigravity App/IDE, Antigravity CLI,
DeepSeek Harness) carrying the surface's home directory, its program name — absent
for a desktop app that has none — and the skills subdirectory it scans. A client
is present when its home directory exists or its program resolves on `PATH`; a
surface with no program is known only by its directory. With neither mark the step
writes nothing and leaves no directory behind. An installed CLI that has never
run has no home yet, so publication creates that home (mode `0700`) together
with `skills/<skill>` instead of waiting for the tool's first run. The layout
each tool documents, with its evidence and unconfirmed items, is
`meiva_builtins/skills/platform/meiva-guide/external-agents/references/supported-clients.md`.
Because the step has `host_global=True`, `converge_host` runs it only from the
default-home prod checkout and skips both `.worktrees/` and `.claude/worktrees/`
checkouts.

For each present client, the target is
`<client-home>/skills/<skill-name>`. Meiva creates a complete staged copy
beside that target. One validated in-memory source snapshot supplies the
manifest, digest, and staged bytes for every present client, so publication
never re-reads a moving Git checkout or mixes source generations. Pathname and
opened-handle metadata are compared by stable file identity across those API
families, while full before/after signatures remain local to each family. A private per-client, per-skill ledger under
`$MEIVA_HOME/configs/external-agent-skills/` records an installation identity,
installed generation and digest, active transaction, exact cleanup records,
and terminal private-retention records;
the target's `.meiva-managed.json` marker must match that external authority. The
digest covers relative paths, entry kinds, file bytes, and file and directory
modes. Each transaction, installed generation, and cleanup record also carries
the expected path manifest. Moving a residue from transaction state to cleanup
state is one atomic ledger write, so no live stage or prior copy loses its
durable pointer. Write-ahead stage-publication and target-claim phases precede
their no-replace renames. Recovery reconciles source and destination presence
against the transaction marker, digest, and manifest; a merely colliding or
otherwise ambiguous generation-shaped path never becomes Meiva-owned.

Each client and skill owns `<client>-<skill>.json` and `.lock`, so the guide is
`<client>-meiva-guide.json` and the executor manual is
`<client>-impersonator-guide.json`. Legacy
operator (`<client>.json`) and deployment (`<client>-deploy-meiva-cluster.json`)
ledgers and their targets are preserved and no longer refreshed. The bridge
never adopts them as the guide or removes their user content. A conflict at the
new guide target remains a preserved conflict under the existing ownership rules.

A per-target cross-process lock serializes convergence. A source change stages
a complete no-follow copy, atomically claims the current target, and verifies
the claimed directory before activation. A mismatch is restored without
overwriting a path that appeared late: claim, activation, and restoration all
use atomic no-replace renames. Activation is recorded before cleanup, so a
cleanup failure leaves the new copy authoritative. Cleanup first records and
atomically moves each residue tree to a quarantine under the private ledger
root. For every file, the ledger stores the source and quarantine relative
paths and durably transitions through `source`, `claiming`, `quarantine`, and
`retained` around its no-replace rename. An interrupted claim never adopts a
destination from its deterministic name or matching content. No supported
portable primitive can unlink the verified inode rather than a mutable
pathname, so cleanup terminally retains the small private tree without unlink,
chmod, or retry. The ledger recovers interrupted staged, claimed,
cleanup-claimed, and activated generations;
temporary and prior directories remain scoped to this exact target.

An existing target without the matching external ledger is unmanaged and is
preserved, even if it contains a copied or fabricated marker. Content or
permission changes are user modifications and are also preserved. Source and
external trees reject symbolic links, junctions, reparse points, and
non-regular or multi-link entries. Cleanup changes no file or directory mode.
The copy model uses ordinary directories and files. Per-client
path, lock, and rename failures are labelled warnings and do not abort core
converge; source-integrity failures remain fatal.

A conflict is per client and per skill: an unmanaged or user-modified copy of one
skill in one home is preserved and reported while every other target still
publishes. Cursor and Grok additionally read other tools' skill directories for
compatibility, so one skill may be visible to them through more than one path;
the copies are identical because a single snapshot produces all of them.

The external projection writes no Meiva install-registry row. The canonical
Meiva Guide package separately enters Meiva's load directory through normal skill
convergence and the core update channel (`meiva packages refresh`); the
`.agents/skills/meiva-guide` compatibility link points to the same owner. The
external copy includes its nested skills, scripts, and references.
Its reading links stay within the published Guide tree, including the single
shared skill evaluation contract at `references/evaluation.md`.
Only the rows in `_CLIENTS` are supported. A tool without a documented
user-level skills directory is not published to — Aider, for example, reads a
`CONVENTIONS.md` the user adds explicitly and has no skill directory — and Meiva
never writes the shared `~/.agents/skills`, which several tools already read.

## Key dependencies

- [[docs/skills/project-local.meiva.okf.md]] — the normal visibility path for the repo skill family
- [[docs/skills/load-directory-sync.meiva.okf.md]] — Meiva's distinct runtime load-directory contract
- `cli/commands/lifecycle/converge_host.py` — prod/default-home and worktree gate
