# `meiva.context` is not part of the agent-visible SDK

Date: 2026-11-12
Status: Accepted
Amends: the agent-visible behavior of
[the state/context decision](../context/2026-10-04-meiva-state-and-context.md).

## Context

`meiva.context` entered the agent-visible list on 2026-10-04 with a docstring that taught
`context.sql`, `context.redis` and `context.gateway` as the way to reach the database, Redis and
the gateway. The decision itself only documented the identity, with connection objects "provided
by the framework". In production the docstring became the way operations agents read audit,
telemetry, inbox, task, schedule and cost tables, mostly by browsing `information_schema` and
guessing table names.

## Decision

1. **The SDK no longer shows or recommends the context to agents.** `context` is out of
   `__all_for_meiva__`, the attribute's agent-facing docstring is gone, and `dir(meiva)` and
   `meiva.help()` do not list `context`, `DB` or `REDIS`. The attribute stays importable for
   framework and plugin code, like `meiva.const` and `meiva.extend`. An agent's identity is
   `meiva.self.AGENT_ID`.
2. **Reading cluster data is a Meiva Guide topic.** `meiva-guide/cluster-data` names the
   supported entries first (SDK and CLI) and, where none exists, how to query the database
   read-only through `MEIVA_DB_URL`, with the tables worth knowing.

## What does not change

- The database address and tokens remain in the agent process environment
  (`base/host/env/agent_env.py`); that arrangement is its own decision.
- The database role is not tightened here, and existing calls to `meiva.context` keep working.
- `meiva.state` and `meiva.state_update` are exec-turn plumbing for plugins and were not changed.
