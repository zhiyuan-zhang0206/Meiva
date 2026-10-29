---
type: doc
title: Call sites of a changed signature
description: How `scripts/audit/call_sites.py` finds every call that cannot supply a parameter a signature just gained, why a keyword search and a scoped type-check both miss some, and why it reports rather than enforces by default.
tags:
- scripts
- audit
- testing
---

# Call sites of a changed signature

Adding a required parameter to a shared callable breaks every caller that does not
pass it — and the break is invisible in exactly the two places a migration is
usually validated:

- A scoped `basedpyright` run only knows the trees it was given. The callers under
  `scripts/tests`, `tests/e2e` or a plugin's own tree are the ones a local run
  leaves out, so they fail later, in CI, on a commit that is otherwise green.
- A call that hands the value over *positionally* never contains `parameter=`, so a
  search for the keyword passes it.

`call_sites.py` reads the call sites instead of the text: it parses every Python
file below a root and judges each call to the named function by the argument list
it actually has. It is the "who calls this" question of `where_used.py` asked about
one parameter.

## The spec

```
python scripts/audit/call_sites.py --spec '[MODULE:]FUNCTION:PARAMETER[:POSITION]'
```

- `FUNCTION` is the name the call spells, bare (`build(...)`) or as an attribute
  (`emitter.build(...)` is matched by `build`). Repeat `--spec` to check several
  parameters.
- `MODULE` narrows the walk to files that can reach it — the file defines a
  top-level name the module path ends with, imports the module, or imports one of
  its members. Without it a bare name matches in every file, which is the right
  reading for a distinctive name and noise for a common one.
- `POSITION` (0-based, in the call's own positional arguments) says a call may
  satisfy the parameter positionally. It is the only place the tool needs the
  signature, and it takes it from the operator rather than importing the function:
  the tool has to run *before* the tree imports cleanly with the new signature.

## The verdicts

| verdict | meaning |
|---|---|
| `keyword` | the call passes `parameter=...`; nothing to report |
| `position` | the call has a positional argument at `POSITION`; nothing to report |
| `unproven` | the argument list is expanded (`*args` / `**kwargs`), so it proves nothing either way |
| `missing` | the call cannot carry the parameter |

## Calls written inside a string

A `pytester` fixture writes a whole test module into a triple-quoted string and runs it
in a subprocess. The call inside is a string constant, not a call node, so a walk of
the fixture file passes it — which is how a required parameter reached the merge queue
green on a run that had reported "missing: 0". A literal that starts a definition or an
import at the beginning of a line is therefore read as a module: its calls are judged the
same way and reported at the line the literal starts on, marked
`(inside a string literal)`. Prose that merely mentions a call has none of those markers
and stays out. One that looks like a module but does not parse is `unproven`, not
silent.

`getattr(module, "function")(...)` names the function in a string, so the argument list
at that line says nothing about the parameter; every such mention is `unproven`, marked
`(named by a string)`.

The summary line is part of the contract:
`call sites checked: N; missing: M; unproven: U`. A `checked` of zero under a spec
that names a real function means the `MODULE` narrowing hid the call sites — fix
the spec, not the tree.

## Reporting, not enforcing

The default exit code is 0. A caller may be right to leave the parameter to a
wrapper, or to be reached only through a path its own type-check does cover; that
judgment belongs to the author who made the change, who reads the list.

`--enforce` exits 1 when any call is `missing`. `unproven` never fails an
`--enforce` run: otherwise one `*args` helper up the stack would decide the
verdict for every caller behind it, and a gate nobody can satisfy gets turned off.
`--json` prints `checked` plus one object per finding for a script that wants to
count rather than read.

The tool never edits: it does not insert arguments, and it carries no baseline. A
number nobody paid attention to is worse than a report.

## What it cannot see

- A call through a variable, or `factory()(...)`, has no name to follow. A string-named
  call is reported as unproven rather than followed.
- It does not read the callee's signature, so a spec that omits `POSITION` reports
  a legitimately positional call as `missing`; add the position.
- One spec covers one parameter. A signature that gained two needs two specs.
- A wrapper that forwards `**kwargs` is `unproven`, and the callers behind it are
  not followed.

Parent: [[scripts/audit/docs/audit.meiva.okf.md|audit]].
