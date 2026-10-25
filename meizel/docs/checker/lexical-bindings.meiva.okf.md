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

Function and async-function defaults, decorators, argument annotations and
return annotations contribute their outer bindings. Class bases, keywords and
decorators do too. Definition bodies are separate scopes. Python 3.12 type
parameters are not collected as outer bindings. Annotation views can see a class
namespace; ordinary body closures inherit type parameters but skip class locals.
Nested lambda/comprehension closures use that ordinary view, rather than
following an annotation view back through class locals. Defaults and decorators
retain their enclosing view.

Type aliases bind their declaration name in the enclosing scope. Alias values
and bounds use their annotation view; alias bounds retain their existing fact
traversal. Function/class type-parameter bounds remain unvisited. When a class
name collides with the current definition's type parameter, import identity is
unresolved: the scanner does not simulate CPython compiler cell decisions to
choose an import from that collision.

This is lexical resolution, not a proof of runtime branch ordering or dynamic
rebinding, and does not establish that all binding forms have been modeled.
