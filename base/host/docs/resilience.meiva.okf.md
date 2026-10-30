---
type: doc
title: Retry primitives and the wait seam
description: The shared Policy, retry/aretry executors, classification, jitter and the wait parameter.
tags:
- base
---

# Retry primitives and the wait seam

`base/host/net/resilience.py` owns immutable `Policy` parameters and the shared
`retry` / `aretry` executors: attempts, backoff, jitter, classification, the
idempotency gate and Retry-After handling. `retry` / `aretry` execute a
zero-argument callable without depending on its HTTP client. `http_classifier` is
the common HTTP classification, and a call site composes local overrides through
`with_(permanent=...)` / `with_(transient=...)` instead of re-implementing them.
`jittered` and `extract_retry_after` supply the shared wait calculations.

The wait is a parameter, not a patched global: `retry(policy, *, sleep=...)` and
`aretry(policy, *, asleep=...)` default to this module's own `retry_sleep` and
`retry_asleep`. The defaults are those named functions rather than the stdlib
functions they wrap, because a default binds at definition time: an operation that
already holds its own clock passes its wait per call
(`retry(policy, sleep=timers.sleep)`), so its backoff waits with that clock, a
caller that passes `ManualTimers` records the waits and advances virtual time
instead of blocking, and patching one of the globals cannot silently reach a loop
that already bound its default. `idempotent=False` runs exactly once, and a final
failure stays visible through `on_final_failure` or the raised exception; silent
loss is structurally impossible. Bootstrap fetches use this owner; `base/lm/call`
and the Redis listener keep dedicated loops with their own consumer semantics, and
the remaining Redis audit is a
[future item](../../../future/infra/engineering/retry-consumer-contracts.md).

`jittered` samples the process phase and random noise; `jitter_delay` owns the
mode arithmetic and nonnegative clamp from explicit numeric values. The existing
unknown-mode agent fallback and none-mode behavior remain unchanged. Deterministic
integration tests seed only a fresh child process and compare its actual retry
wait with an independently computed random sample. A separate child runs the async
retry in a fresh process with its own recorder as `asleep`, which proves the
scheduling values and attempt counts the loop asks for, not that one or two real
seconds elapsed.
