---
type: doc
title: "OTLP lower worker and tracing shutdown observations"
description: "How `OtlpWorker` retains failed SDK resources and primary errors across shutdown, and how imported `tracing` bounds arm and retry exit hooks."
tags:
- base
- telemetry
- otlp
- observability
---

# OTLP lower worker and tracing shutdown observations

Part of [[base/telemetry/otlp/docs/telemetry-otlp/exit-flush.meiva.okf.md|OTLP exit flush]].

`OtlpWorker` owns one exporter attempt, including a LoggerProvider or metric
reader created before a later SDK construction failure. Named SDK/exporter
exceptions keep their existing best-effort isolation and diagnostics. A failed
SDK teardown is reported as unfinished, even when the Meiva worker has ended;
its resources are retained and a later retry opportunity cannot overwrite that
owner. Ordinary failed attempts with completed teardown retain the five-minute
retry gate.

Unknown worker errors are reported immediately and retained as their original
objects. The backend's `shutdown()` raises its original primary error after a
finite join, including errors received after an earlier unfinished return. A
secondary teardown failure cannot replace the primary error. `DrainResult`
acknowledges local processing or observation, not collector durability.

An imported `tracing` module registers `shutdown(timeout=2, readiness=PROCESS_RECORDING)`.
The process boot root passes that same resource into initialization; billing and public span
facades observe it without importing the SDK owner. A reservation creates one pending Future,
and entering the actual initializer makes it running. Closing an idle or pending resource
cancels it and prevents later admission. Closing a running initializer does not claim it
finished: only that initializer publishes its result. A cancelled pending read returns false
without spending the timeout diagnostic. A first-use 30-second timeout permits late success.

Shutdown closes arm/retry admission, wakes the retry wait, and finitely observes the same
owners. TraceArm startup and worker joins occur outside admission locks; the existing collector-retry constructor retains its original startup path. Stopping an arm before its
start rejects any later start. If native startup raised and completion was never observed,
shutdown reports unconfirmed completion; that does not assert a live thread was observed.
Shutdown does not take the slow SDK initialization lock.
The retry continues to read settings only after each 300-second wait. Cold exit
never imports tracing or starts its SDK merely to shut it down.

These deadlines bound Meiva's caller observations. They do not terminate native
SDK work, constrain SDK-owned atexit callbacks, or guarantee a finite whole
process exit. Atexit is not a business caller and does not guarantee a nonzero
exit status for an unknown error. The public owner shutdown methods retain and
raise that error, while its immediate diagnostic remains visible.
