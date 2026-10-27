---
type: doc
title: "meizel/ — Ambient Reads"
description: "Only a composition root reaches the world a process runs in: the closed vocabulary of ambient kinds, how acquisition points are found, the [state] and [roots] manifest keys, the policy's reader functions, the ambient-read, stale-root and stale-reader codes, and what the analysis cannot see."
tags:
  - locality
  - tool
---

# meizel/ — Ambient Reads

## What is it

Code that takes the clock, the environment, the home directory, the platform, a process id,
randomness or the process's own configuration, database, event bus or service endpoints instead
of being handed it cannot be tested by handing it a value, and answers differently on another
machine or at another time. The rule is the red line of
[the conventions](../../../docs/conventions/python-conventions.md#composition-roots-and-required-dependencies):
the world is read once, in a composition root, and passed down. Meizel makes that checkable by
recording each point where a file reaches the world and asking its directory manifest whether the
file is allowed to.

The rule governs the `application`, `templates`, `tooling` and `frontend` roles
in Python and TypeScript, with the same `[state]` and `[roots]` entries. TypeScript acquisition uses compiler bindings for imports and
shadowing; its built-in spellings include `process.env` (also `process.env[name]`), `process.cwd()`,
`Date.now()`, zero-argument `new Date()`, `Math.random()`, crypto calls and the browser's `location`,
`localStorage`, `sessionStorage`, `document.cookie`, `navigator`, `matchMedia`. In `ui/web/src`,
`process.env.NEXT_PUBLIC_<NAME>` is a constant of the build, not a read.

## Kinds

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
| `settings`, `db`, `endpoints`, `bus` | the state the repository keeps itself, below |

An interval clock read for a deadline, a duration or a TTL is the plain use of `time`; it is a
window onto the world only where the same function waits, because then how long to wait next is
decided in a loop. A function waits when it calls a name that contains `sleep` (`time.sleep`,
`asyncio.sleep`, `timers.asleep`, `retry_sleep`) or a function of its module that does, however
the helper is named (`_pause()` that sleeps). A function without parameters that is only
`return time.monotonic()` is a clock under another name: a call of it in a function that waits is
the reading, recorded as `_monotonic()`. `loop.time()` on `get_running_loop()` or a `loop`,
`_loop` or `event_loop` is an interval clock. The wall clock is always one.

A call that reads the world only when given nothing (`time.localtime()`, `time.strftime(fmt)`,
`psutil.Process()`) is an acquisition then; given a value it describes that value.

A `random.Random` constructor has an independent seed only when its positional seed or `x=`
argument is a non-None literal, including zero or an empty string/bytes value. Missing seeds,
literal `None`, dynamic seeds and unbounded argument expansion conservatively acquire `random`:
a dynamic value can be `None`, and the checker does not infer its runtime value or type.
This is static evidence of a possible ambient
seed, not proof that every execution reads the machine. A class reference alone acquires nothing.

## State

`[state]` in a directory's `meizel.toml` says which direct-child files hold state of a kind
(`settings`, `db`, `endpoints` or `bus`). The policy table `[ambient]` lists, per kind, the names
through which that state is reached; a name is resolved against each module that declares the
kind. A bare name (`settings`) acquires where it is imported (`from base.config import settings`)
or read as an attribute (`base.config.settings.x`). A name ending in `()` (`get_field()`,
`Database.from_settings()`) acquires only where it is called, so importing a class for an annotation
acquires nothing.

```toml
# base/config/meizel.toml
[state]
"__init__.py" = ["settings"]
```

## Readers

A function that reads the world and hands it on is an acquisition point of its own: the policy
lists it under `[ambient-readers.<kind>]` by its fully qualified call, and a call to it is a read of
that kind at the call site; a process-wide object (`SYSTEM_TIMERS`) is listed without `()` and is
read at every use of the name ([[meizel/docs/checker/ambient-readers.meiva.okf.md|readers]]).

## Roots

`[roots]` declares the files that may read the world, each with the kinds it may read and why. A
bare list of kinds is `manifest-syntax`: it says what a file may read and nothing about why it is the
place to read it. Only direct children are named; a deeper file is declared in its own directory.

```toml
[roots]
"daemon.py" = { kinds = ["settings", "db"], why = "process entry point: builds ConfigBoot and opens the database" }
"boot.py" = { kinds = ["environ", "home"], why = "reads the launch environment before Settings exists" }
```

The reason says what the file reads, not that it is allowed to. A file that many modules outside the roots
import is a middle layer, not a root: changing it asks the `root-middle-layer`
[diff hint](../../../scripts/lint/docs/diff-hints/catalog.meiva.okf.md), whose answer is the value passed
down from the real root.

## The rule

`ambient-read` fires for a non-test file of a governed role (`application`, `templates`, `tooling`)
that acquires a kind its manifest entry does not declare. The key is `file#kind target`
(`daemon.py#clock time.time`) and the owner is the file's directory, so the diagnostic is
recordable debt like any patch: recorded sites pass, a new one fails, a paid one is `debt-paid`. A
file under `purity = "strict"` has no debt: its read is a plain error. A test file records
nothing; what a test sets is the patch family's concern. `none` files are judged too, which is
the point: a shell executes a plan and reads no environment.

`stale-root` is the other direction: a declared kind the file no longer acquires. A root declares
what it reads, not more, so a permission cannot outlive the code that needed it. Only a whole-repository
run knows it.

Every diagnostic carries why, instead and see; the recipe is to read the value in the root and
pass it down, or lift the decision into a pure function of its inputs.

## What it does not see

Only the point of acquisition is recorded; the analysis has no types, so what code does with the
object after it holds it is not followed (`settings.x.y` after the import is a plain read). A
name reached through a re-export of a `[state]` module, an alias assigned to a variable and
called later, `Path("~").expanduser()` and a string passed to `getattr` are not seen. A function
that reads the world for its caller is seen only if the policy lists it as a reader, and a call
to a function of the same module is not an acquisition.

Parent: [[meizel/docs/checker/checker.meiva.okf.md|checker]].
