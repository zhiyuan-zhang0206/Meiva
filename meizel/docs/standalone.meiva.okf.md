---
type: doc
title: "Standalone Python Projects"
description: "Production governance, source admission and independent check ownership for separately installed Python projects."
tags:
  - locality
  - python
---

# Standalone Python Projects

## What is it

`standalone` is production Python with its own installation, wheel and environment.
It receives the same production AST, ambient-state, import, complexity and file-budget
checks as `application`; it is not added to the root wheel, pytest hosts or type-checker
environment. Each project supplies mandatory tests and strict typing in its own frozen
environment. Native package names come from explicit Hatch `wheel.packages` metadata;
unsupported layouts, missing source and ambiguous aliases fail rather than appearing
third-party. Relative and installed-package imports resolve to the same physical graph and
ambient identities. Aliases apply only inside their owning project, preserving the root
project's third-party imports and external Python module commands. Standard-library and
built-in names cannot be aliases. This explicit source contract does not simulate all
installed site-packages or import-hook precedence.

`python -m meizel roles projects` prints the validated project directories, one per line,
for independent CI and pre-push selection. Each requires its own admitted `uv.lock`, an
explicit `cpu` optional dependency group (which may be empty), strict basedpyright with
explicit includes covering every admitted project Python file, wheel packages and test directories, and pytest `testpaths`
with actual Python source. Paths must be relative directories; typing `exclude` and
`ignore` are unsupported. Every Python file under a standalone root must have a project
owner. This metadata contract supplies one project inventory; CI and pre-push consumers
must run each selected project's frozen CPU environment, strict typing and tests. Merely
declaring the role does not install or execute these checks.

Directory scans admit tracked regular files and new nonignored regular files through the
Git source universe. Ignored dependency environments are not production source; a forced
tracked source remains admitted. Explicit leaf audits retain their existing meaning.
