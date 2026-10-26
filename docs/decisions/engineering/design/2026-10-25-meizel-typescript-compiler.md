# Compiler-backed TypeScript as a first-class Meizel language

## Scope

Meizel must analyze Python and TypeScript through the same check, explain,
bootstrap and debt contracts. The external Harness audit exposed false import
sites, missed imports and alias failures in lexical TypeScript extraction.
Full syntax parsing and binding-aware observations are required; proving arbitrary
runtime effects is not. The target scale is a several-thousand-file monorepo.
The user explicitly requested first-class Python and TypeScript checking.

## Prior art

Python supplies `ast` in its standard library. TypeScript's installed compiler
supplies source trees, symbols and module resolution. Meizel already uses that
locked compiler for explicit holder evidence, and the frontend toolchain already
installs it. Tree-sitter would parse syntax but require an additional parser and
separate binding/module resolution. More regex extensions repeat the failures
that the external audit demonstrated.

## Simplest sufficient design

Use a bounded Node subprocess once per TypeScript scan batch, with the existing
locked compiler, and return validated source facts to the Python checker. The
same tracked universe, source overrides, policy, rule codes, manifests and debt
reconciliation govern both languages. Python-only scans never start Node.
Compiler startup or parse failures fail analysis explicitly; no regex fallback
turns unavailable analysis into a clean result. The main check consumes compiler
facts directly rather than asking users to produce a separate evidence artifact.

The language frontend owns parsing and language-specific interpretation. Shared
rules own locality, policy, purity modes, diagnostics and recorded debt. A language
support table states applicable rules and bounded observations; Python-only
fixture/type-introspection rules do not become fictitious TypeScript checks.

## Decisions and consequences

The former stdlib-only constraint remains for the Python checker and Python-only
usage. It no longer forbids the existing Node/compiler dependency for TypeScript
analysis. Sources are parsed, never evaluated. The compiler version remains owned
by the existing frontend lock; no new dependency or resident service is added.
A single bounded process avoids a Node launch per file and bounds compiler heap.
Project compiler configuration governs module resolution; explicit policy aliases
remain supported for existing callers. Compiler facts and source overrides belong
to the same invocation, including staged/snapshot and debt-carry analysis.

The existing optional holder evidence remains descriptive, not a second verdict
engine. It must not be mistaken for coverage of the main rule set. AST parsing
supports the language grammar; unresolved bindings, dynamic imports and runtime
callback effects still require explicit diagnostics or documented limits.

## Alternatives and validation

Retaining regex plus opt-in AST receipts would preserve minimal installation but
leave ordinary checks incomplete. A tree-sitter frontend would add a dependency
and duplicate compiler responsibilities. Rewriting the checker in Node would
unnecessarily move Python rule ownership. These alternatives are rejected.

Validation pairs Python and TypeScript examples for shared rules, exercises
methods/closures/TSX/shadowing/project aliases and failed compiler admission,
and runs normal CLI/debt commands and the pinned external Harness checkout.
Measure batch performance before introducing persistent workers or caches.

The pinned Harness snapshot selects 6,177 TypeScript files (about 62 MB of source).
A 2 GiB heap failed during AST construction; a bounded 4 GiB run completed with
about 3.1 GB peak resident memory. Source expansion into installed dependencies is
disabled, project-resolution configuration is cached, and no full type inference
is requested. This bounds the intended lexical analysis without claiming a full
semantic build. The maximum remains explicit; an exceeded bound is an error.
