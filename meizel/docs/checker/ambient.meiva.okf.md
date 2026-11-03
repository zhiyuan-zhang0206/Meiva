---
type: doc
title: "meizel/ — Ambient Reads"
description: "Only a composition root reaches the world a process runs in: the kinds of ambient reads (the standard library's windows and the state kinds a repository's policy declares), how acquisition points are found, the [state] and [roots] manifest keys, the policy's reader functions, the ambient-read, ambient-unjudged, stale-root and stale-reader codes, and what the analysis cannot see."
tags:
  - locality
  - tool
---

# meizel/ — Ambient Reads

## What is it

Code that takes the command line, the clock, the environment, the home directory, the platform, a process id,
randomness or the state the repository keeps itself (its process configuration, a database handle,
an event bus: whatever its policy declares) instead of being handed it cannot be tested by handing
it a value, and answers differently on another
machine or at another time. The rule is the red line of
[the conventions](../../../docs/conventions/python-conventions.md#composition-roots-and-required-dependencies):
the world is read once, in a composition root, and passed down. Meizel makes that checkable by
recording each point where a file reaches the world and asking its directory manifest whether the
file is allowed to.

The rule governs the `application`, `standalone`, `templates`, `tooling` and `frontend` roles
for both Python and TypeScript. Both languages use the same `[state]` and `[roots]`
manifest entries. TypeScript acquisition uses compiler bindings for imports and
shadowing; its built-in spellings include `process.env`, `process.cwd()`,
`Date.now()`, zero-argument `new Date()`, `Math.random()` and Node/DOM crypto calls; the browser's
environment and the variables the Next.js build inlines are in
[[meizel/docs/checker/typescript.meiva.okf.md|the TypeScript node]].

## Kinds

The kinds of the standard library, a closed vocabulary, the operating-system channels they are
built from, and the table that judges every name of those channels:
[[meizel/docs/ambient-kinds.meiva.okf.md|the ambient kinds node]]. The state a repository keeps
itself is declared by that repository, below.

## State

The checker has no state kinds of its own: a repository declares them as the keys of the policy
table `[ambient]`, and the key set is the declaration. Each key lists the names through which state
of that kind is reached. A kind may not reuse the name of a standard-library kind and is a
lowercase name (`settings`, `db`, `cache`); a key that breaks either is `policy-syntax`.
`[state]` in a directory's `meizel.toml` says which direct-child files hold state of one of the
declared kinds, and `[roots]` may name those kinds beside the standard library's; a kind the policy
does not declare, in either table, is `manifest-syntax`. A name is resolved against each module that
declares the kind. A bare name (`settings`) acquires where it is imported (`from base.config import settings`)
or read as an attribute (`base.config.settings.x`). A name ending in `()` (`get_field()`,
`Database.from_settings()`) acquires only where it is called, so importing a class for an annotation
acquires nothing. `base.host.boundary.untyped.object_of(reader, *args, **kwargs)` is the call
`reader(*args, **kwargs)`, judged the same way when `reader` is a name or an attribute chain; any other
first argument is not guessed at.

```toml
# meizel.policy.toml
[ambient]
settings = ["settings", "get_field()"]
db = ["connect()", "Database.from_settings()"]

# base/config/meizel.toml
[state]
"__init__.py" = ["settings"]
```

`[ambient-readers.<kind>]` accepts the same declared kinds. A kind that leaves the policy leaves
every manifest that names it: the manifest is `manifest-syntax` until the entry goes, and the
debt key (`file#kind target`) of a read of it no longer matches anything.

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

`ambient-read` fires for a non-test file of a governed role (`application`, `standalone`, `templates`, `tooling`)
that acquires a kind its manifest entry does not declare. The key is `file#kind target`
(`daemon.py#clock time.time`) and the owner is the file's directory, so the diagnostic is
recordable debt like any patch: recorded sites pass, a new one fails, a paid one is `debt-paid`. A
file under `purity = "strict"` has no debt: its read is a plain error. A test file records
nothing; what a test sets is the patch family's concern. `none` files are judged too, which is
the point: a shell executes a plan and reads no environment.

`ambient-unjudged` guards the vocabulary itself. The standard-library names that acquire a kind are
judged in `meizel/graph/ambient_surface.py`, name by name, for every module that carries an
operating-system channel; a non-test file that uses a member of such a module that no entry judges
(a function a new interpreter added, a name nobody looked at) fails with this code. It is never
recordable debt: the gap is in the table, not in the file, and the fix is a judgment for the name
(a kind, or the reason it reads nothing) in the table, with the test that compares the table with
the modules themselves ([[meizel/docs/ambient-surface.meiva.okf.md|the ambient surface node]] says how
to judge a name).

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
