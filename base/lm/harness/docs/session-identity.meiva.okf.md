---
type: doc
title: Harness session identity
description: 'The external-harness session registry, bridge attachment claims, and provider identity checks.'
tags:
- base
- library
- llm-inference
- external-agents
---

# Harness session identity

Part of [[base/lm/harness/docs/harness.meiva.okf.md|Provider harness]].

`FileSessionRegistry` writes `agent_id ↔ session_id` (plus cwd, model, resume
count and the harness that wrote it) as soon as the adapter reports the session
started. Writes are a temp file
plus `Path.replace`, so a reader never sees a half-written record, and a corrupt
document raises instead of silently starting a fresh session under an id that
already has history.

A registration is not a second identity: it is the bridge's claim that a session on
this machine belongs to one agent. Three facts gate it — the row says `attached`,
`expires_at` has not passed, and the calling process descends from
`controller_pid` — and the expiry is the one the bridge cannot forget, because the
others degrade silently: a pid gets reused, and a row that never expires would be a
bearer capability nobody can revoke. The bridge rewrites the row at the start of
every turn, so the deadline only runs out when the writing host is gone.

`SessionRecord.harness` makes the registry self-describing. A record written
before the field existed reads as `meiva` — the only harness that predates it —
and every write names its harness, so the fallback can only describe an older
document. `load(agent_id, harness=...)` refuses a record that names another
harness instead of resuming one provider's transcript under another provider's
session, and the wiring passes its harness through a scoped registry rather than
trusting every caller to remember.
