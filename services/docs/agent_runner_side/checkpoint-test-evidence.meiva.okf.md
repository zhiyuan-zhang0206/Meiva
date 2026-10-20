---
type: doc
title: Checkpoint and maintenance test evidence
description: Blocking, acknowledgment-loss and double boundaries in hosted runtime tests.
---

# Checkpoint and maintenance test evidence

`agent_host/tests/history/test_pooled_checkpoint.py` puts an advisory-lock
expression in the actual read: an ordinary SELECT does not wait for a row lock,
so that alone would not prove per-agent read isolation. Another case models a
write committed before its acknowledgment is lost, locks that existing tail,
and requires the real flush to wait. A subsequent skipped write has no database
await but must still obey the same checkpoint thread's serialization lock.
