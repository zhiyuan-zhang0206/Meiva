---
type: doc
title: Executor capability declarations
description: Which SDK surfaces a takeover may use, the three stored states, and how a default is recorded.
tags:
- base
- identity
- security
---

# Executor capability declarations

A request may declare the SDK surfaces its controller may use
(`agent_impersonations.capabilities`, a JSON array of
`base/agents/impersonation/capabilities.py` values). The vocabulary is concrete
and closed: it names SDK surfaces, not abstract authority, and it omits the
connection handles (`meiva.context.sql` / `.redis` / `.gateway`), which the SDK
gate cannot intercept.

Three stored states are distinct:

- an array grants exactly those surfaces, including an empty array that grants none;
- `NULL` is a lease issued before the vocabulary existed and keeps the full surface
  it was issued with. Readers never treat `NULL` as the minimal set — this is the
  whole backward-compatibility contract;
- a non-array value is refused by the column's CHECK, so a reader never guesses.

`capability_declared_by` records how the set arose: `explicit`,
`caller-default`, `launcher-default` or `legacy`.

The vocabulary names authority, not convenience: `external.model` (the
`meiva.understand` surface) is separate from `read` because that call bills the
user and sends local file content to a model outside the cluster, so a lease that
may read must not thereby gain it. The implication runs one way: granting
`external.model` does expose any local file the caller names, so an approval
surface must read that capability as export as well.

A caller-made request that declares nothing gets the minimal set (read plus
messaging). An automatic takeover that declares nothing keeps the historical
`machine` set for the compatibility window and records `launcher-default` plus one
`impersonation_capability_defaulted` event per lease, so an unchosen authority is
visible instead of silent. `renew` preserves the declaration and stamps a
pre-vocabulary row `legacy` without writing a set.

Enforcement belongs to the attachment
([[meiva/impersonation/docs/attachment.meiva.okf.md]]): the gate compares each
admitted SDK entry with the declaration and, per
`impersonation_capability_enforcement`, either records the refusal (`warn`) or
refuses the call (`enforce`).
