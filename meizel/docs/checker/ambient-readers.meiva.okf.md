---
type: doc
title: "meizel/ — Ambient Reader Functions"
description: "The policy's [ambient-readers.<kind>] tables: repository functions that read the world on their caller's behalf, how a call to one is judged, how a re-export is followed, which functions belong in the list and the stale-reader code."
tags:
  - locality
  - tool
---

# meizel/ — Ambient Reader Functions

A function that reads the world and hands it on is an acquisition point of its own. Without
that, a root could expose `os.environ` or `Path.home()` through a one-line function, and every
non-root caller would read the world through it unrecorded, with the read booked in the one
file that is allowed to make it. The policy table `[ambient-readers.<kind>]` lists such
functions, one table per kind of the vocabulary above:

```toml
[ambient-readers.environ]
"base.host.env.registry.process_environment()" = "returns the live os.environ"

[ambient-readers.settings]
"ops.cluster.rpc.RpcClient.system()" = "builds the client from the process settings"
```

A key is the fully qualified call (`pkg.module.name()` or `pkg.module.Class.method()`) of the
module that defines the function, so a function of the same name elsewhere is not the reader. A
class is listed the same way (`"base.clock.Clock()"` is every place that builds one). A key without
the parentheses names an object (`"base.clock.SYSTEM_TIMERS"`): like `settings` under `[ambient]`
it acquires at every use: each read of the imported name (a default argument, a use under
`__main__`) and each read of the attribute (`m.NAME`); an import that nothing uses acquires nothing. The value says what it
reads. A call is an acquisition of the listed kind at the call site and is judged like any other: it
passes in a root that declares the kind and is `ambient-read` anywhere else. A module that imports a
listed name at its top level is followed, and so is a module that imports it from there
(`from pkg import name`, `pkg.name()`, a rename with `as`), so a re-export does not hide the call;
an alias assigned to a variable is not. A reader has one kind, the one a caller asks for:
`resolve_meiva_home()` reads the environment and the user's home, and is listed as `home`.

## Which functions belong in the list

A reader returns something the world decides (the environment, a home path, the platform, a process
id, the current time, a random value, the process settings or a handle built from them), or is the
process-wide object that decides it (`SYSTEM_TIMERS`, the `Clock` that wraps it). It is listed when
it is defined in a root, or is a helper over one in the module that holds it (the zero-argument
`base.paths.logs_dir()` calls `meiva_home()` in its own module, a call the scanner does not count, so
the helper needs an entry of its own), and files that are not roots use it. A function in a file that
is not a root that merely calls a reader needs no entry: the read is recorded there.

## What is not followed

A call-form entry is not followed through an alias assigned to a variable, `partial(reader)`,
`field(default_factory=reader)`, a default argument (`def f(read=reader)`), a call of a subclass
(`Sub.method()`), an import inside `try`/`except ImportError`, a star import, or an instance method
that falls back to the live environment when it is given none; those calls are seen only if the file
that makes them also names the reader. A name-form entry (`SYSTEM_TIMERS`) is acquired at each use, so
its default arguments are. A chain of re-exports that needs more than 12 rounds of the scan (a chain
of 12 modules is always followed) fails the run instead of going unseen.

## Changing the list

An entry must be the module that defines the name (`def`, class, assignment or method): a name the
module only imports is `stale-reader`, so a function that moved and left a re-export behind is found.
It is spelled as it is used: `()` for a function, class or method, none for a value; a value written
with `()` or a function written without matches nothing and is `stale-reader` too.
Adding, moving or renaming a reader changes what is an acquisition, so it is a rule change: the
PR records the sites it finds with `meizel debt record`, and CI compares the growth against the base's
own content (`debt diff` treats `meizel.policy.toml` as rule code). A moved reader makes the same sites
new keys, because a key carries the spelling of the call: `debt prune` and `debt record` rewrite them,
and the count must not grow. An entry that is not qualified, or is listed under two kinds, is
`policy-syntax`; a listed name that does not exist is `stale-reader`, and only a whole-repository run
knows.

Parent: [[meizel/docs/checker/ambient.meiva.okf.md|ambient reads]].
