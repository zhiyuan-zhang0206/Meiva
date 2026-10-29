---
type: doc
title: "meizel/ — Locality Checker"
description: "Meizel checks tracked first-party dependencies against the nested DAG in meizel.toml manifests."
tags:
  - locality
  - tool
---

# meizel/ — Locality Checker

## What is it

Meizel judges one question: may file `f` use file `t`? Every directory declares
a small DAG over its direct children in `meizel.toml`; the checker derives the
facts from source and fails when a fact is not covered by a declaration. The
decision and its rationale live in
[the accepted decision](../../../docs/decisions/engineering/design/simplification/2026-10-11-meizel-nested-dag-locality.md).

The checker never executes inspected source. Tracked files come from `git ls-files -z`.
Python uses stdlib AST; TypeScript uses the locked Node compiler. See TypeScript
scanning below for setup and coverage.

```
python -m meizel check [paths... | --changed files...] [--limit N]
python -m meizel debt [--json] | record <paths...> | prune | carry | diff --against <ref> --on-growth fail|warn
python -m meizel explain <path>
python -m meizel sync [--prune] [--declare [paths...]]
python -m meizel bootstrap --out <dir>
python -m meizel roles list <role>... | check | write
```

`--repo <root>` selects another checkout; `--jobs N` sets parser processes. Exit codes: 0 clean, 1 violations, 2 the tool failed.

## Commands

- `check` is the gate over the whole graph; `paths` narrow what is reported.
  Each diagnostic gives `file:line`, the fact edge, the judging directory `L`,
  and the manifest line to add or public door to use. `check --changed files...` judges only those files.
- `debt` manages recorded violations, a per-directory baseline that may only
  shrink: [[meizel/docs/checker/debt/debt.meiva.okf.md|recorded debt]].
- `explain` shows graph edges and bounded [[meizel/docs/checker/explain.meiva.okf.md|Python source evidence]].
- `sync --prune` drops stale declarations; `sync --declare` writes the legal new
  dependencies of the changed files: [[meizel/docs/checker/sync.meiva.okf.md|keeping manifests current]].
- `bootstrap --out DIR` writes one `meizel.toml` per directory under `DIR` from
  current fact edges (non-edge tables kept) and a `report.json` of edge counts,
  components, witness facts and violation counts; the repository is untouched.

## Fact edges

- `import` and `from ... import` at any nesting level. Relative imports resolve
  against the importing file's directory. `from pkg import name` targets the
  submodule `name` when `pkg/name.py` or `pkg/name/` exists, otherwise
  `pkg/__init__.py` with `name` recorded as a used name. Resolution follows
  `pythonpath = ["."]`: a dotted name maps to a tracked path; directories
  without `__init__.py` are namespace packages.
- Attribute use on an imported module (`mod.name`, `pkg.sub.name`), through any
  submodule chain.
- pytest fixture requests: test and fixture parameters without defaults,
  `usefixtures`, and `getfixturevalue("name")`, resolved to the defining file by
  pytest's order: the file itself, its literal `pytest_plugins`, then each
  ancestor `conftest.py` and its plugins. Parametrized names and `patch`-injected
  arguments are not requests.
- Object-form patching: `monkeypatch.setattr(mod, "name", ...)`,
  `mock.patch.object(mod, "name")`, `patch.multiple(mod, name=...)` use `name`
  in `mod`.
- `[loads]` declarations (below); `path_scopes.toml` mounts
  ([[meizel/docs/checker/mounts.meiva.okf.md|mounts]]).

TypeScript aliases, supported syntax and analysis boundaries:
[[meizel/docs/checker/typescript.meiva.okf.md|TypeScript dependency scanning]].

## The rule

For a fact edge `f -> t`, `L` is the deepest directory containing both; `cf` and
`ct` are the children of `L` holding `f` and `t`.

1. `L`'s `[uses]` declares `cf -> ct`.
2. Every directory from `ct` down to `t`'s parent exports the next child on the
   path. `__init__.py`, `index.ts`, `index.tsx`, `index.mts` and `index.cts` are package doors, no export needed; a child whose name starts
   with an underscore (not a dunder) can never be exported.
