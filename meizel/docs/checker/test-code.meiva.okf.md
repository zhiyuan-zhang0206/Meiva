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
  `test-support/` directory, or the frontend's shared mock named `*-mock.tsx`. The directory rule is
  TypeScript's alone - `tests/fixtures/guards.py` is not test code, so a patch of it is recorded.
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

Parent: [[meizel/docs/checker/patching.meiva.okf.md|patching]].
