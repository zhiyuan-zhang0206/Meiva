---
type: doc
title: Hosted Force Quiescence
description: Original hosted turn ownership spans actual continuation; an exec ends before its tool call returns.
tags: []
---

# Hosted Force Quiescence

Force uses the existing terminate inbound's fixed target incarnation and active
pointer. Database termination is its accepted/applied decision, not process or
continuation completion. The original live host observes only after its actual
serialized turn ends. The command remains unobserved when
HTTP cancellation fails or work survives cancellation.

The scheduler owns one Task per actual turn. It captures that Task before
checking the force command, and never cancels a replacement after validation.
The host shields real awaited work, including thread-backed work, from outer
Task cancellation; accepted force remains visible to existing durable interrupt
checks. An idle force wake uses the same original-host serialized pump without
admitting another runtime.

The host explicitly supplies the original `RuntimeIncarnation`, current
`NativeWorkTarget`, and `HostedTurnResources` through `MeivaContext`. The scope in
`base/native_process/turn_identity.py` identifies the actual turn Task within its
service, while the reusable model cache retains no scope. Copied graph contexts
share it and the original admission reference. An exec ends before its tool call
returns (see [[exec.meiva.okf.md]]): its process group is killed, its leader
reaped, its output drained and its request files deleted, all within a bound. So
no exec outlives the turn Task that started it, and the turn holds no pending
exec resource that could keep a force or compact from completing.

The service keeps late hosted work, such as interrupt subscriptions and settlement
tasks, in one completion span. It reports a late failure immediately without
cancelling other turns and raises it at stop/join; a delayed result never becomes a
synchronous exception in an already returned invocation. A later turn has its own
scope even when it uses the same cached model.

## Remaining boundary

Only the original host's serialized turn pump observes an applied force. If that
host dies first, no successor boot or sweep settles the force on its behalf: the
agent stays terminated with its lifecycle pointer set, resurrecting it is
refused, and the hourly stuck-lifecycle-pointer scan
(`services/upkeep/ttl_reaper/lifecycle_fences.py`) raises
`lifecycle_pointer_stuck` for an operator, who settles the command and clears the
pointer by hand (`docs/conventions/runbook.md`).

A host that dies with an exec running does not strand that exec. The child holds
the read end of a pipe whose write end the host kept, reads end-of-file when the
host disappears, and kills its own process group. A restarted agent host also
empties `run/exec` before it serves. A descendant that left the group with
`setsid` survives, as it always could.
Persistent shell sessions deliberately retain their separate ownership and are
not disposable exec descendants to kill wholesale.

## Contract verification

The host force tests exercise real thread work and exec children. History owner
tests verify that an incomplete settled-write scan propagates its failure; host
reconciliation preserves the claimed row when the public committed-id resolver
fails.
