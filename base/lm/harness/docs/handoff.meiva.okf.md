---
type: doc
title: The handoff document
description: 'The wire format a waking external agent reads first: fixed section order, the versioned field set, the entry points section 3 names, and the per-wake size the host reports.'
tags:
- base
- library
- llm-inference
- external-agents
---

# The handoff document


The handoff is a wire format, not an internal dataclass: the waking agent reads
it as the first message of a fresh session, and its rendered size is a per-wake
cost the host reports (`Handoff.size_bytes()`, and a `HandoffInjected` event).

Section order is fixed — identity, cluster, entry points, do not use, reporting,
task — because an agent that skims it still meets the task last. `HANDOFF_VERSION`
is part of the document (`- handoff version: 1` in section 1) so a provider that
caches or replays a handoff can tell which field set it holds.

Section 3 names where to start, not an inventory: `DEFAULT_ENTRIES` holds the
`meiva` entry — which attaches this session's identity on first use — plus the
three namespaces a turn reaches for first (files, tasks, agents). Listing every
namespace there would become a second copy of `meiva.help()` that drifts from the
SDK; the entry point and its pointer are enough to start.

Changing the field set or the section order means bumping the version.
`tests/test_handoff.py` pins both: the field tuple is compared against
`PINNED_HANDOFF_FIELDS` and the version against a literal, so the edit that
changes the shape fails until the version moves with it.
