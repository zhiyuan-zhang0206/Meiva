---
type: doc
title: "meizel/ — Explain Source Evidence"
description: "Graph boundaries and bounded Python mutation and fixture-request observations."
tags:
  - locality
  - tool
---

# meizel/ — Explain Source Evidence

`python -m meizel explain <path>` keeps the parent graph and crossing fact edges,
then shows bounded Python source observations. These observations do not enter
Fact edges, diagnostics, verdicts, debt, bootstrap JSON or test selection.
The command's successful exit does not prove semantic purity or resource safety.

## Direct parameter append

Scanner records a direct `.append(...)` receiver annotated with unshadowed builtin
`list` in a top-level function, when no local rebinding is observed, including pattern captures. Local fresh copies, rebound parameters, custom or
unannotated receivers, star-import/type-parameter ambiguity, nested functions, lambdas and
comprehensions are excluded. The line identifies function and parameter syntax;
annotations do not establish the runtime type or that the call executed. Aliases,
fields, other mutators and interprocedural effects remain unmeasured. Intentional
accumulator mutation can be useful; an observation is not a refactoring order.

## Explicit fixture requests

Scanner attaches requests to their defining function. Explain starts with tests,
not every fixture in their files. It follows a selected fixture's own explicit
requests using the consuming test's visibility: module, nearest conftest outward,
then plugins. Registered fixture names govern active same-name override depth;
file and definition line identify the provider. Known module stores, imports,
deletions and duplicate definitions make provider/root ownership unknown. An unused heavy fixture is not a
root. The old file-level fixture facts and configured-fixture diagnostics retain
their existing behavior; they are not a precise test-resource graph.

Only owned module functions and literal test-function `usefixtures` decorators
are resolved. Marks on fixture definitions do not establish required requests. Conditional `getfixturevalue` or ordinary marker-construction calls
are syntax observations with unknown receiver/execution, not required resources.
Class/nested scopes, autouse activation, module marks, imported fixture aliases,
dynamic names/options, nonliteral/mixed indirect parametrization and ambiguous provider
registration remain unknown. Same-name recursion stops at an exhausted outer
definition; each test display stops before expanding more than64 request steps or depth32
and explicitly marks omitted paths unknown. This display budget is not a semantic cache. No missing chain
proves absence of implicit requests or runtime effects.

Explicit [[tests/fixtures/docs/static-environment.meiva.okf.md|static ownership and refusal canaries]]
continue to own static-test admission. Explain neither admits a test to that lane
nor measures its setup cost or authorizes removal of native services.
