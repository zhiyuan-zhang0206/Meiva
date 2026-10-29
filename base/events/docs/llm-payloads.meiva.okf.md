---
type: doc
title: LLM event payload semantics
description: Usage-time pricing, provider error identity and completed-compaction attribution.
tags: [contracts]
---

# LLM event payload semantics

## Usage time price snapshot

Rates are USD per million tokens. Optional cache-write subdivisions and their TTL-specific rates retain the call-time input cost breakdown. `calls` provides a per-agent/model counter independently of histograms whose views drop agent identity. Typed payload keys also bound queryable attributes; undeclared keys must not become query fragments.

## Agent attribution

`attribution` states how a usage row's agent identity was established: `caller` (the emitter supplied `for_agent_id`), `process` (the identity bound at telemetry startup), or `unattributed` (no identity was available, so `agent_id` is NULL). It is written by every producer rather than inferred by readers. `llm_usage_metering_failed` reports a direct model call whose accounting could not be written — the call ran, its row is missing, and the event carries `model`, `error_type` and the running `failures` count.

## Provider error dimensions

Every classified failure, including a retried transient, is observable; `fatal` describes whether it aborted the turn. `vendor` is the model-provider key and `model` is the model in force. `provider` identifies the SDK package raising the exception, which multiple vendors may share; it cannot identify whose billing key needs attention.

## Historical silent-idle events

`silent_idle` has no active declaration or producer after removal of the
continuation budget. Existing event rows keep their stored name, category and
payload and remain readable through the historical event APIs. They use the
standard undeclared historical-name tier: observation for informational rows,
anomaly for warning-or-higher rows. Historical continuation notes retain their
`NoteTag` value for checkpoint and timeline compatibility.

Completed responses without a tool call now end normally, even with empty
text or reasoning-only content.
