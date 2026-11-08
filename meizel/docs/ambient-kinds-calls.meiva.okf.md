---
type: doc
title: "meizel/ — Ambient Kinds: What a Call Depends On"
description: "When a call that names a reader is an acquisition: interval clocks that count only in a function that waits, calls that read the world only when they are given nothing, and the seed that makes a random.Random constructor independent."
tags:
  - locality
  - tool
---

# meizel/ — Ambient Kinds: What a Call Depends On

An interval clock read for a deadline, a duration or a TTL is the plain use of `time`; it is a
window onto the world only where the same function waits, because then how long to wait next is
decided in a loop. What it acquires is `monotonic`, the duration kind, and never `clock`: a file
allowed the duration has not been allowed the instant, and `time.time()` beside it still fails. A function waits when it calls a name that contains `sleep` (`time.sleep`,
`asyncio.sleep`, `timers.asleep`, `retry_sleep`) or a function of its module that does, however
the helper is named (`_pause()` that sleeps). A function without parameters that is only
`return time.monotonic()` is a clock under another name: a call of it in a function that waits is
the reading, recorded as `_monotonic()`. `loop.time()` on `get_running_loop()` or a `loop`,
`_loop` or `event_loop` is an interval clock. The wall clock is always one.

A call that reads the world only when it is given nothing (`time.gmtime()`, `time.strftime(fmt)`,
`psutil.Process()`, `tempfile.mkdtemp()` without a `dir`) is an acquisition then, and describes a
value it was handed otherwise.

A `random.Random` constructor has an independent seed only when its positional seed or `x=`
argument is a non-None literal, including zero or an empty string/bytes value. Missing seeds,
literal `None`, dynamic seeds and unbounded argument expansion conservatively acquire `random`:
a dynamic value can be `None`, and the checker does not infer its runtime value or type.
This is static evidence of a possible ambient
seed, not proof that every execution reads the machine. A class reference alone acquires nothing.

## Several channels in one call

One call can read several channels and then acquires each of them: `getpass.getuser()` is `environ` and
`pid`, `tempfile.mkdtemp()` is `environ` and `random`. Each kind is a debt of its own, and a root
declares all of them.

## What a root licence settles

A call a file's `[roots]` declaration licenses is not that file's impurity. `ambient-read` has
already left the acquisition alone, because the file is where that kind of world is read; the
purity rule must not charge for the same call again, so `impure-call` neither reports nor records
it, and a `[debt.impure-call]` entry that still holds it fails as `legal-read-recorded` until
`debt prune` drops it — the number falls, the code still makes the call, and nothing was repaid.

The two rules meet in one of three ways. They meet on the line: the scan records an acquisition of a
licensed kind on the line the purity rule recorded the call on, which is the same call in whatever
language judged it (a TypeScript `crypto.randomUUID()` under a `random` root). They meet on the
name: the ambient table judges the recorded call a reader of a licensed kind, and the call is
unconditional — one with an `unless` clause reads nothing once it is handed the value. They meet on
the filesystem's name: a read of a file derives no acquisition, so a `filesystem` declaration reaches
the purity rule through `FILESYSTEM_READS` in `meizel/graph/licensing.py` — the reads, never a write,
never an `open` whose mode decides, never a descriptor or a listing given no path. A repository that
wants the acquisition records it where the read belongs: a function listed under
`[ambient-readers.filesystem]` in the policy.

A call of another kind keeps its record: `time.sleep` under a `clock` root is an effect of the
clock's own door, not a read of the clock's kind, so the wait stays on the books, and
`Path.write_text` beside a licensed `Path.read_text` stays one too.

Parent: [[meizel/docs/ambient-kinds.meiva.okf.md|the ambient kinds node]].
