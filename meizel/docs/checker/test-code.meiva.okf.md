---
type: doc
title: "meizel/ — Test code"
description: "A patch of test code is not a patch of the code under test: the modules the test-file convention marks, the import condition that makes the exemption conditional, and the fact graph the answer is read from."
tags:
  - locality
  - tool
---

# meizel/ — Test code

A test that drives its own mock reaches no collaborator of the code under test: the module belongs to
the suite, not to what ships. So a `first-party-patch` whose target is such a module is not recorded,
and neither is a `vi.mock` of it. Two conditions hold, and both are facts of the tree rather than
entries in a list:

- The target module is test code by the test-file convention (`is_test_file`), which reads each
  language the way that language names its tests. For Python: `conftest.py`, `test_*.py`,
  `*_test.py`. For TypeScript: a `*.test.*` or `*.spec.*` file, a file under a `tests/` or
  `test-support/` directory, or the frontend's shared mock named `*-test-mock.tsx`. The directory rule
  is TypeScript's alone - `tests/fixtures/guards.py` is not test code, so a patch of it is recorded.
  The mock suffix is the frontend's own convention, the one `local/no-test-only-import` and
  [[ui/web/src/docs/frontend-components/test-only-imports.meiva.okf.md|test-only imports]] already
  state; a bare `-mock` was wider than either and is not test code.
- Nothing outside that convention imports it. This is the condition that matters: the day any file
  outside the convention imports the mock, the module is part of what ships and every patch of it is
  recorded again. A file production reaches is never test support, however it is named.

The checker reads the run's own import graph, so the exemption is exactly as wide as the analysis:
every Python file, and every TypeScript file under a `[typescript]` `scopes` or `import-sources`
path. A `--changed` run knows only the files it scanned, so an import it did not scan cannot clear
the exemption; the full `check` sees the whole graph. It is a rule of the check, not of the tree's
inventory: `debt sites` still lists every patch site in the source, a patch of test code included,
because it reports what the tree holds rather than what the ledger counts.

Nothing about this is declared in a manifest or a policy table: a listed file that production
imports would be stale the moment it is imported, and the import graph already says which modules
ships carry.

## What the rule withdraws today

| module | records | test files that write it | why it is a double |
|---|---:|---|---|
| `ui/web/src/components/agent-sidebar/sidebar-test-mock.tsx` | 135 | `agent-sidebar.test.tsx` (34), `sidebar-stats-cards.test.tsx` (45), `sidebar-tree-and-search.test.tsx` (56) | the rail's scenario object: the props the page hands it, and the settings, stats and plugin declarations it reads |
| `ui/web/src/app/page-test-mock.tsx` | 98 | `page.test.tsx` (51), `page-dispatch.test.tsx` (47) | the home page's `hooksState` and its provider wrapper |
| `ui/web/src/components/inspector/inspector-test-mock.tsx` | 4 | `inspector-panel-chrome.test.tsx` (3), `inspector-panel-notices.test.tsx` (1) | the inspector's `inspectorMockState` |

237 records, every one of them a write into a state object the double exports: no `vi.mock`, and no patch
of a module the suite renders. `python -m meizel debt sites --code first-party-patch --json` lists every
site (333 at `57745fe29`); 237 name one of these modules, 2 sit in `[approved-patches.files]`, and 94 are
recorded. A repayment lowers these numbers and a new site raises them, and
`meizel/tests/languages/test_test_code_accounting.py` fails until this table and the tree agree; the same
test injects a production import of each double into the run's fact graph and asserts every record comes
back, which is the condition above under test rather than in prose.

The name is the whole review surface of this half, and that is why the suffix is `-test-mock` and not
`-mock`. While the rule
read a bare `-mock`, a new file whose name happened to end that way was test code the moment it appeared:
a `probe-state-mock.ts` written for that measurement, with one test writing its state, produced no record,
and importing it from a production file brought the record back. `page-mock.tsx` and
`page-fixtures-mock.tsx` were exempt under that reading and are recorded now; neither holds a record
today, which is why narrowing the suffix moved no count.

Parent: [[meizel/docs/checker/patching.meiva.okf.md|patching]].
