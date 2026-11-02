---
type: doc
title: "meizel/ — Ambient Kinds"
description: "The closed vocabulary of ambient kinds — `environ`, `clock`, `home`/`cwd`, `platform`, `pid`, `random` and the repository's own state — and the spellings by which analysis records each acquisition."
tags:
  - locality
  - tool
---

# meizel/ — Ambient Kinds

A closed vocabulary (`AmbientKind` in `meizel/graph/model.py`); an unknown kind in a manifest is
`manifest-syntax`. The graph model also owns the standard-library spellings used by the scanner.
Diff hints share the closed kind vocabulary when reading settings roots for `service-locator`.

| kind | acquired by |
|---|---|
| `environ` | `os.environ`, `os.getenv`, `os.putenv` |
| `clock` | `time.time[_ns]`, `datetime.now/utcnow/today`, `date.today`, `time.gmtime/localtime/ctime/asctime()` and `time.strftime(fmt)` without a time value; `time.monotonic`, `time.perf_counter` (and `_ns`) and `loop.time()` only in a function that also waits |
| `home`, `cwd` | `Path.home`, `os.path.expanduser`, `.expanduser()`; `os.getcwd`, `Path.cwd` |
| `platform` | `sys.platform`, `os.name`, `os.uname`, `socket.gethostname`, `platform.*` |
| `pid` | `os.getpid`, `os.getppid`, `os.getpgrp`, `os.getuid`, `os.geteuid`, `os.getgid`, `os.getegid`, `threading.get_ident`, `threading.get_native_id`, `psutil.Process()` |
| `random` | `random.*` (literal non-None seeded generators and class references excluded), `secrets.*` (not `secrets.compare_digest`), `uuid.uuid1`, `uuid.uuid4`, `os.urandom` |
| `settings`, `db`, `endpoints`, `bus` | the state the repository keeps itself, described in the ambient reads node |

An interval clock read for a deadline, a duration or a TTL is the plain use of `time`; it is a
window onto the world only where the same function waits, because then how long to wait next is
decided in a loop. A function waits when it calls a name that contains `sleep` (`time.sleep`,
`asyncio.sleep`, `timers.asleep`, `retry_sleep`) or a function of its module that does, however
the helper is named (`_pause()` that sleeps). A function without parameters that is only
`return time.monotonic()` is a clock under another name: a call of it in a function that waits is
the reading, recorded as `_monotonic()`. `loop.time()` on `get_running_loop()` or a `loop`,
`_loop` or `event_loop` is an interval clock. The wall clock is always one.

A call that reads the world only when it is given nothing (`time.localtime()`, `time.strftime(fmt)`,
`psutil.Process()`) is an acquisition then, and describes a value it was handed otherwise.

A `random.Random` constructor has an independent seed only when its positional seed or `x=`
argument is a non-None literal, including zero or an empty string/bytes value. Missing seeds,
literal `None`, dynamic seeds and unbounded argument expansion conservatively acquire `random`:
a dynamic value can be `None`, and the checker does not infer its runtime value or type.
This is static evidence of a possible ambient
seed, not proof that every execution reads the machine. A class reference alone acquires nothing.

Parent: [[meizel/docs/checker/ambient.meiva.okf.md|the ambient reads rules]].
