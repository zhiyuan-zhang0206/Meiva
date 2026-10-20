---
type: doc
title: Hosted Runtime Admission
description: One UPDATE admits a hosted turn; the circuit breaker is the only gate before it.
tags: []
---

# Hosted Runtime Admission

`agent/ownership/hosted.py:admit_hosted_runtime()` grants execution to the local
agent host with one `UPDATE` of the agent's row (machine, status running/idling): it
writes the host boot as `runtime_owner` and keeps or mints the `runtime_generation`.
The context carries that admitted identity through the entire turn. A terminal
status, another home machine or an open circuit breaker cannot
be bypassed by a delayed wake. A host that is exiting refuses new turns in process
(`TurnScheduler.stop_admission`), not in this UPDATE.

Ownership survives normal idle and runtime-cache eviction. Settlement matches the
original incarnation, so a late turn cannot rewrite a replacement's state.

Restart and terminate are durable inbounds. Native claim returns to END, the
host flushes the final checkpoint, then applies the accepted lifecycle command.
Admission reads no deployment-wide state: the agent's own row decides it, so a
restart's continuation is admitted by the same rule as any other wake.
Single-flight remains held through the original continuation and settlement.
Restart releases the incarnation for the next admission; terminate leaves the
agent identity terminal. There is no child-process admission, OS launch budget,
canonical per-agent process record or `/exited` callback.

A force terminate writes `terminated` itself and asks the host to cancel the running
turn; acceptance means the interruption was enqueued, not that an uncooperative turn
has finished. The exec child dies with its host (control pipe EOF) or with its turn.

Related: [[../../ownership/docs/ownership.meiva.okf.md]], [[../../docs/lifecycle.meiva.okf.md]], and
[[services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md]].
