---
type: doc
title: Guarded lifecycle write surfaces
description: Required keys and observed targets for the keyed launch retry.
tags: [gateway, idempotency]
---

# Guarded lifecycle writes

This route requires principal-v1 credential scope and a caller key. Retired
unversioned retry-launch ingress has no fallback. Cancel and compact are plain
unkeyed requests ([[gateway/agents/docs/control-acceptance.meiva.okf.md]]).

| Method and route | Retry policy | Effect / evidence |
|---|---|---|
| `POST /api/keyed/v1/agents/{agent_id}/retry-launch` | keyed | required principal-scoped key and observed prior attempt fix one new launch identity |

Same-intent recovery retains the original key and target. Current consumer:
[[gateway/agents/docs/launch-retry.meiva.okf.md]].
