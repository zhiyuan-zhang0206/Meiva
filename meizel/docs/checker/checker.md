# meizel/ — Locality Checker

## What is it

Meizel judges one question: may file `f` use file `t`? Every directory declares
a small DAG over its direct children in `meizel.toml`; the checker derives the
facts from source and fails when a fact is not covered by a declaration. The
decision and its rationale live in
[the accepted decision](../../../docs/decisions/engineering/design/simplification/2026-10-11-meizel-nested-dag-locality.md).

The checker never executes inspected source. Tracked files come from `git ls-files -z`
(untracked source/manifest files trigger a `stderr` warning suggesting `git add -N`);
scan results are cached by content digest in `<git-common-dir>/meizel-scan-cache-v1.sqlite3`.
Python uses stdlib AST; TypeScript uses the locked Node compiler. See TypeScript
scanning below for setup and coverage.

```
python -m meizel check [paths... | --changed files...] [--limit N] [--budget-input snapshot.json]
python -m meizel debt [--json] | record <paths...> | prune | carry | diff --against <ref> --on-growth fail|warn
python -m meizel explain <path>
python -m meizel impacted [--python] [paths...]
python -m meizel sync [--prune] [--declare [paths...]]
python -m meizel bootstrap --out <dir>
python -m meizel roles list <role>... | projects | check | write
```

`--repo <root>` selects another checkout; `--jobs N` sets parser processes. Exit codes: 0 clean, 1 violations, 2 the tool failed.

## Commands

- `check` is the gate over the whole graph; `paths` narrow what is reported.
  Each diagnostic gives `file:line`, the fact edge, the judging directory `L`,
  and the manifest line to add or public door to use. `check --changed files...` judges only those files.
- `debt` manages recorded violations, a per-directory baseline that may only
  shrink: [recorded debt](debt/debt.md).
- `explain` shows graph edges and bounded [Python source evidence](explain.md).
- `impacted` prints tracked `.py` paths in the transitive reverse-import closure
  of `paths` (default: files changed since `origin/main`), exiting 2 (fail-closed)
  if any tracked `.py` file cannot be read or parsed.
- `debt propagation` measures the world reads a call site inherits through a
  function and records nothing for: [world reads that travel through a function](debt/propagation.md).
- `sync --prune` drops stale declarations; `sync --declare` writes the legal new
  dependencies of the changed files: [keeping manifests current](sync.md).
- `bootstrap --out DIR` writes one `meizel.toml` per directory under `DIR` from
  current fact edges (non-edge tables kept) and a `report.json` of edge counts,
  components, witness facts and violation counts; the repository is untouched.

## Budget headroom

`check` publishes one complete table in `.cache/meizel/budgets.md`, independently
of its diagnostic `--limit`. It reports the existing source file/directory budgets;
`--changed` marks the edited files and their ancestor directories. Only changed
rows with at most ten percent remaining enter the human summary. A full run with
no changed surface still publishes the full table and has no near-cap summary.
Existing hard gates retain their limits and exit behavior; this report adds none.
An additional Source column binds each numeric row to its displayed owner, snapshot
and measurement provenance.
Unavailable measurements are identified explicitly rather than represented as zero.

A standalone Memory pool retains ownership of its own limits. To include one,
explicitly export its snapshot and pass it with `--budget-input` (repeatable):

```sh
python /path/to/vault/validate.py --budget-report --bundle /path/to/vault \
  --changed memory/topic/note.md > /tmp/pool-budgets.json
python -m meizel check --budget-input /tmp/pool-budgets.json --changed app/edited.py
```

Coverage is explicit: a missing pool input is `not-provided`, a requested unreadable
or malformed input is `unavailable`, and empty rows, missing kinds or missing
provenance are `partial`. `complete` means the declared scope has readings and
provenance; it proves neither live freshness nor deployment. Unknown readings get
no fabricated zero row. Source metadata remains visible beside the complete table
and any changed-row summary.

The Meiva scaffold's `memory-template` filesystem scope and the live Vault owner's
`memory-pool` tracked/staged scope are separate. A template snapshot never satisfies
live pool coverage. The live owner's required kinds include the hook-owned index
character budget; omitting it keeps that owner partial. Limits and counting remain
with their owners rather than being copied into the consumer.

Snapshots describe the readings at export time; regenerate after an edit. The
checker never discovers a live Vault or executes the code of an inspected project.
A missing/unreadable snapshot appears as unavailable and leaves the graph verdict
unchanged. An unwritable artifact falls back to printing the complete table.

## Fact edges

- `import` and `from ... import` at any nesting level. Relative imports resolve
  against the importing file's directory. `from pkg import name` targets the
  submodule `name` when `pkg/name.py` or `pkg/name/` exists, otherwise
  `pkg/__init__.py` with `name` recorded as a used name. Root imports follow
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
  ([mounts](mounts.md)).

TypeScript aliases, supported syntax and analysis boundaries:
[TypeScript dependency scanning](typescript.md).

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

Dynamic imports, `exec`/`compile`, non-literal Python subprocesses, string patch targets
on first-party code, `sys.modules` writes, and attribute names or `__getattr__` hooks that only
the run resolves are governed by
[[meizel/docs/checker/restricted/restricted.md|restricted Python, `[loads]`, `[external]`, and `[dynamic-attrs]`]].

## Frontend dependencies

The TypeScript analyzer answers only from a frontend tree this checkout installed from its own
lockfile; a tree that is missing, was installed by the retired npm path, or belongs to another
lockfile is refused before node starts. States, remedies and exit code:
[frontend dependencies](../frontend-deps.md).

## Manifest

`meizel.toml` keys are `purity` / `[purity]` ([purity](purity.md)),
`exports` (list), `[uses]` ([narrowed endpoints](narrowing.md)), `[loads]`, `[external]` and `[dynamic-attrs]`
(sites in [the policy file](policy/policy.md)), the policy's
`[injected-calls]` ([declared injected calls](policy/injected-calls.md)),
`[state]` and `[roots]`
([ambient reads](ambient.md)) and `[debt.<code>]`. Names are
direct children; `meizel.toml` is outside the directory budget. A directory without one has no
local edges and exports its door.

## When it fails

The codes the checker reports - `undeclared-edge`, `not-exported`, `private-module`, the restricted
Python and patch codes, the 15 purity and container codes, the ambient codes, `unresolved-import`,
`unlisted-allowance`, the stale and policy codes, `debt-paid` and the manifest codes - sit beside the
rule each belongs to in [the diagnostic codes](../diagnostics.md).

## Layout

`cli.py` parses commands; `analysis.py` assembles universe, scans, manifests and diagnostics;
`universe.py` reads git; `scan.py` and the files beside it extract facts from one file;
`manifest.py` validates manifests; `rule.py` projects and judges; `graphs.py` holds components and
cycles; the rest implement the other commands.

Parent: [meizel](../meizel.md).
