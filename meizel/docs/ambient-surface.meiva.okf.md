---
type: doc
title: "meizel/ — Ambient Surface"
description: "The table that judges every public name of the standard-library channel modules as an ambient kind or as no read, the `unless`, interval and method forms an entry can take, the ambient-unjudged guard and the test that keep the table closed, how to judge a new name, and the judgments that differ from the obvious spelling."
tags:
  - locality
  - tool
---

# meizel/ — Ambient Surface

`ambient_surface.py` holds, for the modules that carry the channels (`CHANNEL_MODULES`: `os`,
`os.path`, `sys`, `time`, `datetime`, `platform`, `socket`, `getpass`, `pwd`, `grp`, `tempfile`,
`shutil`, `locale`, `uuid`, `random`, `secrets`, `threading`, `multiprocessing`, `resource`,
`sysconfig`, `pathlib`, `zoneinfo`), one judgment for every public name, in groups of names that
share a reason. A judgment is a kind, or `None` with the reason the name is not a read of the
world: a type, a constant, an effect on files the purity layer judges, a function of its inputs.
The scanner looks a dotted name up by its longest judged prefix (`os.environ.get` is judged as
`os.environ`, `datetime.datetime.now` as itself).

Three kinds of entry carry more than a kind:

- A name with an `unless` clause (`tempfile.mkdtemp`, `shutil.which`, `time.strftime`,
  `datetime.fromtimestamp`) reads the world only when the call does not give the parameter a
  value. It is the clause a policy reader uses, read for the call: `mkdtemp(dir=base)` reads
  nothing, `mkdtemp()` reads `TMPDIR`, and an explicit `None` is still a read. A reference to the
  name without a call acquires nothing.
- An interval clock (`interval`) is the plain use of `time` for a deadline, a duration or a TTL; it
  is a window onto the world only in a function that also waits ([[meizel/docs/ambient-kinds.meiva.okf.md|the ambient kinds node]] describes when).
- A method whose receiver the analysis cannot resolve, because it has no types (`METHOD_READS`):
  `parser.parse_args()` reads `sys.argv`, `moment.astimezone()` the host's zone,
  `path.absolute()` and `Path("~").expanduser()` the working directory and the home. A path built
  from the function's own parameters is the caller's value, so it reads nothing.


## Closed by construction

The table is closed in two ways, so that a read the vocabulary forgot cannot pass as a name that was
never looked at:

- `ambient-unjudged` is a plain error (never recordable debt) on a non-test file that uses a member
  of a channel module that no entry judges (`os.some_new_function`). The member of the module, not
  the module, is what is judged: `import os.path` alone is not a use. A private name
  (`sys._getframe`) is outside the interface and is judged only where an entry lists it
  (`sys._xoptions`).
- `meizel/graph/tests/test_ambient_surface.py` compares the table with the modules themselves: every
  public name of every channel module on this interpreter, and every name the type checker's
  typeshed lists for any platform or interpreter version, must be judged by an entry of its own
  (or by a pattern for a whole family, as `os.O_*`); an entry that names nothing, or a name two
  groups both judge, fails. An interpreter upgrade that adds `os.some_function` fails the test
  before it can reach the rule.

### Judging a new name

When `ambient-unjudged` or the test names a name:

1. Ask which channel the call learns something from that the caller did not name. A function of
   its arguments (a path join, a format) reads none. The file system and the network are not
   kinds: they are the purity layer's.
2. If it reads a channel, pick the kind of that channel above. If the read depends on an optional
   argument, give the group an `unless` clause for that parameter; if it is a clock read for a
   duration, mark it `interval`; if only a method name betrays it, add it to `METHOD_READS`.
3. If it reads nothing, give the group `None` and one sentence of why. A constant, a type and an
   effect each have a shared sentence in the table; reuse it.
4. A kind the channels above do not cover is a change to the vocabulary: it needs the maintainers,
   a channel row here and an `AmbientKind` member.

## Judgments that differ from the obvious spelling

- `Path.resolve` and `os.path.realpath` are not `cwd` reads. Both canonicalise a path, nearly
  every use is on a path that is already absolute (the repository's roughly 560 `resolve()` and
  `absolute()` sites in about 290 files include none on a relative literal), the analysis cannot tell the relative receiver from the absolute
  one, and the purity layer already judges them as file-system reads. A relative path is read
  where it was made (`sys.argv`, `Path.cwd`). `os.path.abspath`, `Path.absolute` and
  `os.path.relpath` without a start exist to anchor at the working directory and are `cwd`.
- `tempfile.gettempprefix` returns the constant `tmp` and reads nothing. `tempfile.gettempdir`
  and a temporary file created without a `dir` read `TMPDIR`.
- `time.localtime(secs)`, `time.ctime(secs)` and `time.mktime` are `clock` even when given a value:
  the same instant reads differently in another zone. `time.gmtime(secs)` does not.
- `os.sep`, `os.linesep`, `sys.byteorder`, `sys.version_info` and the like format paths or describe
  the interpreter build the deployment pins; they do not identify the host and are not read.
  `sys.platform` and `os.name` do, and are `platform`.
- `sys.path` and `sys.modules` are the interpreter's own mutable state: they are written far more
  often than they inform a decision.
- `pwd` and `grp` lookups are `pid`: they answer who a uid is on this host, and the lookup of the
  process's own user is the common use.
- Only the standard library's channel modules are closed. A third-party reader (`psutil.Process`)
  is listed when the repository uses it as a reader, and TypeScript acquisition has its own
  spellings ([[meizel/docs/checker/typescript.meiva.okf.md|the TypeScript node]]).

Parent: [[meizel/docs/ambient-kinds.meiva.okf.md|the ambient kinds node]].
