---
type: doc
title: "meizel/ — Type Assertion Ratchet"
description: "Canonical typing.cast calls in production Python, their source positions and stable recorded-debt groups."
tags:
  - locality
  - tool
---

# meizel/ — Type Assertion Ratchet

`typing.cast` asserts a type without validating its value. Meizel reports each
canonical call in tracked production Python as `type-cast`, independently of
manifest purity modes. Existing test-file and test-path classification excludes
tests and their helpers. Importing or referencing the callable is not a call.

This ledger ratchet narrows the original plan's blanket hard error in strict
purity directories: a `purity = "strict"` file may retain only its authorized
untouched-base type-cast groups/counts. An unauthorized key or a count increase
fails. This does not redefine strict purity or make its other purity violations
recordable, and does not claim that strict files contain no old casts.

The scanner uses its existing import binding resolution: direct imports, renamed
imports and imported-module attributes are recognized. Local parameters, stores,
match captures and expression scopes keep their lexical meaning; see
[[meizel/docs/checker/lexical-bindings.meiva.okf.md|lexical bindings]]. Other
modules' `cast` methods and ordinary same-named collaborators are not typing
assertions. Quoted type arguments do not change the call's identity. A real call
inside `TYPE_CHECKING` or another syntactic branch remains a call.

This does not follow callable values through assignments, containers,
first-party re-exports, dynamic attribute access or interprocedural data flow.
It is not proof that every runtime invocation has been found. Function/class type-parameter bounds retain the existing scanner's unvisited
boundary; type-alias bounds retain their existing traversal in the proper
annotation view. Class/current-type-parameter collisions remain unresolved
rather than being asserted as imports. This rule does not claim complete Python
annotation-scope analysis or every runtime call.

## Position and recorded group

A source site contains its line, UTF-8 byte column and lexical qualified name.
The column appears in the diagnostic message, so two calls on one line remain
two diagnostics and two occurrences. Positions are for locating a call, not its
baseline identity.

A site's debt belongs to its file's directory, with a stable
`file.py#cast qualified.name` key. Module code uses `<module>`; named class and
function bodies append their names. Definition headers belong to their outer
scope. Lambda defaults and the first comprehension iterator also belong to the
outer scope. Anonymous bodies append fixed `<lambda>`, `<listcomp>`, `<setcomp>`,
`<dictcomp>` or `<genexpr>` tokens. Same-kind anonymous siblings share a group;
there is no line number or sibling ordinal in a key.

Each call contributes one occurrence. Moving lines preserves the group/count;
adding another call exceeds its recorded count. Removing calls requires
`debt prune`; moving a file can use `debt carry`. This group-count contract does
not identify individual sibling expressions or detect an equal-count replacement
within one unchanged key.

## Introducing the rule

A new rule's initial baseline must come from its actual findings in fixed,
untouched base source. The checker implementation used for that scan and all
base source hashes must be identified. Only this code's original per-owner/key
counts authorize its new records, including records for the checker itself.
New files and new keys have an original-content allowance of zero.

The normal CI rule-change debt comparison still runs. Its disposable base
snapshot overlays changed checker files from the head to execute the new rule;
that snapshot alone cannot prove that new checker source existed in the original
base. The independent untouched-base scan supplies that distinction. Neither
comparison authorizes another violation code, an allowance, or new content debt.
