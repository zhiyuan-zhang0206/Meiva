---
type: doc
title: "meizel/ — Type-only Guards"
description: "The `type-checking` code: direct `if TYPE_CHECKING:` guards in governed Python, the roles and `[type-checking.files]` policy analysis applies, and how `diagnose` and the standalone adapter report them."
tags:
  - locality
  - tool
---

# meizel/ — Type-only Guards

`type-checking` is a hard diagnostic, not recordable debt or a purity mode. The Python scanner
records direct guards resolved to `typing.TYPE_CHECKING` while retaining all facts inside both
branches. Analysis applies the checked root manifest's application/standalone/tooling/templates/tests roles
and that root's `[type-checking.files]` policy. Helpers and root/package conftest entries are
covered; direct `test_*.py` modules, examples and frontend helpers are not newly governed.
Boolean formulas, negated guards and dynamic lookups are outside this direct-guard rule.

The standalone code-structure adapter shares one checked-repository Universe, Policy and role
set per invocation and reuses parsed trees in the same scanner. It preserves untracked and
explicit resolved-alias coverage that tracked-regular-file analysis cannot replace. The
sibling-import and harness-runtime import accounting rules retain their separate contracts.

`diagnose(..., policy=...)` uses the explicit policy for type-only guard diagnostics. Its legacy
purity pass still uses its original empty Policy; this API does not promise that all existing
purity diagnostics equal full analysis.

Parent: [[meizel/docs/meizel.meiva.okf.md|the locality rules]].
