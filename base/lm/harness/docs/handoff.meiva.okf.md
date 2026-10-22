---
type: doc
title: The handoff document
description: 'The wire format a waking external agent reads first: fixed section order, the versioned field set, the entry points section 3 names, the per-wake size the host reports, and completed-key replay.'
tags:
- base
- library
- llm-inference
- external-agents
---

# The handoff document

Part of [[base/lm/harness/docs/harness.meiva.okf.md|Provider harness]].


The handoff is a wire format, not an internal dataclass: the waking agent reads
it as the first message of a fresh session, and its rendered size is a per-wake
cost the host reports (`Handoff.size_bytes()`, and a `HandoffInjected` event).

Section order is fixed — identity, cluster, entry points, boundaries, reporting,
instructions — because an agent that skims it still meets the instructions last.
`HANDOFF_VERSION` pins the dataclass field set across adapters.

Section 3 names where to start, not an inventory: `DEFAULT_ENTRIES` holds the
`meiva` entry — which attaches this session's identity on first use — plus
`meiva.agents` and `meiva.self` (`pause_heartbeat`, `terminate`), while plugins
contribute their own cluster namespaces (`meiva.tasks`, `meiva.memory`,
`meiva.schedules`). External harnesses use their own native file, shell, search,
and in-turn background tools, so `meiva.files` is not exposed in `DEFAULT_ENTRIES`.
Listing every namespace there would become a second copy of `meiva.help()` that
drifts from the SDK; the entry point and its pointer are enough to start.

Changing the field set or the section order means bumping the version.
`tests/test_handoff.py` pins both: the field tuple is compared against
`PINNED_HANDOFF_FIELDS` and the version against a literal, so the edit that
changes the shape fails until the version moves with it.

Completed-key replay preserves the original reply and usage but sets
`HarnessTurn.replayed`; the graph does not log another provider charge for it.
This cache is local to an adapter session, not durable across session retirement.
