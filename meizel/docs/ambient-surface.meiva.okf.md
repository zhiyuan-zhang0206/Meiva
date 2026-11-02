---
type: doc
title: "meizel/ — Ambient Surface"
description: "The table that judges every public name of the standard-library channel modules as an ambient kind or as no read, the `unless`, interval and method forms an entry can take, the ambient-unjudged guard and the tests that keep the table closed, how to judge a new name, the judgments that differ from the obvious spelling, the modules inspected and left out, and the forms the analysis cannot see."
tags:
  - locality
  - tool
---

# meizel/ — Ambient Surface

`ambient_surface.py` holds, for the modules that carry the channels (`CHANNEL_MODULES`: `os`,
`os.path`, `sys`, `time`, `datetime`, `platform`, `socket`, `getpass`, `pwd`, `grp`, `tempfile`,
`shutil`, `locale`, `uuid`, `random`, `secrets`, `threading`, `multiprocessing`, `resource`,
`sysconfig`, `pathlib`, `zoneinfo`, `fileinput`, `site`, `email.utils`, `netrc`, `ssl`), one judgment for every
public name, in groups of names that share a reason. The builtin `input` is judged as the name
`builtins.input` (the scanner spells an unshadowed `input` that way); `builtins` is not a channel
module, because its other names read nothing. A judgment is a kind, or `None` with the reason the name is not a read of the
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
- No channel module is judged by a bare `*`: a test fails on one. A family of constants may take a
  prefix pattern (`os.O_*`, `socket.AF_*`), because its members differ in nothing that matters,
  but a function a module gains later must be listed by hand, so that it cannot inherit a kind
  nobody chose.
- A second test pins every name that carries a kind (`_READS`, 249 names written out by
  hand, not derived from the table): moving `socket.if_nametoindex` into the network group, or
  dropping it from the host group, turns it red, which the completeness test alone would not do
  when the name stays judged by some entry. The names each module defines are read from a child
  that runs `channel_module_names.py`, a script that imports the standard library only, in a
  `python -I` interpreter, so that a library another test imported cannot add names.

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

The judgments that differ from the obvious spelling, the modules inspected and left out, and the
forms the analysis cannot see are in [[meizel/docs/ambient-surface-judgments.meiva.okf.md|the ambient
surface judgments node]].

Parent: [[meizel/docs/ambient-kinds.meiva.okf.md|the ambient kinds node]].
