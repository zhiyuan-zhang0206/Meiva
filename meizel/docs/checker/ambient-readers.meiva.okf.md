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
module that defines the function, so a function of the same name elsewhere is not the reader.
The value says what it reads. A call is an acquisition of the listed kind at the call site and is
judged like any other: it passes in a root that declares the kind and is `ambient-read` anywhere
else. A module that imports a listed function at its top level is followed, and so is a module
that imports it from there (`from pkg import name`, `pkg.name()`, a rename with `as`), so a
re-export does not hide the call; an alias assigned to a variable is not. A reader has one kind,
the one a caller asks for: `resolve_meiva_home()` reads the environment and the user's home, and
is listed as `home`.

A reader is listed when its job is to return something the world decides (the environment, a
home path, the platform, a process id, the current time, a random value, the process settings or
a handle built from them), it is defined in a root or is a zero-argument helper over one, and
files that are not roots call it. A function that merely calls a reader in a file that is not a
root needs no entry: the read is recorded there.

An entry that is not a qualified call, or is listed under two kinds, is `policy-syntax`; a listed
function that no longer exists is `stale-reader`, and only a whole-repository run knows.

Parent: [[meizel/docs/checker/ambient.meiva.okf.md|ambient reads]].
