---
type: doc
title: Rehearsing against another ledger root
description: How to try admission-run on a real host without writing the machine's own admission verdict — the --home flag, what it moves, and what it deliberately does not.
tags: [scripts, operations, admission]
---

# Rehearsing against another ledger root

The gate keeps two things under one root: the job-class table (`job-classes.json`) and
the event log (`events.jsonl`), both under `$MEIVA_HOME/admission`. That root is the
machine's own record — an operator reading it afterwards is reading what this host
actually decided. A rehearsal that lands there leaves a decision nobody made: on 2026-10-31 a check run
of `--peak-mb 600 -- /usr/bin/true` appended a `warned` event that was
indistinguishable from a real verdict except by remembering it was not. (The
`disk-floor-state.json` overwrite recorded alongside it came from the #7890 review
copy, not from this tool: the merged script writes `events.jsonl` and the job-class
table only.)

`--home <dir>` moves that root for one run:

```bash
python scripts/host_ops/admission_run.py --home /tmp/admission-tryout --peak-mb 600 -- ./check
```

It changes where the run writes and nothing else. It does **not** change the formula,
the thresholds, the disk floor, the concurrency decision or the exit status: a
rehearsal that lowers `--jobs` in `/tmp` would have lowered it in the real ledger too,
so the answer transfers — only the record does not. Without the flag the default root
is used unchanged, and a test pins that default so the flag cannot quietly move
production's ledger.

To confirm a rehearsal stayed out of production, compare the real log's line count
before and after (`wc -l $MEIVA_HOME/admission/events.jsonl`); after a run that
touched nothing, it is identical, while the rehearsal root holds the new event and the
state file.

Parent: [[admission-run.meiva.okf.md]].
