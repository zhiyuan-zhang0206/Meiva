---
type: doc
title: "meizel/ — Python Lexical Bindings"
description: "Import-origin resolution for expression scopes and match captures."
tags:
  - locality
  - tool
---

# meizel/ — Python Lexical Bindings

Import origins respect lexical bindings, including lambda parameters,
comprehension targets and match captures. Lambda defaults and the first
comprehension iterator are evaluated in the enclosing scope; the expression
bodies use their own scope. An assignment expression in a lambda default binds
in the enclosing scope, while its body does not leak bindings outward.

Functions, lambdas and comprehensions do not close over class namespace
bindings. Attribute and subscript comprehension targets still contribute the
existing source facts and restricted-form checks.

This is lexical resolution, not a proof of runtime branch ordering or dynamic
rebinding. Function-definition defaults, annotations and decorators retain the
existing scanner behavior; this expression-scope contract does not establish
that all binding forms have been modeled.
