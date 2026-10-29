---
type: doc
title: "meizel/ — The Allowance-Removal Gate"
description: "What a removed declaration needs in the pull request body, how a Trunk batch is judged when its tree carries the batches queued before it, and what an unreadable record does."
tags:
  - locality
  - tool
---

# meizel/ — The Allowance-Removal Gate

`meizel.policy.toml` is the closed list of allowances, so an entry that disappears narrows what the
checker still reports and every other gate goes quiet at once: `check` finds nothing left to say, and
`debt diff` sees debt disappear, the one direction it allows. The removal therefore carries its own
record, in the pull request body, one line per table or entry that lost a declaration:

```text
Policy-Removal: <table>[.<entry>] — <why it is no longer needed> — <what replaces it, or the gap it leaves>
```

Both fields after the name are required and neither may be a placeholder. One line may name a table
(`ambient-readers.environ`) or a rule (`ambient-state`) when that much went at once — a deleted
section is explained once, not once per name — and it reaches that table or rule alone.
`scripts/lint/policy_removal.py` fails the pull request while a removal is unanswered and writes
`N removed, M added` into the check summary either way; a widened table needs no line. The record is
the pull request's title and body. A run with no pull request reads the commits `--against..HEAD`,
which is what a local run has; `--record <file>` reads a file.

## A Trunk batch

The queue writes a batch's body itself, and its branch keeps each member's commit behind a merge the
job's shallow checkout never walks to, so there the record is the pull request(s) the batch names and
tests, read through `gh`. A batch with nothing removed is never held up by a record it cannot read.
With a removal standing, a record this run could not read is missing evidence rather than a clean
answer: the run fails, names the record it could not read, and leaves the removal to its reviewer.

A batch also tests the batches queued before it, because the queue merges each one onto the tree it
tested last, and their changes are not in its record. A removal this batch does not answer is
therefore followed to the commit that took the declaration away — the policy is parsed before and
after every non-merge commit in `--against..HEAD` that touched it, so a commit is named for exactly
what its diff removed — and answered by that commit's own pull request, read through `gh`. Nothing
else may answer it: a pull request that names the entry but did not remove it cannot speak for that
change, and a commit that more than one pull request contains is answered only when all of them
declare it. A merge is never the owner: it carries a branch's change a second time, and the queue's
own merges belong to no pull request. The one exception is a conflict resolved by deleting an entry
both parents still carry — no commit made that removal — and the run names that merge instead of
reporting no removal at all. That merge cannot be edited, but the batch's record is its members'
bodies: a `Policy-Removal` line in any one member's body answers the removal before any attribution
runs, which is how a conflict resolution's deletion is repaired.

The job's checkout is shallow. Only when a removal is left to attribute does the gate complete the
history itself, with one `git fetch --unshallow origin HEAD` — everything the remote holds, never a
depth or a date guessed at. A checkout that stays shallow names the commit it stops at inside
`--against..HEAD` rather than reporting no commit at all, and a fetch that cannot run — a remote with
no `HEAD` of its own, no credentials — says why instead. A removal whose commit, or whose commit's
pull request, cannot be read stays unanswered with the reason named in the summary; it is never
reported as clean.

## Renames and history

When a rename or a move touches a listed path or name, the same PR updates the entry (a renamed file
leaves a stale one), and that removal says which one it was.

Historical Git comparisons use an explicit snapshot decoder for the five retired `ambient-state`
lists: `db-modules`, `db-primitives`, `db-handles`, `bus-handles` and `endpoint-handles`. Each still
requires a list of non-empty strings; every other unknown or malformed entry is rejected. `policy
diff` retains those entries so their retirement is reported. Selector and debt comparison normalize
only disposable base snapshots through this owner; the working-tree parser continues to reject the
retired vocabulary.

Parent: [[meizel/docs/checker/policy/policy.meiva.okf.md|the policy file]].
