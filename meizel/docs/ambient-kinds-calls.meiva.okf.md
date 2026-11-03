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
decided in a loop. A function waits when it calls a name that contains `sleep` (`time.sleep`,
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

Parent: [[meizel/docs/ambient-kinds.meiva.okf.md|the ambient kinds node]].
