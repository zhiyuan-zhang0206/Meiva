---
type: doc
title: Admission run
description: Start a memory-heavy host job at the concurrency the data volume can absorb, and record what was lowered.
tags: [scripts, operations, memory]
---

# Admission run

`scripts/host_ops/admission_run.py` prices a job before it starts — per-worker
peak RSS times the requested workers, against the free space on the data volume —
and starts it at the concurrency that fits. It lowers, never refuses: a host that
refuses to run its own maintenance is worse than one that runs it slowly.

The rule exists because a host cannot absorb an arbitrary number of
memory-heavy workers. On 2026-10-20 four starts of one eval on a macOS host grew
the swapfile by 8 GB within a minute and pushed `/System/Volumes/Data` to
92.5-92.9%, 0.1-0.5 points under the 93% hard line; one 8-worker start was
killed by the disk guard after 13 minutes of work. On the Linux host the same
shape appears as one process holding 8.8 GiB plus 3.7 GiB of pinned device
memory.

```
budget   = room this host has for the job - --reserve-gib (default 12)
per item = --peak-mb x --swap-multiplier (default 2.0) / 1024
jobs     = clamp(1, requested, floor(budget / per item))
```

**The room is the resource the job's peak actually consumes, and it differs by
platform.** On macOS a job's peak becomes swapfile on the data volume, so the
reading is the volume's free space (`statvfs` cross-checked against `df -P`). On
Linux swap does not grow the volume, so the reading is the kernel's own
`MemAvailable` from `/proc/meminfo`. A host whose platform has neither reading
reports `unknown` and keeps the requested concurrency with a line saying so — a
missing reader is never read as a comfortable zero.

`--peak-mb` therefore means "peak resident memory of one worker", and on a host
with a GPU it is **RSS plus the device memory that job pins** (measured 8.81 GiB
+ 3.7 GiB on 2026-10-20); the pin is invisible in RSS, and on a host whose GPU
shares physical memory it is the same budget. `nvidia-smi`'s
`memory.used` is recorded in the event when the tool exists and reported as
`unknown` when it does not — it never enters the formula as a zero.

`--reserve-gib 12` is the space one 5-6 GB job needs for its swapfile plus margin
(measured 1.45x-1.8x of resident size on 2026-10-20). `--swap-multiplier 2.0` is
the same measurement rounded up. Kernel pressure stays in the decision as a
downgrade only on macOS: level 2 (warn) halves the budget and level 4 (critical)
starts one worker. Linux records the kernel's PSI stall average
(`some avg10`) in the event instead, because `MemAvailable` already prices the
job continuously and a second invented threshold would drift from the kernel.
`swap used/total` is printed and never enters the formula — on 2026-10-20 12:55
the macOS host showed 29.7 GB of swap with 6.77 GB of process RSS: that is stale
paged-out data occupying disk, not live memory demand.

## Declaring a job

`--peak-mb` is the peak RSS of one worker and is required the first time. After
that the script's own name is the key in `$MEIVA_HOME/admission/job-classes.json`
and `--peak-mb` may be omitted. A missing declaration is an error (exit 2) rather
than a default: a guessed default is a silent wrong answer. Each run measures its
process group's summed RSS once a second and writes the per-worker peak back, so
the table learns from what actually ran.

```bash
python scripts/host_ops/admission_run.py --peak-mb 600 -- .venv/bin/python eval.py --jobs 8
python scripts/host_ops/admission_run.py -- .venv/bin/python eval.py --jobs 8
python scripts/host_ops/admission_run.py --no-admission "12-minute smoke run" -- ./bench
```

The concurrency is read from the command's own `--jobs`, `--eval-jobs` or `-j`
when it names one, and that token is rewritten to the lowered value. A command
without such a flag is started unchanged, with a line saying the plan could not
be applied.

## What it prints and records

One line on stdout when the concurrency changes, nothing when the job fits:

```
admission: jobs 8->2 (lowered: free 26.1 GiB - reserve 12.0 GiB = 14.1 GiB / 5.47 GiB per worker; pressure 1; total = ...)
```

`--no-admission <reason>` is the only way past the gate, and it needs the reason:
the run is recorded as `bypassed` with that text. Every interesting start appends
one JSON event to `$MEIVA_HOME/admission/events.jsonl` (`adjusted`,
`overcommitted`, `unstable`, `bypassed`, `measured`). A split between `df` and
`statvfs` wider than 2 points marks the sample `unstable`: the requested
concurrency is kept and the line says why, rather than acting on a bad read.

It raises no alert, signals no process and judges no machine's health. The disk
and agent-host guards own those decisions, and neither reads this tool's budget.

Parent: [[scripts/docs/scripts.meiva.okf.md]].