3. Every name used from `t` is public. A leading underscore (dunders excepted)
   makes a name private to its defining file, tests and fixtures included: a
   fixture requested from another file has a public name.

Local graphs are acyclic. A test's use of a fixture from an ancestor
`conftest.py` needs no declaration. Importing `a.b.c` initialises `a` and `a.b`;
that is loading and creates no edge.

## Restricted Python

Binding resolution finds these through aliases (`from importlib import
import_module as im`): `importlib.import_module`, `__import__`,
`runpy.run_module`, `runpy.run_path`, `importlib.util.spec_from_file_location`,
`exec`, `compile`, a Python subprocess that runs anything but a literal file or module
([[meizel/docs/checker/python-commands.meiva.okf.md|commands]]), a `pytest_plugins` that is not a literal, string patch
targets that name first-party code (`mock.patch("a.b")`, `monkeypatch.setattr("a.b", v)`;
stdlib and third-party targets are fine, an unreadable target is not), and
writing a first-party module into `sys.modules` by name (`sys.modules["a.b"] = ...`,
`del`, `pop`, `setdefault`, `update`, `monkeypatch.setitem/delitem`, `patch.dict`).
Reading `sys.modules` is not restricted. A file that owns a dynamic load declares it:

```toml
[loads]
"loader.py" = ["meiva_builtins"]   # repository-relative directories
```

Each entry becomes a fact edge from the file to the directory, judged by the
same rule; the file is then exempt from the restricted-form errors. A loading file only loads.

A file that runs code from outside the repository (user scripts, agent code, a
module named by operator configuration) declares it under `[external]` with a reason:

```toml
[external]
"runner.py" = "executes the schedule script the user wrote"
```

The file is then exempt from `exec`, `compile` and the dynamic-import forms; no edge is made. Other restricted forms (`sys.modules` writes, python commands,
string patches) still apply. The reason must be non-empty; an entry whose file
has none of the exempt forms is `stale-external`.

Plugin and schedule-script discovery is `[external]`, not `[loads]`.

## Manifest

`meizel.toml` keys are `purity` / `[purity]` ([[meizel/docs/checker/purity.meiva.okf.md|purity]]),
`exports` (list), `[uses]` ([[meizel/docs/checker/narrowing.meiva.okf.md|narrowed endpoints]]), `[loads]` and `[external]`
(sites in [[meizel/docs/checker/policy/policy.meiva.okf.md|the policy file]]), `[state]` and `[roots]`
([[meizel/docs/checker/ambient.meiva.okf.md|ambient reads]]) and `[debt.<code>]`. Names are
direct children; `meizel.toml` is outside the directory budget. A directory without one has no
local edges and exports its door.

## When it fails

Diagnostic codes: `undeclared-edge`, `not-exported`, `private-module`, `private-name`,
`cycle-edge`, `restricted-python`, the patch codes and `missing-patch-target` ([[meizel/docs/checker/patching.meiva.okf.md|patching]]),
the 15 purity and bounded-container codes ([[meizel/docs/checker/purity.meiva.okf.md|purity]]),
`ambient-read`, `legal-read-recorded`, `stale-root`, `stale-reader`, `stale-approval`,
`fake-entropy` ([[meizel/docs/checker/fake-entropy.meiva.okf.md|fake entropy]]),
`unresolved-import`, `unlisted-allowance`,
`stale-load`, `stale-external`, `policy-syntax`, `debt-paid`, and the manifest codes
(`manifest-` + syntax, unknown-key, unknown-child, duplicate, self-edge, cycle, export-private,
stale-edge, debt, roles). An edge that closes a cycle says so; the way out is a public door or injection.

## Layout

`cli.py` parses commands; `analysis.py` assembles universe, scans, manifests and diagnostics;
`universe.py` reads git; `scan.py` and the files beside it extract facts from one file;
`manifest.py` validates manifests; `rule.py` projects and judges; `graphs.py` holds components and
cycles; the rest implement the other commands.

Parent: [[meizel/docs/meizel.meiva.okf.md|meizel]].
