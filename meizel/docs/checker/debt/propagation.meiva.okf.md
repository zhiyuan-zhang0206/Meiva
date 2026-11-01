---
type: doc
title: "meizel/ — World Reads That Travel Through a Function"
description: "The world reads a call site inherits but records nothing for: what `debt propagation` counts, why those call sites are not in the ledger, and what would let them enter it."
tags:
  - locality
  - tool
---

# meizel/ — World Reads That Travel Through a Function

## What is it

`ambient-read` records the point of acquisition: the file that calls
`os.geteuid()`, `time.time()`, `sys.platform` or a `[state]` name. It does not
follow the value out of that function. A caller of `ensure_private_dir()` that
hands it no `euid` inherits the world read and records nothing, and so does every
caller above it — the call names a function of the repository, so no rule fires.

`meizel debt propagation` measures that surface. It changes nothing: no
`[debt]` table is read or written, and the number it prints is not part of any
total.

## What it counts

One pass over the governed production code (`application`, `standalone`,
`templates`, `tooling`, `frontend`; test files excluded; a file declared under
`[roots]` excluded, because a composition root reads by design):

- **the acquiring functions** — a function whose body holds an acquisition, as
  the same scanner `check` runs reports it;
- **the closure** — every function that reaches one of them through a call, at
  any depth;
- **the sites** — a production call whose callee is in the closure and whose
  caller is not itself an acquiring function.

The command prints the site count, the ten widest functions by number of sites,
and then every site as `path:line -> function`.

## The count is an upper bound

A call that does pass the value — `ensure_private_dir(path, euid=euid)` — cannot
be told apart from one that does not without knowing which parameter carries the
world value, so it is counted too. The printed line says so. Read the number as
"how many call sites can inherit a world read", never as "how many debts are
missing".

Nor is it complete in the other direction: a call that reaches a wrapper through
a re-export (`from pkg import name` where `pkg/__init__.py` imported it from
`pkg.mod`) resolves to `pkg.name`, not to the wrapper's qualified name, and is
not counted.

## Why these sites are not in the ledger

Registering the wrappers under `[ambient-readers]` would make every call site an
acquisition: an entry function called from dozens of places becomes dozens of
entries, and the ledger stops describing debt and starts describing the call
graph (#7139). The `unless` clause is the mechanism that would fix this — a
reader that reads only when it is not handed a value — but each entry needs its
carrying parameter decided by hand, and a wrong one silently stops charging for
a real read.

## When this becomes debt

The upgrade is a change of measurement, not of will: when the carrying parameter
of a wrapper can be decided (a written `unless` per wrapper, or a rule that reads
it off the body), the sites that do not pass it become `ambient-read` entries and
the ledger grows by that number. Until then this command is the instrument, and
the list is where the next repayment starts.

## How to run it

```console
$ python -m meizel debt propagation            # the count, the ten widest, every site
$ python -m meizel debt propagation --top 25   # a longer ranking
```

It runs the whole analysis, so it costs about what `check` costs.

Parent: [[meizel/docs/checker/debt/debt.meiva.okf.md|recorded locality debt]].
