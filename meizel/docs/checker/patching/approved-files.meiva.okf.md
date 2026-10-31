---
type: doc
title: "meizel/ — Files approved to patch"
description: "Which test-support files may patch without being recorded, why only three purposes qualify, the count budget that caps the list, and what a stale or refused entry fails as."
tags:
  - locality
  - tool
---

# meizel/ — Files approved to patch

A few test-support files patch on purpose, because their job is to keep the suite off the host or
to put a child process in a state no parameter reaches. `[approved-patches.files]` in
[[meizel/docs/checker/policy/policy.meiva.okf.md|the policy]] lists such a file with its reason, and
none of its patches is recorded or judged. Three purposes qualify: an autouse guard, the entry of a
subprocess a test starts, and the global setup of the test runner. Production code and test modules
(`test_*.py`, `*_test.py`, `*.test.ts`, `*.spec.ts`) cannot be listed (`policy-syntax`): a test that
patches is converted. No baseline counts an approved file's patches, so
`meizel/tests/languages/test_parity.py` pins them (`python -m meizel debt approved` prints them): one
more patch or file is an edit a reviewer reads, as is every `policy diff` entry. An entry whose file
is gone or patches nothing is `stale-approval`; `missing-patch-target` is a hard error even there. The
rule comparison keeps the base's table: approving a file drops the record, and removing an approval
may record the patches it hid.

## The list is a budget

`[approved-patches] "max-files"` in the policy states how many entries the list may hold. A run that
finds more fails as `approved-budget` at the ceiling's line, so the list is paid down or grown by an
edit of that number — never by appending a line that no ceiling notices. A `max-files` the run cannot
read as a single count is `policy-syntax`, so a budget nobody can measure cannot pass for one that was
never set (`meizel/contracts/policy.py` `_approved_budget`).

Parent: [[meizel/docs/checker/patching/patching.meiva.okf.md|patching]].
