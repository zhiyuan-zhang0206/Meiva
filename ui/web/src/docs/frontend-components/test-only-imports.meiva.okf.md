---
type: doc
title: Test-only modules
description: Which frontend modules exist only for tests, and the lint rule that keeps a shipped module from importing one.
tags:
- frontend
---

# Test-only modules

Three shapes are test-only by the naming convention this tree already uses:

- a `*-test-mock` module (`page-test-mock.tsx`, `inspector-test-mock.tsx`,
  `timeline-test-mock.tsx`, …);
- anything under a `test-support/` directory;
- a `*.test.*` or `*.spec.*` file.

Tests import them freely. A shipped module must not: `eslint-rules/no-test-only-import.mjs`
(`local/no-test-only-import`, error for `src/**/*.{ts,tsx}`) refuses the import,
and test files plus the doubles themselves are exempt because they are the
intended importers. The reason is not style — an import of a double drags its
fixtures and fake transports into the bundle, and turns a test double into a
production dependency that no test would notice going stale.

A dependency that a test needs to control is the same problem in another shape:
pass it as a value ([[ui/web/src/docs/frontend-components/code-blocks.meiva.okf.md|Code blocks]],
[[ui/web/src/docs/frontend-components/markdown-surfaces.meiva.okf.md|Markdown Surfaces]]),
never as a `__…ForTests` setter exported from a shipped module.

Parent: [[ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md]].
