---
type: doc
title: "meizel/ — Collaborator Any Ratchet"
description: "Direct canonical typing.Any collaborator annotations in production Python, their source positions and stable recorded-debt groups."
tags:
  - locality
  - tool
---

# meizel/ — Collaborator Any Ratchet

`typing.Any` on a parameter, return, or class-field annotation disables static type checking across a collaborator boundary without narrowing or validating the contract. `meizel` reports every direct canonical `typing.Any` annotation in tracked production Python as `any-collaborator`.

Existing annotations are bounded in `[debt.any-collaborator]` tables inside `meizel.toml` manifests whose `ledger` is `strict-to-be`, using the same non-increasing debt ledger as other debtable `meizel` codes (`python -m meizel debt diff --against <ref>`). An unauthorized key or a count increase fails `python -m meizel check`.

## Scope and canonical resolution

- **Production Python only:** files with `test_file` or `is_test_path(path)` are excluded.
- **Direct root `typing.Any` only:** `from typing import Any`, `from typing import Any as Alias`, `import typing; typing.Any`, `import typing as t; t.Any`, and single-layer string annotations (`"Any"`, `"t.Any"`) whose parsed expression resolves to `"typing.Any"` in the active lexical `Scope`.
- **Collaborator slots:**
  - `FunctionDef` and `AsyncFunctionDef` parameters (`posonlyargs`, `args`, `vararg`, `kwonlyargs`, `kwarg`, including `self`/`cls` and dunder methods) with slot `<arg.arg>` and role `parameter`, evaluated in `Scope.annotation_scope(node.type_params)` under prospective qualname `(*qualnames, node.name)`.
  - `FunctionDef` and `AsyncFunctionDef` return annotations with slot `return` and role `return`, evaluated in the same annotation scope.
  - Class-body `AnnAssign` statements whose target is an `ast.Name` (`self.scope.is_class`) with slot `<target.id>` and role `class-field`, under the enclosing class qualname.
- **Shadowing:** a parameter, local variable, class attribute, type parameter, or non-`typing` import named `Any` shadows the import and is not reported.
- **Non-matches:** `object`, container/union/wrapper leaves (`Any | None`, `Optional[Any]`, `list[Any]`, `dict[str, Any]`, `Callable[..., Any]`), function-local variable annotations, and attribute annotations (`self.x: Any`) are not matched by `any-collaborator`.

## Debt key format

Each annotation produces a diagnostic with:

- `code`: `Code.ANY_COLLABORATOR` (`"any-collaborator"`)
- `debt_key`: `DebtKey(owner, "<rel_to_owner>#any <qualname>.<slot>")`

Keys carry no line numbers, so unrelated edits in the same file do not churn the baseline. Multiple annotations on the same `(path, qualname, slot)` (such as overloaded signatures or duplicate class-field declarations) increment that key's integer cap in `[debt.any-collaborator]`.
