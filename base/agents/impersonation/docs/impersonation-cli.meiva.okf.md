---
type: doc
title: Impersonation CLI parameters and transport
description: Required session parameters, bounded defaults, and relay credentials.
tags: [base, identity, lifecycle]
---

# Impersonation CLI parameters and transport

## CLI parameters: explicit, with four named exceptions

The `meiva impersonate` tree spells out every parameter that decides behavior
(user ruling 2026-09-20; task #4102): `request` requires `--ttl` and
`--batch-window`, `renew` requires `--ttl`, and a missing value is a usage
error before any command runs. `send` carries the lease form — it delivers as
the borrowed `agent:<id>`, attested by the controller session. Four options
keep a default because one value is the only reading:

- `list` / `inbox` `--limit` 100 — presentation only; the service validates
  1..1000 (task #3696 exception inventory).
- `inbox` `--wait` 0 — the unique "return immediately" value; any other number
  waits, so the default cannot be confused with consent to block.
- `say` `--phase` `commentary` — the in-progress reply is the routine phase;
  `final` is a deliberate close.
- `relay` `--debounce` 0.5 — internal host machinery (the relay coalesces wake
  hints); not an operator parameter.

See [[meiva/impersonation/docs/attachment.meiva.okf.md]],
[external agent procedure](../../../../docs/conventions/agents/agent-impersonation.md), and
[host relay setup](../../../../docs/conventions/agents/agent-impersonation-hosts.md).

The relay credential is minted once per lease under the native owner and lease
locks; a second mint returns nothing and a relay is never rotated or replaced.
Already in-flight host RPCs may complete later: no distributed exactly-once
claim is made. `relay_degraded_reason` and `relay_degraded_at` describe the last
observed failure, not an assertion that the channel is currently unhealthy.
`terminal_notices.py` owns the ended-lease notice text and its single submission
(nothing is recorded or retried), while `host_transport.py` owns actual Codex
acceptance and CLI compatibility imports.
