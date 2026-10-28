---
type: doc
title: "meizel/ — TypeScript compiler analysis"
description: "Compiler-backed TypeScript facts, shared rule contracts, project resolution, explicit failures and language-specific coverage."
tags: [locality, tool]
---

# TypeScript compiler analysis

Python and TypeScript enter the same `check`, `bootstrap`, `sync`, `explain` and
`debt` pipeline. Python uses the standard-library AST; TypeScript uses the locked
TypeScript compiler installed in the **tool checkout's** `ui/web/node_modules`.
Install Node and run `npm ci` in that directory before checking TypeScript. Meizel
never installs dependencies or executes the inspected code. Python-only checks do
not launch Node. CLI checks and bootstrap reports identify source counts by backend.

## Source and project selection

Tracked `.ts`, `.tsx`, `.mts` and `.cts` files are selected by default. Declaration
files provide dependency targets but are not runtime rule subjects. The checked
repository can narrow selection in its tracked `meizel.policy.toml`:

- `[typescript].scopes` selects fully checked directories. With no scopes, all
  TypeScript sources receive the applicable rules.
- `import-sources` adds callers whose imports into those directories are included;
  it does not assert that the callers' behavior has been checked. With explicit
  scopes, only edges into those scopes are judged (the existing policy contract).
  A `[roots]` table written in a directory outside the scopes exempts nothing —
  the exemption is read when that file's own reads are judged, and no file there
  is — so `check` reports each such table as a warning naming the scopes that
  were selected instead of passing over it in silence.
- `extensions` can narrow the supported extensions.

The nearest `tsconfig.json`, its `extends`, `paths`, module-resolution mode and
installed package exports govern imports. Project dependencies/config extensions
must be available in the inspected checkout. No installation is attempted.
Optional `aliases` override compiler paths for deliberate source mappings:
`@pkg=packages/pkg/src/index.ts`, `@pkg/*=packages/pkg/src/*`, or `@/=src/`.
The compiler performs extension substitution, including `.js` to `.ts`.
Only tracked repository targets become first-party facts; unresolved relative,
workspace-package and configured-alias imports are explicit `unresolved-import`
diagnostics. Computed loaders require the same policy-approved `[loads]` or
`[external]` declaration as Python; stale declarations fail the common check.

One Node process with a 4 GiB heap limit and a 180-second deadline parses the selected source snapshot. The compiler
program contains selected roots and default libraries, without recursively
expanding every installed dependency into its binding context. Module resolution
is performed separately for each import using its project's options. The checker
uses lexical symbols and declarations; it does not request full semantic/type
checking or prove effects of arbitrary calls. Run the project's `tsc` separately.
Malformed syntax/configuration, missing compiler, version mismatch, timeout,
process failure or malformed/incomplete compiler output fail the run (exit 2).
There is no regex fallback and no partial clean result.

## Shared contracts and language coverage

Both frontends emit the common `Fact`, `PurityItem`, `Patch`, acquisition and type
boundary records. The existing rules own manifests, locality, cycles, purity
modes, diagnostic codes, stable debt keys and debt growth checks. A new frontend
does not carry a second policy engine. TypeScript symbol moves use AST ranges,
including top-level arrow functions and braces inside strings.
Source selection and scanning share the checker's admitted policy, so malformed
policy retains the same path-attributed diagnostic in both languages.

| Concern | Python | TypeScript |
| --- | --- | --- |
| Dependency graph | Imports, module attributes, pytest fixtures | Imports/re-exports, namespace attributes, import types, literal `import()`/unshadowed `require()`, import-equals |
| Structure | Function/class AST, parameter and behavioral-surface budgets | Functions, methods, arrows, classes and the same default budgets |
| Type boundary | `typing.Any`, `typing.cast`, untyped boundary annotations | Explicit `any`, type assertions, explicit untyped boundary annotations; inferred types are allowed |
| State/effects | Configured calls, async, unfrozen dataclasses | Configured calls/hooks, async, mutable value classes and known module collection allocations |
| Encapsulation | Nested writes, deep attribute chains, enum subsets | Compiler-node equivalents, with lexical import/shadowing resolution |
| Ambient state | Roots/state/ambient policy | The same contracts, including clock, environment, randomness and configured state |
| Test patching | Python patch forms and fixture rules | Vitest/Jest module/spies/global/environment replacements and imported-object writes |
| Runtime-specific rules | Python commands, `TYPE_CHECKING`, pytest fixtures | Not applicable; these do not count as TypeScript coverage |

Constants and type-only declarations do not inflate behavioral surface counts.
`as const` and `satisfies` are not unsafe type assertions. Known frozen Map/Set
surfaces still permit collection-content changes. Unknown factory results are
not claimed to be mutable allocations. Static rule results do not establish
whole-program purity, deep immutability, runtime cycle behavior or domain-level
architectural quality. Counts from different policies and coverage are not quality
rankings. The optional [[meizel/docs/checker/typescript-evidence.meiva.okf.md|holder
receipt]] supplies separate, bounded provenance observations.

## Environment reads

`ambient-read` ([[meizel/docs/checker/ambient.meiva.okf.md|ambient reads]]) counts `process.env` (also `process.env[name]`),
`process.cwd()`, `Date.now()`, `Math.random()`, crypto calls and the browser's `location`, `localStorage`,
`sessionStorage`, `document.cookie` (`environ`), `navigator` and `matchMedia` (`platform`), bare or behind
`window.`, `globalThis.` or `self.`, called, indexed or passed as a value. Only the global counts: a local, a
property or an imported package of the same name does not, nor does `typeof location` or a type position.
Not seen: a destructuring of `window` (`const {location} = window`) and `parent.`/`top.` prefixes.

`process.env.NEXT_PUBLIC_<NAME>` read in `ui/web/src` (`INLINED_ENV_ROOTS` in `typescript_compiler/config.py`) is not
a read: the Next.js build replaces it with the value it saw, in client, server and edge bundles, when the variable is
set at build time (`define-env.js`, `static-env.js`; an unset one is read at run time on the server). The root must be
the global `process` and the hop to `env` must not be optional (`process?.env` is not replaced by Turbopack).
A write (`=`, `+=`, `??=`, `delete`, `++`), a computed key, a destructuring or a copy of `process.env`, other variables
and any file the build does not bundle still count.

Parent: [[meizel/docs/checker/checker.meiva.okf.md|the checker]].
