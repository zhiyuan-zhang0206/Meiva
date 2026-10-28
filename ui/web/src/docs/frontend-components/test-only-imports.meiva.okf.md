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

## Before adding or renaming a rule like this: scan the existing names

The name is the only thing the rule can see, so the name is the violation
surface: a double that does not end in `*-test-mock` is treated as a shipped
module, and the moment it imports `test-support/` or another double it is
reported. Adding or renaming a naming-convention rule therefore starts with a
scan of the tree, not with the rule text:

```bash
git ls-files 'ui/web/src/**' | grep -Ei 'mock' | grep -v '\.test\.'
```

Every hit either matches `*-test-mock` / lives under `test-support/` (already
exempt) or is a near miss that the rule will judge as production code. List the
near misses in the same change, and rename them rather than widening the
pattern: exempting `*-mock` or `*-fixtures-mock` would turn "the name contains
mock" into an opt-out and retire the gate. The 2026-10-29 scan found four near
misses in `ui/web/src` — `app/control/config/config-test-fixtures-mock.tsx`,
`app/insights/run/[agents]/run-timeline-test-fixtures-mock.tsx`,
`components/run-timeline/canvas/run-timeline-test-canvas-mock.ts` and
`lib/state/user-settings-mock.ts` (not the same file as
`test-support/user-settings-mock.ts`, which is exempt by its directory).

Parent: [[ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md]].
