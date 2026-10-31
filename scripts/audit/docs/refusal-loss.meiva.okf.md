---
type: doc
title: "Refusal loss audit"
description: "scripts/audit/refusal_loss.py reads the commits that pay recorded meizel debt down and reports every refusal each one removed instead of moving; a hunk that deletes a raise and adds nothing in its place fails the run."
tags: [scripts, audit, meizel]
---

# Refusal loss audit

`scripts/audit/refusal_loss.py` answers one question about a change: did paying recorded debt
down delete a refusal instead of moving it? Run it from the checkout it should read:

```
.venv/bin/python scripts/audit/refusal_loss.py [--base origin/main | --range A..B | --since "60 days ago"]
                                                [--blocking deletion|unreplaced] [--receipt] [--json] [--no-block]
```

## Why it exists

Paying recorded debt down moves code: a decision leaves a shell and becomes a value in a pure
module. That move can also delete a refusal — a `raise` whose check no longer runs — and the
tests stay green, because they assert that something raises, not which refusal or where. Two
such deletions shipped: a platform guard replaced by a call whose result was dropped
(`base/cluster/postgres.py::boot_id`), and a load-time dependency check deleted while the code it
guarded stayed (`checkpoint_postgres_walks.py`). Both were found by a human reading the diff an
audit produced, not by a gate.

## What it reads

Only a change that reduces recorded debt is read: its `*meizel.toml` diff must delete more lines
than it adds. A change that pays no debt down has nothing to check, and reading it would only add
noise to every other pull request. Over each such commit's Python diff, two lenses read every
hunk:

- `dropped-refusal` — the hunk deletes a `raise` whose literal message no longer appears anywhere
  in that commit's tree. The refusal was deleted or reworded away rather than moved.
- `default-allow` — the hunk deletes a `raise` and adds a fallback in the same place
  (`return None`, `pass`, `except`, an `or` default), so the refusal may have become a silent
  default.

A message that sits on a continuation line is still found: a removed `raise` statement is read
across the lines its parentheses leave open.

## What blocks

`--blocking deletion` (the default) fails the run only on a hunk that adds nothing at all: the
shape that deleted a guard and kept the code it guarded. It is the one shape a merge gate can
assert without reading the change. `--blocking unreplaced` widens the failure to every hunk that
deleted a refusal and added nothing that raises in its place — the shape that also catches a
guard replaced by a call whose result nobody reads, at the cost of flagging hunks that only
reworded a refusal.

Exit code `1` means a blocking hit under the selected rule, `0` means the change is clean or every
hit is only listed; `--no-block` reports without failing, and `--json` prints the same findings as
objects. A finding records both verdicts, so one scan answers both rules. A hunk in
a test file, or in a file the commit deletes, carries no finding: deleting a module takes its
refusals with it.

## Measured

Against this repository on 2026-10-31, `--since "60 days ago"` read 466 debt-reducing commits and
reported 264 hits; the default rule blocked 61 of them and `--blocking unreplaced` blocked 152.
The read-only audit that motivated the check found three real behaviour deletions in the same
window: the default rule catches one of the three, and `unreplaced` catches all three.
`unreplaced` blocks 152 of the same 264 hits — two and a half times the default — because it also
flags a hunk that reworded a refusal without dropping it, which is why it stays a flag.

## The line a pull request carries

```
refusal-loss: hits=<hits> blocking=<blocking> unreplaced=<unreplaced>
```

`--receipt` prints exactly this line and nothing else, from a single scan, and it is the line to
paste into a pull request body while pull-request CI is dispatch-only. Three labelled counts in
one line means no reader has to guess whether a bare number is the hit count or the blocking one,
and the pull request body carries the same string the script produced rather than a hand-copied
approximation of it. When `unreplaced` is above zero, the same line continues with the author's
per-case conclusions: `refusal-loss: hits=… blocking=… unreplaced=… reviewed: <what each one was>`.

The merge gate on a change that pays recorded debt down requires that line, and a change whose
`unreplaced` count is above zero carries the author's per-case conclusions beside it.

Parent: [[scripts/audit/docs/audit.meiva.okf.md]].
