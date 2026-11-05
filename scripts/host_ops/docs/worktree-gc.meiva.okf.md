---
type: doc
title: Worktree reclaimer
description: Delete merged, clean, cool-off-passed worktrees while the volume is low — the gate, the moment-of-deletion re-check and the three scope rules that keep it from deleting live work.
tags: [scripts, operations, disk, git]
---

# Worktree reclaimer

`scripts/host_ops/worktree_gc` reclaims a checkout's merged, clean, aged worktrees
and nothing else. It exists because the disk fell 14.4 GiB in ten hours on
2026-10-23 while ~2.1 GiB worktrees accumulated and nothing reclaimed them until
a human noticed: the judgement already existed in a probe another agent runs, and
what was missing was something that acts on it, at night, without a person.

```bash
python -m scripts.host_ops.worktree_gc --dry-run          # say what would go
python -m scripts.host_ops.worktree_gc --root ~/.meiva/source
python -m scripts.host_ops.worktree_gc --report-hours 24  # the skip-reason tally
```

Every path and threshold is a flag with a `WORKTREE_GC_*` fallback and a default
equal to the operator's own home, so one copy runs against a live volume and
another against a fixture. That is not a testing convenience: it is what makes
each judgement below answerable by a test.

## The four brakes

**The disk gate.** Two independent readings of one fact, either of which opens
it: fewer than 30 GiB free, or the volume at or above 85 % used. Above both, a
round reclaims nothing and says so.

**The look/act split.** The judge — the probe, the blind check, the tree list and
the surface check — runs on every round whatever the gate says. A judge that went
blind must be discovered while the volume is comfortable, not on the night the
disk is full. Only the reclaim path (one `gh` and one `du` per tree, then the
deletion itself) waits for the gate. Someone reading the code later will ask why
the probe runs with the gate closed; this is why.

**The moment-of-deletion re-check.** The probe's verdict is a snapshot. Between
that snapshot and `git worktree remove` a tree can become dirty, gain a live
process, gain a commit, or its PR can stop being merged — 2026-10-20 showed two
trees vanishing under a waiter. Re-deriving the whole table is right for a human
checking one tree and too heavy for a loop that has already swept, so the gate
re-reads only the facts that can change. If the probe ever grows a new blocking
condition, it belongs here too.

**Two cool-offs.** "The tree is older than an hour" is not a guard against
deleting work in progress. On 2026-10-30 a tree was reclaimed 5.6 minutes after
its PR merged, while its result was still being read; its last commit was 2.26 h
old, so every age guard passed. The merge happens in the shared checkout, so the
tree's own last commit never moves when it matters most — only `mergedAt` sees
that clock. Hence 30 minutes on the merge and 60 on the last commit: the second
is the probe's own freshness guard, so a round running without the snapshot
judge is never looser than one that has it.

## Scope: what may block, and what may only report

A **repo-scoped** fact is reported on a line of its own and may never veto one
tree. `git stash` lives in the shared repo, so one entry used to freeze every
tree under it; the stash inventory now answers the only question that matters —
how many, how old the oldest is, and whether the commit each was taken on still
has a tree that contains it — on a line that cannot block.

Only **tree-scoped** facts block: the directory is gone, the working tree is
dirty, a rebase/merge/cherry-pick is half-finished, HEAD moved off the snapshot,
a process names the tree, the tree holds the default branch, its PR is no longer
merged at this tip, or one of the two cool-offs is still running.

A detached HEAD is a question, not a verdict. It has no branch whose PR could
prove the work is merged, so the answer comes from the commits instead: a head
that is an ancestor of `origin/main` holds nothing main lacks, and the tree is
eligible on the same terms as a merged branch. Only a head main does not have —
or one that cannot be compared, because `origin/main` is missing — stays
blocked. A stale `origin/main` can hide a merge and so keep a tree; it can never
invent one, so this rule's only possible mistake is the safe one.

A hold another agent keeps is consulted and never rewritten: it is not this
tool's state, and the bookkeeping that expires it stays where it belongs.

## Emptiness is the bug, not the quiet

A probe that reads nothing prints the same thing as a repo with nothing to
reclaim. On 2026-11-03 the probe lost its `rows.append` for trees that exist —
it landed inside the path-gone branch — so it reported `trees: []` for two days
while a dozen trees stood there, and every round skipped as "no candidates". The
round therefore compares the probe's rows against `git worktree list`, and a
mismatch is an alert, not a quiet night. Deleting on the deletion gate alone in
that state needs `--allow-blind-judge`; without it the round only reports.

The same asymmetry, in the other direction: a probe that is not installed is a
known absence rather than a failure, and so is a ledger nobody is writing. Both
degrade to the deletion gate and say which of the two reasons it was. Raising
instead used to cost the whole root on a machine that simply has no probe.

## Two surfaces

`git worktree list` is the only enumeration this tool may act on — a directory is
not evidence that a tree is registered. But the two surfaces disagree in ways
worth reporting:

* a registered tree whose directory is gone (a zombie registration; `prune` clears it);
* a directory whose `.git` points into this repo but that nothing registers (a
  ghost that holds disk and that no `prune` will ever free);
* a directory whose `.git` resolves elsewhere (another repository's worktree —
  four healthy trees of a different checkout live under `~/.meiva/worktrees/` on
  another machine, and a parent-directory diff would have deleted live work);
* an entry that is not a worktree at all.

The last two are reported as foreign and never touched.

## Several roots

`--root` is repeatable: the shared checkout and the runtime `~/.meiva/source`
are different checkouts on the same volume. Branch refs are never deleted in any
root: `worktree remove` deletes no ref, a ref costs a few hundred bytes, and the
tree can be rebuilt from it in seconds — deleting it was the only irreversible
half of the operation.

## What a round leaves behind

One ledger line per decision that reached a tree, appended in the order the
decisions happened, plus one summary line per round. The per-tree line is what
makes "the cool-off works" checkable rather than claimed; `--report-hours` is one
groupby over it.

The output is a contract with the schedule that runs this: the per-tree line once
gained a verdict column and the alert's regex silently stopped matching, so the
one-line `tally: ...` summary exists and is the thing to grep.

## Reporting without an agent

A schedule process has no agent identity, so the SDK's message call cannot be the
channel that reports a blind judge. The alert goes over the gateway's own HTTP
interface with the agent-host unit's token, and the address is configuration of
the home (`--gateway`, `WORKTREE_GC_GATEWAY`, the unit's `MEIVA_GATEWAY_URL`,
then the home's `.env`). An empty address means "cannot alert" and says so; no
address is ever guessed.
