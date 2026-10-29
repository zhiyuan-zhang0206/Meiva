---
type: doc
title: "meizel/ — Recorded Locality Debt"
description: "Strict and strict-to-be directories: how a meizel.toml records violations a directory may keep, why the baseline can only shrink, and the debt, record, prune and diff commands."
tags:
  - locality
  - tool
---

# meizel/ — Recorded Locality Debt

## What is it

The gate cannot wait for every violation to be fixed first. A directory can
record the violations it has today in its own `meizel.toml`; `check` then
stops new ones at once and the recorded ones are repaid a step at a time. How a
repository wires this into hooks and CI is that repository's policy.

## Two modes, no switch

A directory's mode follows from its baseline:

- **strict**: nothing recorded. Every violation fails.
- **strict-to-be**: something recorded. A recorded violation passes, a new one
  fails, and a recorded debt that no longer occurs also fails (`debt-paid`,
  repaid by `debt prune`). The baseline can therefore only go down, and a
  directory whose baseline reaches zero is strict again.

These are debt states, separate from manifest `purity` modes. An empty baseline
does not establish semantic purity, effect freedom or test-resource requirements.

There is no lenient mode and no per-directory switch. A violation in a
strict-to-be directory names the directory and how many debts it carries; the
fix is to repair it, since `debt record` is for deliberate exceptions.

## The baseline is local

A baseline sits in the directory that judges the violation, like a declared
edge, so concurrent repayments touch different files:

- an edge violation (`undeclared-edge`, `not-exported`, `private-module`,
  `private-name`, `cycle-edge`) belongs to the lowest common directory `L` of
  the fact edge;
- `restricted-python` belongs to the directory of the file.

```toml
[debt.private-name]
"test_a.py -> a.py#_helper" = 3

[debt.restricted-python]
"loader.py#importlib.import_module" = 1

[debt.cycle-edge]
"x/m.py -> y/n.py" = 2
```

Paths are relative to the owning directory. A key carries no line number, so
moving code inside a file leaves the baseline alone; the count is how many
occurrences are allowed. Tables are sorted, one debt a line. `[debt]` does not
count toward the directory budget. `cycle-edge` records the facts on the declared edges that close a cycle;
the declared graph without those edges must be acyclic, or `manifest-cycle` fails.
Each fact on such an edge counts like any other: another import line in the pair
is a new violation, so the recorded facts can only shrink. When an edge closes no
cycle any more, or is no longer declared, its entries are `debt-paid`. A malformed
`[debt]` is `manifest-debt`. Unresolved imports, stale declarations and manifest
syntax errors cannot be recorded.

## What one entry says

A `[debt.<code>]` line records a count, and may name the owner paying it down and the condition it
waits for:

```toml
[debt.ambient-read]
"page.py#clock time.time()" = 2                                   # no owner yet
"boot.py#environ os.environ" = { count = 1, owner = "#7139", until = "waits for the Settings port" }
```

Both spellings read back as one entry. `count` is required and positive; `owner` and `until` are
optional, and an empty or unknown field is `manifest-debt` rather than dropped — a misspelt `owner`
must not read as "no owner". A baseline that names nobody stays byte-identical (a bare count), and
`record` and `prune` keep the metadata while they change the count.

`check` says how many entries name no owner, and `debt --unowned` lists them as
`<manifest>: <code> <key> (<count>)`. That number is a to-do, not a gate: requiring an owner on every
new entry is a later change, once the recorded baseline has been backfilled.

## Commands

- `debt` inventories manifests without source analysis, even for unparsable code.
  JSON adds `basis` describing that limit and `unowned` (the entries naming no owner, and the
  occurrences they carry); its legacy `directories.strict_to_be` and `strict` mean with/without
  recorded debt, not manifest purity modes. `--unowned` appends a list of them below the
  tables, one entry per line.
- `debt record <paths...>` writes the current violations under the paths into
  their owners' manifests, creating one where there is none (stage it: `check`
  reads tracked files). It is the only way a baseline grows: `check` and
  `bootstrap` never write one, and `record` never lowers a count. Cycle
  back-edges are chosen greedily and thinned to the ones needed. Violations
  that cannot be recorded are printed and the exit code is 1.
- `debt prune` lowers counts and drops entries to what still occurs.
- `debt resolve <meizel.toml>...` resolves merge conflicts inside `[debt.*]` tables: recorded debt is
  derived, so both sides' entries are kept, the lower count wins where both changed, and `prune` follows.
  Two sides that name different owners are refused: who pays a debt down is not derived, so a person
  decides it. A conflict outside the debt tables is refused too; nothing is written.
- `debt carry [--base ref]` follows a rename: it relocates the recorded entries of a moved file,
  patch target or symbol to the owner and key they now have, never adding one, and writes the
  manifest a moved debt needs. See
  [[meizel/docs/checker/debt/carry.meiva.okf.md|carrying recorded debt to a moved file]].
- `debt sites --code <patch code> [--target-prefix P]... [--json] [paths...]`
  lists where the source holds each patch of a code: file, line, form and
  target, sorted by target. A prefix is dotted (`lib.mod` holds `lib.mod.f`, not
  `lib.mod2`), several are a union, and paths limit the scan. It reads the
  source, not the baseline, so it lists what a codemod has to change whether or
  not it is recorded.
- `debt approved [--json]` lists the files `[approved-patches.files]` approves
  with the patches each holds now. They are not debt and no baseline counts them.
- `debt diff --against <ref> --on-growth fail|warn` compares the recorded totals per code with those at
  a git ref, and reports `rules` and `content` apart whenever the change edits what is measured.
  `--by-target` adds the recorded patch debt per target group. The caller chooses `fail` or `warn`;
  there is no default. See [[meizel/docs/checker/debt/diff.meiva.okf.md|the two measures of a debt diff]].

## Depths of `check`

- `check --changed files...` is the pre-commit depth. It reads every manifest
  (so the declared graphs are checked in full), scans only the named Python files
  plus the conftests and plugins that fixture resolution needs, and judges those
  files' fact edges and restricted forms, every `[loads]` declaration, and the
  recorded debt about the named files. It does not look for stale declarations,
  and the effect of a manifest edit on files that did not change waits for the
  full run.
- `check` analyses the whole repository, including stale declarations and
  cycles. It is the depth for pre-push and CI.
- `debt diff` is the extra CI step against the merge base.

Parent: [[meizel/docs/checker/checker.meiva.okf.md|the checker]].
