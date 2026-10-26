---
type: doc
title: "meizel/ — Fake Entropy"
description: "An interval clock, an object address or a process-local counter used as an identifier or a random draw, and a private wrapper over an interval clock that nothing injects: what the fake-entropy code finds, what it leaves alone and how it is recorded."
tags:
  - locality
  - tool
---

# meizel/ — Fake Entropy

## What is it

`time.monotonic_ns()`, `time.perf_counter()`, `id(obj)` and `next(itertools.count())` look like a fresh
value on every call, so code reaches for them where it needs an identifier or a random number: a run id
from `time.monotonic_ns()`, an idempotency key from `perf_counter_ns()` and a sequence number, a
linear-congruential draw over a clock reading, `uuid5` over a name that holds one. They are not entropy.
Two processes started together draw the same value, a restart repeats a counter and an address is reused
once its object is gone. And because they are neither the wall clock nor a random call, they pass
[[meizel/docs/checker/ambient.meiva.okf.md|`ambient-read`]], which `uuid.uuid4()` and `random.random()`
fail. The rule reports the use as the randomness it stands in for.

`fake-entropy` is recordable debt of the directory, keyed `file#reading in where`
(`store.py#time.perf_counter_ns in an f-string`); a file under `purity = "strict"` has none. It
governs the same roles as `ambient-read`, in Python files; a test file records nothing.

## What it finds

A reading is *spent* when it reaches:

| where | examples |
|---|---|
| a format | `f"{x}"`, `str(x)`, `hex(x)`, `format(x)`, `repr(x)`, `"{}".format(x)`, `x.hex()`, `x.encode()` |
| a digest or a derived name | `hash(x)`, `hashlib.sha256(...)`, `uuid.uuid3/uuid5(...)`, `uuid.UUID(int=x)` |
| a seed | `random.Random(x)`, `random.seed(x)` |
| a keyword that names an identity | `run_id=x`, `key=x`, `seed=x`, `nonce=x`, `token=x` |
| a remainder or a bit operation | `x % n`, `x & 0xFFFFFFFF`, `x << k` (the linear-congruential draw) |

The readings are `time.monotonic`, `time.perf_counter` and their `_ns` forms, `id(x)` and
`next(<name bound to itertools.count()>)`. A name assigned from a reading carries it to the next use in
the same function. A process-local counter is spent only in the first four rows: `n % period` is how a
sampler counts.

The second shape is a module's own wrapper over the clock: a private function without parameters whose
only statement is `return time.monotonic()`, in a module that never uses the name as a value. The
wrapper hides the clock read from the rule on sleeping functions and is a seam only a patch can reach.
One the module injects (`now: Callable[[], float] = _monotonic`, `Waiter(now=_monotonic)`) is an injection
point and is left alone, and so is a public one: another module may inject it.

## What it leaves alone

- A duration: a difference of two readings carries nothing, so `elapsed = time.monotonic() - started`
  and `f"{time.monotonic() - started:.1f}s"` are the plain use of an interval clock. A deadline
  (`time.monotonic() + limit`) that is only compared is not spent either.
- An identity set (`seen.add(id(item))`), and `id` in `__repr__` and `__str__`.
- A reading stored (`cache[key] = time.monotonic()`) or passed on without reaching a row above.
- The wall clock and the random calls: `ambient-read` judges those.

The fix is the one the red line asks for: entropy has one carrier, the process's `random.Random`. Pass it
down from the composition root and mint identifiers with `new_uuid(rng)`; a module's clock is a required
`Timers` parameter.

Parent: [[meizel/docs/checker/ambient.meiva.okf.md|ambient reads]].
