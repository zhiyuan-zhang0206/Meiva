---
type: doc
title: "meizel/ — Declared Injected Calls"
description: "A path method is matched by its attribute name, so a callable handed in as a parameter reads as Path.read_text. The policy's [injected-calls] table gives one site an exit with a reason, a budget and a stale check, and the run counts it apart from the reads."
tags:
  - locality
  - tool
---

# meizel/ — Declared Injected Calls

## What is it

`impure-call` matches a path method by its attribute name: `deps.read_text(...)` is reported as
`Path.read_text` whether `deps` is a `Path` or a callable an entry handed in. The match is not
narrowed to receivers the analysis can prove are paths — `p = some_path(); p.read_text()` falls
outside such a proof — because a missed read costs more than a reported one. The false positive gets
an exit instead, on the one site its author names.

## The declaration

```toml
[injected-calls]
"max-sites" = ["1"]

[injected-calls.sites]
"cli/commands/repo.py#read_many#deps.read_text" = "read= is the reader the entry hands in, not a Path"
```

A site is `path.py#function#receiver.method`: the file, the function that makes the call, and the
call itself. The receiver must be a parameter of that function, which is what makes the claim
checkable — a receiver built in the function (`Path("x")`, a local bound from one, `self.path`) is a
path, and no declaration reaches it.

## What the run does with it

- The site is not reported as `impure-call`, so no manifest records it as debt.
- `meizel check` counts it apart — `N sites declared an injected callable, not a read` — so the
  number of real reads is not reduced by it and the two never merge into one figure.
- `max-sites` is a budget: more declared sites than the ceiling fails as `approved-budget`.
- A site the file no longer makes (the call is gone, or the receiver is not a parameter) fails as
  `stale-approval`. That refusal is the counter-proof: declaring a real `Path.read_text` is reported,
  never hidden.

Parent: [[meizel/docs/checker/checker.meiva.okf.md|the checker]].
