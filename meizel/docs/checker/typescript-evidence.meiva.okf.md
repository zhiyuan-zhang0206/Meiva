---
type: doc
title: TypeScript holder source evidence
description: Explicit locked-compiler receipts for module-owned collection initializers, their validation and unknown boundaries.
tags: [tool, locality, testing]
---

# TypeScript holder source evidence

`explain` can show non-gating source observations from an explicitly supplied
TypeScript compiler index. Ordinary `check`, `analyze` and `explain` never start
Node or install dependencies. Absence of observations is not evidence of purity,
immutability, effect freedom or static-test admission.

Generate one batch with the locked compiler already installed under `ui/web`:

```sh
node meizel/source_evidence/typescript/producer.mjs --repo "$PWD" ui/web/src/lib/example.ts > /tmp/ts-holder-index.json
.venv/bin/python -m meizel --jobs 1 explain ui/web/src/lib/example.ts --typescript-index /tmp/ts-holder-index.json
```

Pass every selected runtime `.ts`/`.tsx` file when explaining a directory; a
missing, duplicate or unexpected source is rejected. The tool installation
owns the compiler/adapter dependencies; `--repo` owns the selected source files.
The producer uses one compiler context, restricts source resolution to the
explicit files and locked default libraries, and never evaluates source code.
It uses the compiler library's public APIs, not the source-note index protocol,
note ownership traversal or an additional regex scope parser.

## Evidence and limits

The first extractor follows only SourceFile-direct variable initializers,
transparent type/parenthesis syntax, simple literal property paths and an
`Object.freeze` wrapper with known receiver and callee declarations. It requires
all relevant declarations to come from the locked default library. Source-local
shadowing, imported/aliased/mixed or unresolved bindings remain unknown.

A module-owned const holder can reach a mutable-capable Map or Set. This is an
allocation/reference observation, not proof of actual sharing, writes or runtime
effects. Freeze provenance belongs only to the directly wrapped collection;
freezing an outer object does not freeze a nested collection. A shallow frozen
surface does not prevent collection-content mutation. Property segments are
displayed with JSON bracket escaping to preserve literal keys.
Standard-library declaration provenance does not rule out runtime replacement.
Class fields/static blocks, namespaces, conditional/block declarations, factories,
IIFEs, arrays, alias/assignment/escape flow and other execution regions are not
resolved. A spread, duplicate, computed, shorthand or accessor property makes
the corresponding whole holder path unknown. Function-local allocations are not
automatically module shared or pure; no general scope/effect/type system is used.

## Admission and ownership

The standard-library-only loader decodes the exact version-one JSON shape into
frozen dataclasses and tuples, rejecting unknown fields and invalid raw types.
The immutable index records source hashes/ranges, compiler version, adapter and
standard-library dependency hashes, and declaration locations. The explicit
loader reads the supplied artifact and verifies its exact selected-source set,
repository root, schema, source/dependency hashes and locked compiler identity.
It delegates actual UTF-8 disk decoding to the existing `read_utf8_text` owner;
this is I/O, not a claim of semantic purity. Invalid or stale receipts fail with
`MeizelError`, rather than becoming empty successful evidence. Valid parser
failure receipts are displayed as unavailable. Statuses distinguish not-requested,
not-applicable, unavailable and observed; observed records may still be unknown.

The CLI passes a required typed report value to the existing I/O-free renderer.
TS hashes describe an independent AST snapshot: current Analysis does not carry
a comparable digest, so graph/diagnostic snapshot identity is not asserted.
These are local source receipts, not signed authentication of a producer.

The existing Python input-list/fixture evidence and its display limits remain.
Source observations do not enter PurityItem, Fact, Code, diagnostics, verdict,
recorded debt, bootstrap JSON, selectors or static admission. The baseline-only
`debt` report retains its separate evidence basis. No caching or speedup claim is
part of this feature. Owner: [[meizel/docs/checker/purity.meiva.okf.md]].
