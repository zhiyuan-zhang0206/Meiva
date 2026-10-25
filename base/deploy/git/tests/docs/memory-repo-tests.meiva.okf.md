---
type: doc
title: Memory repository tests
description: Real checkout refresh coverage and existing initialization command recorder.
tags: [tests, memory]
---

# Memory repository tests

Refresh cases use real temporary checkouts and a local bare remote. They
verify selected-branch contents, retained unpublished commits on divergence,
and keep-local operation with no remote. The gateway's Git hook test waits for
an unrelated request to complete, proving event-loop progress during Git work
without replacing `pull_main` or `asyncio.to_thread`.

Initialization operation tests now use real local checkouts and bare remotes
for branch selection, retained dirty work, remote removal and gitignore
publication. Provisioning decisions are exercised through the builtin public
scaffold in its own test directory, including real loopback HTTP bundle
bootstrap. Status reads a real checkout and FETCH_HEAD with a supplied zone.

The three existing run_bounded replacements remain narrow delegation tests:
they verify argv, per-command timeout, stripped stdout and exception propagation.
Their historical tree-kill name is not evidence of an actual descendant process
cleanup test. Two small cap tests still replace the existing cap constant; HTTP
transport and response iteration themselves use a real loopback listener.
