---
type: doc
title: The disk floor
description: When admission-run refuses to start new work — the two data-volume lines (23 GiB refuse, 24 GiB warn), why the kernel's swap growth cannot be vetoed directly, and what the floor does and does not cover.
tags: [scripts, operations, disk, memory]
---

# The disk floor

macOS grows the swap pool 1 GiB at a time, and every one of those gigabytes comes out of
the data volume. Once the volume passes `MEIVA_TRACE_DISK_WATERMARK` (0.9) newly started
agents stop recording spans, so a memory decision quietly costs observability — on
2026-10-25 the pool had grown 3 GiB in 33 hours on the macmini while the volume sat at 88%.
The kernel owns that growth: there is no userspace hook before a swapfile is created, so the
only place the growth can be declined is upstream, where new demand is admitted.

Two lines, both configurable (`--disk-floor-gib` 23, `--disk-warn-gib` 24), read from the
data volume's free space — the same statvfs/`df` reading the plan uses, never the snapshot's
`disk_pct`, which differs by 1-1.5 points and would fabricate a drop that never happened:

- **below 23 GiB on three consecutive readings** — refuse: one line naming the machine, free
  space, swap, kernel pressure and the floor, a `refused` event, and exit **3**. Nothing
  starts. Three readings, ten seconds apart, because the swap pool moves a gigabyte at a time
  and a single sample near the line is a coin flip: on 2026-10-30 the macmini crossed this
  line 127 times in one day of ten-second samples while nothing about the machine changed.
- **below 24 GiB** — warn: the same line, a `warned` event, and the job starts. Two lines
  rather than one because a warning and a refusal in the same second leaves no room to react.
- **the refusal is a state** — once refused, the host stays refused until one reading reaches
  24.5 GiB (`--disk-reset-margin-gib` 1.5 above the line, wider than one swapfile) and that
  state is kept in `disk-floor-state.json` under the admission home, so the next invocation
  cannot reopen on the same wobble that closed it. Three readings plus this margin took the
  same 127 crossings down to 7.
- a host already above the reset line is not near the edge: it costs one reading and no
  waiting, and `--disk-samples`/`--disk-sample-interval` tune the probe (1 restores the old
  single-sample verdict).
- `--dry-run` is never refused (it starts nothing), and a Linux reading is `MemAvailable`, not
  a data volume, so the floor does not apply there.
- `--no-admission <reason>` remains the only way past it, recorded as `bypassed`.

The floor covers the jobs that go through this tool. A process an agent starts directly is
still governed by the disk and agent-host guards, not by this file. A floor at 23 GiB is also
a capacity limit: a host sitting at 26 GiB begins refusing after two more swapfile births.


Parent: [[scripts/host_ops/docs/admission-run/admission-run.meiva.okf.md|Admission run]].
