---
type: doc
title: Impersonation checkpoint receipt test
description: Why the receipt-retry regression uses a real state graph.
---

# Impersonation checkpoint receipt test

The receipt-retry test uses a real compiled StateGraph and MemorySaver so the
additive reducer actually runs. A fake checkpoint dictionary would not establish
whether a failed receipt write causes the same delta to be applied twice. The
idle callback receives its unused state positionally; it does not adapt state
values or execute application work. LangGraph's overloads expose partially
unknown generic types and require the parameter name `state` in their callback
protocol. The narrow type directives keep this real runtime seam while the
test explicitly checks the resulting checkpoint value after both settlements.

A failed checkpoint flush is produced by the database. `checkpoint_inserts_failing` in
`agent/tests/impersonation/lease_support.py` installs a trigger that refuses every
INSERT into the `checkpoints` table for the length of a block, so the production history
saver (`throttled_saver`, wrapped with the hosted runner's write throttle) meets a real
failed write when it flushes, keeps its buffered tail and succeeds on retry once the
trigger is dropped. The saver is not subclassed and carries no fault state. No test writes
to the saver's private flush attribute.
