---
type: doc
title: "meizel/ — The Policy File"
description: "meizel.policy.toml is the single closed list of allowances (external sites, dynamic loads, default-argument injections, and the exemptions of the lint rules); what a manifest may and may not grant, how the lints read their tables, and how a change is reported."
tags:
  - locality
  - tool
---

# meizel/ — The Policy File

## What is it

Some forms are legitimate only where a person decided they are: a file that loads
code at run time (`[loads]`), a file that runs code from outside the repository
(`[external]`), and default-argument injection. A manifest used to grant these by
itself, with a reason in free text. A reason in prose stops nothing, so the grant
moved out: `meizel.policy.toml` at the repository root is the one place that lists
every allowance, and the only hard-coded one.

```toml
[external]
"app/runner.py" = "executes the schedule script the user wrote"

[loads]
"app/loader.py" = ["meiva_builtins"]

[default-arguments]
```

## Narrowing a language's scope

`[typescript].scopes` is the one table that narrows a rule's reach: with no
scopes every TypeScript source is judged, and listing directories judges only
those (`import-sources` keeps callers' edges, not their behavior — see
[[meizel/docs/checker/typescript.meiva.okf.md|the TypeScript node]]). Widening is
the deliberate direction: a directory that joins the list becomes judged with a
new manifest of its own, its existing reads turn into recorded debt, and the
recorded-debt gate reports the growth as the rules' doing rather than the
change's. Narrowing again is a decision review must see.

## Lint-rule tables

The same file holds the exemptions of the lint rules that live in `meizel/`, one top-level
table per rule, so no rule keeps a list of exceptions in its code. A table has sections of
two shapes: `name = "reason"` entries (a file, a site, an environment variable; the reason is
required) and lists of names (a vocabulary of callees). The rules and their sections are the
closed list `RULES` in `meizel/allowances.py`, which is also the only reader: a typo in a
table or section name is `policy-syntax`, not an exemption that silently allows nothing.

| Table | Rule | Sections |
|---|---|---|
| `ambient` | the names through which the repository's own state is reached (`meizel/contracts/ambient_read.py`, [[meizel/docs/checker/ambient.meiva.okf.md|ambient reads]]) | `settings`, `db`, `endpoints`, `bus` (lists; `name()` acquires only when called) |
| `ambient-readers.<kind>` | functions that read the world on their caller's behalf (`meizel/contracts/ambient_read.py`, [[meizel/docs/checker/ambient-readers.meiva.okf.md|readers]]) | one table per kind of `AmbientKind`: `pkg.module.name()` or `pkg.module.Class.method()`, or `pkg.module.NAME` for an object read at every use, and what it reads |
| `ambient-state` | module-level state (`meizel/state/ambient_state`) | `sink-callees`, `sink-callee-suffixes`, `wiring-callees`, `pure-callees`, `pure-chain-callees`, `skill-script-patterns`, `config-modules`, `config-readers` (lists); `sink-facades`, `pure-repo-callees`, `sites` |
| `clock-lattice` | lattice-vocabulary constants | `family-modules` (list); `independent-clocks` (`path::NAME`) |
| `single-owner.<decision>` | one owner per decision | one table of module paths per decision, e.g. `postgres-dial`, plus `<decision>-owners` lists |
| `no-os-environ` | a test sets an environment variable that `Settings` models | `provider-key-vars`, `live-read-vars`, `files` (test files that exercise the settings-free boundary); `test-only-dirs`, `rule-inputs` (lists) |
| `turn-scoped-config` | per-agent settings reads | `files`, `plugin-mechanism-files`; `scan-dirs`, `extra-files`, `rule-inputs` (lists) |
| `path-imports` | runtime `sys.path` mutation | `scope`, `skills-scope` (lists) |
| `python-commands` | bare Python subprocess invocations | `argv-wrappers` (list) |
| `approved-patches` | test-support files that patch on purpose (`meizel/contracts/analysis.py`, [[meizel/docs/checker/patching.meiva.okf.md|patching]]) | `files`; `max-files` (the list's budget: past it a run fails as `approved-budget`) |
| `purity` | mechanical purity rules (`strict` / `strict-to-be`) | `impure-imports`, `impure-calls`, `impure-fixtures`, `max-uses`, `max-exports`, `max-public-symbols`, `max-params`, `max-attr-hops` (lists) |
| `type-checking` | `if TYPE_CHECKING:` | `files` |
| `typescript` | TypeScript/TSX scanning (`meizel/language/typescript.py`) | `scopes`, `import-sources`, `aliases`, `extensions`, `impure-calls`, `impure-hooks` (lists) |

An entry that names a file or a site still goes stale when the file is gone or the site is no
longer reported; the lint fails and says which table to remove it from. A lint reads the file
when it is imported, and a change to the file widens a changed-files run to the full scan, so
removing an entry is judged against the whole repository.

## What is checked

- A manifest `[external]` site, or a `[loads]` site with its directories, that the
  policy does not list is `unlisted-allowance`. The reason written in a manifest
  has no effect. Where the policy lists nothing, nothing is allowed.
- A malformed policy, an unknown table or section, or an entry of the wrong shape is
  `policy-syntax`, reported at the policy's own path.
- `[default-arguments]` is closed and empty: injecting a collaborator through a
  default parameter is not allowed. If it ever is, the allowed values are written
  in that table and checked by the tool.
- An entry the policy lists but no manifest uses is harmless; removing code never
  needs a policy edit.

## Changing it

Agents do not edit this file unless the task names the entry. `python -m meizel policy
diff --against <ref>` lists every entry added, removed or changed since the ref.

`python -m meizel policy surface --against <ref>` reports the same comparison, plus every manifest's
`[roots]` and `[state]`, as one markdown block for a check summary: each declaration by direction -
`narrower` where the checker stops reporting a site, `wider` where it starts, `moved` where a
declaration followed its file in git's own rename, `unknown` where the table's own format does not
decide it, and the sources it could not read, named rather than counted as empty. It lists and never
fails: adding an allowance has no gate of its own, so this block is the one place a quiet widening
shows up. In a Trunk batch the tree also carries the batches queued ahead of it, so a line may belong
to one of them.

Adding is quiet and removing is not. An entry that disappears narrows what the checker still
reports, so it carries its own record in the pull request body; every added, removed, modified or
moved declaration is listed, and a removal is never answered by a pull request that did not make
it. [[meizel/docs/checker/policy/removal-gate.meiva.okf.md|The allowance-removal gate]] owns the
line's shape, the batch rule and what an unreadable record does.

Parent: [[meizel/docs/checker/checker.meiva.okf.md|the checker]].
