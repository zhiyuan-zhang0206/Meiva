---
type: doc
title: Hosted checkpoint write integrity
description: Exact parent validation, runtime generation fencing, and atomic upstream checkpoint writes on one pool lease.
tags: []
---

# Hosted checkpoint write integrity

`services/agent_runner/agent_host/pooled_checkpoint.py:PooledPostgresSaver`
owns the hosted checkpoint persistence boundary. Each `aput` and `aput_writes`
borrows one exclusive pool connection and enters a PostgreSQL transaction.

Before writing checkpoint, blob or intermediate write rows, the write transaction
atomically verifies the runner's fencing token against `agents_meta`:
```sql
SELECT runtime_generation, status FROM agents_meta WHERE id = %s FOR SHARE
```
The row lock (`FOR SHARE`) prevents concurrent takeover or status transition from
completing until the write commits. When the admitted runner holds an incarnation,
its `runtime_generation` must match `agents_meta` and the agent must still be
`running`. If Runner B took over and rotated `runtime_generation`, or if an unfenced
write targets an active agent, the transaction raises `StaleIncarnationError` and
rolls back immediately, persisting zero rows to `checkpoints`, `checkpoint_blobs`,
or `checkpoint_writes`.

Within the same transaction, a config naming a parent must find that exact
`(thread_id, checkpoint_ns, checkpoint_id)` row. The `FOR KEY SHARE` lock remains
held until the child transaction commits. A first checkpoint without a parent is
legal; another namespace's or thread's row does not satisfy the check.

The connection-bound upstream `AsyncPostgresSaver` still owns serialization,
blob/checkpoint inserts, pipeline synchronization and cursor cleanup. The outer
transaction commits them together or rolls them back on error or cancellation.
A missing parent raises `MissingCheckpointParentError` before any child blobs
are written. The guard does not pick the latest checkpoint, reconnect a branch
or convert the parent to an empty value.

The production N-step wrapper runs outside this boundary. It corrects a
coalesced save's parent to the last persisted config before the guard runs;
normal first writes, append, full snapshots and final flush keep their existing
behavior. Delta-bearing threads retire that throttle and preserve every exact
parent. Their cold reads still replay stored writes using the existing reader.

The pinned graph runtime can attempt a successor save after a previous save
fails or is cancelled. The guard rejects a successor whose parent never
persisted; Python exception context retains the original save error or
cancellation. The existing loud-failure wrapper reports the failed write.

This is a check-and-commit guarantee, not a foreign key or a guard against every
later retention operation. The row lock blocks deletion during this transaction;
it cannot prevent a later explicitly authorized deletion.
No historical branch is rewritten or repaired by a failed save.
